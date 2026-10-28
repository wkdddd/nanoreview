"""Validate reviewer findings and assemble the final review report.

This module owns no candidate accumulation. The run's validated dimension
results live in the coordinator-owned blackboard (the single result store), so
the finalizer is a pure function of a candidate set:

* :meth:`ReviewFinalizer.validate_findings` turns one reviewer's accepted
  ``findings`` array into a validated :class:`ReviewDimensionResult`.
* :meth:`ReviewFinalizer.judge` applies judge verdicts to a given candidate set
  and returns a *new* set, so judge and report always read the same version.
* :meth:`ReviewFinalizer.finalize` renders the report from that set.

Nothing here reads or writes run state, and a failed reviewer is recorded as
``incomplete`` with an explicit reason rather than silently as success.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoreview.agent.review_state import bound_child_error
from nanoreview.review.output.validator import ReviewValidator, ValidationContext
from nanoreview.review.types import (
    FindingVerdict,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
    ReviewJudgeDecision,
    ReviewJudgedFinding,
    ReviewJudgeVerdict,
    normalize_review_dimension,
)

if TYPE_CHECKING:
    from nanoreview.review.output.judge import JudgeExecutionResult, ReviewJudge, ReviewJudgeStats
    from nanoreview.review.types import FileSkipSummary

#: Reviewer failure reasons are bounded: a provider trace or transcript must
#: never inflate the run's audit trail or the report.
_REVIEWER_ERROR_MAX_CHARS = 500

#: Status priority when the same dimension is recorded twice. A later, better
#: outcome must not be downgraded by a stale one.
_STATUS_PRIORITY: dict[str, int] = {
    "validated": 3,
    "no_findings": 2,
    "incomplete": 1,
    "error": 0,
    "skipped_disallowed": -1,
}


def reviewer_failure_reason(status: str, detail: str) -> str:
    """Bounded, explicit reason for a reviewer that did not complete."""
    label = status or "unknown"
    text = " ".join(detail.split())
    if text:
        return f"reviewer status={label}: {text}"[:_REVIEWER_ERROR_MAX_CHARS]
    return f"reviewer status={label}: no result output"


@dataclass
class ReviewFinalizerResult:
    """Output of the finalizer process."""

    report_markdown: str
    dimensions: list[ReviewDimensionResult] = field(default_factory=list)
    needs_confirmation: list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]] = field(
        default_factory=list
    )
    errors: list[str] = field(default_factory=list)


class ReviewFinalizer:
    """Validates findings and renders the report for one review run.

    The instance carries the run's fixed validation context (workspace, changed
    files, allowed dimensions, mode, selected dimensions, skipped files and the
    planned-no-reviewer explanation) and nothing else. It is safe to reuse for
    every dimension of a run because it accumulates no state.
    """

    def __init__(
        self,
        workspace: str,
        changed_files: list[str] | None = None,
        *,
        allowed_dimensions: list[str] | set[str] | None = None,
        local_target: str | None = None,
        mode: str = "special",
        selected_dimensions: list[str] | tuple[str, ...] | None = None,
        skipped_files: list["FileSkipSummary"] | tuple["FileSkipSummary", ...] = (),
        planner_summary: str = "",
    ) -> None:
        self._workspace = workspace
        self._changed_files = list(changed_files or [])
        self._local_target = local_target
        self._ctx = ValidationContext(
            workspace=workspace,
            changed_files=self._changed_files,
            local_target=local_target,
        )
        self._validator = ReviewValidator(self._ctx)
        self._allowed_dimensions = self._normalize_allowed_dimensions(allowed_dimensions)
        self._mode = mode
        self._selected_dimensions = tuple(selected_dimensions or ())
        self._skipped_files = tuple(skipped_files)
        #: Bounded explanation rendered when planning deliberately dispatched no
        #: reviewer. An empty value means "reviewers were expected", so a report
        #: with no dimension results is a real gap rather than a triage outcome.
        self._planner_summary = planner_summary

    # -- validation ---------------------------------------------------------

    def dimension_failure(
        self,
        dimension: str,
        failure_reason: str,
    ) -> ReviewDimensionResult:
        """Record one dimension whose reviewer failed or was cancelled."""
        normalized = self._normalize_dimension(dimension)
        return ReviewDimensionResult(
            dimension=normalized,
            status="incomplete",
            errors=[bound_child_error(failure_reason) or "reviewer did not complete"],
        )

    def validate_findings(
        self,
        dimension: str,
        findings: list[dict[str, Any]],
    ) -> ReviewDimensionResult:
        """Validate one reviewer's accepted findings for *dimension*.

        The findings array has already passed the reviewer output contract, so
        this step is the *review* validation: target boundary, line range,
        evidence presence and per-dimension rules. Candidates that fail are
        recorded as rejected/uncertain instead of being dropped.
        """
        normalized = self._normalize_dimension(dimension)
        if self._allowed_dimensions is not None and normalized not in self._allowed_dimensions:
            logger.warning(
                "review.finalizer.skip_disallowed dimension={} allowed={}",
                dimension,
                sorted(self._allowed_dimensions),
            )
            return ReviewDimensionResult(
                dimension=normalized,
                status="skipped_disallowed",
                errors=[f"Skipped disallowed review dimension: {dimension}"],
            )
        if not findings:
            return ReviewDimensionResult(dimension=normalized, status="no_findings")
        candidates = [
            self._dict_to_candidate(item, normalized) for item in findings
        ]
        return self._validator.validate_candidates(candidates, normalized)

    # -- judge --------------------------------------------------------------

    async def judge(
        self,
        judge: "ReviewJudge | None",
        dimensions: tuple[ReviewDimensionResult, ...],
    ) -> tuple["JudgeExecutionResult", tuple[ReviewDimensionResult, ...]]:
        """Apply AI judge verdicts to a given candidate set.

        Returns the judge's execution result and the *new* candidate set: the
        caller records both, so the report and the recorded verdicts always come
        from one candidate version. Candidates without a verdict — including
        every candidate of a failed batch — are marked ``needs_confirmation``
        rather than inheriting their hard verdicts, so a candidate is never
        presented as having passed the judge.
        """
        from nanoreview.review.output.judge import JudgeExecutionResult

        if judge is None:
            logger.info("review.finalizer.judge.unavailable reason=not_created")
            judged, stats = self._mark_unjudged(
                dimensions, "AI judge is unavailable; manual verification required"
            )
            return (
                JudgeExecutionResult(verdicts={}, stats=stats, usage={}, error=None),
                judged,
            )
        try:
            result = await judge.judge_dimensions(list(dimensions))
        except Exception as exc:
            # Cancellation is a BaseException and propagates untouched; any
            # other escaping judge failure degrades to needs_confirmation.
            logger.warning("review.finalizer.judge.failed reason={}", exc)
            judged, stats = self._mark_unjudged(
                dimensions, "AI judge failed; manual verification required"
            )
            return (
                JudgeExecutionResult(
                    verdicts={}, stats=stats, usage={}, error=bound_child_error(exc)
                ),
                judged,
            )
        if result.error:
            logger.warning("review.finalizer.judge.partial_failure reason={}", result.error)
        if not result.verdicts:
            if self._candidate_total(dimensions) > 0:
                logger.warning(
                    "review.finalizer.judge.no_verdicts candidates={}",
                    self._candidate_total(dimensions),
                )
                judged, stats = self._mark_unjudged(
                    dimensions,
                    "AI judge returned no verdicts; manual verification required",
                )
                return (
                    JudgeExecutionResult(
                        verdicts={},
                        stats=stats,
                        usage=result.usage,
                        error=result.error,
                    ),
                    judged,
                )
            return result, self._apply_hard_verdicts(dimensions)
        merged = tuple(
            replace(dimension, judged=self._judged_for(dimension, result.verdicts))
            for dimension in dimensions
        )
        logger.info("review.finalizer.judge.applied dimensions={}", len(merged))
        return result, merged

    # -- report -------------------------------------------------------------

    def finalize(
        self,
        dimensions: tuple[ReviewDimensionResult, ...],
        *,
        target_name: str | None = None,
        judge_stats: "ReviewJudgeStats | None" = None,
    ) -> ReviewFinalizerResult:
        """Render the report from a candidate set.

        A run with no dimension results is only an error when reviewers were
        expected. When planning finished with no assignments (every evidence
        unit dismissed or left unexamined), the report states that outcome
        explicitly instead of claiming the review failed to run.
        """
        errors: list[str] = []
        planned_no_reviewers = bool(self._planner_summary) and not dimensions
        if not dimensions and not planned_no_reviewers:
            errors.append("No review dimension results were produced.")
        final_dimensions = list(self._apply_hard_verdicts(dimensions))
        needs_confirmation = self._needs_confirmation(final_dimensions)
        from nanoreview.review.output.report import render_review_report

        try:
            report = render_review_report(
                target_name or self._target_name(),
                final_dimensions,
                mode=self._mode,
                selected_dimensions=self._selected_dimensions,
                skipped_files=self._skipped_files,
                judge_stats=judge_stats,
                planner_summary=self._planner_summary,
            )
        except Exception as exc:
            logger.error("report rendering failed: {}", exc)
            report = (
                f"## Code Review Report: {target_name or self._target_name()}\n\n"
                f"### Error\n\nReport rendering failed: {exc}\n"
            )
            errors.append(str(exc))
        return ReviewFinalizerResult(
            report_markdown=report,
            dimensions=final_dimensions,
            needs_confirmation=needs_confirmation,
            errors=errors,
        )

    def _target_name(self) -> str:
        return self._local_target or "target"

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _judged_for(
        dimension: ReviewDimensionResult,
        verdicts: dict[str, ReviewJudgeVerdict],
    ) -> list[ReviewJudgedFinding]:
        judged: list[ReviewJudgedFinding] = []
        for candidate in dimension.accepted:
            judged.append(
                ReviewJudgedFinding(
                    candidate=candidate,
                    hard_verdict=ReviewFindingVerdict(
                        verdict=FindingVerdict.ACCEPTED,
                        reason="hard validation accepted",
                    ),
                    judge_verdict=ReviewFinalizer._verdict_or_needs_confirmation(
                        verdicts, candidate
                    ),
                )
            )
        for candidate, hard in dimension.uncertain:
            judged.append(
                ReviewJudgedFinding(
                    candidate=candidate,
                    hard_verdict=hard,
                    judge_verdict=ReviewFinalizer._verdict_or_needs_confirmation(
                        verdicts, candidate
                    ),
                )
            )
        return judged

    @staticmethod
    def _verdict_or_needs_confirmation(
        verdicts: dict[str, ReviewJudgeVerdict],
        candidate: ReviewFindingCandidate,
    ) -> ReviewJudgeVerdict:
        from nanoreview.review.output.judge import ReviewJudge

        verdict = verdicts.get(ReviewJudge.candidate_id(candidate))
        if verdict is not None:
            return verdict
        return ReviewJudgeVerdict(
            decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
            reason="AI judge returned no verdict for this candidate",
            confidence="low",
            severity=candidate.severity,
        )

    def _mark_unjudged(
        self,
        dimensions: tuple[ReviewDimensionResult, ...],
        reason: str,
    ) -> tuple[tuple[ReviewDimensionResult, ...], "ReviewJudgeStats | None"]:
        """Mark every accepted/uncertain candidate as needing confirmation."""
        from nanoreview.review.output.judge import ReviewJudgeStats

        total = self._candidate_total(dimensions)
        judged_dimensions = tuple(
            replace(
                dimension,
                judged=[
                    *(
                        ReviewJudgedFinding(
                            candidate=candidate,
                            hard_verdict=ReviewFindingVerdict(
                                verdict=FindingVerdict.ACCEPTED,
                                reason="hard validation accepted",
                            ),
                            judge_verdict=self._confirmation(candidate, reason),
                        )
                        for candidate in dimension.accepted
                    ),
                    *(
                        ReviewJudgedFinding(
                            candidate=candidate,
                            hard_verdict=hard,
                            judge_verdict=self._confirmation(candidate, reason),
                        )
                        for candidate, hard in dimension.uncertain
                    ),
                ],
            )
            for dimension in dimensions
        )
        if total == 0:
            return judged_dimensions, None
        return judged_dimensions, ReviewJudgeStats(
            total_candidates=total,
            sent_candidates=0,
            returned_verdicts=0,
            needs_confirmation=total,
            batches=0,
        )

    @staticmethod
    def _confirmation(
        candidate: ReviewFindingCandidate, reason: str
    ) -> ReviewJudgeVerdict:
        return ReviewJudgeVerdict(
            decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
            reason=reason,
            confidence="low",
            severity=candidate.severity,
        )

    @staticmethod
    def _apply_hard_verdicts(
        dimensions: tuple[ReviewDimensionResult, ...],
    ) -> tuple[ReviewDimensionResult, ...]:
        """Fill judged lists from hard verdicts when no judge pass has run."""
        filled: list[ReviewDimensionResult] = []
        for dimension in dimensions:
            if dimension.judged:
                filled.append(dimension)
                continue
            judged: list[ReviewJudgedFinding] = [
                ReviewJudgedFinding(
                    candidate=candidate,
                    hard_verdict=ReviewFindingVerdict(
                        verdict=FindingVerdict.ACCEPTED,
                        reason="hard validation accepted",
                    ),
                )
                for candidate in dimension.accepted
            ]
            judged.extend(
                ReviewJudgedFinding(candidate=candidate, hard_verdict=verdict)
                for candidate, verdict in dimension.uncertain
            )
            filled.append(replace(dimension, judged=judged))
        return tuple(filled)

    @staticmethod
    def _needs_confirmation(
        dimensions: list[ReviewDimensionResult],
    ) -> list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]]:
        """Candidates whose final verdict still requires manual confirmation."""
        items: list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]] = []
        for dimension in dimensions:
            for item in dimension.judged:
                if item.final_verdict != FindingVerdict.UNCERTAIN:
                    continue
                reason = item.hard_verdict
                if item.judge_verdict is not None:
                    reason = ReviewFindingVerdict(
                        verdict=FindingVerdict.UNCERTAIN,
                        reason=item.judge_verdict.reason,
                    )
                items.append((item.candidate, reason))
            for candidate, verdict in dimension.uncertain:
                if not dimension.judged:
                    items.append((candidate, verdict))
        return items

    @staticmethod
    def _candidate_total(dimensions: tuple[ReviewDimensionResult, ...]) -> int:
        return sum(
            len(dimension.accepted) + len(dimension.uncertain)
            for dimension in dimensions
        )

    def _normalize_dimension(self, dimension: str) -> str:
        return normalize_review_dimension(dimension) or dimension.strip().lower()

    @staticmethod
    def _normalize_allowed_dimensions(
        allowed_dimensions: list[str] | set[str] | None,
    ) -> set[str] | None:
        if not allowed_dimensions:
            return None
        normalized = {
            dimension
            for item in allowed_dimensions
            if (dimension := normalize_review_dimension(str(item)))
        }
        return normalized or None

    def _dict_to_candidate(self, d: dict, dimension: str) -> ReviewFindingCandidate:
        return ReviewFindingCandidate(
            severity=str(d.get("severity", "medium")).lower(),
            dimension=dimension,
            file=str(d.get("file", "")),
            line=d.get("line"),
            title=str(d.get("title", "")),
            evidence=str(d.get("evidence", "")),
            impact=str(d.get("impact", "")),
            recommendation=str(d.get("recommendation", "")),
            details=dict(d.get("details") or {}),
            confidence=str(d.get("confidence", "high")),
            source=str(d.get("source", "")),
        )


__all__ = [
    "ReviewFinalizer",
    "ReviewFinalizerResult",
    "reviewer_failure_reason",
]
