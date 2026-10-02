"""Contract tests for ``ReviewResult`` and the review -> conversation handoff.

``ReviewResult`` is the single terminal summary the review side hands to the
session side. Two invariants must hold no matter how a run ended:

* the handoff state must never overstate what exists — a missing report
  artifact is ``failed``, and a completed run that reported gaps is ``partial``
  rather than ``complete``;
* the result itself must be wire-safe — no full report body, no absolute
  server path, no live object. The artifact stays the authoritative copy.
"""

from __future__ import annotations

from typing import Any

from nanoreview.agent.review_state import (
    JudgeBatchState,
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
)
from nanoreview.review.result import (
    GAP_MAX_CHARS,
    REVIEW_REPORT_SOURCE,
    ReviewHandoffState,
    render_handoff_block,
    render_handoff_directive,
    render_review_context_index,
    result_from_run_state,
    result_from_session_metadata,
)
from nanoreview.review.types import ReviewMetaKey

REPORT_REF = "review-artifacts/run-a.json"


def _run_state(
    *,
    status: ReviewRunStatus = ReviewRunStatus.COMPLETED,
    report_ref: str | None = REPORT_REF,
    snapshot_ref: str | None = "review-snapshots/run-a.json",
    summary: str = "",
) -> ReviewRunState:
    return ReviewRunState(
        run_id="run-a",
        session_key="cli:review",
        input_fingerprint="fp",
        phase=ReviewPhase.DONE,
        status=status,
        report_ref=report_ref,
        snapshot_ref=snapshot_ref,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Handoff classification: three states, never overstated
# ---------------------------------------------------------------------------


def test_completed_run_without_gaps_is_a_complete_handoff() -> None:
    result = result_from_run_state(_run_state())

    assert result.handoff is ReviewHandoffState.COMPLETE
    assert result.is_terminal is True
    assert result.has_report is True
    assert result.gaps == ()
    assert result.error == ""


def test_run_without_a_report_artifact_is_failed_however_it_ended() -> None:
    for status in (
        ReviewRunStatus.COMPLETED,
        ReviewRunStatus.ERROR,
        ReviewRunStatus.STOPPED,
    ):
        result = result_from_run_state(_run_state(status=status, report_ref=None))

        assert result.handoff is ReviewHandoffState.FAILED, status
        assert result.has_report is False
        # A failure must be explainable, never rendered as a silent success.
        assert result.error


def test_completed_run_with_recorded_gaps_is_partial() -> None:
    state = _run_state()
    state.reviewer_state("security").status = "completed"
    state.reviewer_state("performance").status = "error"
    state.reviewer_state("performance").error = "provider timeout"

    result = result_from_run_state(state)

    assert result.handoff is ReviewHandoffState.PARTIAL
    assert "security" in result.coverage
    assert "performance" not in result.coverage
    assert any("performance" in gap for gap in result.gaps)


def test_terminal_failure_with_a_report_is_partial_not_complete() -> None:
    result = result_from_run_state(_run_state(status=ReviewRunStatus.ERROR))

    assert result.handoff is ReviewHandoffState.PARTIAL
    assert result.status is ReviewRunStatus.ERROR
    assert result.error


def test_running_run_has_no_terminal_result() -> None:
    """A live run is not terminal: nothing may be handed to the conversation."""
    result = result_from_run_state(
        _run_state(status=ReviewRunStatus.RUNNING, report_ref=None)
    )

    assert result.is_terminal is False
    assert result.handoff is ReviewHandoffState.FAILED


# ---------------------------------------------------------------------------
# Gaps / coverage / error fields
# ---------------------------------------------------------------------------


def test_unfinished_reviewers_and_judge_batches_become_gaps() -> None:
    state = _run_state()
    state.reviewer_state("bug").status = "stopped"
    state.reviewer_state("bug").error = "review stopped before the run finished"
    batch = state.judge_batches.setdefault("judge-1", JudgeBatchState(batch_id="judge-1"))
    batch.status = "error"
    batch.error = "judge unavailable"
    state.add_warning("report artifact write failed")

    gaps = result_from_run_state(state).gaps

    assert any("'bug'" in gap and "stopped before" in gap for gap in gaps)
    assert any("'judge-1'" in gap and "judge unavailable" in gap for gap in gaps)
    assert "report artifact write failed" in gaps


def test_a_completed_reviewer_is_never_reported_as_a_gap() -> None:
    state = _run_state()
    reviewer = state.reviewer_state("security")
    reviewer.status = "completed"
    reviewer.error = "stale text left over from an earlier attempt"

    result = result_from_run_state(state)

    assert result.coverage == ("security",)
    assert result.gaps == ()


def test_gap_text_is_bounded_and_deduplicated() -> None:
    state = _run_state()
    reviewer = state.reviewer_state("bug")
    reviewer.status = "error"
    reviewer.error = "x" * (GAP_MAX_CHARS * 4)
    # ``add_warning`` de-duplicates, so inject directly to exercise ``_gaps``.
    state.warnings.extend(["same warning", "same warning"])

    gaps = result_from_run_state(state).gaps

    assert len(gaps) == 2
    assert all(len(gap) <= GAP_MAX_CHARS for gap in gaps)
    assert gaps.count("same warning") == 1


def test_failure_summary_prefers_the_last_warning() -> None:
    state = _run_state(status=ReviewRunStatus.ERROR, report_ref=None)
    state.add_warning("first")
    state.add_warning("artifact write failed")

    result = result_from_run_state(state)

    assert result.error == "artifact write failed"
    assert result.summary == "artifact write failed"


def test_summary_falls_back_to_the_status_when_no_warning_exists() -> None:
    result = result_from_run_state(
        _run_state(status=ReviewRunStatus.STOPPED, report_ref=None)
    )

    assert "stopped" in result.error


# ---------------------------------------------------------------------------
# Wire safety
# ---------------------------------------------------------------------------


def test_payload_is_wire_safe_and_names_its_provenance() -> None:
    state = _run_state()
    state.reviewer_state("security").status = "completed"
    state.findings.append(
        {
            "severity": "high",
            "file": "app.py",
            "line": 12,
            "title": "unbounded read",
        }
    )

    payload = result_from_run_state(state).as_payload()

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


def test_payload_omits_unset_optional_fields() -> None:
    payload = result_from_run_state(
        _run_state(
            status=ReviewRunStatus.ERROR, report_ref=None, snapshot_ref=None
        )
    ).as_payload()

    assert "report_ref" not in payload
    assert "snapshot_ref" not in payload
    assert payload["error"]


# ---------------------------------------------------------------------------
# Restored-from-metadata path
# ---------------------------------------------------------------------------


def _metadata(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        ReviewMetaKey.RUN_ID: "run-a",
        ReviewMetaKey.STATUS: "completed",
        ReviewMetaKey.REPORT_REF: REPORT_REF,
        ReviewMetaKey.SNAPSHOT_REF: "review-snapshots/run-a.json",
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
    assert result.snapshot_ref == "review-snapshots/run-a.json"
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


def test_context_index_is_compact_and_never_carries_the_report() -> None:
    result = result_from_run_state(_run_state())

    index = render_review_context_index(result)

    assert REVIEW_REPORT_SOURCE in index
    assert "run_id=run-a" in index
    assert f"report_ref={REPORT_REF}" in index
    assert "## " not in index


def test_handoff_block_injects_the_complete_report_verbatim() -> None:
    result = result_from_run_state(_run_state())
    report = "## Code Review Report\n\nNo actionable issues found."

    block = render_handoff_block(result, report)

    assert "handoff=complete" in block
    assert report in block
    assert "read-only" in block


def test_failed_handoff_states_the_failure_and_the_partial_results() -> None:
    state = _run_state(status=ReviewRunStatus.ERROR, report_ref=None, summary="boom")
    state.findings.append(
        {"severity": "high", "file": "app.py", "line": 3, "title": "unbounded read"}
    )
    state.add_warning("artifact write failed")
    result = result_from_run_state(state)

    block = render_handoff_block(result, None)

    assert "handoff=failed" in block
    assert "no review report artifact is available" in block
    assert "cannot be retried or resumed" in block
    assert "boom" in block
    assert "app.py" in block
    assert "artifact write failed" in block
    # A failed handoff must never render the report framing as if it existed.
    assert "## Code Review Report" not in block


def test_handoff_directive_freezes_the_read_only_rule_at_system_level() -> None:
    directive = render_handoff_directive(result_from_run_state(_run_state()))

    assert "run_id=run-a" in directive
    assert "handoff=complete" in directive
    assert "must not be modified" in directive
