"""ReviewLoop execution contract tests.

``ReviewLoop`` is the single supervisor of one review run. These tests pin the
parts the rest of the system depends on:

* the fixed phase pipeline ``PREPARE -> PLAN -> REVIEW -> FINALIZE -> CLEANUP
  -> DONE``, with ``DONE`` written only after cleanup returned *and* the
  terminal metadata was durably saved;
* cleanup or terminal-save failures keep the run ``running`` (gate closed) and
  report a bounded error instead of publishing an unprovable ``DONE``;
* a produced report (plus its artifact) for the success path, and an explicit
  bounded failure instead of a clean report for every fatal failure;
* reviewer/Judge failures never degrade into a false ``no_findings`` pass;
* planning and artifact-write failures settle the run as ``error`` while
  cancellation settles it as ``stopped``;
* usage from the coordinator, every reviewer and the Judge rolls up into one
  run total.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nanoreview.agent.review_loop import (
    ReviewLoop,
    ReviewLoopOutcome,
    ReviewPlanningError,
    ReviewTurnRequest,
)
from nanoreview.agent.review_state import (
    ReviewArtifactStore,
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
)
from nanoreview.agent.runner import AgentRunResult, AgentRunner
from nanoreview.bus.events import InboundMessage
from nanoreview.providers.base import LLMResponse, ToolCallRequest
from nanoreview.review.output.judge import ReviewJudge, ReviewJudgeConfig
from nanoreview.review.planning.planner import ReviewPreparation
from nanoreview.review.result import ReviewHandoffState
from nanoreview.review.types import (
    ALL_REVIEW_ROLES,
    EvidenceReference,
    ReviewAction,
    ReviewEvidenceBundle,
    ReviewMetaKey,
    ReviewPlan,
)
from nanoreview.session.manager import SessionManager

SESSION_KEY = "cli:review"


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
        return AgentRunResult(final_content="", messages=[], usage=dict(self.usage))


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


class _BlockingPlanner:
    """Planner that never returns, so the run can be cancelled mid-flight."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, spec):
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


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
        self.cancelled_sessions: list[str] = []

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

    async def cancel_by_session(self, session_key: str) -> int:
        self.cancelled_sessions.append(session_key)
        return 0


class _FailingCleanupSubagents(_Subagents):
    """Cleanup that reports every child release as failed."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def cancel_by_session(self, session_key: str) -> int:
        self.cancelled_sessions.append(session_key)
        raise self._error


class _InterruptedCleanupSubagents(_Subagents):
    """Cleanup interrupted by a second cancellation."""

    async def cancel_by_session(self, session_key: str) -> int:
        self.cancelled_sessions.append(session_key)
        raise asyncio.CancelledError()


class _FailingSaveSessions(SessionManager):
    """``SessionManager`` whose terminal-metadata write always fails.

    Only the terminal write is blocked: the loop's in-run progress saves must
    keep working so the failure under test is the one that publishes ``DONE``.
    """

    _TERMINAL = {"completed", "error", "stopped"}

    def save(self, session, *, fsync: bool = False) -> None:
        metadata = session.metadata
        if (
            metadata.get(ReviewMetaKey.PHASE) == ReviewPhase.DONE.value
            and metadata.get(ReviewMetaKey.STATUS) in self._TERMINAL
        ):
            raise OSError("disk full")
        super().save(session, fsync=fsync)


class _PhaseRecorder(ReviewLoop):
    """``ReviewLoop`` that records every phase the run enters.

    ``DONE`` is written by direct assignment (a terminal run rejects
    ``enter_phase``), so the recorder appends it explicitly.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.phases: list[str] = []

    def _complete_run(self, run_state, status, session=None):  # type: ignore[override]
        result = super()._complete_run(run_state, status, session)
        if run_state is not None:
            self.phases.append(run_state.phase.value)
        return result


@pytest.fixture
def prepared(monkeypatch):
    """Replace PREPARE's planner entry with the plan/evidence under test.

    PREPARE (plan/evidence/prompt resolution) has its own coverage in
    ``tests/review/test_preprocessor.py`` and the admission tests; these cases
    pin the PLAN/REVIEW/FINALIZE/CLEANUP/DONE pipeline for given inputs.
    """

    def _install(
        plan: ReviewPlan,
        evidence: ReviewEvidenceBundle,
        *,
        prompt: str = "plan",
    ) -> None:
        async def _prepare(_messages, _meta, progress_callback=None):
            return ReviewPreparation(plan, prompt, evidence)

        monkeypatch.setattr(
            "nanoreview.agent.review_loop.prepare_code_review_context", _prepare
        )

    return _install


def _build_loop(
    tmp_path: Path,
    *,
    runner: Any,
    subagents: Any,
    judge: ReviewJudge | None = None,
    loop_cls: type[ReviewLoop] = ReviewLoop,
    sessions: SessionManager | None = None,
    artifact_store: ReviewArtifactStore | None = None,
) -> ReviewLoop:
    return loop_cls(
        workspace=tmp_path,
        sessions=sessions or SessionManager(tmp_path),
        runner=runner,
        subagents=subagents,
        model="test",
        max_tool_result_chars=1000,
        judge_factory=(lambda: judge) if judge is not None else None,
        artifact_store=artifact_store,
    )


def _register_run(loop: ReviewLoop, *, plan: ReviewPlan | None = None) -> ReviewRunState:
    state = ReviewRunState(
        run_id="run-000111222333",
        session_key=SESSION_KEY,
        input_fingerprint="fp",
        plan=plan,
    )
    loop.runs[SESSION_KEY] = state
    return state


def _request(**overrides: Any) -> ReviewTurnRequest:
    params: dict[str, Any] = {
        "session_key": SESSION_KEY,
        "session": None,
        "msg": InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="review",
            content="Review target",
        ),
        "metadata": {},
    }
    params.update(overrides)
    return ReviewTurnRequest(**params)


# ---------------------------------------------------------------------------
# Phase pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_run_walks_every_phase_then_done(
    tmp_path, prepared, monkeypatch
) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    plan = _plan("security", "bug")
    prepared(plan, _evidence())
    subagents = _Subagents()
    loop = _build_loop(
        tmp_path, runner=_PlanRunner(), subagents=subagents, loop_cls=_PhaseRecorder
    )
    state = _register_run(loop)
    entered: list[str] = []
    original_enter = ReviewRunState.enter_phase

    def _record_enter(self, phase):  # noqa: ANN001 - patched class method
        original_enter(self, phase)
        if self.phase is phase:
            entered.append(phase.value)

    monkeypatch.setattr(ReviewRunState, "enter_phase", _record_enter)

    outcome = await loop.execute(_request())

    assert entered == ["prepare", "plan", "review", "finalize", "cleanup"]
    assert loop.phases == ["done"]  # type: ignore[attr-defined]
    assert state.status is ReviewRunStatus.COMPLETED
    assert state.phase is ReviewPhase.DONE
    assert state.report_ref is not None
    assert outcome.produces_report is True
    assert outcome.stop_reason == ""
    assert outcome.result is not None
    assert outcome.result.handoff is ReviewHandoffState.COMPLETE
    # Cleanup ran before the run was allowed to turn terminal.
    assert subagents.cancelled_sessions == [SESSION_KEY]


@pytest.mark.asyncio
async def test_progress_in_persisted_metadata_matches_phase(tmp_path, prepared) -> None:
    """The persisted review_phase follows the pipeline while the run is live."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    prepared(_plan("security"), _evidence())
    sessions = SessionManager(tmp_path)
    loop = _build_loop(
        tmp_path,
        runner=_SingleSecurityPlanRunner(),
        subagents=_Subagents(),
        sessions=sessions,
    )
    _register_run(loop)
    session = sessions.get_or_create(SESSION_KEY)

    await loop.execute(_request(session=session))

    assert session.metadata[ReviewMetaKey.STATUS] == "completed"
    assert session.metadata[ReviewMetaKey.PHASE] == "done"
    assert session.metadata[ReviewMetaKey.REPORT_REF] == loop.artifacts.reference_for(
        "run-000111222333"
    )


# ---------------------------------------------------------------------------
# Planning failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_failure_settles_run_as_error(tmp_path, prepared) -> None:
    """Planning retries happen inside AgentRunner; the loop settles ``error``.

    The planner runs once, the concrete failure is surfaced in the outcome, and
    the run is closed with a bounded reason instead of a clean report.
    """
    runner = _NoPlanRunner()
    prepared(_plan("security"), _evidence())
    loop = _build_loop(tmp_path, runner=runner, subagents=_Subagents())
    state = _register_run(loop)

    outcome = await loop.execute(_request())

    assert runner.calls == 1
    assert outcome.stop_reason == "error"
    assert outcome.produces_report is False
    assert "not a tool call" in (outcome.error or "")
    assert outcome.report_markdown is not None
    assert "not a tool call" in outcome.report_markdown
    assert state.status is ReviewRunStatus.ERROR
    assert state.phase is ReviewPhase.DONE
    assert state.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["compression_failed", "compression_limit"])
async def test_compression_stop_settles_run_as_error(
    tmp_path, prepared, stop_reason: str
) -> None:
    """A compression-stopped coordinator run surfaces as a bounded failure."""
    prepared(_plan("security"), _evidence())
    loop = _build_loop(
        tmp_path,
        runner=_CompressionStopRunner(
            stop_reason, "sync compression failed after 2 attempts: no content"
        ),
        subagents=_Subagents(),
    )
    state = _register_run(loop)

    outcome = await loop.execute(_request())

    assert outcome.stop_reason == "error"
    assert "compression" in (outcome.error or "")
    assert state.status is ReviewRunStatus.ERROR


@pytest.mark.asyncio
async def test_missing_plan_fails_before_any_dispatch(tmp_path, monkeypatch) -> None:
    """A run whose plan cannot be resolved never reaches the reviewers."""

    async def _no_plan(_messages, _meta, progress_callback=None):
        return ReviewPreparation(None, "fallback")

    monkeypatch.setattr(
        "nanoreview.agent.review_loop.prepare_code_review_context", _no_plan
    )
    subagents = _Subagents()
    loop = _build_loop(tmp_path, runner=_PlanRunner(), subagents=subagents)
    state = _register_run(loop)

    outcome = await loop.execute(_request())

    assert outcome.stop_reason == "error"
    assert "no review plan" in (outcome.error or "")
    assert subagents.calls == []
    assert state.status is ReviewRunStatus.ERROR
    assert state.phase is ReviewPhase.DONE


@pytest.mark.asyncio
async def test_local_diff_without_evidence_explains_how_to_continue(
    tmp_path, prepared
) -> None:
    plan = ReviewPlan(
        target=str(tmp_path / "app.py"),
        target_name="app.py",
        target_type="local",
        action=ReviewAction.DIFF,
        roles=[ALL_REVIEW_ROLES["security"]],
        routing_mode="auto",
    )
    prepared(plan, ReviewEvidenceBundle())
    loop = _build_loop(tmp_path, runner=_NoPlanRunner(), subagents=_Subagents())
    state = _register_run(loop)

    outcome = await loop.execute(_request())

    assert "Switch Scope to Repo" in (outcome.error or "")
    assert state.status is ReviewRunStatus.ERROR


@pytest.mark.asyncio
async def test_evidence_unavailable_without_changes_explains_skipped_units(
    tmp_path, prepared
) -> None:
    prepared(_plan("security"), ReviewEvidenceBundle())
    loop = _build_loop(tmp_path, runner=_NoPlanRunner(), subagents=_Subagents())
    _register_run(loop)

    outcome = await loop.execute(_request())

    assert "Review evidence unavailable" in (outcome.error or "")


# ---------------------------------------------------------------------------
# Reviewer dispatch and collection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_program_dispatches_planned_dimensions_without_bus_injection(
    tmp_path, prepared
) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = _Subagents()
    prepared(_plan("security", "bug"), _evidence())
    loop = _build_loop(tmp_path, runner=_PlanRunner(), subagents=subagents)
    _register_run(loop)

    outcome = await loop.execute(_request())

    assert [call["label"] for call in subagents.calls] == ["security", "bug"]
    # Review results are never published to the bus: the program dispatches each
    # dimension with an explicit reviewer profile and collects from the queue.
    assert all("deliver_to_bus" not in call for call in subagents.calls)
    assert [call["origin_metadata"]["profile_id"] for call in subagents.calls] == [
        "security",
        "bug",
    ]
    assert "No actionable issues found" in (outcome.report_markdown or "")


@pytest.mark.asyncio
async def test_run_aggregates_agent_usage_into_run_state(tmp_path, prepared) -> None:
    """Coordinator and reviewer tokens must roll up into one run total.

    Judge usage is exercised in tests/review/test_judge.py; the loop folds
    ``JudgeExecutionResult.usage`` into the run at the same boundary.
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
    prepared(_plan("security", "bug"), _evidence())
    loop = _build_loop(tmp_path, runner=plan_runner, subagents=subagents)
    state = _register_run(loop)

    await loop.execute(_request())

    assert state.usage == {
        "prompt_tokens": 210,  # 10 (coordinator) + 2 x 100 (reviewers)
        "completion_tokens": 42,
        "total_tokens": 252,
    }
    assert state.reviewers["security"].usage == dict(subagents.usage)
    assert state.reviewers["bug"].status == "completed"


@pytest.mark.asyncio
async def test_failed_reviewer_is_recorded_as_error_and_report_is_incomplete(
    tmp_path, prepared
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
    prepared(_plan("security", "bug"), _evidence())
    loop = _build_loop(tmp_path, runner=_PlanRunner(), subagents=subagents)
    state = _register_run(loop)

    outcome = await loop.execute(_request())

    failed = state.reviewers["bug"]
    assert failed.status == "error"
    assert failed.error
    assert "reviewer status=error" in failed.error
    assert "crashed while reading app.py" in failed.error

    completed = state.reviewers["security"]
    assert completed.status == "completed"
    assert completed.error == ""

    # Usage is folded in for both reviewers, so a partial failure does not
    # silently drop the tokens actually spent.
    assert state.usage == {
        "prompt_tokens": 200,
        "completion_tokens": 40,
        "total_tokens": 240,
    }
    assert failed.usage == dict(subagents.usage)
    assert completed.usage == dict(subagents.usage)

    # The run completes but hands over as partial, never as a clean pass.
    assert state.status is ReviewRunStatus.COMPLETED
    assert outcome.result is not None
    assert outcome.result.handoff is ReviewHandoffState.PARTIAL
    assert outcome.result.gaps


@pytest.mark.asyncio
async def test_missing_subagent_status_is_not_silently_treated_as_success(
    tmp_path, prepared
) -> None:
    """An unreported terminal status is a failure, never a silent success.

    The failed reviewer's raw output is still valid empty-findings JSON, so
    without an explicit failure handoff the finalizer would parse it into a
    clean ``no_findings`` dimension and the report would claim a full pass.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    subagents = _Subagents()
    subagents.status_by_label = {"security": "", "bug": "ok"}
    prepared(_plan("security", "bug"), _evidence())
    loop = _build_loop(tmp_path, runner=_PlanRunner(), subagents=subagents)
    state = _register_run(loop)

    outcome = await loop.execute(_request())

    assert state.reviewers["security"].status == "error"
    assert "status=unknown" in state.reviewers["security"].error
    assert state.reviewers["bug"].status == "completed"

    # The report must reflect the failure instead of "No actionable issues
    # found": the failed dimension is incomplete even though its raw payload
    # was parseable.
    assert "incomplete" in (outcome.report_markdown or "").lower()
    assert "No actionable issues found" not in (outcome.report_markdown or "")


@pytest.mark.asyncio
async def test_failed_reviewer_with_valid_json_is_reported_incomplete(
    tmp_path, prepared
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
    prepared(_plan("security", "bug"), _evidence())
    loop = _build_loop(tmp_path, runner=_PlanRunner(), subagents=subagents)
    state = _register_run(loop)

    outcome = await loop.execute(_request())

    assert state.reviewers["bug"].status == "error"
    assert "reviewer status=error" in state.reviewers["bug"].error
    assert "incomplete" in (outcome.report_markdown or "").lower()
    assert "No actionable issues found" not in (outcome.report_markdown or "")


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


def _run_with_judge(
    provider: _FakeJudgeProvider,
    *,
    finding_json: str = _SECURITY_FINDING_JSON,
    tmp_path,
    prepared,
) -> tuple[ReviewLoop, _Subagents, ReviewRunState]:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = _Subagents()
    if finding_json:
        subagents.result_by_label = {"security": finding_json}
    prepared(_plan("security"), _evidence())
    loop = _build_loop(
        tmp_path,
        runner=_SingleSecurityPlanRunner(),
        subagents=subagents,
        judge=_build_judge(provider),
    )
    return loop, subagents, _register_run(loop)


@pytest.mark.asyncio
async def test_judge_success_marks_batch_completed(tmp_path, prepared) -> None:
    """A clean judge pass (even all needs_confirmation) is recorded completed."""
    provider = _FakeJudgeProvider(usage={"total_tokens": 30})
    loop, subagents, state = _run_with_judge(provider, tmp_path=tmp_path, prepared=prepared)

    outcome = await loop.execute(_request())

    assert len(provider.calls) == 1
    batch = state.judge_batches["judge"]
    assert batch.status == "completed"
    assert batch.error == ""
    assert batch.stats["total_candidates"] == 1
    assert batch.stats["needs_confirmation"] == 1  # empty verdicts -> confirm
    assert batch.usage == {"total_tokens": 30}
    assert state.usage["total_tokens"] == 30
    assert "No actionable issues found" not in (outcome.report_markdown or "")
    assert len(subagents.calls) == 1


@pytest.mark.asyncio
async def test_judge_failure_marks_batch_error(tmp_path, prepared) -> None:
    """A judge provider failure records an error batch, never a false pass."""
    provider = _FakeJudgeProvider(error=RuntimeError("judge provider down"))
    loop, subagents, state = _run_with_judge(provider, tmp_path=tmp_path, prepared=prepared)

    # The judge failure is a judge-level problem: it does not abort the run.
    outcome = await loop.execute(_request())

    batch = state.judge_batches["judge"]
    assert batch.status == "error"
    assert batch.error
    assert "judge provider down" in batch.error
    assert "No actionable issues found" not in (outcome.report_markdown or "")
    assert len(subagents.calls) == 1
    assert state.status is ReviewRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_judge_failed_batch_keeps_consumed_usage(tmp_path, prepared) -> None:
    """A failed judge batch still reports its tokens to the run state."""
    provider = _FakeJudgeProvider(
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        prose=True,
    )
    loop, subagents, state = _run_with_judge(provider, tmp_path=tmp_path, prepared=prepared)

    outcome = await loop.execute(_request())

    batch = state.judge_batches["judge"]
    assert batch.status == "error"
    assert batch.error
    assert len(provider.calls) > 1  # the model was retried before failing
    # Every attempted call was billed; none of it may be dropped.
    expected = 15 * len(provider.calls)
    assert batch.usage["total_tokens"] == expected
    assert state.usage["total_tokens"] == expected
    assert "No actionable issues found" not in (outcome.report_markdown or "")
    assert len(subagents.calls) == 1


@pytest.mark.asyncio
async def test_judge_no_candidates_does_not_call_provider(tmp_path, prepared) -> None:
    """No candidates means no judge request and no leftover usage reading."""
    provider = _FakeJudgeProvider(usage={"total_tokens": 9999})
    loop, _subagents, state = _run_with_judge(
        provider, finding_json="", tmp_path=tmp_path, prepared=prepared
    )

    await loop.execute(_request())

    assert provider.calls == []
    batch = state.judge_batches["judge"]
    # Empty result is a completed batch, not a false error, and no usage leaks in.
    assert batch.status == "completed"
    assert batch.error == ""
    assert batch.stats == {}
    assert batch.usage == {}


# ---------------------------------------------------------------------------
# Artifact failure and cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_write_failure_settles_run_as_error(tmp_path, prepared) -> None:
    """A report that cannot be persisted is never handed over as complete."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    prepared(_plan("security"), _evidence())
    # A one-byte artifact budget makes every write fail deterministically.
    store = ReviewArtifactStore(tmp_path, max_bytes=1)
    loop = _build_loop(
        tmp_path,
        runner=_SingleSecurityPlanRunner(),
        subagents=_Subagents(),
        artifact_store=store,
    )
    state = _register_run(loop)

    outcome = await loop.execute(_request())

    assert state.status is ReviewRunStatus.ERROR
    assert state.phase is ReviewPhase.DONE
    assert state.report_ref is None
    assert outcome.produces_report is True
    assert outcome.stop_reason == "error"
    assert outcome.result is not None
    assert outcome.result.handoff is ReviewHandoffState.FAILED
    assert "artifact" in (outcome.error or "")


@pytest.mark.asyncio
async def test_cancelled_run_is_cleaned_up_then_settled_stopped(
    tmp_path, prepared
) -> None:
    """Cancellation waits for cleanup and persists ``stopped`` + ``DONE``."""
    prepared(_plan("security"), _evidence())
    planner = _BlockingPlanner()
    subagents = _Subagents()
    loop = _build_loop(tmp_path, runner=planner, subagents=subagents)
    state = _register_run(loop)

    task = asyncio.create_task(loop.execute(_request()))
    await asyncio.wait_for(planner.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.status is ReviewRunStatus.STOPPED
    assert state.phase is ReviewPhase.DONE
    assert subagents.cancelled_sessions == [SESSION_KEY]


@pytest.mark.asyncio
async def test_finalize_reuses_cleanup_and_settles_children(tmp_path) -> None:
    """``/stop`` finalizes through the same CLEANUP -> DONE tail."""
    subagents = _Subagents()
    loop = _build_loop(tmp_path, runner=_PlanRunner(), subagents=subagents)
    state = _register_run(loop)
    state.enter_phase(ReviewPhase.REVIEW)
    state.reviewer_state("security").status = "completed"
    state.reviewer_state("bug").status = "running"

    result = await loop.finalize(SESSION_KEY, ReviewRunStatus.STOPPED)

    assert result is not None
    assert result.status is ReviewRunStatus.STOPPED
    assert state.phase is ReviewPhase.DONE
    assert state.reviewers["security"].status == "completed"
    assert state.reviewers["bug"].status == "stopped"
    assert state.reviewers["bug"].error
    assert subagents.cancelled_sessions == [SESSION_KEY]


@pytest.mark.asyncio
async def test_finalize_settles_a_run_that_never_started(tmp_path) -> None:
    """A run cancelled in PREPARE is settled, never silently dropped.

    The admission metadata already claims ``running``; dropping the live run
    would leave that claim behind with no failure result to hand over, so the
    run is closed as the requested terminal status with a bounded reason.
    """
    subagents = _Subagents()
    sessions = SessionManager(tmp_path)
    loop = _build_loop(
        tmp_path, runner=_PlanRunner(), subagents=subagents, sessions=sessions
    )
    state = _register_run(loop)
    session = sessions.get_or_create(SESSION_KEY)
    session.metadata.update(state.metadata_payload())
    sessions.save(session)

    result = await loop.finalize(SESSION_KEY, ReviewRunStatus.STOPPED)

    assert result is not None
    assert result.status is ReviewRunStatus.STOPPED
    assert state.status is ReviewRunStatus.STOPPED
    assert state.phase is ReviewPhase.DONE
    assert state.warnings
    assert loop.get(SESSION_KEY) is state
    assert loop.result(SESSION_KEY) is not None
    assert session.metadata[ReviewMetaKey.STATUS] == "stopped"
    assert session.metadata[ReviewMetaKey.PHASE] == "done"
    assert session.metadata[ReviewMetaKey.SUMMARY]
    assert subagents.cancelled_sessions == [SESSION_KEY]


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_the_run_running(tmp_path, prepared) -> None:
    """A release that cannot be confirmed must not publish ``DONE``.

    The report artifact exists, so the report text is still delivered, but the
    run stays ``running`` (gate closed) and the outcome carries the bounded
    cleanup error instead of a terminal status the process cannot prove.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    prepared(_plan("security"), _evidence())
    sessions = SessionManager(tmp_path)
    subagents = _FailingCleanupSubagents(RuntimeError("shutdown timed out"))
    loop = _build_loop(
        tmp_path,
        runner=_SingleSecurityPlanRunner(),
        subagents=subagents,
        sessions=sessions,
    )
    state = _register_run(loop)

    outcome = await loop.execute(_request())

    assert outcome.stop_reason == "error"
    assert "cleanup" in (outcome.error or "")
    assert outcome.result is None
    assert outcome.report_markdown is not None
    assert state.status is ReviewRunStatus.RUNNING
    assert state.phase is ReviewPhase.CLEANUP
    assert loop.running(SESSION_KEY) is state
    assert loop.result(SESSION_KEY) is None
    assert sessions.get_or_create(SESSION_KEY).metadata.get(
        ReviewMetaKey.STATUS
    ) != "completed"


@pytest.mark.asyncio
async def test_terminal_save_failure_keeps_the_run_running(tmp_path, prepared) -> None:
    """A failed terminal save must not turn the in-memory run into ``DONE``.

    The gate is decided from the live run, so publishing the terminal status
    before the metadata is durable would open the conversation while disk still
    claims ``running``. The metadata write is rolled back and the run stays
    running with the bounded save error reported.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    prepared(_plan("security"), _evidence())
    sessions = _FailingSaveSessions(tmp_path)
    loop = _build_loop(
        tmp_path,
        runner=_SingleSecurityPlanRunner(),
        subagents=_Subagents(),
        sessions=sessions,
    )
    state = _register_run(loop)
    session = sessions.get_or_create(SESSION_KEY)

    outcome = await loop.execute(_request(session=session))

    assert outcome.stop_reason == "error"
    assert "could not be persisted" in (outcome.error or "")
    assert outcome.result is None
    assert state.status is ReviewRunStatus.RUNNING
    assert state.phase is not ReviewPhase.DONE
    assert loop.running(SESSION_KEY) is state
    # The terminal metadata was rolled back, so neither the cached session nor
    # a fresh read from disk claims the run finished.
    assert session.metadata.get(ReviewMetaKey.PHASE) != "done"
    reloaded = SessionManager(tmp_path).get_or_create(SESSION_KEY)
    assert reloaded.metadata.get(ReviewMetaKey.PHASE) != "done"
    # The delivered report itself carries the settle failure, so the user is
    # told the exit is blocked instead of only seeing a clean report.
    assert outcome.produces_report is True
    assert "Review settlement failed" in (outcome.report_markdown or "")
    assert "could not be persisted" in (outcome.report_markdown or "")


@pytest.mark.asyncio
async def test_unsettled_report_without_report_text_still_names_the_failure(
    tmp_path,
) -> None:
    """A settle failure with no report text still delivers the bounded reason."""
    finalizer_result = cast(Any, SimpleNamespace(report_markdown=None))
    reason = "review cleanup could not cancel all child tasks (RuntimeError)"

    outcome = ReviewLoop._unsettled_outcome(finalizer_result, reason)

    assert isinstance(outcome, ReviewLoopOutcome)
    assert outcome.produces_report is True
    assert outcome.stop_reason == "error"
    assert "Review settlement failed" in (outcome.report_markdown or "")
    # The bounded reason is also still available on the outcome itself.
    assert "cancel all child tasks" in (outcome.error or "")


@pytest.mark.asyncio
async def test_finalize_keeps_the_gate_when_cleanup_fails(tmp_path) -> None:
    """``/stop`` over a failing cleanup returns no result and keeps the gate."""
    subagents = _FailingCleanupSubagents(RuntimeError("shutdown timed out"))
    loop = _build_loop(tmp_path, runner=_PlanRunner(), subagents=subagents)
    state = _register_run(loop)
    state.enter_phase(ReviewPhase.REVIEW)

    assert await loop.finalize(SESSION_KEY, ReviewRunStatus.STOPPED) is None
    assert state.status is ReviewRunStatus.RUNNING
    assert loop.running(SESSION_KEY) is state


@pytest.mark.asyncio
async def test_finalize_keeps_the_gate_when_the_save_fails(tmp_path) -> None:
    """``/stop`` whose terminal write fails keeps the run unsettled."""
    sessions = _FailingSaveSessions(tmp_path)
    loop = _build_loop(
        tmp_path,
        runner=_PlanRunner(),
        subagents=_Subagents(),
        sessions=sessions,
    )
    state = _register_run(loop)
    state.enter_phase(ReviewPhase.REVIEW)

    assert await loop.finalize(SESSION_KEY, ReviewRunStatus.STOPPED) is None
    assert state.status is ReviewRunStatus.RUNNING
    assert loop.running(SESSION_KEY) is state


@pytest.mark.asyncio
async def test_second_cancellation_leaves_the_run_for_a_retry(
    tmp_path, prepared
) -> None:
    """A cancel that interrupts cleanup never publishes ``DONE``.

    The run stays ``running`` so the turn task's cancellation handler (or a
    restart) settles it; the alternative — writing ``stopped`` over child work
    that was never released — is what this pins against.
    """
    prepared(_plan("security"), _evidence())
    planner = _BlockingPlanner()
    subagents = _InterruptedCleanupSubagents()
    loop = _build_loop(tmp_path, runner=planner, subagents=subagents)
    state = _register_run(loop)

    task = asyncio.create_task(loop.execute(_request()))
    await asyncio.wait_for(planner.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.status is ReviewRunStatus.RUNNING
    assert state.phase is ReviewPhase.CLEANUP
    assert loop.result(SESSION_KEY) is None
    assert any("interrupted" in warning for warning in state.warnings)
    assert subagents.cancelled_sessions == [SESSION_KEY]


@pytest.mark.asyncio
async def test_execute_without_a_live_run_reports_failure(tmp_path) -> None:
    loop = _build_loop(tmp_path, runner=_PlanRunner(), subagents=_Subagents())

    outcome = await loop.execute(_request())

    assert isinstance(outcome, ReviewLoopOutcome)
    assert outcome.stop_reason == "error"
    assert outcome.error is not None


def test_review_planning_error_is_the_module_contract() -> None:
    assert issubclass(ReviewPlanningError, RuntimeError)


# ---------------------------------------------------------------------------
# PREPARE input resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_rejects_a_run_without_a_plan(tmp_path, monkeypatch) -> None:
    """PREPARE fails loudly when the persisted target resolves to nothing."""

    async def _no_plan(_messages, _meta, progress_callback=None):
        return ReviewPreparation(None, "fallback")

    monkeypatch.setattr(
        "nanoreview.agent.review_loop.prepare_code_review_context", _no_plan
    )
    loop = _build_loop(tmp_path, runner=_PlanRunner(), subagents=_Subagents())
    state = _register_run(loop)

    with pytest.raises(ReviewPlanningError, match="no review plan"):
        await loop._prepare_review(_request(), state)


@pytest.mark.asyncio
async def test_prepare_syncs_review_metadata_onto_the_session(
    tmp_path, monkeypatch
) -> None:
    """Resolved navigation metadata is mirrored back onto the session."""
    plan = _plan("security")
    sessions = SessionManager(tmp_path)

    async def _prepare(_messages, meta, progress_callback=None):
        meta[ReviewMetaKey.ALLOWED_DIMENSIONS] = ["security"]
        meta[ReviewMetaKey.LOCAL_ROOT] = str(tmp_path)
        return ReviewPreparation(plan, "prompt", _evidence())

    monkeypatch.setattr(
        "nanoreview.agent.review_loop.prepare_code_review_context", _prepare
    )
    loop = _build_loop(
        tmp_path, runner=_PlanRunner(), subagents=_Subagents(), sessions=sessions
    )
    state = _register_run(loop)
    session = sessions.get_or_create(SESSION_KEY)

    inputs = await loop._prepare_review(_request(session=session), state)

    assert session.metadata[ReviewMetaKey.ALLOWED_DIMENSIONS] == ["security"]
    assert session.metadata[ReviewMetaKey.LOCAL_ROOT] == str(tmp_path)
    # The prompt belongs to the coordinator envelope, ahead of the turn messages.
    assert inputs.coordinator_messages[0] == {"role": "system", "content": "prompt"}
    assert inputs.plan is plan
    assert inputs.validation_workspace == str(tmp_path)
