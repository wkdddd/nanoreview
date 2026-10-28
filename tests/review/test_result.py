"""Contract tests for ``ReviewResult`` and the review -> conversation handoff.

``ReviewResult`` is the single terminal summary the review side hands to the
session side. It is always built from the run's blackboard projection, so these
tests drive the real board store instead of hand-building a mutable state
object. Two invariants must hold no matter how a run ended:

* the handoff state must never overstate what exists — a missing report
  artifact is ``failed``, and a completed run that reported gaps is ``partial``
  rather than ``complete``;
* the result itself must be wire-safe — no full report body, no absolute
  server path, no live object. The artifact stays the authoritative copy.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nanoreview.agent.review_board import (
    CHILD_COMPLETED,
    CHILD_ERROR,
    CHILD_STOPPED,
    ReviewBoardStore,
    ReviewBoardWriter,
)
from nanoreview.agent.review_state import ReviewArtifactStore, ReviewRunStatus
from nanoreview.review.result import (
    GAP_MAX_CHARS,
    REVIEW_REPORT_SOURCE,
    ReviewHandoffState,
    render_handoff_block,
    render_handoff_directive,
    render_review_context_index,
    result_from_board,
    result_from_session_metadata,
)
from nanoreview.review.types import ReviewMetaKey
from nanoreview.session.manager import SessionManager

REPORT_REF = "review-artifacts/run-a.json"
SESSION_KEY = "cli:review"
RUN_ID = "run-a"
SNAPSHOT_REF = "review-snapshots/run-a.json"


class _FailingArtifacts(ReviewArtifactStore):
    """Artifact store whose writes always fail (a full disk, a read-only root)."""

    def write(self, artifact: dict[str, Any]) -> str | None:  # noqa: D102 - stub
        return None


@pytest.fixture
def boards(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return _store(workspace)


def _store(workspace, *, artifacts=None) -> ReviewBoardStore:
    return ReviewBoardStore(
        sessions=SessionManager(workspace),
        artifacts=artifacts or ReviewArtifactStore(workspace),
    )


def _writer(store: ReviewBoardStore, *, snapshot_ref: str | None = SNAPSHOT_REF):
    """Create one run's board and return its writer handle."""
    store.create(
        SimpleNamespace(
            run_id=RUN_ID,
            session_key=SESSION_KEY,
            input_fingerprint="fp",
            snapshot_ref=snapshot_ref,
            plan=None,
        )
    )
    writer = store.writer(SESSION_KEY)
    assert writer is not None
    return writer


async def _finished(
    store: ReviewBoardStore,
    *,
    status: ReviewRunStatus = ReviewRunStatus.COMPLETED,
    report: str = "## Code Review Report\n\nNo actionable issues found.",
    findings: tuple[dict[str, Any], ...] = (),
    snapshot_ref: str | None = SNAPSHOT_REF,
) -> ReviewBoardWriter:
    """Drive a run to *status*, optionally persisting a report first."""
    writer = _writer(store, snapshot_ref=snapshot_ref)
    if report:
        await writer.record_report(
            report_markdown=report, findings=findings, verdicts=[]
        )
    await writer.settle(status)
    return writer


def _result(store: ReviewBoardStore):
    view = store.view(SESSION_KEY)
    assert view is not None
    return result_from_board(view)


# ---------------------------------------------------------------------------
# Handoff classification: three states, never overstated
# ---------------------------------------------------------------------------


async def test_completed_run_without_gaps_is_a_complete_handoff(boards) -> None:
    await _finished(boards)

    result = _result(boards)

    assert result.handoff is ReviewHandoffState.COMPLETE
    assert result.is_terminal is True
    assert result.has_report is True
    assert result.gaps == ()
    assert result.error == ""


async def test_run_without_a_report_artifact_is_failed_however_it_ended(
    boards,
) -> None:
    for status in (
        ReviewRunStatus.COMPLETED,
        ReviewRunStatus.ERROR,
        ReviewRunStatus.STOPPED,
    ):
        await _writer(boards).settle(status)
        result = _result(boards)

        assert result.handoff is ReviewHandoffState.FAILED, status
        assert result.has_report is False
        # A failure must be explainable, never rendered as a silent success.
        assert result.error
        boards.drop(SESSION_KEY)


async def test_completed_run_with_recorded_gaps_is_partial(boards) -> None:
    writer = _writer(boards)
    await writer.record_report(
        report_markdown="## Code Review Report", findings=(), verdicts=[]
    )
    await writer.reviewer_settled("security", status=CHILD_COMPLETED)
    await writer.reviewer_settled(
        "performance", status=CHILD_ERROR, error="provider timeout"
    )
    await writer.settle(ReviewRunStatus.COMPLETED)

    result = _result(boards)

    assert result.handoff is ReviewHandoffState.PARTIAL
    assert "security" in result.coverage
    assert "performance" not in result.coverage
    assert any("performance" in gap for gap in result.gaps)


async def test_terminal_failure_with_a_report_is_partial_not_complete(
    boards,
) -> None:
    await _finished(boards, status=ReviewRunStatus.ERROR)

    result = _result(boards)

    assert result.handoff is ReviewHandoffState.PARTIAL
    assert result.status is ReviewRunStatus.ERROR
    assert result.error


async def test_running_run_has_no_terminal_result(boards) -> None:
    """A live run is not terminal: nothing may be handed to the conversation."""
    _writer(boards)

    result = _result(boards)

    assert result.is_terminal is False
    assert result.handoff is ReviewHandoffState.FAILED


# ---------------------------------------------------------------------------
# Gaps / coverage / error fields
# ---------------------------------------------------------------------------


async def test_unfinished_reviewers_and_judge_batches_become_gaps(boards) -> None:
    writer = _writer(boards)
    await writer.reviewer_settled(
        "bug",
        status=CHILD_STOPPED,
        error="review stopped before the run finished",
    )
    await writer.judge_started("judge-1")
    await writer.judge_settled(
        "judge-1", status=CHILD_ERROR, error="judge unavailable"
    )
    await writer.add_warning("report artifact write failed")

    gaps = _result(boards).gaps

    assert any("'bug'" in gap and "stopped before" in gap for gap in gaps)
    assert any("'judge-1'" in gap and "judge unavailable" in gap for gap in gaps)
    assert "report artifact write failed" in gaps


async def test_a_completed_reviewer_is_never_reported_as_a_gap(boards) -> None:
    writer = _writer(boards)
    await writer.reviewer_settled(
        "security",
        status=CHILD_COMPLETED,
        error="stale text left over from an earlier attempt",
    )

    result = _result(boards)

    assert result.coverage == ("security",)
    assert result.gaps == ()


async def test_gap_text_is_bounded_and_deduplicated(boards) -> None:
    writer = _writer(boards)
    await writer.reviewer_settled(
        "bug", status=CHILD_ERROR, error="x" * (GAP_MAX_CHARS * 4)
    )
    # ``add_warning`` de-duplicates at the board; ``_gaps`` de-duplicates again
    # so a repeated warning can never inflate the gap list.
    await writer.add_warning("same warning")
    await writer.add_warning("same warning")

    gaps = _result(boards).gaps

    assert len(gaps) == 2
    assert all(len(gap) <= GAP_MAX_CHARS for gap in gaps)
    assert gaps.count("same warning") == 1


async def test_failure_summary_prefers_the_last_warning(boards) -> None:
    writer = _writer(boards)
    await writer.add_warning("first")
    await writer.add_warning("artifact write failed")
    await writer.settle(ReviewRunStatus.ERROR)

    result = _result(boards)

    assert result.error == "artifact write failed"
    assert result.summary == "artifact write failed"


async def test_summary_falls_back_to_the_status_when_no_warning_exists(
    boards,
) -> None:
    await _writer(boards).settle(ReviewRunStatus.STOPPED)

    result = _result(boards)

    assert "stopped" in result.error


# ---------------------------------------------------------------------------
# Wire safety
# ---------------------------------------------------------------------------


async def test_payload_is_wire_safe_and_names_its_provenance(boards) -> None:
    writer = _writer(boards)
    await writer.record_report(
        report_markdown="## Code Review Report\n\nbody",
        findings=(
            {
                "severity": "high",
                "file": "app.py",
                "line": 12,
                "title": "unbounded read",
            },
        ),
        verdicts=[],
    )
    await writer.reviewer_settled("security", status=CHILD_COMPLETED)
    await writer.settle(ReviewRunStatus.COMPLETED)

    payload = _result(boards).as_payload()

    assert payload["source"] == REVIEW_REPORT_SOURCE
    assert payload["run_id"] == "run-a"
    assert payload["handoff"] == "complete"
    assert payload["report_ref"] == REPORT_REF
    assert payload["coverage"] == ["security"]
    # The report body and the findings stay in the artifact, not on the wire.
    assert "report_markdown" not in payload
    assert "findings" not in payload
    assert not any(
        isinstance(value, str) and (value.startswith("/") or ":" in value)
        for value in payload.values()
    )


async def test_payload_omits_unset_optional_fields(boards) -> None:
    await _finished(
        boards, status=ReviewRunStatus.ERROR, report="", snapshot_ref=None
    )

    payload = _result(boards).as_payload()

    assert "report_ref" not in payload
    assert "snapshot_ref" not in payload
    assert payload["error"]


# ---------------------------------------------------------------------------
# A failed artifact write keeps the findings but has no report reference
# ---------------------------------------------------------------------------


async def test_failed_artifact_write_keeps_findings_without_a_report_ref(
    tmp_path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = _store(workspace, artifacts=_FailingArtifacts(workspace))
    writer = _writer(store)
    reference = await writer.record_report(
        report_markdown="## Code Review Report\n\nbody",
        findings=(
            {"severity": "high", "file": "app.py", "line": 3, "title": "unbounded"},
        ),
        verdicts=[],
    )
    await writer.settle(ReviewRunStatus.ERROR)

    view = store.view(SESSION_KEY)
    assert reference is None
    result = result_from_board(view)

    assert result.handoff is ReviewHandoffState.FAILED
    assert result.report_ref is None
    # The candidates that were validated before the write failed stay recorded.
    assert result.findings and result.findings[0]["file"] == "app.py"


# ---------------------------------------------------------------------------
# Restored-from-metadata path
# ---------------------------------------------------------------------------


def _metadata(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        ReviewMetaKey.RUN_ID: "run-a",
        ReviewMetaKey.STATUS: "completed",
        ReviewMetaKey.REPORT_REF: REPORT_REF,
        ReviewMetaKey.SNAPSHOT_REF: SNAPSHOT_REF,
        ReviewMetaKey.INPUT_FINGERPRINT: "fp",
    }
    values.update(overrides)
    return values


def test_metadata_without_a_run_has_no_result() -> None:
    assert result_from_session_metadata("cli:plain", {}) is None
    assert result_from_session_metadata("cli:plain", None) is None
    assert (
        result_from_session_metadata("cli:plain", {ReviewMetaKey.RUN_ID: ""}) is None
    )


def test_restored_completed_run_is_a_complete_handoff() -> None:
    result = result_from_session_metadata("cli:review", _metadata())

    assert result is not None
    assert result.handoff is ReviewHandoffState.COMPLETE
    assert result.snapshot_ref == SNAPSHOT_REF
    assert result.input_fingerprint == "fp"


def test_restored_running_run_is_reported_as_failed_not_resumable() -> None:
    """No live executor exists after a restart, so the run never resumes."""
    result = result_from_session_metadata(
        "cli:review",
        _metadata(**{ReviewMetaKey.STATUS: "running", ReviewMetaKey.REPORT_REF: None}),
    )

    assert result is not None
    assert result.is_terminal is False
    assert result.handoff is ReviewHandoffState.FAILED
    assert "interrupted" in result.error


def test_unknown_persisted_status_degrades_to_error() -> None:
    result = result_from_session_metadata(
        "cli:review", _metadata(**{ReviewMetaKey.STATUS: "exploded"})
    )

    assert result is not None
    assert result.status is ReviewRunStatus.ERROR
    # The artifact reference survives, so the report stays readable — but the
    # run is not ``completed``, so the handoff is explicitly partial.
    assert result.handoff is ReviewHandoffState.PARTIAL
    assert "error" in result.error


def test_unknown_persisted_status_without_a_report_is_failed() -> None:
    result = result_from_session_metadata(
        "cli:review",
        _metadata(**{ReviewMetaKey.STATUS: "exploded", ReviewMetaKey.REPORT_REF: None}),
    )

    assert result is not None
    assert result.status is ReviewRunStatus.ERROR
    assert result.handoff is ReviewHandoffState.FAILED
    assert result.error


def test_restored_error_run_keeps_the_persisted_reason() -> None:
    """The bounded failure reason survives a restart instead of degrading."""
    reason = "planning failed: no reviewer profile for 'security'"
    result = result_from_session_metadata(
        "cli:review",
        _metadata(
            **{
                ReviewMetaKey.STATUS: "error",
                ReviewMetaKey.REPORT_REF: None,
                ReviewMetaKey.ERROR: reason,
                ReviewMetaKey.SUMMARY: reason,
            }
        ),
    )

    assert result is not None
    assert result.status is ReviewRunStatus.ERROR
    assert result.handoff is ReviewHandoffState.FAILED
    assert result.error == reason
    assert result.summary == reason


def test_restored_stopped_run_prefers_the_persisted_reason() -> None:
    """A stopped run with an artifact still explains itself after a restart."""
    reason = "review cleanup was interrupted by another cancellation"
    result = result_from_session_metadata(
        "cli:review",
        _metadata(**{ReviewMetaKey.STATUS: "stopped", ReviewMetaKey.ERROR: reason}),
    )

    assert result is not None
    assert result.status is ReviewRunStatus.STOPPED
    assert result.handoff is ReviewHandoffState.PARTIAL
    assert result.error == reason
    assert result.summary == reason


def test_restored_terminal_run_without_a_reason_stays_generic() -> None:
    """Older metadata without the reason keys still degrades to a bounded error."""
    result = result_from_session_metadata(
        "cli:review",
        _metadata(**{ReviewMetaKey.STATUS: "error", ReviewMetaKey.REPORT_REF: None}),
    )

    assert result is not None
    assert result.error
    assert result.summary == result.error


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


async def test_context_index_is_compact_and_never_carries_the_report(boards) -> None:
    await _finished(boards)

    index = render_review_context_index(_result(boards))

    assert REVIEW_REPORT_SOURCE in index
    assert "run_id=run-a" in index
    assert f"report_ref={REPORT_REF}" in index
    assert "## " not in index


async def test_handoff_block_injects_the_complete_report_verbatim(boards) -> None:
    report = "## Code Review Report\n\nNo actionable issues found."
    await _finished(boards, report=report)

    block = render_handoff_block(_result(boards), report)

    assert "handoff=complete" in block
    assert report in block
    assert "read-only" in block


async def test_failed_handoff_states_the_failure_and_the_partial_results(
    tmp_path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = _store(workspace, artifacts=_FailingArtifacts(workspace))
    writer = _writer(store)
    await writer.record_report(
        report_markdown="boom",
        findings=(
            {"severity": "high", "file": "app.py", "line": 3, "title": "unbounded read"},
        ),
        verdicts=[],
    )
    await writer.add_warning("artifact write failed")
    await writer.settle(ReviewRunStatus.ERROR)

    block = render_handoff_block(result_from_board(store.view(SESSION_KEY)), None)

    assert "handoff=failed" in block
    assert "no review report artifact is available" in block
    assert "cannot be retried or resumed" in block
    assert "boom" in block
    assert "app.py" in block
    assert "artifact write failed" in block


async def test_handoff_directive_freezes_the_read_only_rule_at_system_level(
    boards,
) -> None:
    await _finished(boards)

    directive = render_handoff_directive(_result(boards))

    assert "run_id=run-a" in directive
    assert "handoff=complete" in directive
    assert "must not be modified" in directive
