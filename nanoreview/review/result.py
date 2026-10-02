"""Structured terminal result of one review run.

``ReviewResult`` is the review-side result contract shared by the review
supervisor (``ReviewLoop``), the session coordinator, and every transport.
It answers one question for the rest of the system: *a review run ended — what
did it actually produce, and can the owning session move on to conversation?*

The contract carries no live objects: no tasks, providers, callbacks or
locks. The full report stays in its persisted artifact and is reached through
``report_ref``; the result only holds a bounded digest plus coverage and gaps.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from nanoreview.agent.review_state import (
    REVIEW_SUMMARY_MAX_CHARS,
    REVIEW_TERMINAL_STATUSES,
    ReviewRunState,
    ReviewRunStatus,
)
from nanoreview.review.types import ReviewMetaKey

#: Provenance marker attached to the review_context index message and to the
#: injected handoff so a reader can always tell ReviewAgent output from
#: Conversation Agent output.
REVIEW_REPORT_SOURCE = "review_agent"

#: Gap text is bounded so a huge provider trace cannot inflate the result.
GAP_MAX_CHARS = 300


class ReviewHandoffState(StrEnum):
    """How complete the review -> conversation handoff is.

    ``COMPLETE``: the run finished successfully and its report artifact is on
    disk. ``PARTIAL``: a report artifact exists but the run reported gaps
    (failed dimensions, judge trouble, warnings), so the report is explicitly
    incomplete. ``FAILED``: no usable report artifact exists at all — the run
    ended without one, or the process died before the report was persisted.
    """

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """Terminal result of one review run, as seen by the session coordinator."""

    run_id: str
    session_key: str
    status: ReviewRunStatus
    handoff: ReviewHandoffState
    report_ref: str | None = None
    snapshot_ref: str | None = None
    input_fingerprint: str = ""
    summary: str = ""
    findings: tuple[Mapping[str, Any], ...] = ()
    coverage: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    error: str = ""
    warnings: tuple[str, ...] = ()
    usage: Mapping[str, int] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in REVIEW_TERMINAL_STATUSES

    @property
    def has_report(self) -> bool:
        return bool(self.report_ref)

    def as_payload(self) -> dict[str, Any]:
        """Wire-safe payload for API/CLI/WebUI consumers.

        Never contains absolute server paths, the full report, or reviewer
        transcripts.
        """
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status.value,
            "handoff": self.handoff.value,
            "source": REVIEW_REPORT_SOURCE,
            "coverage": list(self.coverage),
            "gaps": list(self.gaps),
        }
        if self.report_ref:
            payload["report_ref"] = self.report_ref
        if self.snapshot_ref:
            payload["snapshot_ref"] = self.snapshot_ref
        if self.input_fingerprint:
            payload["input_fingerprint"] = self.input_fingerprint
        if self.error:
            payload["error"] = self.error
        return payload


def _bounded(text: Any, limit: int = GAP_MAX_CHARS) -> str:
    return " ".join(str(text or "").split())[:limit]


def _coverage(state: ReviewRunState) -> tuple[str, ...]:
    """Dimensions that actually returned a terminal successful review."""
    return tuple(
        dimension
        for dimension, reviewer in sorted(state.reviewers.items())
        if reviewer.status == "completed"
    )


def _gaps(state: ReviewRunState) -> tuple[str, ...]:
    """Explicit, deduplicated reasons the produced report is incomplete.

    A finished reviewer keeps its terminal state and is not a gap. Anything
    that never reached ``completed``, a judge batch that did not complete, and
    every recorded run warning are surfaced so the report is never read as
    more complete than it is.
    """
    gaps: list[str] = []
    for dimension, reviewer in sorted(state.reviewers.items()):
        if reviewer.status == "completed":
            continue
        detail = reviewer.error or f"status={reviewer.status}"
        gaps.append(_bounded(f"dimension '{dimension}' not reviewed ({detail})"))
    for batch_id, batch in sorted(state.judge_batches.items()):
        if batch.status == "completed":
            continue
        detail = batch.error or f"status={batch.status}"
        gaps.append(_bounded(f"judge batch '{batch_id}' incomplete ({detail})"))
    gaps.extend(_bounded(warning) for warning in state.warnings)
    seen: set[str] = set()
    ordered: list[str] = []
    for gap in gaps:
        if gap and gap not in seen:
            seen.add(gap)
            ordered.append(gap)
    return tuple(ordered)


def _classify_handoff(
    status: ReviewRunStatus,
    report_ref: str | None,
    gaps: tuple[str, ...],
) -> ReviewHandoffState:
    """Classify the handoff from the terminal status, artifact, and gaps.

    A missing artifact is always ``FAILED``: partial results must never be
    rendered as a usable report. A successful run with recorded gaps is
    ``PARTIAL`` rather than ``COMPLETE`` so the first conversation turn states
    the limitation instead of implying full coverage.
    """
    if not report_ref:
        return ReviewHandoffState.FAILED
    if status is not ReviewRunStatus.COMPLETED or gaps:
        return ReviewHandoffState.PARTIAL
    return ReviewHandoffState.COMPLETE


def result_from_run_state(
    state: ReviewRunState,
    *,
    session_metadata: Mapping[str, Any] | None = None,
) -> ReviewResult:
    """Build the structured result of *state*.

    ``session_metadata`` supplies the fields that live on the session rather
    than the run (currently the snapshot reference) for runs restored without
    a live in-process state.
    """
    metadata = session_metadata or {}
    snapshot_ref = state.snapshot_ref or metadata.get(ReviewMetaKey.SNAPSHOT_REF)
    gaps = _gaps(state)
    handoff = _classify_handoff(state.status, state.report_ref, gaps)
    error = ""
    if state.status is not ReviewRunStatus.COMPLETED:
        error = _bounded(state.warnings[-1]) if state.warnings else (
            f"review ended with status '{state.status.value}'"
        )
    elif not state.report_ref:
        # A completed run whose artifact is gone is still a failed handoff, so
        # it must carry a reason rather than an empty error.
        error = "the review report artifact is missing"
    return ReviewResult(
        run_id=state.run_id,
        session_key=state.session_key,
        status=state.status,
        handoff=handoff,
        report_ref=state.report_ref,
        snapshot_ref=str(snapshot_ref) if snapshot_ref else None,
        input_fingerprint=state.input_fingerprint,
        summary=state.summary or error,
        findings=tuple(state.findings),
        coverage=_coverage(state),
        gaps=gaps,
        error=error,
        warnings=tuple(state.warnings),
        usage=dict(state.usage),
    )


def result_from_session_metadata(
    session_key: str, metadata: Mapping[str, Any] | None
) -> ReviewResult | None:
    """Rebuild a result from persisted session metadata alone.

    Used when no in-process run state exists — after a restart, or for a
    session read by a transport that never owned the run. Returns ``None``
    when the session carries no review run at all.

    A run whose persisted status is still ``running`` has no live executor and
    never persisted a result: it is reported as a failed handoff rather than
    being left permanently gated.
    """
    values = metadata or {}
    run_id = values.get(ReviewMetaKey.RUN_ID)
    if not isinstance(run_id, str) or not run_id:
        return None
    raw_status = values.get(ReviewMetaKey.STATUS)
    try:
        status = ReviewRunStatus(str(raw_status))
    except ValueError:
        status = ReviewRunStatus.ERROR
    report_ref = values.get(ReviewMetaKey.REPORT_REF)
    report_ref = report_ref if isinstance(report_ref, str) and report_ref else None
    snapshot_ref = values.get(ReviewMetaKey.SNAPSHOT_REF)
    fingerprint = values.get(ReviewMetaKey.INPUT_FINGERPRINT)
    persisted_error = values.get(ReviewMetaKey.ERROR)
    persisted_error = (
        _bounded(persisted_error) if isinstance(persisted_error, str) else ""
    )
    persisted_summary = values.get(ReviewMetaKey.SUMMARY)
    persisted_summary = (
        _bounded(persisted_summary, REVIEW_SUMMARY_MAX_CHARS)
        if isinstance(persisted_summary, str)
        else ""
    )
    if status is ReviewRunStatus.RUNNING:
        error = "review was interrupted before it produced a result"
    elif status is ReviewRunStatus.COMPLETED:
        error = "" if report_ref else (
            "the review report artifact is missing"
        )
    else:
        error = persisted_error or (
            f"review ended with status '{status.value}' without a report artifact"
        )
    gaps: tuple[str, ...] = ()
    handoff = _classify_handoff(status, report_ref, gaps)
    if not error and status is not ReviewRunStatus.COMPLETED:
        error = f"review ended with status '{status.value}'"
    return ReviewResult(
        run_id=run_id,
        session_key=session_key,
        status=status,
        handoff=handoff,
        report_ref=report_ref,
        snapshot_ref=str(snapshot_ref) if isinstance(snapshot_ref, str) and snapshot_ref else None,
        input_fingerprint=str(fingerprint) if isinstance(fingerprint, str) else "",
        summary=persisted_summary or error,
        error=error,
    )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def render_review_context_index(result: ReviewResult) -> str:
    """Compact, replayable index entry written into ``Session.messages``.

    The index is deliberately small: it tells every consumer that a review
    happened, which run and artifact it belongs to, and how complete it is.
    The authoritative content stays in the report artifact.
    """
    lines = [
        "[ReviewAgent report index]",
        f"source={REVIEW_REPORT_SOURCE}",
        f"run_id={result.run_id}",
        f"status={result.status.value}",
        f"handoff={result.handoff.value}",
    ]
    if result.report_ref:
        lines.append(f"report_ref={result.report_ref}")
    if result.snapshot_ref:
        lines.append(f"snapshot_ref={result.snapshot_ref}")
    if result.coverage:
        lines.append(f"coverage={', '.join(result.coverage)}")
    if result.gaps:
        lines.append(f"gaps={len(result.gaps)}")
    return "\n".join(lines)


def _handoff_framing(result: ReviewResult) -> str:
    """Provenance + read-only + completeness header for the handoff block."""
    lines = [
        "[ReviewAgent handoff]",
        f"source={REVIEW_REPORT_SOURCE}",
        f"run_id={result.run_id}",
        f"status={result.status.value}",
        f"handoff={result.handoff.value}",
    ]
    if result.report_ref:
        lines.append(f"report_ref={result.report_ref}")
    return "\n".join(lines)


def render_handoff_block(result: ReviewResult, report_markdown: str | None) -> str:
    """Render the full first-turn handoff context for one review result.

    The block always states the handoff state and the coverage gaps. When a
    report artifact exists its markdown is injected verbatim — the spec
    forbids silently substituting a summary for the complete report. When no
    artifact exists, the block states the failure and what *is* available
    (bounded findings, warnings) instead of pretending a report exists.

    ``report_markdown`` is the artifact's report text, or ``None`` when the
    caller could not load it (missing/partial handoff).
    """
    parts = [_handoff_framing(result)]
    if result.gaps:
        parts.append(
            "Coverage gaps (this report is explicitly incomplete):\n"
            + "\n".join(f"- {gap}" for gap in result.gaps)
        )
    if report_markdown:
        parts.append("Full review report (authoritative, read-only):\n\n" + report_markdown)
    else:
        parts.append(
            "Handoff failed: no review report artifact is available for this run.\n"
            + (f"Reason: {result.error}\n" if result.error else "")
            + "The original review cannot be retried or resumed in this session. "
            "Only the partial results below may be used."
        )
        if result.summary:
            parts.append("Available partial result:\n" + result.summary)
        if result.findings:
            findings = "\n".join(
                f"- [{finding.get('severity', '?')}] {finding.get('file', '?')}: "
                f"{finding.get('title', '?')}"
                for finding in result.findings
            )
            parts.append("Recorded findings (unverified against a report):\n" + findings)
        if result.warnings:
            parts.append(
                "Warnings:\n" + "\n".join(f"- {warning}" for warning in result.warnings)
            )
    parts.append(
        "The report artifact and the review run state are authoritative and must "
        "not be rewritten or reopened by this conversation."
    )
    return "\n\n".join(parts)


def render_handoff_directive(result: ReviewResult) -> str:
    """Short system-prompt directive injected on the first conversation turn.

    States the provenance, the handoff state and the read-only rule at system
    level, so the constraint survives even if the replayed history is later
    compressed.
    """
    return (
        "This session already completed one code review executed by ReviewAgent "
        f"(run_id={result.run_id}, status={result.status.value}, "
        f"handoff={result.handoff.value}). The review report is injected below "
        "as read-only context; the original report artifact and the review run "
        "state are authoritative and must not be modified. Continue in "
        "conversation: discuss the findings, and repair or verify code in the "
        "original target repository when asked."
    )


__all__ = [
    "GAP_MAX_CHARS",
    "REVIEW_REPORT_SOURCE",
    "ReviewHandoffState",
    "ReviewResult",
    "render_handoff_block",
    "render_handoff_directive",
    "render_review_context_index",
    "result_from_run_state",
    "result_from_session_metadata",
]
