"""ReviewLoop execution contract tests.

``ReviewLoop`` is the pipeline of one review run, executed against a blackboard
it does not own: the coordinator creates and writes the board, and the loop only
requests writes through a narrow writer. These tests pin the parts the rest of
the system depends on:

* the fixed phase pipeline ``PREPARE -> PLAN -> REVIEW -> FINALIZE -> CLEANUP
  -> DONE``, with ``DONE`` published only after cleanup returned *and* the
  terminal metadata was durably saved;
* planning is one structured answer validated against the real planner
  contract — a rejected answer fails planning instead of dispatching reviewers;
* cleanup or terminal-save failures keep the run ``running`` (gate closed) and
  report a bounded error instead of publishing an unprovable ``DONE``;
* a produced report (plus its artifact) for the success path, and an explicit
  bounded failure instead of a clean report for every fatal failure;
* reviewer/Judge failures never degrade into a false ``no_findings`` pass;
* planning and artifact-write failures settle the run as ``error`` while
  cancellation settles it as ``stopped``;
* usage from the planner, every reviewer and the Judge rolls up into one run
  total.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nanoreview.agent.llm_runtime import ROLE_REVIEWER, LLMRuntime
from nanoreview.agent.review_board import (
    CHILD_COMPLETED,
    CHILD_ERROR,
    CHILD_STOPPED,
    JudgeBatchSnapshot,
    ReviewBoardStore,
    ReviewBoardView,
    ReviewBoardWriter,
    ReviewerSnapshot,
)
from nanoreview.agent.review_loop import (
    REVIEWER_MODEL_REQUEST_LIMIT,
    ReviewLoop,
    ReviewLoopOutcome,
    ReviewPlanningError,
    ReviewTurnRequest,
    review_budget_contract,
)
from nanoreview.agent.review_state import (
    ReviewArtifactStore,
    ReviewPhase,
    ReviewRunStatus,
)
from nanoreview.agent.runner import AgentRunner
from nanoreview.bus.events import InboundMessage
from nanoreview.providers.base import GenerationSettings, LLMResponse
from nanoreview.review.input.snapshot import build_snapshot
from nanoreview.review.output.judge import ReviewJudge, ReviewJudgeConfig
from nanoreview.review.planning.planner import ReviewPreparation
from nanoreview.review.result import ReviewHandoffState
from nanoreview.review.types import (
    ALL_REVIEW_ROLES,
    EvidenceReference,
    ReviewAction,
    ReviewAssignment,
    ReviewEvidenceBundle,
    ReviewMetaKey,
    ReviewPlan,
    SkippedReviewUnit,
)
from nanoreview.session.manager import SessionManager

SESSION_KEY = "cli:review"
RUN_ID = "run-000111222333"
SNAPSHOT_REF = "review-snapshots/run-000111222333.json"


def _plan(*roles: str) -> ReviewPlan:
    return ReviewPlan(
        target="repo",
        target_name="repo",
        target_type="local",
        action=ReviewAction.DIFF,
        roles=[ALL_REVIEW_ROLES[role] for role in roles],
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
            # Answers prose instead of the verdict payload, so the batch ends in
            # ``invalid_output`` after real (billable) model calls.
            return LLMResponse(
                content="No verdicts.",
                usage=dict(self._usage) if self._usage else {},
            )
        return LLMResponse(
            content=json.dumps({"verdicts": []}),
            usage=dict(self._usage) if self._usage else {},
        )


def _build_judge(provider: _FakeJudgeProvider) -> ReviewJudge:
    """Judge sharing one ``AgentRunner``/provider with the plan run.

    The runtime is the one a review run freezes at admission; the Judge never
    re-derives model parameters of its own.
    """
    return ReviewJudge(
        runner=AgentRunner(provider),
        runtime=LLMRuntime.capture(
            provider, model="test-model", context_window_tokens=200_000
        ).with_generation(max_tokens=128),
        config=ReviewJudgeConfig(),
    )


def _security_findings() -> list[dict[str, Any]]:
    """A finding whose details match the security profile schema and whose
    evidence is present in the target file, so the validator accepts it into the
    judge scope."""
    return [
        {
            "severity": "high",
            "file": "app.py",
            "line": 1,
            "title": "Hardcoded secret",
            "evidence": "value = 1",
            "impact": "bad",
            "recommendation": "fix",
            "details": {
                "trust_boundary": "auth",
                "attack_preconditions": "untrusted input",
                "attack_path": "secret storage",
            },
        }
    ]


class _RuntimeProvider:
    """Provider stand-in for frozen runtimes; no request is sent through it."""

    def __init__(self, **generation: Any) -> None:
        self.generation = GenerationSettings(**generation)


def _reviewer_runtime(
    *, window: int = 200_000, max_tokens: int = 8_192
) -> LLMRuntime:
    """A frozen reviewer runtime, the shape the coordinator freezes at admission."""
    return LLMRuntime.capture(
        _RuntimeProvider(max_tokens=max_tokens),
        model="reviewer-model",
        context_window_tokens=window,
        role=ROLE_REVIEWER,
    )


class _Subagents:
    max_concurrent_subagents = 2

    #: Usage reported by every reviewer result metadata.
    usage: dict[str, int] = {}

    #: Terminal ``subagent_status`` reported by every reviewer result.
    status: str = "ok"

    #: Per-dimension ``subagent_status`` override, keyed by subagent label.
    status_by_label: dict[str, str] = {}

    #: Per-dimension structured findings payload, keyed by subagent label.
    payload_by_label: dict[str, list[dict[str, Any]]] = {}

    #: Per-dimension raw reviewer output, keyed by subagent label.
    result_by_label: dict[str, str] = {}

    def __init__(self) -> None:
        self.results: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.calls: list[dict] = []
        self.cancelled_sessions: list[str] = []

    def get_running_count(self) -> int:
        return 0

    def default_runtime(self) -> LLMRuntime:
        """Reviewer runtime used when a request arrives without a frozen one."""
        return _reviewer_runtime()

    async def spawn(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        label = kwargs["label"]
        findings = self.payload_by_label.get(label, [])
        metadata = {
            "subagent_label": label,
            "subagent_status": self.status_by_label.get(label, self.status),
            "subagent_result": self.result_by_label.get(
                label, json.dumps({"findings": findings})
            ),
            # The reviewer's accepted structured answer. The loop reads findings
            # from here, never from the raw text.
            "subagent_payload": {"dimension": label, "findings": findings},
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


class _BlockingSubagents(_Subagents):
    """Reviewers that never return, so the run can be cancelled mid-review."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def spawn(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


class _TrackingSubagents(_Subagents):
    """Counts reviewers in flight so the concurrency cap is observable.

    The base stub reports a constant ``get_running_count()`` of zero and hands
    its result back the moment it is spawned, so it can never show whether the
    limits actually queue anything. This variant keeps an in-flight counter that
    only drops when the loop collects a result, which is exactly the window the
    limits are meant to bound.
    """

    def __init__(self, max_concurrent: int = 2) -> None:
        super().__init__()
        self.max_concurrent_subagents = max_concurrent
        self.in_flight = 0
        self.peak = 0

    def get_running_count(self) -> int:
        return self.in_flight

    async def spawn(self, **kwargs: Any) -> str:
        result = await super().spawn(**kwargs)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        return result

    async def wait_for_session_result(self, _session_key: str, *, timeout: float):
        message = await super().wait_for_session_result(_session_key, timeout=timeout)
        self.in_flight -= 1
        return message


class _FailingCleanupSubagents(_Subagents):
    """Cleanup that reports every child release as failed."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def cancel_by_session(self, session_key: str) -> int:
        self.cancelled_sessions.append(session_key)
        raise self._error


class _InterruptedCleanupSubagents(_BlockingSubagents):
    """Reviewers that block, with cleanup interrupted by a second cancellation."""

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
    ) -> None:
        async def _prepare(_messages, _meta, progress_callback=None):
            return ReviewPreparation(plan, evidence)

        monkeypatch.setattr(
            "nanoreview.agent.review_loop.prepare_code_review_context", _prepare
        )

    return _install


def _build_loop(
    tmp_path: Path,
    *,
    subagents: Any,
    max_concurrent_subagents: int = 4,
    judge: ReviewJudge | None = None,
    sessions: SessionManager | None = None,
    artifact_store: ReviewArtifactStore | None = None,
) -> ReviewLoop:
    session_manager = sessions or SessionManager(tmp_path)
    boards = ReviewBoardStore(
        sessions=session_manager,
        artifacts=artifact_store or ReviewArtifactStore(tmp_path),
    )
    return ReviewLoop(
        workspace=tmp_path,
        sessions=session_manager,
        subagents=subagents,
        boards=boards,
        max_concurrent_subagents=max_concurrent_subagents,
        max_tool_result_chars=1000,
        judge_factory=(lambda _runtime: judge) if judge is not None else None,
    )


def _register_run(loop: ReviewLoop, *, plan: ReviewPlan | None = None) -> ReviewBoardWriter:
    """Create the run's blackboard through the store the loop reads."""
    loop._boards.create(  # noqa: SLF001 - the store is the only board writer
        SimpleNamespace(
            run_id=RUN_ID,
            session_key=SESSION_KEY,
            input_fingerprint="fp",
            snapshot_ref=SNAPSHOT_REF,
            plan=plan,
        )
    )
    writer = loop._boards.writer(SESSION_KEY)  # noqa: SLF001
    assert writer is not None
    return writer


def _board(loop: ReviewLoop) -> ReviewBoardView:
    view = loop._boards.view(SESSION_KEY)  # noqa: SLF001
    assert view is not None
    return view


def _is_running(loop: ReviewLoop) -> bool:
    return loop._boards.running(SESSION_KEY) is not None  # noqa: SLF001


def _reviewer(view: ReviewBoardView, dimension: str) -> ReviewerSnapshot:
    reviewer = view.reviewer(dimension)
    assert reviewer is not None, dimension
    return reviewer


def _judge_batch(view: ReviewBoardView) -> JudgeBatchSnapshot:
    batches = view.judge_batches
    assert len(batches) == 1, batches
    return batches[0]


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
    prepared(_plan("security", "bug"), _evidence())
    subagents = _Subagents()
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    entered: list[str] = []
    original_advance = ReviewBoardWriter.advance

    async def _record_advance(self, phase):  # noqa: ANN001 - patched method
        entered.append(phase.value)
        await original_advance(self, phase)

    monkeypatch.setattr(ReviewBoardWriter, "advance", _record_advance)

    outcome = await loop.execute(_request())

    assert entered == ["prepare", "plan", "review", "finalize", "cleanup"]
    # ``DONE`` is written by the settle step, so it never appears among the
    # phases a run walks while it is still live.
    assert "done" not in entered
    view = _board(loop)
    assert view.status is ReviewRunStatus.COMPLETED
    assert view.phase is ReviewPhase.DONE
    assert view.report_ref is not None
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
        subagents=_Subagents(),
        sessions=sessions,
    )
    _register_run(loop)
    session = sessions.get_or_create(SESSION_KEY)

    await loop.execute(_request(session=session))

    assert session.metadata[ReviewMetaKey.STATUS] == "completed"
    assert session.metadata[ReviewMetaKey.PHASE] == "done"
    assert session.metadata[ReviewMetaKey.REPORT_REF] == _board(loop).report_ref


# ---------------------------------------------------------------------------
# Planning failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_plan_fails_before_any_dispatch(tmp_path, monkeypatch) -> None:
    """A run whose plan cannot be resolved never reaches the reviewers."""

    async def _no_plan(_messages, _meta, progress_callback=None):
        return ReviewPreparation(None, "fallback")

    monkeypatch.setattr(
        "nanoreview.agent.review_loop.prepare_code_review_context", _no_plan
    )
    subagents = _Subagents()
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    outcome = await loop.execute(_request())

    assert outcome.stop_reason == "error"
    assert "no review plan" in (outcome.error or "")
    assert subagents.calls == []
    view = _board(loop)
    assert view.status is ReviewRunStatus.ERROR
    assert view.phase is ReviewPhase.DONE


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
    )
    prepared(plan, ReviewEvidenceBundle())
    loop = _build_loop(tmp_path, subagents=_Subagents())
    _register_run(loop)

    outcome = await loop.execute(_request())

    assert "Stage or modify a file inside the target" in (outcome.error or "")
    assert _board(loop).status is ReviewRunStatus.ERROR


@pytest.mark.asyncio
async def test_diff_without_evidence_reports_skipped_units(tmp_path, prepared) -> None:
    evidence = ReviewEvidenceBundle(
        skipped=(SkippedReviewUnit(path="src/big.py", reason="token_limit_exceeded"),)
    )
    prepared(_plan("security"), evidence)
    loop = _build_loop(tmp_path, subagents=_Subagents())
    _register_run(loop)

    outcome = await loop.execute(_request())

    # A diff run that produced no accepted evidence still fails, and the
    # message names the unreviewed units so the coverage gap is visible.
    assert "Diff review cannot start" in (outcome.error or "")
    assert "src/big.py" in (outcome.error or "")
    assert "token_limit_exceeded" in (outcome.error or "")


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
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    outcome = await loop.execute(_request())

    # The program builds one assignment per selected reviewer, in the order the
    # user selected them (no model decides or reorders this).
    assert [call["label"] for call in subagents.calls] == ["security", "bug"]
    # Review results are never published to the bus: the program dispatches each
    # dimension with an explicit reviewer profile and collects from the queue.
    assert all("deliver_to_bus" not in call for call in subagents.calls)
    assert [call["origin_metadata"]["profile_id"] for call in subagents.calls] == [
        "security",
        "bug",
    ]
    # The host-injected evidence is proved to the reviewer so a valid
    # ``findings: []`` answer is not misread as incomplete.
    assert all(
        call["origin_metadata"]["assigned_evidence"] == 1 for call in subagents.calls
    )
    assert "No actionable issues found" in (outcome.report_markdown or "")


@pytest.mark.asyncio
async def test_general_reviewer_is_dispatched_like_any_other(
    tmp_path, prepared
) -> None:
    """``general`` is a reviewer name, not a routing mode: it runs on its own."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = _Subagents()
    prepared(_plan("general"), _evidence())
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    await loop.execute(_request())

    assert [call["label"] for call in subagents.calls] == ["general"]
    assert [
        call["origin_metadata"]["profile_id"] for call in subagents.calls
    ] == ["general"]
    view = _board(loop)
    assert [assignment.dimension for assignment in view.assignments] == ["general"]


@pytest.mark.asyncio
async def test_general_can_be_combined_with_specialized_reviewers(
    tmp_path, prepared
) -> None:
    """A selection may mix ``general`` with any specialized reviewer."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = _Subagents()
    prepared(_plan("general", "security"), _evidence())
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    await loop.execute(_request())

    assert [call["label"] for call in subagents.calls] == ["general", "security"]
    view = _board(loop)
    assert [assignment.dimension for assignment in view.assignments] == [
        "general",
        "security",
    ]


@pytest.mark.asyncio
async def test_all_five_reviewers_can_be_dispatched(tmp_path, prepared) -> None:
    """The four-assignment ceiling is gone: all five reviewers may run."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = _Subagents()
    roles = ("bug", "security", "performance", "maintainability", "general")
    prepared(_plan(*roles), _evidence())
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    await loop.execute(_request())

    assert [call["label"] for call in subagents.calls] == list(roles)
    view = _board(loop)
    assert [assignment.dimension for assignment in view.assignments] == list(roles)
    assert all(
        assignment.evidence_ids == ("ev-001",) for assignment in view.assignments
    )


@pytest.mark.asyncio
async def test_assignments_cover_every_accepted_evidence_unit(
    tmp_path, prepared
) -> None:
    """Every reviewer receives the whole accepted set, with no second filter."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    evidence = ReviewEvidenceBundle(
        references=(
            EvidenceReference(
                id="ev-001",
                path="app.py",
                start_line=1,
                end_line=1,
                excerpt="## app.py:1-1\nvalue = 1",
            ),
            EvidenceReference(
                id="ev-002",
                path="db.py",
                start_line=7,
                end_line=9,
                excerpt="## db.py:7-9\nquery()",
            ),
        ),
        summary="## app.py:1-1",
    )
    subagents = _Subagents()
    prepared(_plan("security", "bug"), evidence)
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    await loop.execute(_request())

    view = _board(loop)
    assert [assignment.evidence_ids for assignment in view.assignments] == [
        ("ev-001", "ev-002"),
        ("ev-001", "ev-002"),
    ]
    # The dispatched task metadata proves the same count reached the reviewer.
    assert all(
        call["origin_metadata"]["assigned_evidence"] == 2
        for call in subagents.calls
    )


@pytest.mark.asyncio
async def test_budget_skipped_units_are_visible_in_the_run_state(
    tmp_path, prepared
) -> None:
    """Budget-skipped units stay in the audit trail instead of vanishing."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    evidence = ReviewEvidenceBundle(
        references=(
            EvidenceReference(
                id="ev-001",
                path="app.py",
                start_line=1,
                end_line=1,
                excerpt="## app.py:1-1\nvalue = 1",
            ),
        ),
        skipped=(
            SkippedReviewUnit(
                path="src/big.py",
                reason="budget_exhausted",
                start_line=10,
                end_line=20,
            ),
        ),
        summary="## app.py:1-1",
    )
    subagents = _Subagents()
    prepared(_plan("security"), evidence)
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    outcome = await loop.execute(_request())

    view = _board(loop)
    assert view.skipped_files == ("src/big.py",)
    assert view.evidence_stats["accepted_units"] == 1
    assert view.evidence_stats["skipped_units"] == 1
    assert "src/big.py" in (outcome.report_markdown or "")


@pytest.mark.asyncio
async def test_five_reviewers_queue_within_the_per_review_concurrency_limit(
    tmp_path, prepared
) -> None:
    """Five selections queue instead of all running at once.

    The manager-wide budget is deliberately higher than the per-review limit,
    so only the per-review limit can produce the observed peak.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = _TrackingSubagents(max_concurrent=10)
    prepared(
        _plan("bug", "security", "performance", "maintainability", "general"),
        _evidence(),
    )
    loop = _build_loop(tmp_path, subagents=subagents, max_concurrent_subagents=2)
    _register_run(loop)

    await loop.execute(_request())

    assert len(subagents.calls) == 5
    assert subagents.peak == 2


@pytest.mark.asyncio
async def test_global_reviewer_budget_queues_when_the_per_review_limit_is_higher(
    tmp_path, prepared
) -> None:
    """The manager-wide reviewer budget also bounds concurrency.

    Here the per-review limit is above the global budget, so only the global
    budget can produce the observed peak.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = _TrackingSubagents(max_concurrent=2)
    prepared(
        _plan("bug", "security", "performance", "maintainability", "general"),
        _evidence(),
    )
    loop = _build_loop(tmp_path, subagents=subagents, max_concurrent_subagents=5)
    _register_run(loop)

    await loop.execute(_request())

    assert len(subagents.calls) == 5
    assert subagents.peak == 2


@pytest.mark.asyncio
async def test_run_aggregates_agent_usage_into_the_board(tmp_path, prepared) -> None:
    """Reviewer tokens must roll up into one run total.

    There is no planner run any more, so the total is exactly the reviewer
    usage. Judge usage is exercised in tests/review/test_judge.py; the loop
    folds ``JudgeExecutionResult.usage`` into the run at the same boundary.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    subagents = _Subagents()
    subagents.usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }
    prepared(_plan("security", "bug"), _evidence())
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    await loop.execute(_request())

    view = _board(loop)
    assert view.usage == {
        "prompt_tokens": 200,  # 2 x 100 (reviewers)
        "completion_tokens": 40,
        "total_tokens": 240,
    }
    assert _reviewer(view, "security").usage == dict(subagents.usage)
    assert _reviewer(view, "bug").status == CHILD_COMPLETED


@pytest.mark.asyncio
async def test_failed_reviewer_is_recorded_as_error_and_report_is_incomplete(
    tmp_path, prepared
) -> None:
    """A reviewer failure must surface as ``error`` with a reason, not success.

    There is no outer reviewer retry: the run keeps the success path behaviour
    (validation, usage, callback) but the report is explicitly marked incomplete.
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
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    outcome = await loop.execute(_request())

    view = _board(loop)
    failed = _reviewer(view, "bug")
    assert failed.status == CHILD_ERROR
    assert failed.error
    assert "reviewer status=error" in failed.error
    assert "crashed while reading app.py" in failed.error

    completed = _reviewer(view, "security")
    assert completed.status == CHILD_COMPLETED
    assert completed.error == ""

    # Usage is folded in for both reviewers, so a partial failure does not
    # silently drop the tokens actually spent.
    assert view.usage == {
        "prompt_tokens": 200,
        "completion_tokens": 40,
        "total_tokens": 240,
    }
    assert failed.usage == dict(subagents.usage)
    assert completed.usage == dict(subagents.usage)

    # The run completes but hands over as partial, never as a clean pass.
    assert view.status is ReviewRunStatus.COMPLETED
    assert outcome.result is not None
    assert outcome.result.handoff is ReviewHandoffState.PARTIAL
    assert outcome.result.gaps


@pytest.mark.asyncio
async def test_missing_subagent_status_is_not_silently_treated_as_success(
    tmp_path, prepared
) -> None:
    """An unreported terminal status is a failure, never a silent success.

    The failed reviewer's payload is still a valid empty findings array, so
    without an explicit failure handoff the finalizer would record a clean
    ``no_findings`` dimension and the report would claim a full pass.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    subagents = _Subagents()
    subagents.status_by_label = {"security": "", "bug": "ok"}
    prepared(_plan("security", "bug"), _evidence())
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    outcome = await loop.execute(_request())

    view = _board(loop)
    assert _reviewer(view, "security").status == CHILD_ERROR
    assert "status=unknown" in _reviewer(view, "security").error
    assert _reviewer(view, "bug").status == CHILD_COMPLETED

    # The report must reflect the failure instead of "No actionable issues
    # found": the failed dimension is incomplete even though its payload was
    # well formed.
    assert "incomplete" in (outcome.report_markdown or "").lower()
    assert "No actionable issues found" not in (outcome.report_markdown or "")


@pytest.mark.asyncio
async def test_a_failed_reviewer_with_a_valid_payload_is_reported_incomplete(
    tmp_path, prepared
) -> None:
    """A non-ok terminal status wins over a parseable reviewer payload."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    subagents = _Subagents()
    subagents.status_by_label = {"bug": "error"}
    subagents.payload_by_label = {"bug": []}
    prepared(_plan("security", "bug"), _evidence())
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    outcome = await loop.execute(_request())

    assert _reviewer(_board(loop), "bug").status == CHILD_ERROR
    assert "reviewer status=error" in _reviewer(_board(loop), "bug").error
    assert "incomplete" in (outcome.report_markdown or "").lower()
    assert "No actionable issues found" not in (outcome.report_markdown or "")


@pytest.mark.asyncio
async def test_a_succeeded_reviewer_without_a_payload_is_a_failure(
    tmp_path, prepared
) -> None:
    """``status=ok`` alone is not enough: the accepted payload is the result.

    A reviewer that reported success but carried no structured findings would
    otherwise be recorded as a clean ``no_findings`` dimension.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    class _NoPayloadSubagents(_Subagents):
        async def spawn(self, **kwargs: Any) -> str:
            started = await super().spawn(**kwargs)
            queued = self.results.get_nowait()
            queued.metadata.pop("subagent_payload", None)
            await self.results.put(queued)
            return started

    prepared(_plan("security"), _evidence())
    loop = _build_loop(tmp_path, subagents=_NoPayloadSubagents())
    _register_run(loop)

    outcome = await loop.execute(_request())

    reviewer = _reviewer(_board(loop), "security")
    assert reviewer.status == CHILD_ERROR
    assert "no structured findings payload" in reviewer.error
    assert "No actionable issues found" not in (outcome.report_markdown or "")


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


def _run_with_judge(
    provider: _FakeJudgeProvider,
    *,
    findings: list[dict[str, Any]] | None = None,
    tmp_path,
    prepared,
) -> tuple[ReviewLoop, _Subagents]:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = _Subagents()
    if findings is not None:
        subagents.payload_by_label = {"security": findings}
    prepared(_plan("security"), _evidence())
    loop = _build_loop(
        tmp_path,
        subagents=subagents,
        judge=_build_judge(provider),
    )
    _register_run(loop)
    return loop, subagents


@pytest.mark.asyncio
async def test_judge_success_marks_batch_completed(tmp_path, prepared) -> None:
    """A clean judge pass (even all needs_confirmation) is recorded completed."""
    provider = _FakeJudgeProvider(usage={"total_tokens": 30})
    loop, subagents = _run_with_judge(
        provider, findings=_security_findings(), tmp_path=tmp_path, prepared=prepared
    )

    outcome = await loop.execute(_request())

    assert len(provider.calls) == 1
    view = _board(loop)
    batch = _judge_batch(view)
    assert batch.status == CHILD_COMPLETED
    assert batch.error == ""
    assert batch.stats["total_candidates"] == 1
    assert batch.stats["needs_confirmation"] == 1  # empty verdicts -> confirm
    assert batch.usage == {"total_tokens": 30}
    assert view.usage["total_tokens"] == 30
    assert "No actionable issues found" not in (outcome.report_markdown or "")
    assert len(subagents.calls) == 1


@pytest.mark.asyncio
async def test_judge_failure_marks_batch_error(tmp_path, prepared) -> None:
    """A judge provider failure records an error batch, never a false pass."""
    provider = _FakeJudgeProvider(error=RuntimeError("judge provider down"))
    loop, subagents = _run_with_judge(
        provider, findings=_security_findings(), tmp_path=tmp_path, prepared=prepared
    )

    # The judge failure is a judge-level problem: it does not abort the run.
    outcome = await loop.execute(_request())

    view = _board(loop)
    batch = _judge_batch(view)
    assert batch.status == CHILD_ERROR
    assert batch.error
    assert "judge provider down" in batch.error
    assert "No actionable issues found" not in (outcome.report_markdown or "")
    assert len(subagents.calls) == 1
    assert view.status is ReviewRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_judge_failed_batch_keeps_consumed_usage(tmp_path, prepared) -> None:
    """A failed judge batch still reports its tokens to the run."""
    provider = _FakeJudgeProvider(
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        prose=True,
    )
    loop, subagents = _run_with_judge(
        provider, findings=_security_findings(), tmp_path=tmp_path, prepared=prepared
    )

    outcome = await loop.execute(_request())

    view = _board(loop)
    batch = _judge_batch(view)
    assert batch.status == CHILD_ERROR
    assert batch.error
    assert len(provider.calls) > 1  # the model was re-prompted before failing
    # Every attempted call was billed; none of it may be dropped.
    expected = 15 * len(provider.calls)
    assert batch.usage["total_tokens"] == expected
    assert view.usage["total_tokens"] == expected
    assert "No actionable issues found" not in (outcome.report_markdown or "")
    assert len(subagents.calls) == 1


@pytest.mark.asyncio
async def test_judge_no_candidates_does_not_call_provider(tmp_path, prepared) -> None:
    """No candidates means no judge request and no leftover usage reading."""
    provider = _FakeJudgeProvider(usage={"total_tokens": 9999})
    loop, _subagents = _run_with_judge(
        provider, findings=None, tmp_path=tmp_path, prepared=prepared
    )

    await loop.execute(_request())

    assert provider.calls == []
    batch = _judge_batch(_board(loop))
    # Empty result is a completed batch, not a false error, and no usage leaks in.
    assert batch.status == CHILD_COMPLETED
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
        subagents=_Subagents(),
        artifact_store=store,
    )
    _register_run(loop)

    outcome = await loop.execute(_request())

    view = _board(loop)
    assert view.status is ReviewRunStatus.ERROR
    assert view.phase is ReviewPhase.DONE
    assert view.report_ref is None
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
    subagents = _BlockingSubagents()
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    task = asyncio.create_task(loop.execute(_request()))
    await asyncio.wait_for(subagents.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    view = _board(loop)
    assert view.status is ReviewRunStatus.STOPPED
    assert view.phase is ReviewPhase.DONE
    assert subagents.cancelled_sessions == [SESSION_KEY]


@pytest.mark.asyncio
async def test_finalize_reuses_cleanup_and_settles_children(tmp_path) -> None:
    """``/stop`` finalizes through the same CLEANUP -> DONE tail."""
    subagents = _Subagents()
    loop = _build_loop(tmp_path, subagents=subagents)
    writer = _register_run(loop)
    await writer.advance(ReviewPhase.REVIEW)
    await writer.start_assignments(
        (
            ReviewAssignment(dimension="security", focus="auth", evidence_ids=("ev-001",)),
            ReviewAssignment(dimension="bug", focus="logic", evidence_ids=("ev-001",)),
        )
    )
    await writer.reviewer_settled("security", status=CHILD_COMPLETED)
    await writer.reviewer_started("bug", evidence_assigned=1)

    result = await loop.finalize(SESSION_KEY, ReviewRunStatus.STOPPED)

    assert result is not None
    assert result.status is ReviewRunStatus.STOPPED
    view = _board(loop)
    assert view.phase is ReviewPhase.DONE
    assert _reviewer(view, "security").status == CHILD_COMPLETED
    assert _reviewer(view, "bug").status == CHILD_STOPPED
    assert _reviewer(view, "bug").error
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
        tmp_path, subagents=subagents, sessions=sessions
    )
    _register_run(loop)
    session = sessions.get_or_create(SESSION_KEY)
    session.metadata.update(_board(loop).metadata_payload())
    sessions.save(session)

    result = await loop.finalize(SESSION_KEY, ReviewRunStatus.STOPPED)

    assert result is not None
    assert result.status is ReviewRunStatus.STOPPED
    view = _board(loop)
    assert view.status is ReviewRunStatus.STOPPED
    assert view.phase is ReviewPhase.DONE
    assert view.warnings
    assert _is_running(loop) is False
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
        subagents=subagents,
        sessions=sessions,
    )
    _register_run(loop)

    outcome = await loop.execute(_request())

    assert outcome.stop_reason == "error"
    assert "cleanup" in (outcome.error or "")
    assert outcome.result is None
    assert outcome.report_markdown is not None
    view = _board(loop)
    assert view.status is ReviewRunStatus.RUNNING
    assert view.phase is ReviewPhase.CLEANUP
    assert _is_running(loop) is True
    assert loop.result(SESSION_KEY) is None
    assert sessions.get_or_create(SESSION_KEY).metadata.get(
        ReviewMetaKey.STATUS
    ) != "completed"


@pytest.mark.asyncio
async def test_terminal_save_failure_keeps_the_run_running(tmp_path, prepared) -> None:
    """A failed terminal save must not turn the live run into ``DONE``.

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
        subagents=_Subagents(),
        sessions=sessions,
    )
    _register_run(loop)
    session = sessions.get_or_create(SESSION_KEY)

    outcome = await loop.execute(_request(session=session))

    assert outcome.stop_reason == "error"
    assert "could not be persisted" in (outcome.error or "")
    assert outcome.result is None
    view = _board(loop)
    assert view.status is ReviewRunStatus.RUNNING
    assert view.phase is not ReviewPhase.DONE
    assert _is_running(loop) is True
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
    loop = _build_loop(tmp_path, subagents=subagents)
    writer = _register_run(loop)
    await writer.advance(ReviewPhase.REVIEW)

    assert await loop.finalize(SESSION_KEY, ReviewRunStatus.STOPPED) is None
    assert _board(loop).status is ReviewRunStatus.RUNNING
    assert _is_running(loop) is True


@pytest.mark.asyncio
async def test_finalize_keeps_the_gate_when_the_save_fails(tmp_path) -> None:
    """``/stop`` whose terminal write fails keeps the run unsettled."""
    sessions = _FailingSaveSessions(tmp_path)
    loop = _build_loop(
        tmp_path,
        subagents=_Subagents(),
        sessions=sessions,
    )
    writer = _register_run(loop)
    await writer.advance(ReviewPhase.REVIEW)

    assert await loop.finalize(SESSION_KEY, ReviewRunStatus.STOPPED) is None
    assert _board(loop).status is ReviewRunStatus.RUNNING
    assert _is_running(loop) is True


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
    subagents = _InterruptedCleanupSubagents()
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    task = asyncio.create_task(loop.execute(_request()))
    await asyncio.wait_for(subagents.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    view = _board(loop)
    assert view.status is ReviewRunStatus.RUNNING
    assert view.phase is ReviewPhase.CLEANUP
    assert loop.result(SESSION_KEY) is None
    assert any("interrupted" in warning for warning in view.warnings)
    assert subagents.cancelled_sessions == [SESSION_KEY]


@pytest.mark.asyncio
async def test_execute_without_a_live_run_reports_failure(tmp_path) -> None:
    loop = _build_loop(tmp_path, subagents=_Subagents())

    outcome = await loop.execute(_request())

    assert isinstance(outcome, ReviewLoopOutcome)
    assert outcome.stop_reason == "error"
    assert outcome.error is not None


def test_review_planning_error_is_the_module_contract() -> None:
    assert issubclass(ReviewPlanningError, RuntimeError)


# ---------------------------------------------------------------------------
# Frozen review runtimes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_run_binds_the_frozen_reviewer_runtime(tmp_path, prepared) -> None:
    """Reviewers run on the runtime frozen at admission, not on live defaults.

    The window also decides the evidence budget: a run started on a 128k model
    must not be measured against the conversation agent's window, and the same
    number must reach the navigation metadata that gates evidence dispatch.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    prepared(_plan("security"), _evidence())
    subagents = _Subagents()
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)
    runtime = _reviewer_runtime(window=131_072, max_tokens=4_096)

    await loop.execute(_request(reviewer_runtime=runtime))

    limits = subagents.calls[0]["execution_limits"]
    assert limits.runtime is runtime
    assert limits.max_iterations == REVIEWER_MODEL_REQUEST_LIMIT
    assert (
        subagents.calls[0]["origin_metadata"][ReviewMetaKey.DIFF_CONTEXT_WINDOW_TOKENS]
        == 131_072
    )


@pytest.mark.asyncio
async def test_a_request_without_a_runtime_falls_back_to_the_live_selection(
    tmp_path, prepared
) -> None:
    """Direct loop construction keeps working with the subagent manager's runtime."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    prepared(_plan("security"), _evidence())
    subagents = _Subagents()
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)

    await loop.execute(_request())

    limits = subagents.calls[0]["execution_limits"]
    assert limits.runtime is not None
    assert limits.runtime.role == ROLE_REVIEWER
    assert limits.max_iterations == REVIEWER_MODEL_REQUEST_LIMIT


def test_review_budget_contract_is_derived_from_the_runtime() -> None:
    """The audit contract records the run's numbers, never project-wide constants."""
    runtime = _reviewer_runtime(window=64_000, max_tokens=2_048)

    assert review_budget_contract(runtime) == {
        "context_window_tokens": 64_000,
        "reviewer_max_output_tokens": 2_048,
        "reviewer_model_request_limit": 30,
    }


@pytest.mark.asyncio
async def test_run_records_evidence_and_assignment_stats_on_the_board(
    tmp_path, prepared
) -> None:
    """The run's audit trail records the accepted evidence and the task split."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    prepared(_plan("security", "bug"), _evidence())
    loop = _build_loop(tmp_path, subagents=_Subagents())
    _register_run(loop)

    await loop.execute(_request())

    view = _board(loop)
    assert view.evidence_stats["accepted_units"] == 1
    assert view.evidence_stats["skipped_units"] == 0
    assert view.evidence_stats["status"] == "ok"
    assert [dict(entry) for entry in view.assignment_stats] == [
        {"dimension": "security", "evidence_units": 1},
        {"dimension": "bug", "evidence_units": 1},
    ]
    # Both reviewers received the whole accepted set: no second filtering step.
    assert [assignment.evidence_ids for assignment in view.assignments] == [
        ("ev-001",),
        ("ev-001",),
    ]


@pytest.mark.asyncio
async def test_plan_snapshot_records_evidence_and_assignments(
    tmp_path, prepared
) -> None:
    """The frozen audit record carries what was accepted and how it was split.

    The snapshot is the run's only preserved input record, so the acceptance
    counts, the authorized evidence IDs and the per-reviewer task split must be
    readable there — not only on the board, which does not survive a restart.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    prepared(_plan("security", "bug"), _evidence())
    loop = _build_loop(tmp_path, subagents=_Subagents())
    writer = _register_run(loop)
    loop._snapshots.write(  # noqa: SLF001 - the store is the only snapshot writer
        build_snapshot(
            run_id=writer.run_id,
            session_key=SESSION_KEY,
            action="diff",
            target_type="local",
            target=str(tmp_path),
            input_fingerprint="fp",
            net_diff={"app.py": "@@ -0,0 +1 @@\n+value = 1"},
            changed_files=["app.py"],
        )
    )

    await loop.execute(_request())

    snapshot = loop._snapshots.read(writer.run_id)  # noqa: SLF001
    assert snapshot is not None
    evidence = snapshot["evidence"]
    assert evidence["accepted_units"] == 1
    assert evidence["accepted_ids"] == ["ev-001"]
    assert evidence["skipped_units"] == 0
    assert snapshot["assignments"] == [
        {"dimension": "security", "evidence_units": 1},
        {"dimension": "bug", "evidence_units": 1},
    ]
    # The pre-Planner keys are gone rather than written empty.
    assert "planner_manifest" not in snapshot
    assert "planner_triage" not in snapshot


@pytest.mark.asyncio
async def test_prepare_failure_settles_run_as_error(tmp_path, monkeypatch) -> None:
    """A failed evidence preparation settles the run instead of hanging."""
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("prefetch backend is down")

    monkeypatch.setattr(
        "nanoreview.agent.review_loop.prepare_code_review_context", _boom
    )
    loop = _build_loop(tmp_path, subagents=_Subagents())
    _register_run(loop)

    outcome = await loop.execute(_request())

    assert "prefetch backend is down" in (outcome.error or "")
    assert _board(loop).status is ReviewRunStatus.ERROR


@pytest.mark.asyncio
async def test_changed_file_boundary_comes_from_the_admitted_snapshot(
    tmp_path, prepared
) -> None:
    """The validator diff boundary is the snapshot's changed set, not empty.

    A regression here silently disabled ``validator``'s out-of-scope check (so
    findings in unmodified files were accepted) and made coverage report an
    empty changed-file list.
    """
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    prepared(_plan("security", "bug"), _evidence())
    subagents = _Subagents()
    loop = _build_loop(tmp_path, subagents=subagents)
    _register_run(loop)
    loop._snapshots.write(
        build_snapshot(
            run_id=RUN_ID,
            session_key=SESSION_KEY,
            action="diff",
            target_type="local",
            target=str(tmp_path),
            input_fingerprint="fp",
            net_diff={"app.py": "@@ -0,0 +1 @@\n+value = 1"},
            changed_files=["app.py", "src/db.py"],
        )
    )

    await loop.execute(_request())

    assert list(_board(loop).changed_files) == ["app.py", "src/db.py"]
    # The frozen diff is an internal planning input; it never reaches reviewer
    # metadata (where it would be persisted with the subagent result).
    assert all(
        ReviewMetaKey.FROZEN_DIFF not in call["origin_metadata"]
        for call in subagents.calls
    )


@pytest.mark.asyncio
async def test_review_reviews_the_frozen_snapshot_diff_not_the_worktree(
    tmp_path, monkeypatch
) -> None:
    """The plan is built from the snapshot net diff, ignoring live edits.

    ``prepare_code_review_context`` is the PREPARE entry point, so the frozen
    diff handoff (``ReviewMetaKey.FROZEN_DIFF``) must be present in the metadata
    it receives before prefetch can dispatch against it.
    """
    from nanoreview.review.input.snapshot import ReviewSnapshotStore

    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    async def _prepare(_messages, meta, progress_callback=None):
        captured.update(meta)
        return ReviewPreparation(_plan("security"), "plan", _evidence())

    monkeypatch.setattr(
        "nanoreview.agent.review_loop.prepare_code_review_context", _prepare
    )
    loop = _build_loop(tmp_path, subagents=_Subagents())
    _register_run(loop)
    ReviewSnapshotStore(tmp_path).write(
        build_snapshot(
            run_id=RUN_ID,
            session_key=SESSION_KEY,
            action="diff",
            target_type="local",
            target=str(tmp_path),
            input_fingerprint="fp",
            net_diff={"app.py": "@@ -0,0 +1 @@\n+frozen"},
            changed_files=["app.py"],
        )
    )

    await loop.execute(_request())

    assert captured[ReviewMetaKey.FROZEN_DIFF] == {
        "patches": {"app.py": "@@ -0,0 +1 @@\n+frozen"},
        "skipped": {},
    }


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
    loop = _build_loop(tmp_path, subagents=_Subagents())
    writer = _register_run(loop)

    with pytest.raises(ReviewPlanningError, match="no review plan"):
        await loop._prepare_review(_request(), _board(loop), writer)


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
        return ReviewPreparation(plan, _evidence())

    monkeypatch.setattr(
        "nanoreview.agent.review_loop.prepare_code_review_context", _prepare
    )
    loop = _build_loop(
        tmp_path, subagents=_Subagents(), sessions=sessions
    )
    writer = _register_run(loop)
    session = sessions.get_or_create(SESSION_KEY)

    inputs = await loop._prepare_review(_request(session=session), _board(loop), writer)

    assert session.metadata[ReviewMetaKey.ALLOWED_DIMENSIONS] == ["security"]
    assert session.metadata[ReviewMetaKey.LOCAL_ROOT] == str(tmp_path)
    assert inputs.plan is plan
    assert inputs.validation_workspace == str(tmp_path)
