"""Tests for the review blackboard, input fingerprint, and report artifacts.

The run's live state is the coordinator-owned blackboard (``review_board.py``)
plus the durable pieces kept in ``review_state.py`` (phase/status vocabulary,
fingerprint, artifact store). These tests drive the real board store rather than
hand-building a mutable state object, because the invariant under test is that
*only* the store writes a board and that every write is serialized per run.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanoreview.agent.review_board import (
    CHILD_COMPLETED,
    CHILD_ERROR,
    CHILD_PENDING,
    CHILD_RUNNING,
    CHILD_STOPPED,
    ReviewBoardError,
    ReviewBoardPersistenceError,
    ReviewBoardStore,
    ReviewerRunState,
)
from nanoreview.agent.review_state import (
    CHILD_ERROR_MAX_CHARS,
    REVIEW_ARTIFACTS_DIR_NAME,
    ReviewArtifactError,
    ReviewArtifactStore,
    ReviewPhase,
    ReviewRunStatus,
    bound_child_error,
    build_report_artifact,
    compute_input_fingerprint,
    compute_review_input_fingerprint,
    new_review_run_id,
    serialize_finalizer_result,
)
from nanoreview.review.output.finalizer import ReviewFinalizerResult
from nanoreview.review.types import (
    FindingVerdict,
    ReviewAssignment,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
    ReviewJudgeDecision,
    ReviewJudgedFinding,
    ReviewJudgeVerdict,
    ReviewMetaKey,
)
from nanoreview.session.manager import SessionManager

RUN_ID = "run-000111222333"
SESSION_KEY = "websocket:chat-1"
SNAPSHOT_REF = "review-snapshots/run-000111222333.json"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _store(workspace: Path, *, artifacts: ReviewArtifactStore | None = None) -> ReviewBoardStore:
    return ReviewBoardStore(
        sessions=SessionManager(workspace),
        artifacts=artifacts or ReviewArtifactStore(workspace),
    )


@pytest.fixture
def boards(tmp_path: Path) -> ReviewBoardStore:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return _store(workspace)


def _create(
    store: ReviewBoardStore,
    *,
    snapshot_ref: str | None = SNAPSHOT_REF,
    run_id: str = RUN_ID,
    session_key: str = SESSION_KEY,
):
    """Register one run's board and return its write handle."""
    store.create(
        SimpleNamespace(
            run_id=run_id,
            session_key=session_key,
            input_fingerprint="fp-1",
            snapshot_ref=snapshot_ref,
            plan=None,
        )
    )
    writer = store.writer(session_key)
    assert writer is not None
    return writer


def _view(store: ReviewBoardStore):
    view = store.view(SESSION_KEY)
    assert view is not None
    return view


def _assignment(dimension: str, *, evidence: tuple[str, ...] = ()) -> ReviewAssignment:
    return ReviewAssignment(dimension=dimension, focus="x", evidence_ids=evidence)


class _FailingSessions(SessionManager):
    """Session manager whose durable save always fails."""

    def save(self, session):  # noqa: D102 - stub
        raise OSError("disk full")


def _candidate(
    title: str,
    *,
    severity: str = "high",
    file: str = "src/app.py",
    line: int | None = 1,
    dimension: str = "security",
) -> ReviewFindingCandidate:
    return ReviewFindingCandidate(
        severity=severity,
        dimension=dimension,
        file=file,
        line=line,
        title=title,
        evidence="evidence",
        impact="impact",
        recommendation="fix it",
    )


def _accepted(title: str, **kw) -> ReviewJudgedFinding:
    return ReviewJudgedFinding(
        candidate=_candidate(title, **kw),
        hard_verdict=ReviewFindingVerdict(
            verdict=FindingVerdict.ACCEPTED, reason="hard validation accepted"
        ),
        judge_verdict=ReviewJudgeVerdict(
            decision=ReviewJudgeDecision.ACCEPT, reason="supported", confidence="high"
        ),
    )


def _uncertain(title: str, **kw) -> ReviewJudgedFinding:
    return ReviewJudgedFinding(
        candidate=_candidate(title, **kw),
        hard_verdict=ReviewFindingVerdict(
            verdict=FindingVerdict.UNCERTAIN, reason="unclear evidence"
        ),
        judge_verdict=ReviewJudgeVerdict(
            decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
            reason="unclear",
            confidence="low",
        ),
    )


# ---------------------------------------------------------------------------
# Board lifecycle
# ---------------------------------------------------------------------------


def test_create_registers_a_running_prepare_board(boards: ReviewBoardStore) -> None:
    view = boards.create(
        SimpleNamespace(
            run_id=RUN_ID,
            session_key=SESSION_KEY,
            input_fingerprint="fp-1",
            snapshot_ref=SNAPSHOT_REF,
            plan=None,
        )
    )

    assert view.run_id == RUN_ID
    assert view.session_key == SESSION_KEY
    assert view.snapshot_ref == SNAPSHOT_REF
    assert view.phase is ReviewPhase.PREPARE
    assert view.status is ReviewRunStatus.RUNNING
    assert view.is_running and not view.is_terminal and not view.is_done


def test_view_and_running_read_the_same_board(boards: ReviewBoardStore) -> None:
    _create(boards)

    assert boards.view(SESSION_KEY) is not None
    assert boards.running(SESSION_KEY) is not None
    assert boards.view("websocket:other") is None


def test_running_returns_none_once_the_run_is_terminal(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.settle(ReviewRunStatus.ERROR)

        assert boards.view(SESSION_KEY) is not None
        assert boards.running(SESSION_KEY) is None

    asyncio.run(_run())


def test_drop_removes_the_board_and_returns_its_last_view(
    boards: ReviewBoardStore,
) -> None:
    _create(boards)

    dropped = boards.drop(SESSION_KEY)

    assert dropped is not None and dropped.run_id == RUN_ID
    assert boards.view(SESSION_KEY) is None
    assert boards.writer(SESSION_KEY) is None
    assert boards.drop(SESSION_KEY) is None


def test_drop_unstarted_only_removes_a_prepare_run(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        _create(boards)
        assert boards.drop_unstarted(SESSION_KEY, run_id=RUN_ID) is not None
        assert boards.view(SESSION_KEY) is None

        started = _create(boards)
        await started.advance(ReviewPhase.PLAN)
        # A run that already left PREPARE may still produce work, so it is kept.
        assert boards.drop_unstarted(SESSION_KEY, run_id=RUN_ID) is None
        assert boards.view(SESSION_KEY) is not None

    asyncio.run(_run())


def test_drop_unstarted_ignores_a_run_this_turn_does_not_own(
    boards: ReviewBoardStore,
) -> None:
    """Only the ending turn's own run is released, never a queued admission."""

    async def _run() -> None:
        _create(boards)
        assert boards.drop_unstarted(SESSION_KEY, run_id="run-other") is None
        view = boards.view(SESSION_KEY)
        assert view is not None and view.run_id == RUN_ID

    asyncio.run(_run())


def test_drop_unstarted_clears_the_persisted_run_identity(tmp_path: Path) -> None:
    """A released run must not linger on the session as an unfinished duplicate."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = SessionManager(workspace)
    store = ReviewBoardStore(
        sessions=sessions, artifacts=ReviewArtifactStore(workspace)
    )

    async def _run() -> None:
        _create(store)
        session = sessions.get_or_create(SESSION_KEY)
        session.metadata[ReviewMetaKey.RUN_ID] = RUN_ID
        session.metadata[ReviewMetaKey.STATUS] = ReviewRunStatus.RUNNING.value
        sessions.save(session)

        assert store.drop_unstarted(SESSION_KEY, run_id=RUN_ID) is not None

        released = sessions.get_or_create(SESSION_KEY)
        assert ReviewMetaKey.RUN_ID not in released.metadata
        assert ReviewMetaKey.STATUS not in released.metadata

    asyncio.run(_run())


def test_writes_after_drop_raise_the_board_error(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        boards.drop(SESSION_KEY)

        with pytest.raises(ReviewBoardError):
            await writer.add_warning("late")

    asyncio.run(_run())


def test_a_writer_for_another_run_id_is_rejected(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        stale = _create(boards)
        boards.drop(SESSION_KEY)
        # A new run replaces the old board under the same session key.
        _create(boards, run_id="run-ffffffffffff")

        with pytest.raises(ReviewBoardError, match="run mismatch"):
            await stale.add_warning("belongs to the dropped run")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Phase and metadata
# ---------------------------------------------------------------------------


def test_advance_moves_through_the_phases(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        for phase in (ReviewPhase.PLAN, ReviewPhase.REVIEW, ReviewPhase.FINALIZE):
            await writer.advance(phase)
            assert _view(boards).phase is phase

    asyncio.run(_run())


def test_advance_is_ignored_once_the_run_is_terminal(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.advance(ReviewPhase.REVIEW)
        await writer.settle(ReviewRunStatus.COMPLETED)

        await writer.advance(ReviewPhase.PREPARE)

        assert _view(boards).phase is ReviewPhase.DONE

    asyncio.run(_run())


def test_running_metadata_payload_shape(boards: ReviewBoardStore) -> None:
    _create(boards)

    payload = _view(boards).metadata_payload()

    assert payload == {
        ReviewMetaKey.RUN_ID: RUN_ID,
        ReviewMetaKey.STATUS: "running",
        ReviewMetaKey.PHASE: "prepare",
        ReviewMetaKey.INPUT_FINGERPRINT: "fp-1",
    }
    assert ReviewMetaKey.REPORT_REF not in payload
    assert ReviewMetaKey.SUMMARY not in payload
    assert ReviewMetaKey.ERROR not in payload


def test_warnings_are_deduplicated_and_blank_ones_ignored(
    boards: ReviewBoardStore,
) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.add_warning("w-1")
        await writer.add_warning("w-1")
        await writer.add_warning("  ")

        assert _view(boards).warnings == ("w-1",)

    asyncio.run(_run())


def test_terminal_payload_persists_the_bounded_summary_and_reason(
    boards: ReviewBoardStore,
) -> None:
    """A restart must be able to read *why* a run ended, not just that it did."""

    async def _run() -> None:
        writer = _create(boards)
        await writer.add_warning("plan failed: no reviewer profile for 'security'")
        await writer.settle(ReviewRunStatus.ERROR)

        payload = _view(boards).metadata_payload()

        assert payload[ReviewMetaKey.STATUS] == "error"
        assert payload[ReviewMetaKey.PHASE] == "done"
        assert payload[ReviewMetaKey.ERROR] == (
            "plan failed: no reviewer profile for 'security'"
        )

    asyncio.run(_run())


def test_terminal_payload_bounds_the_persisted_reason(
    boards: ReviewBoardStore,
) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.add_warning("x" * (CHILD_ERROR_MAX_CHARS + 50))
        await writer.settle(ReviewRunStatus.STOPPED)

        assert len(_view(boards).metadata_payload()[ReviewMetaKey.ERROR]) == (
            CHILD_ERROR_MAX_CHARS
        )

    asyncio.run(_run())


def test_completed_payload_carries_no_failure_reason(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.add_warning("reviewer warning")
        await writer.settle(ReviewRunStatus.COMPLETED)

        payload = _view(boards).metadata_payload()

        assert payload[ReviewMetaKey.STATUS] == "completed"
        assert ReviewMetaKey.ERROR not in payload

    asyncio.run(_run())


def test_persist_metadata_mirrors_the_board_onto_the_session(
    boards: ReviewBoardStore,
) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.advance(ReviewPhase.REVIEW)
        await writer.persist_metadata()

        session = boards._sessions.get_or_create(SESSION_KEY)  # noqa: SLF001
        assert session.metadata[ReviewMetaKey.RUN_ID] == RUN_ID
        assert session.metadata[ReviewMetaKey.PHASE] == "review"
        assert session.metadata[ReviewMetaKey.STATUS] == "running"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Reviewer / judge child state
# ---------------------------------------------------------------------------


def test_start_assignments_opens_one_slot_per_dimension(
    boards: ReviewBoardStore,
) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.start_assignments(
            (_assignment("bug"), _assignment("security"))
        )

        view = _view(boards)
        assert {reviewer.dimension: reviewer.status for reviewer in view.reviewers} == {
            "bug": CHILD_PENDING,
            "security": CHILD_PENDING,
        }
        assert view.reviewer("bug") is not None
        assert view.reviewer("performance") is None

    asyncio.run(_run())


def test_reviewer_started_records_its_evidence_count(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.start_assignments((_assignment("bug"),))
        await writer.reviewer_started("bug", evidence_assigned=3)

        reviewer = _view(boards).reviewer("bug")
        assert reviewer is not None
        assert reviewer.status == CHILD_RUNNING
        assert reviewer.evidence_assigned == 3

    asyncio.run(_run())


def test_reviewer_settled_records_status_usage_and_counters(
    boards: ReviewBoardStore,
) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.start_assignments((_assignment("bug"),))
        await writer.reviewer_started("bug", evidence_assigned=2)
        await writer.reviewer_settled(
            "bug",
            status=CHILD_COMPLETED,
            usage={"input_tokens": 10, "total_tokens": 12},
            duplicate_reads=1,
            duplicate_searches=4,
        )

        reviewer = _view(boards).reviewer("bug")
        assert reviewer is not None
        assert reviewer.status == CHILD_COMPLETED
        assert reviewer.error == ""
        assert reviewer.evidence_assigned == 2
        assert dict(reviewer.usage) == {"input_tokens": 10, "total_tokens": 12}
        assert reviewer.duplicate_reads == 1
        assert reviewer.duplicate_searches == 4

    asyncio.run(_run())


def test_completed_is_terminal_for_a_reviewer_slot(boards: ReviewBoardStore) -> None:
    """A late failure can never downgrade a reviewer that already completed."""

    async def _run() -> None:
        writer = _create(boards)
        await writer.start_assignments((_assignment("bug"),))
        await writer.reviewer_settled("bug", status=CHILD_COMPLETED)
        await writer.reviewer_settled("bug", status=CHILD_ERROR, error="late")

        reviewer = _view(boards).reviewer("bug")
        assert reviewer is not None
        assert reviewer.status == CHILD_COMPLETED
        assert reviewer.error == ""

    asyncio.run(_run())


def test_a_failed_reviewer_keeps_a_bounded_reason(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.start_assignments((_assignment("bug"),))
        await writer.reviewer_settled(
            "bug", status=CHILD_ERROR, error="x" * (CHILD_ERROR_MAX_CHARS + 50)
        )

        reviewer = _view(boards).reviewer("bug")
        assert reviewer is not None
        assert reviewer.status == CHILD_ERROR
        assert len(reviewer.error) == CHILD_ERROR_MAX_CHARS

    asyncio.run(_run())


def test_stopped_is_distinct_from_completed(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.start_assignments((_assignment("bug"),))
        await writer.reviewer_settled(
            "bug", status=CHILD_STOPPED, error="review stopped"
        )

        reviewer = _view(boards).reviewer("bug")
        assert reviewer is not None
        assert reviewer.status == CHILD_STOPPED
        assert reviewer.status != CHILD_COMPLETED
        assert reviewer.error == "review stopped"

    asyncio.run(_run())


def test_record_dimension_replaces_by_dimension(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.record_dimension(
            ReviewDimensionResult(dimension="bug", status="validated")
        )
        await writer.record_dimension(
            ReviewDimensionResult(dimension="security", status="validated")
        )
        await writer.record_dimension(
            ReviewDimensionResult(dimension="bug", status="failed")
        )

        dimensions = {item.dimension: item.status for item in _view(boards).dimensions}
        assert dimensions == {"bug": "failed", "security": "validated"}

    asyncio.run(_run())


def test_judge_batch_lifecycle(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.judge_started("judge")
        assert _view(boards).judge_batches[0].status == CHILD_RUNNING

        await writer.judge_settled(
            "judge",
            status=CHILD_COMPLETED,
            stats={"total": 2},
            usage={"total_tokens": 7},
        )

        batch = _view(boards).judge_batches[0]
        assert batch.status == CHILD_COMPLETED
        assert batch.error == ""
        assert dict(batch.stats) == {"total": 2}
        assert dict(batch.usage) == {"total_tokens": 7}

    asyncio.run(_run())


def test_judge_failure_keeps_a_bounded_reason(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.judge_started("judge")
        await writer.judge_settled(
            "judge", status=CHILD_ERROR, error="y" * (CHILD_ERROR_MAX_CHARS + 50)
        )

        batch = _view(boards).judge_batches[0]
        assert batch.status == CHILD_ERROR
        assert len(batch.error) == CHILD_ERROR_MAX_CHARS

    asyncio.run(_run())


def test_record_judged_replaces_the_candidate_sets(boards: ReviewBoardStore) -> None:
    """Judge verdicts and the report must read the same candidate version."""

    async def _run() -> None:
        writer = _create(boards)
        await writer.record_dimension(
            ReviewDimensionResult(dimension="security", status="validated")
        )
        judged = (
            ReviewDimensionResult(
                dimension="security", status="validated", judged=[_accepted("Real bug")]
            ),
        )
        await writer.record_judged(judged, {"accepted": 1})

        view = _view(boards)
        assert view.dimensions == judged
        assert view.judge_stats == {"accepted": 1}

    asyncio.run(_run())


def test_board_usage_accumulates_across_agent_runs(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.add_usage(
            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        )
        await writer.add_usage(
            {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
        )
        assert dict(_view(boards).usage) == {
            "prompt_tokens": 150,
            "completion_tokens": 30,
            "total_tokens": 180,
        }

        # Empty, non-numeric, negative and None payloads are ignored.
        await writer.add_usage(None)
        await writer.add_usage({})
        await writer.add_usage({"total_tokens": "not-a-number"})
        await writer.add_usage({"total_tokens": -5})
        assert _view(boards).usage["total_tokens"] == 180

    asyncio.run(_run())


def test_reviewer_state_usage_accumulates() -> None:
    reviewer = ReviewerRunState(dimension="security")
    reviewer.add_usage({"total_tokens": 11})
    reviewer.add_usage({"prompt_tokens": 4, "total_tokens": 9})
    assert reviewer.usage == {"prompt_tokens": 4, "total_tokens": 20}


def test_a_view_is_a_frozen_snapshot(boards: ReviewBoardStore) -> None:
    """A view taken before a write must not observe that write."""

    async def _run() -> None:
        writer = _create(boards)
        await writer.start_assignments((_assignment("bug"),))
        before = _view(boards)

        await writer.reviewer_started("bug", evidence_assigned=1)

        assert before.reviewer("bug") is not None
        assert before.reviewer("bug").status == CHILD_PENDING
        assert _view(boards).reviewer("bug").status == CHILD_RUNNING

    asyncio.run(_run())


def test_concurrent_reviewer_completions_are_serialized(
    boards: ReviewBoardStore,
) -> None:
    """Two reviewers settling at once must both land (no lost update)."""

    async def _run() -> None:
        writer = _create(boards)
        await writer.start_assignments(
            (_assignment("bug"), _assignment("security"), _assignment("performance"))
        )
        await asyncio.gather(
            writer.reviewer_settled("bug", status=CHILD_COMPLETED, usage={"total_tokens": 5}),
            writer.reviewer_settled(
                "security", status=CHILD_COMPLETED, usage={"total_tokens": 7}
            ),
            writer.reviewer_settled(
                "performance", status=CHILD_ERROR, error="boom"
            ),
            writer.add_usage({"total_tokens": 11}),
        )

        view = _view(boards)
        assert {
            reviewer.dimension: reviewer.status for reviewer in view.reviewers
        } == {
            "bug": CHILD_COMPLETED,
            "security": CHILD_COMPLETED,
            "performance": CHILD_ERROR,
        }

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Report artifact recording
# ---------------------------------------------------------------------------


def test_record_report_persists_the_artifact_and_records_its_ref(
    boards: ReviewBoardStore,
) -> None:
    async def _run() -> None:
        writer = _create(boards)
        findings = ({"id": "F001", "title": "Real bug"},)
        ref = await writer.record_report(
            report_markdown="# report", findings=findings, verdicts=[]
        )

        assert ref == f"{REVIEW_ARTIFACTS_DIR_NAME}/{RUN_ID}.json"
        view = _view(boards)
        assert view.report_ref == ref
        assert view.findings == findings
        assert view.summary == "# report"

    asyncio.run(_run())


def test_a_failed_artifact_write_records_a_warning_and_no_ref(
    tmp_path: Path,
) -> None:
    class _FailingArtifacts(ReviewArtifactStore):
        def write(self, artifact: dict[str, Any]) -> str | None:  # noqa: D102 - stub
            return None

    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = _store(workspace, artifacts=_FailingArtifacts(workspace))

    async def _run() -> None:
        writer = _create(store)
        ref = await writer.record_report(
            report_markdown="# report", findings=(), verdicts=[]
        )

        assert ref is None
        view = _view(store)
        assert view.report_ref is None
        assert "Failed to persist the review report artifact." in view.warnings

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Settle
# ---------------------------------------------------------------------------


def test_settle_publishes_done_after_persisting(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        view = await writer.settle(ReviewRunStatus.COMPLETED)

        assert view.status is ReviewRunStatus.COMPLETED
        assert view.phase is ReviewPhase.DONE
        assert view.is_terminal and view.is_done
        session = boards._sessions.get_or_create(SESSION_KEY)  # noqa: SLF001
        assert session.metadata[ReviewMetaKey.STATUS] == "completed"
        assert session.metadata[ReviewMetaKey.PHASE] == "done"

    asyncio.run(_run())


def test_settle_is_idempotent(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.settle(ReviewRunStatus.COMPLETED)
        again = await writer.settle(ReviewRunStatus.ERROR)

        assert again.status is ReviewRunStatus.COMPLETED

    asyncio.run(_run())


def test_settle_as_error_or_stopped_fails_unfinished_children(
    boards: ReviewBoardStore,
) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.start_assignments((_assignment("bug"), _assignment("security")))
        await writer.reviewer_started("bug", evidence_assigned=1)
        await writer.reviewer_settled("security", status=CHILD_COMPLETED)
        await writer.judge_started("judge")

        view = await writer.settle(ReviewRunStatus.STOPPED)

        assert view.reviewer("bug").status == CHILD_STOPPED
        assert view.reviewer("bug").error == "review stopped before the run finished"
        # A completed reviewer is left alone.
        assert view.reviewer("security").status == CHILD_COMPLETED
        assert view.judge_batches[0].status == CHILD_STOPPED

    asyncio.run(_run())


def test_settle_as_completed_does_not_rewrite_children(
    boards: ReviewBoardStore,
) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.start_assignments((_assignment("bug"),))
        # A completed run with a pending slot is an inconsistency the board
        # reports (``incomplete_dimensions``) instead of silently "fixing".
        view = await writer.settle(ReviewRunStatus.COMPLETED)

        assert view.reviewer("bug").status == CHILD_PENDING
        assert view.coverage_payload()["incomplete_dimensions"] == ["bug"]

    asyncio.run(_run())


def test_a_failed_terminal_save_keeps_the_run_running(tmp_path: Path) -> None:
    """The conversation gate must never open over a state it cannot prove."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = ReviewBoardStore(
        sessions=_FailingSessions(workspace),
        artifacts=ReviewArtifactStore(workspace),
    )

    async def _run() -> None:
        writer = _create(store)
        with pytest.raises(ReviewBoardPersistenceError):
            await writer.settle(ReviewRunStatus.COMPLETED)

        view = _view(store)
        assert view.status is ReviewRunStatus.RUNNING
        assert view.phase is not ReviewPhase.DONE
        assert any("could not be persisted" in item for item in view.warnings)

    asyncio.run(_run())


def test_a_failed_terminal_save_restores_the_previous_metadata(
    tmp_path: Path,
) -> None:
    class _Sessions(SessionManager):
        """First save succeeds, the terminal save fails."""

        def __init__(self, workspace: Path) -> None:
            super().__init__(workspace)
            self.calls = 0

        def save(self, session):  # noqa: D102 - stub
            self.calls += 1
            if self.calls > 1:
                raise OSError("disk full")
            super().save(session)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = _Sessions(workspace)
    store = ReviewBoardStore(
        sessions=sessions, artifacts=ReviewArtifactStore(workspace)
    )

    async def _run() -> None:
        writer = _create(store)
        await writer.persist_metadata()  # first save succeeds
        with pytest.raises(ReviewBoardPersistenceError):
            await writer.settle(ReviewRunStatus.ERROR)

        session = sessions.get_or_create(SESSION_KEY)
        # The failed terminal write was rolled back to the pre-settle values.
        assert session.metadata[ReviewMetaKey.STATUS] == "running"
        assert session.metadata[ReviewMetaKey.PHASE] == "prepare"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_new_review_run_id_is_filesystem_safe() -> None:
    run_id = new_review_run_id()
    assert run_id.startswith("run-")
    assert len(run_id) == len("run-") + 12


def test_input_fingerprint_is_stable_and_input_sensitive() -> None:
    kwargs = {
        "target": "src/app.py",
        "target_type": "local",
        "action": "repo",
        "roles": ["security", "tests"],
        "scope": {"root": "/work/repo"},
        "evidence_manifest": [{"id": "ev-1", "path": "a.py", "tokens": 10}],
    }
    first = compute_input_fingerprint(**kwargs)
    # Role order and duplicate roles must not change the fingerprint.
    second = compute_input_fingerprint(
        **{**kwargs, "roles": ["tests", "security", "tests"]}
    )
    assert first == second
    assert len(first) == 64

    different_target = compute_input_fingerprint(**{**kwargs, "target": "src/other.py"})
    assert different_target != first

    different_evidence = compute_input_fingerprint(
        **{**kwargs, "evidence_manifest": [{"id": "ev-2", "path": "a.py", "tokens": 10}]}
    )
    assert different_evidence != first


def test_input_fingerprint_normalizes_defaults() -> None:
    # A missing action now defaults to ``diff`` (repo review is gone), so the
    # default must normalize identically to an explicit ``DIFF``.
    assert compute_input_fingerprint(
        target=None, target_type=None, action=None
    ) == compute_input_fingerprint(target="", target_type="AUTO", action="DIFF")


def test_compute_review_input_fingerprint_with_empty_inputs() -> None:
    assert compute_review_input_fingerprint(None, None) == compute_input_fingerprint(
        target=None,
        target_type=None,
        action=None,
        roles=None,
        scope=None,
        evidence_manifest=None,
        extra_metadata=None,
    )


# ---------------------------------------------------------------------------
# Report artifact serialization
# ---------------------------------------------------------------------------


def test_build_report_artifact_uses_board_fields(boards: ReviewBoardStore) -> None:
    async def _run() -> None:
        writer = _create(boards)
        await writer.add_usage({"total_tokens": 42})
        await writer.add_warning("w-1")
        await writer.record_report(
            report_markdown="# report",
            findings=({"title": "finding-1"},),
            verdicts=[{"final_verdict": "accepted"}],
        )
        await writer.settle(ReviewRunStatus.COMPLETED)
        view = _view(boards)

        artifact = build_report_artifact(
            view, report_markdown="# report", verdicts=[{"final_verdict": "accepted"}]
        )

        assert artifact["run_id"] == RUN_ID
        assert artifact["session_key"] == SESSION_KEY
        assert artifact["status"] == "completed"
        assert artifact["input_fingerprint"] == "fp-1"
        assert artifact["report_markdown"] == "# report"
        assert artifact["findings"] == [{"title": "finding-1"}]
        assert artifact["verdicts"] == [{"final_verdict": "accepted"}]
        assert artifact["usage"] == {"total_tokens": 42}
        assert artifact["warnings"] == ["w-1"]
        assert artifact["created_at"]

    asyncio.run(_run())


def test_build_report_artifact_status_overrides_a_running_board(
    boards: ReviewBoardStore,
) -> None:
    """The artifact is persisted before the run status flips to terminal.

    ``ReviewLoop`` must therefore pass the decided terminal status, otherwise a
    completed report would be written with ``status: running`` and later
    consumers would treat a finished report as still in flight.
    """
    _create(boards)
    view = _view(boards)

    artifact = build_report_artifact(
        view, report_markdown="# report", status=ReviewRunStatus.COMPLETED
    )

    assert artifact["status"] == "completed"
    # Serializing never mutates the board.
    assert _view(boards).status is ReviewRunStatus.RUNNING


def test_build_report_artifact_records_coverage_and_boundary(
    boards: ReviewBoardStore,
) -> None:
    """The artifact carries the review boundary and per-reviewer summary."""

    async def _run() -> None:
        writer = _create(boards)
        await writer.record_accepted_input(
            plan=None,
            input_fingerprint="fp-1",
            changed_files=["src/app.py", "src/db.py"],
            skipped_files=["vendor/lib.py"],
            evidence_stats={
                "accepted_units": 3,
                "skipped_units": 1,
                "status": "ok",
                "reason": "",
            },
            assignment_stats=[
                {"dimension": "security", "evidence_units": 2},
                {"dimension": "performance", "evidence_units": 2},
            ],
        )
        await writer.start_assignments(
            (_assignment("security"), _assignment("performance"))
        )
        await writer.reviewer_started("security", evidence_assigned=2)
        await writer.reviewer_settled(
            "security",
            status=CHILD_COMPLETED,
            usage={"input_tokens": 10},
            duplicate_reads=1,
            duplicate_searches=4,
        )
        await writer.reviewer_settled(
            "performance", status=CHILD_ERROR, error="reviewer timed out"
        )

        artifact = build_report_artifact(_view(boards), report_markdown="# report")

        assert artifact["snapshot_ref"] == SNAPSHOT_REF
        coverage = artifact["coverage"]
        assert coverage["changed_files"] == ["src/app.py", "src/db.py"]
        assert coverage["skipped_files"] == ["vendor/lib.py"]
        assert coverage["evidence"]["accepted_units"] == 3
        assert coverage["evidence"]["skipped_units"] == 1
        assert coverage["assignments"] == [
            {"dimension": "security", "evidence_units": 2},
            {"dimension": "performance", "evidence_units": 2},
        ]
        assert coverage["incomplete_dimensions"] == ["performance"]
        reviewer_cov = coverage["reviewers"]["security"]
        assert reviewer_cov["status"] == "completed"
        assert reviewer_cov["evidence_assigned"] == 2
        assert reviewer_cov["duplicate_reads"] == 1
        assert reviewer_cov["duplicate_searches"] == 4
        assert reviewer_cov["usage"] == {"input_tokens": 10}

    asyncio.run(_run())


def test_build_report_artifact_can_record_a_failed_run(
    boards: ReviewBoardStore,
) -> None:
    _create(boards)

    artifact = build_report_artifact(
        _view(boards), report_markdown="", status=ReviewRunStatus.ERROR
    )

    assert artifact["status"] == "error"


def test_serialize_finalizer_result_ids_confirmed_findings_only() -> None:
    """Only confirmed findings get a user-facing report-local ID."""
    dimension = ReviewDimensionResult(
        dimension="security",
        status="validated",
        judged=[_accepted("Real bug"), _uncertain("Maybe", file="b.py")],
    )

    findings, verdicts = serialize_finalizer_result(
        ReviewFinalizerResult(report_markdown="# report", dimensions=[dimension])
    )

    by_title = {finding["title"]: finding for finding in findings}
    assert by_title["Real bug"]["id"] == "F001"
    assert "id" not in by_title["Maybe"]
    assert len(verdicts) == 2


def test_finding_ids_reach_the_artifact(boards: ReviewBoardStore) -> None:
    """The artifact mirrors the Markdown finding IDs."""

    async def _run() -> None:
        writer = _create(boards)
        await writer.record_report(
            report_markdown="# report",
            findings=({"id": "F001", "title": "Real bug"},),
            verdicts=[],
        )

        artifact = build_report_artifact(_view(boards), report_markdown="# report")

        assert artifact["findings"][0]["id"] == "F001"

    asyncio.run(_run())


def test_bound_child_error_collapses_and_bounds_reason() -> None:
    assert bound_child_error(None) == ""
    assert bound_child_error("  two   spaces  ") == "two spaces"
    long_reason = "x" * (CHILD_ERROR_MAX_CHARS + 50)
    assert len(bound_child_error(long_reason)) == CHILD_ERROR_MAX_CHARS


# ---------------------------------------------------------------------------
# Artifact store
# ---------------------------------------------------------------------------


class TestReviewArtifactStore:
    def _store(self, tmp_path: Path) -> ReviewArtifactStore:
        return ReviewArtifactStore(tmp_path / "workspace")

    def _artifact(self, **overrides) -> dict:
        payload: dict = {
            "run_id": "run-abc123def456",
            "session_key": "websocket:chat-1",
            "status": "completed",
            "input_fingerprint": "fp-1",
            "report_markdown": "# report",
            "findings": [],
            "verdicts": [],
            "usage": {},
            "warnings": [],
            "created_at": "2026-09-15T00:00:00+00:00",
        }
        payload.update(overrides)
        return payload

    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        artifact = self._artifact()
        ref = store.write(artifact)
        assert ref == f"{REVIEW_ARTIFACTS_DIR_NAME}/run-abc123def456.json"

        loaded = store.read(
            run_id="run-abc123def456",
            session_key="websocket:chat-1",
            input_fingerprint="fp-1",
        )
        assert loaded == artifact

    def test_write_rejects_invalid_run_id(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        assert store.write(self._artifact(run_id="../escape")) is None
        assert store.write(self._artifact(run_id="")) is None
        assert not (tmp_path / "workspace" / REVIEW_ARTIFACTS_DIR_NAME).exists()

    def test_write_rejects_oversized_artifact(self, tmp_path: Path) -> None:
        store = ReviewArtifactStore(tmp_path, max_bytes=64)
        assert store.write(self._artifact()) is None

    def test_read_missing_artifact_raises_404(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        with pytest.raises(ReviewArtifactError) as excinfo:
            store.read(run_id="run-missing", session_key="s", input_fingerprint="f")
        assert excinfo.value.status == 404

    def test_read_corrupt_artifact_raises_409(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.write(self._artifact())
        path = store.path_for("run-abc123def456")
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ReviewArtifactError) as excinfo:
            store.read(
                run_id="run-abc123def456",
                session_key="websocket:chat-1",
                input_fingerprint="fp-1",
            )
        assert excinfo.value.status == 409

    def test_read_non_dict_artifact_raises_409(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.write(self._artifact())
        path = store.path_for("run-abc123def456")
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(ReviewArtifactError) as excinfo:
            store.read(
                run_id="run-abc123def456",
                session_key="websocket:chat-1",
                input_fingerprint="fp-1",
            )
        assert excinfo.value.status == 409

    def test_read_oversized_artifact_raises_409(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.write(self._artifact())
        path = store.path_for("run-abc123def456")
        path.write_text("x" * 128, encoding="utf-8")
        small = ReviewArtifactStore(tmp_path / "workspace", max_bytes=64)
        with pytest.raises(ReviewArtifactError) as excinfo:
            small.read(
                run_id="run-abc123def456",
                session_key="websocket:chat-1",
                input_fingerprint="fp-1",
            )
        assert excinfo.value.status == 409

    @pytest.mark.parametrize(
        ("content_overrides", "reason"),
        [
            ({"run_id": "run-other789012"}, "artifact run id mismatch"),
            ({"session_key": "websocket:chat-2"}, "artifact session mismatch"),
            ({"input_fingerprint": "fp-2"}, "artifact fingerprint mismatch"),
        ],
    )
    def test_read_mismatched_artifact_raises_409(
        self, tmp_path: Path, content_overrides: dict, reason: str
    ) -> None:
        store = self._store(tmp_path)
        # Write under the requested run id so the file exists at the expected
        # path, then rewrite its content to simulate a mismatched artifact.
        store.write(self._artifact())
        path = store.path_for("run-abc123def456")
        tampered = self._artifact(**content_overrides)
        path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ReviewArtifactError, match=reason) as excinfo:
            store.read(
                run_id="run-abc123def456",
                session_key="websocket:chat-1",
                input_fingerprint="fp-1",
            )
        assert excinfo.value.status == 409

    def test_path_for_rejects_unsafe_run_ids(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        with pytest.raises(ReviewArtifactError):
            store.path_for("../../etc/passwd")
        with pytest.raises(ReviewArtifactError):
            store.path_for("")

    def test_reference_for_uses_wire_relative_path(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        ref = store.reference_for("run-abc123def456")
        assert ref == f"{REVIEW_ARTIFACTS_DIR_NAME}/run-abc123def456.json"
        assert not Path(ref).is_absolute()
