from __future__ import annotations

import asyncio

import pytest

from nanoreview.agent.orchestration import (
    ReviewExecutionContext,
    ReviewOrchestrator,
    ReviewPlanningError,
)
from nanoreview.agent.review_state import ReviewRunState
from nanoreview.agent.runner import AgentRunner, AgentRunResult
from nanoreview.bus.events import InboundMessage
from nanoreview.providers.base import LLMResponse, ToolCallRequest
from nanoreview.review.output.judge import ReviewJudge, ReviewJudgeConfig
from nanoreview.review.types import (
    ALL_REVIEW_ROLES,
    EvidenceReference,
    ReviewAction,
    ReviewEvidenceBundle,
    ReviewPlan,
)


def _plan(*roles: str) -> ReviewPlan:
    return ReviewPlan(
        target="repo",
        target_name="repo",
        target_type="local",
        action=ReviewAction.REPO,
        roles=[ALL_REVIEW_ROLES[role] for role in roles],
        routing_mode="explicit",
    )


def _evidence() -> ReviewEvidenceBundle:
    return ReviewEvidenceBundle(
        references=(
            EvidenceReference(
                id="ev-001",
                path="app.py",
                start_line=1,
                end_line=1,
                excerpt="## app.py:1-1\nvalue = 1",
            ),
        ),
        summary="## app.py:1-1",
    )


class _NoPlanRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, spec):
        self.calls += 1
        assert spec.tool_choice == {
            "type": "function",
            "function": {"name": "submit_review_plan"},
        }
        return AgentRunResult(final_content="not a tool call", messages=[])


class _PlanRunner:
    usage: dict[str, int] = {}

    async def run(self, spec):
        tool = spec.tools.get("submit_review_plan")
        assert tool is not None
        result = await tool.execute(
            assignments=[
                {
                    "dimension": "security",
                    "focus": "authentication state",
                    "evidence_ids": ["ev-001"],
                },
                {
                    "dimension": "bug",
                    "focus": "exception paths",
                    "evidence_ids": ["ev-001"],
                },
            ]
        )
        assert result == "review plan accepted"
        return AgentRunResult(
            final_content="", messages=[], usage=dict(self.usage)
        )


class _FakeJudgeProvider:
    """Provider fake for the judge, recording calls and returning canned verdicts."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        usage: dict[str, int] | None = None,
        prose: bool = False,
    ) -> None:
        self.calls: list[dict] = []
        self._error = error
        self._usage = usage
        self._prose = prose

    async def chat_with_retry(self, **kwargs: dict) -> LLMResponse:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        if self._prose:
            # Answers prose instead of the terminal tool, so the batch ends in
            # terminal_tool_failed after real (billable) model calls.
            return LLMResponse(
                content="No verdicts.",
                usage=dict(self._usage) if self._usage else {},
            )
        tool_call = ToolCallRequest(
            id="call-1", name="submit_verdicts", arguments={"verdicts": []}
        )
        return LLMResponse(
            content=None,
            tool_calls=[tool_call],
            usage=dict(self._usage) if self._usage else {},
        )


def _build_judge(provider: _FakeJudgeProvider) -> ReviewJudge:
    """Judge sharing one ``AgentRunner``/provider/model with the plan run."""
    return ReviewJudge(
        runner=AgentRunner(provider),
        model="test-model",
        config=ReviewJudgeConfig(
            context_window_tokens=200_000,
            max_tokens=128,
            timeout_seconds=5,
        ),
    )


#: A finding whose details match the security profile schema and whose evidence
#: is present in the target file, so the validator accepts it into the judge scope.
_SECURITY_FINDING_JSON = (
    '{"submitted":true,"findings":[{"severity":"high","file":"app.py","line":1,'
    '"title":"Hardcoded secret","evidence":"value = 1","impact":"bad",'
    '"recommendation":"fix","details":{"trust_boundary":"auth",'
    '"attack_preconditions":"untrusted input","attack_path":"secret storage"}}],'
    '"errors":[]}'
)


class _SingleSecurityPlanRunner:
    """Planner that submits exactly one security dimension (for judge tests)."""

    async def run(self, spec):
        tool = spec.tools.get("submit_review_plan")
        result = await tool.execute(
            assignments=[
                {
                    "dimension": "security",
                    "focus": "authentication state",
                    "evidence_ids": ["ev-001"],
                }
            ]
        )
        assert result == "review plan accepted"
        return AgentRunResult(final_content="", messages=[], usage={})


class _Subagents:
    max_concurrent_subagents = 2

    #: Usage reported by every reviewer result metadata.
    usage: dict[str, int] = {}

    #: Terminal ``subagent_status`` reported by every reviewer result.
    status: str = "ok"

    #: Per-dimension ``subagent_status`` override, keyed by subagent label.
    status_by_label: dict[str, str] = {}

    #: Per-dimension raw reviewer output override, keyed by subagent label.
    result_by_label: dict[str, str] = {}

    def __init__(self) -> None:
        self.results: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.calls: list[dict] = []

    def get_running_count(self) -> int:
        return 0

    async def spawn(self, **kwargs):
        self.calls.append(kwargs)
        label = kwargs["label"]
        metadata = {
            "subagent_label": label,
            "subagent_status": self.status_by_label.get(label, self.status),
            "subagent_result": self.result_by_label.get(
                label, '{"submitted":true,"findings":[],"errors":[]}'
            ),
        }
        if self.usage:
            metadata["subagent_usage"] = dict(self.usage)
        await self.results.put(
            InboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id="cli:review",
                content="completed",
                metadata=metadata,
            )
        )
        return "Review subagent started"

    async def wait_for_session_result(self, _session_key: str, *, timeout: float):
        return await asyncio.wait_for(self.results.get(), timeout=timeout)


@pytest.mark.asyncio
async def test_coordinator_plan_failure_raises_planning_error(tmp_path) -> None:
    """Planning retries happen inside AgentRunner (terminal_retry_limit); the
    orchestrator runs the planner once and surfaces the concrete failure."""
    runner = _NoPlanRunner()
    orchestrator = ReviewOrchestrator(
        runner=runner,
        subagents=_Subagents(),
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )

    with pytest.raises(ReviewPlanningError, match="not a tool call"):
        await orchestrator.execute(
            coordinator_messages=[{"role": "system", "content": "plan"}],
            plan=_plan("security"),
            evidence=_evidence(),
            context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
            validation_workspace=str(tmp_path),
        )

    assert runner.calls == 1


class _CompressionStopRunner:
    """Coordinator runner stub stopped by run-level compression."""

    def __init__(self, stop_reason: str, error: str) -> None:
        self.stop_reason = stop_reason
        self.error = error

    async def run(self, spec):
        return AgentRunResult(
            final_content=None,
            messages=list([*spec.frozen_messages, *spec.working_messages]),
            stop_reason=self.stop_reason,
            error=self.error,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["compression_failed", "compression_limit"])
async def test_coordinator_compression_stop_raises_planning_error(
    tmp_path, stop_reason: str
) -> None:
    """A compression-stopped coordinator run surfaces as ReviewPlanningError."""
    orchestrator = ReviewOrchestrator(
        runner=_CompressionStopRunner(
            stop_reason, "sync compression failed after 2 attempts: no content"
        ),
        subagents=_Subagents(),
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )

    with pytest.raises(ReviewPlanningError, match="compression"):
        await orchestrator.execute(
            coordinator_messages=[{"role": "system", "content": "plan"}],
            plan=_plan("security"),
            evidence=_evidence(),
            context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
            validation_workspace=str(tmp_path),
        )


@pytest.mark.asyncio
async def test_program_dispatches_planned_dimensions_without_bus_injection(tmp_path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = _Subagents()
    orchestrator = ReviewOrchestrator(
        runner=_PlanRunner(),
        subagents=subagents,
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )

    report = await orchestrator.execute(
        coordinator_messages=[{"role": "system", "content": "plan"}],
        plan=_plan("security", "bug"),
        evidence=_evidence(),
        context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
        validation_workspace=str(tmp_path),
    )

    assert [call["label"] for call in subagents.calls] == ["security", "bug"]
    assert all(call["deliver_to_bus"] is False for call in subagents.calls)
    assert "No actionable issues found" in report


@pytest.mark.asyncio
async def test_execute_run_aggregates_agent_usage_into_run_state(tmp_path) -> None:
    """Coordinator and reviewer tokens must roll up into one run total.

    Judge usage is exercised in tests/review/test_judge.py; the orchestrator
    folds ``JudgeExecutionResult.usage`` into the run at the same boundary.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    plan_runner = _PlanRunner()
    plan_runner.usage = {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
    }
    subagents = _Subagents()
    subagents.usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }
    orchestrator = ReviewOrchestrator(
        runner=plan_runner,
        subagents=subagents,
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )
    run_state = ReviewRunState(
        run_id="run-usage-1", session_key="cli:review", input_fingerprint="fp"
    )

    await orchestrator.execute_run(
        coordinator_messages=[{"role": "system", "content": "plan"}],
        plan=_plan("security", "bug"),
        evidence=_evidence(),
        context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
        validation_workspace=str(tmp_path),
        run_state=run_state,
    )

    assert run_state.usage == {
        "prompt_tokens": 210,  # 10 (coordinator) + 2 x 100 (reviewers)
        "completion_tokens": 42,
        "total_tokens": 252,
    }
    assert run_state.reviewers["security"].usage == dict(subagents.usage)
    assert run_state.reviewers["bug"].status == "completed"


@pytest.mark.asyncio
async def test_failed_reviewer_is_recorded_as_error_and_report_is_incomplete(
    tmp_path,
) -> None:
    """A reviewer failure must surface as ``error`` with a reason, not success.

    There is no outer reviewer retry: the run keeps the success path behaviour
    (ingest, usage, callback) but the report is explicitly marked incomplete.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    subagents = _Subagents()
    subagents.status_by_label = {"bug": "error"}
    subagents.result_by_label = {"bug": "Error: reviewer crashed while reading app.py"}
    subagents.usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }
    orchestrator = ReviewOrchestrator(
        runner=_PlanRunner(),
        subagents=subagents,
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )
    run_state = ReviewRunState(
        run_id="run-reviewer-error", session_key="cli:review", input_fingerprint="fp"
    )

    outcome = await orchestrator.execute_run(
        coordinator_messages=[{"role": "system", "content": "plan"}],
        plan=_plan("security", "bug"),
        evidence=_evidence(),
        context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
        validation_workspace=str(tmp_path),
        run_state=run_state,
    )

    failed = run_state.reviewers["bug"]
    assert failed.status == "error"
    assert failed.error
    assert "reviewer status=error" in failed.error
    assert "crashed while reading app.py" in failed.error

    completed = run_state.reviewers["security"]
    assert completed.status == "completed"
    assert completed.error == ""

    # Usage is folded in for both reviewers, so a partial failure does not
    # silently drop the tokens actually spent.
    assert run_state.usage == {
        "prompt_tokens": 200,
        "completion_tokens": 40,
        "total_tokens": 240,
    }
    assert failed.usage == dict(subagents.usage)
    assert completed.usage == dict(subagents.usage)

    # The failed dimension is ingested and the report explicitly says so.
    dimensions = {d.dimension: d for d in outcome.finalizer_result.dimensions}
    assert dimensions["bug"].status == "incomplete"
    assert "incomplete" in outcome.report_markdown.lower()


@pytest.mark.asyncio
async def test_missing_subagent_status_is_not_silently_treated_as_success(
    tmp_path,
) -> None:
    """An unreported terminal status is a failure, never a silent success.

    The failed reviewer's raw output is still valid empty-findings JSON, so
    without an explicit failure handoff the finalizer would parse it into a
    clean ``no_findings`` dimension and the report would claim a full pass.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    subagents = _Subagents()
    subagents.status_by_label = {"security": "", "bug": "ok"}
    orchestrator = ReviewOrchestrator(
        runner=_PlanRunner(),
        subagents=subagents,
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )
    run_state = ReviewRunState(
        run_id="run-reviewer-unreported", session_key="cli:review", input_fingerprint="fp"
    )

    outcome = await orchestrator.execute_run(
        coordinator_messages=[{"role": "system", "content": "plan"}],
        plan=_plan("security", "bug"),
        evidence=_evidence(),
        context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
        validation_workspace=str(tmp_path),
        run_state=run_state,
    )

    assert run_state.reviewers["security"].status == "error"
    assert "status=unknown" in run_state.reviewers["security"].error
    assert run_state.reviewers["bug"].status == "completed"

    # The report must reflect the failure instead of "No actionable issues
    # found": the failed dimension is incomplete even though its raw payload
    # was parseable.
    dimensions = {d.dimension: d for d in outcome.finalizer_result.dimensions}
    assert dimensions["security"].status == "incomplete"
    assert dimensions["security"].errors
    assert dimensions["bug"].status == "no_findings"
    assert "incomplete" in outcome.report_markdown.lower()
    assert "No actionable issues found" not in outcome.report_markdown


@pytest.mark.asyncio
async def test_failed_reviewer_with_valid_json_is_reported_incomplete(
    tmp_path,
) -> None:
    """A non-ok terminal status wins over a parseable reviewer payload.

    Even when the failed reviewer returns well-formed findings JSON, the
    finalizer must record the dimension as incomplete, not no_findings.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    subagents = _Subagents()
    subagents.status_by_label = {"bug": "error"}
    subagents.result_by_label = {
        "bug": '{"submitted":true,"findings":[],"errors":[]}',
    }
    orchestrator = ReviewOrchestrator(
        runner=_PlanRunner(),
        subagents=subagents,
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )
    run_state = ReviewRunState(
        run_id="run-reviewer-json-failure", session_key="cli:review", input_fingerprint="fp"
    )

    outcome = await orchestrator.execute_run(
        coordinator_messages=[{"role": "system", "content": "plan"}],
        plan=_plan("security", "bug"),
        evidence=_evidence(),
        context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
        validation_workspace=str(tmp_path),
        run_state=run_state,
    )

    assert run_state.reviewers["bug"].status == "error"
    assert "reviewer status=error" in run_state.reviewers["bug"].error

    dimensions = {d.dimension: d for d in outcome.finalizer_result.dimensions}
    assert dimensions["bug"].status == "incomplete"
    assert "incomplete" in outcome.report_markdown.lower()
    assert "No actionable issues found" not in outcome.report_markdown


@pytest.mark.asyncio
async def test_local_diff_without_evidence_explains_how_to_continue(tmp_path) -> None:
    plan = ReviewPlan(
        target=str(tmp_path / "app.py"),
        target_name="app.py",
        target_type="local",
        action=ReviewAction.DIFF,
        roles=[ALL_REVIEW_ROLES["security"]],
        routing_mode="auto",
    )
    orchestrator = ReviewOrchestrator(
        runner=_NoPlanRunner(),
        subagentmanager=_Subagents(),
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )

    with pytest.raises(ReviewPlanningError, match="Switch Scope to Repo"):
        await orchestrator.execute(
            coordinator_messages=[],
            plan=plan,
            evidence=ReviewEvidenceBundle(),
            context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
            validation_workspace=str(tmp_path),
        )


def _run_with_judge(
    provider: _FakeJudgeProvider,
    *,
    subagents: _Subagents | None = None,
    finding_json: str = _SECURITY_FINDING_JSON,
    tmp_path=None,
) -> tuple[ReviewOrchestrator, _Subagents, ReviewRunState]:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = subagents or _Subagents()
    if finding_json:
        subagents.result_by_label = {"security": finding_json}
    orchestrator = ReviewOrchestrator(
        runner=_SingleSecurityPlanRunner(),
        subagents=subagents,
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=_build_judge(provider),
    )
    run_state = ReviewRunState(
        run_id="run-judge", session_key="cli:review", input_fingerprint="fp"
    )
    return orchestrator, subagents, run_state


@pytest.mark.asyncio
async def test_execute_run_judge_success_marks_batch_completed(tmp_path) -> None:
    """A clean judge pass (even all needs_confirmation) is recorded completed."""
    provider = _FakeJudgeProvider(usage={"total_tokens": 30})
    orchestrator, subagents, run_state = _run_with_judge(provider, tmp_path=tmp_path)

    outcome = await orchestrator.execute_run(
        coordinator_messages=[{"role": "system", "content": "plan"}],
        plan=_plan("security"),
        evidence=_evidence(),
        context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
        validation_workspace=str(tmp_path),
        run_state=run_state,
    )

    assert len(provider.calls) == 1
    batch = run_state.judge_batches["judge"]
    assert batch.status == "completed"
    assert batch.error == ""
    assert batch.stats["total_candidates"] == 1
    assert batch.stats["needs_confirmation"] == 1  # empty verdicts -> confirm
    assert batch.usage == {"total_tokens": 30}
    assert run_state.usage["total_tokens"] == 30
    assert outcome.finalizer_result.needs_confirmation
    assert "No actionable issues found" not in outcome.report_markdown
    assert len(subagents.calls) == 1


@pytest.mark.asyncio
async def test_execute_run_judge_failure_marks_batch_error(tmp_path) -> None:
    """A judge provider failure records an error batch, never a false pass."""
    provider = _FakeJudgeProvider(error=RuntimeError("judge provider down"))
    orchestrator, subagents, run_state = _run_with_judge(provider, tmp_path=tmp_path)

    # The judge failure is a judge-level problem: it does not abort the run.
    outcome = await orchestrator.execute_run(
        coordinator_messages=[{"role": "system", "content": "plan"}],
        plan=_plan("security"),
        evidence=_evidence(),
        context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
        validation_workspace=str(tmp_path),
        run_state=run_state,
    )

    batch = run_state.judge_batches["judge"]
    assert batch.status == "error"
    assert batch.error
    assert "judge provider down" in batch.error
    # Candidates still surface as needs_confirmation, not accepted or no-findings.
    assert outcome.finalizer_result.needs_confirmation
    assert "No actionable issues found" not in outcome.report_markdown
    assert len(subagents.calls) == 1


@pytest.mark.asyncio
async def test_execute_run_judge_failed_batch_keeps_consumed_usage(tmp_path) -> None:
    """A failed judge batch still reports its tokens to the run state."""
    provider = _FakeJudgeProvider(
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        prose=True,
    )
    orchestrator, subagents, run_state = _run_with_judge(provider, tmp_path=tmp_path)

    outcome = await orchestrator.execute_run(
        coordinator_messages=[{"role": "system", "content": "plan"}],
        plan=_plan("security"),
        evidence=_evidence(),
        context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
        validation_workspace=str(tmp_path),
        run_state=run_state,
    )

    batch = run_state.judge_batches["judge"]
    assert batch.status == "error"
    assert batch.error
    assert len(provider.calls) > 1  # the model was retried before failing
    # Every attempted call was billed; none of it may be dropped.
    expected = 15 * len(provider.calls)
    assert batch.usage["total_tokens"] == expected
    assert run_state.usage["total_tokens"] == expected
    assert outcome.finalizer_result.needs_confirmation
    assert "No actionable issues found" not in outcome.report_markdown
    assert len(subagents.calls) == 1


@pytest.mark.asyncio
async def test_execute_run_judge_no_candidates_does_not_call_provider(tmp_path) -> None:
    """No candidates means no judge request and no leftover usage reading."""
    provider = _FakeJudgeProvider(usage={"total_tokens": 9999})
    orchestrator, _subagents, run_state = _run_with_judge(
        provider, finding_json="", tmp_path=tmp_path
    )

    await orchestrator.execute_run(
        coordinator_messages=[{"role": "system", "content": "plan"}],
        plan=_plan("security"),
        evidence=_evidence(),
        context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
        validation_workspace=str(tmp_path),
        run_state=run_state,
    )

    assert provider.calls == []
    batch = run_state.judge_batches["judge"]
    # Empty result is a completed batch, not a false error, and no usage leaks in.
    assert batch.status == "completed"
    assert batch.error == ""
    assert batch.stats == {}
    assert batch.usage == {}
