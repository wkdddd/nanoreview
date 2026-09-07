"""Review finalizer: parse subagent results, validate, and render reports."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from nanoreview.review.output.judge import ReviewJudge, ReviewJudgeStats
from nanoreview.review.output.report import render_review_report
from nanoreview.review.output.validator import ReviewValidator, ValidationContext
from nanoreview.review.types import (
    FileSkipSummary,
    FindingVerdict,
    GitHubDiffEvidence,
    ReviewBudgetSkip,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
    ReviewJudgeDecision,
    ReviewJudgedFinding,
    ReviewJudgeVerdict,
    normalize_review_dimension,
)


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
    """Parses subagent outputs, validates findings, produces final report."""

    _INCOMPLETE_ERROR_PATTERNS = (
        "github api rate limited",
        "failed to fetch github repository context",
        "unable to fetch",
        "could not fetch",
        "context unavailable",
        "no repository context",
        "error:",
        "failed",
        "blocked",
        "disabled",
        "no structured findings",
        "invalid json",
        "无法审查",
        "无法直接拉取",
        "未找到",
    )

    def __init__(
        self,
        workspace: str,
        changed_files: list[str] | None = None,
        *,
        allowed_dimensions: list[str] | set[str] | None = None,
        local_target: str | None = None,
        remote_diff: GitHubDiffEvidence | None = None,
        routing_mode: str = "explicit",
        selected_dimensions: list[str] | tuple[str, ...] | None = None,
        budget_skipped: list[ReviewBudgetSkip] | tuple[ReviewBudgetSkip, ...] = (),
        skipped_files: list[FileSkipSummary] | tuple[FileSkipSummary, ...] = (),
    ) -> None:
        self._workspace = workspace
        self._changed_files = list(changed_files or [])
        self._local_target = local_target
        self._ctx = ValidationContext(
            workspace=workspace,
            changed_files=self._changed_files,
            local_target=local_target,
            remote_diff=remote_diff,
        )
        self._validator = ReviewValidator(self._ctx)
        self._dimensions: list[ReviewDimensionResult] = []
        self._errors: list[str] = []
        self._allowed_dimensions = self._normalize_allowed_dimensions(allowed_dimensions)
        self._routing_mode = routing_mode
        self._selected_dimensions = tuple(selected_dimensions or ())
        self._budget_skipped = tuple(budget_skipped)
        self._skipped_files = tuple(skipped_files)
        self._judge_stats: ReviewJudgeStats | None = None

    def set_allowed_dimensions(self, allowed_dimensions: list[str] | set[str] | None) -> None:
        self._allowed_dimensions = self._normalize_allowed_dimensions(allowed_dimensions)

    def set_validation_context(
        self,
        *,
        workspace: str,
        changed_files: list[str] | None = None,
        local_target: str | None = None,
        remote_diff: GitHubDiffEvidence | None = None,
    ) -> None:
        if self._dimensions:
            logger.warning("review.finalizer.validation_context_ignored reason=already_ingested")
            return
        self._workspace = workspace
        self._changed_files = list(changed_files or [])
        self._local_target = local_target
        self._ctx = ValidationContext(
            workspace=workspace,
            changed_files=self._changed_files,
            local_target=local_target,
            remote_diff=remote_diff,
        )
        self._validator = ReviewValidator(self._ctx)

    @property
    def dimensions(self) -> list[ReviewDimensionResult]:
        return list(self._dimensions)

    def ingest_messages(self, messages: list[dict[str, Any]]) -> int:
        """Ingest structured subagent outputs from runner messages."""
        count = 0
        for message in messages:
            meta = self._subagent_metadata(message)
            if not meta:
                continue
            dimension = str(meta.get("subagent_label") or meta.get("label") or "unknown")
            raw_output = self._subagent_raw_output(message, meta)
            if not raw_output.strip():
                logger.warning("review.finalizer.skip_empty dimension={}", dimension)
                continue
            self.ingest_subagent_output(dimension, raw_output)
            count += 1
        logger.info("review.finalizer.ingest messages={} dimensions={}", len(messages), count)
        return count

    def ingest_subagent_output(self, dimension: str, raw_output: str) -> ReviewDimensionResult:
        """Parse one subagent's raw text output and validate its candidates."""
        normalized_dimension = normalize_review_dimension(dimension) or dimension.strip().lower()
        if self._allowed_dimensions is not None and normalized_dimension not in self._allowed_dimensions:
            logger.warning(
                "review.finalizer.skip_disallowed dimension={} allowed={}",
                dimension,
                sorted(self._allowed_dimensions),
            )
            self._errors.append(f"Skipped disallowed review dimension: {dimension}")
            return ReviewDimensionResult(
                dimension=normalized_dimension or "unknown",
                status="skipped_disallowed",
            )
        dimension = normalized_dimension
        incomplete_reason = self._incomplete_reason(raw_output)
        if incomplete_reason:
            result = ReviewDimensionResult(
                dimension=dimension,
                status="incomplete",
                errors=[incomplete_reason],
            )
            self._upsert_dimension(result)
            return result
        candidates = self._parse_candidates(dimension, raw_output)
        if not candidates:
            result = ReviewDimensionResult(
                dimension=dimension,
                status="no_findings",
            )
            self._upsert_dimension(result)
            return result
        result = self._validator.validate_candidates(candidates, dimension)
        self._upsert_dimension(result)
        return result

    _STATUS_PRIORITY: dict[str, int] = {
        "validated": 3, "no_findings": 2, "incomplete": 1, "error": 0, "skipped_disallowed": -1
    }

    def _upsert_dimension(self, result: ReviewDimensionResult) -> None:
        """Insert or update a dimension result,removing the redundant dimensions."""
        for i, existing in enumerate(self._dimensions):
            if existing.dimension == result.dimension:
                if self._STATUS_PRIORITY.get(result.status, 0) > self._STATUS_PRIORITY.get(existing.status, 0):
                    self._dimensions[i] = result
                return
        self._dimensions.append(result)

    def get_needs_confirmation(self) -> list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]]:
        """Return candidates whose final verdict requires manual confirmation.

        For dimensions already through the judge this collects judged entries
        whose final verdict is uncertain (needs_confirmation), surfacing the
        judge's reason. Uncertain candidates that have not been judged yet
        fall through from ``d.uncertain``. This keeps
        ``ReviewFinalizerResult.needs_confirmation`` consistent with the
        report's Needs Confirmation section.
        """
        items: list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]] = []
        for d in self._dimensions:
            if d.judged:
                for item in d.judged:
                    if item.final_verdict != FindingVerdict.UNCERTAIN:
                        continue
                    reason = item.hard_verdict
                    if item.judge_verdict is not None:
                        reason = ReviewFindingVerdict(
                            verdict=FindingVerdict.UNCERTAIN,
                            reason=item.judge_verdict.reason,
                        )
                    items.append((item.candidate, reason))
            else:
                items.extend(d.uncertain)
        return items

    async def apply_judge(self, judge: ReviewJudge | None) -> None:
        """Apply AI judge verdicts to every accepted/uncertain candidate.

        When the judge is unavailable (not created, disabled, or failing
        outright), candidates are explicitly marked ``needs_confirmation``
        instead of silently inheriting their hard verdicts — a candidate must
        never be presented as having passed the judge without a verdict.
        """
        if judge is None:
            logger.info("review.finalizer.judge.unavailable reason=not_created")
            self._judge_stats = self._mark_unjudged_needs_confirmation(
                "AI judge is unavailable; manual verification required"
            )
            return
        try:
            verdicts = await judge.judge_dimensions(self._dimensions)
        except Exception as exc:
            logger.warning("review.finalizer.judge.failed reason={}", exc)
            self._judge_stats = self._mark_unjudged_needs_confirmation(
                "AI judge failed; manual verification required"
            )
            return
        self._judge_stats = getattr(judge, "last_stats", None)
        if not verdicts:
            total = self._candidate_total()
            if total > 0:
                # Defensive: the judge produced no verdicts at all while
                # candidates exist — surface them for manual verification.
                logger.warning(
                    "review.finalizer.judge.no_verdicts candidates={}", total
                )
                self._judge_stats = self._mark_unjudged_needs_confirmation(
                    "AI judge returned no verdicts; manual verification required"
                )
            else:
                self._apply_judged_defaults()
            return
        for dimension in self._dimensions:
            judged: list[ReviewJudgedFinding] = []
            for candidate in dimension.accepted:
                hard = ReviewFindingVerdict(
                    verdict=FindingVerdict.ACCEPTED,
                    reason="hard validation accepted",
                )
                judged.append(ReviewJudgedFinding(
                    candidate=candidate,
                    hard_verdict=hard,
                    judge_verdict=self._verdict_or_needs_confirmation(
                        verdicts, candidate,
                        "AI judge returned no verdict for this candidate",
                    ),
                ))
            for candidate, hard in dimension.uncertain:
                judged.append(ReviewJudgedFinding(
                    candidate=candidate,
                    hard_verdict=hard,
                    judge_verdict=self._verdict_or_needs_confirmation(
                        verdicts, candidate,
                        "AI judge returned no verdict for this candidate",
                    ),
                ))
            dimension.judged = judged
        logger.info("review.finalizer.judge.applied dimensions={}", len(self._dimensions))

    @staticmethod
    def _verdict_or_needs_confirmation(
        verdicts: dict[str, ReviewJudgeVerdict],
        candidate: ReviewFindingCandidate,
        missing_reason: str,
    ) -> ReviewJudgeVerdict:
        """Return the judge verdict, or an explicit needs_confirmation fallback."""
        verdict = verdicts.get(ReviewJudge.candidate_id(candidate))
        if verdict is not None:
            return verdict
        return ReviewJudgeVerdict(
            decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
            reason=missing_reason,
            confidence="low",
            severity=candidate.severity,
        )

    def _candidate_total(self) -> int:
        return sum(
            len(dimension.accepted) + len(dimension.uncertain)
            for dimension in self._dimensions
        )

    def _mark_unjudged_needs_confirmation(self, reason: str) -> ReviewJudgeStats | None:
        """Mark every accepted/uncertain candidate as needs_confirmation.

        Used when the judge cannot run: candidates keep their hard verdicts for
        traceability but carry an explicit judge verdict requiring manual
        verification, so the report never presents them as judge-accepted.
        Returns the judge statistics for the unavailable path (or None when
        there are no candidates to judge).
        """
        total = 0
        for dimension in self._dimensions:
            judged: list[ReviewJudgedFinding] = []
            for candidate in dimension.accepted:
                judged.append(ReviewJudgedFinding(
                    candidate=candidate,
                    hard_verdict=ReviewFindingVerdict(
                        verdict=FindingVerdict.ACCEPTED,
                        reason="hard validation accepted",
                    ),
                    judge_verdict=ReviewJudgeVerdict(
                        decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
                        reason=reason,
                        confidence="low",
                        severity=candidate.severity,
                    ),
                ))
                total += 1
            for candidate, hard in dimension.uncertain:
                judged.append(ReviewJudgedFinding(
                    candidate=candidate,
                    hard_verdict=hard,
                    judge_verdict=ReviewJudgeVerdict(
                        decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
                        reason=reason,
                        confidence="low",
                        severity=candidate.severity,
                    ),
                ))
                total += 1
            dimension.judged = judged
        if total == 0:
            return None
        return ReviewJudgeStats(
            total_candidates=total,
            sent_candidates=0,
            returned_verdicts=0,
            needs_confirmation=total,
            batches=0,
        )

    def _apply_judged_defaults(self) -> None:
        """Fill judged lists with hard verdicts when no judge pass has run."""
        for dimension in self._dimensions:
            if dimension.judged:
                continue
            judged: list[ReviewJudgedFinding] = []
            judged.extend(
                ReviewJudgedFinding(
                    candidate=candidate,
                    hard_verdict=ReviewFindingVerdict(
                        verdict=FindingVerdict.ACCEPTED,
                        reason="hard validation accepted",
                    ),
                )
                for candidate in dimension.accepted
            )
            judged.extend(
                ReviewJudgedFinding(candidate=candidate, hard_verdict=verdict)
                for candidate, verdict in dimension.uncertain
            )
            dimension.judged = judged

    def finalize(self, target_name: str) -> ReviewFinalizerResult:
        """Produce final report markdown from all ingested dimensions."""
        if not self._dimensions:
            self._errors.append("No review dimension results were produced.")
        self._apply_judged_defaults()
        needs_confirmation = self.get_needs_confirmation()
        try:
            report = render_review_report(
                target_name,
                self._dimensions,
                routing_mode=self._routing_mode,
                selected_dimensions=self._selected_dimensions,
                budget_skipped=self._budget_skipped,
                skipped_files=self._skipped_files,
                judge_stats=self._judge_stats,
            )
        except Exception as exc:
            logger.error("report rendering failed: {}", exc)
            report = f"## Code Review Report: {target_name}\n\n### Error\n\nReport rendering failed: {exc}\n"
            self._errors.append(str(exc))

        return ReviewFinalizerResult(
            report_markdown=report,
            dimensions=self._dimensions,
            needs_confirmation=needs_confirmation,
            errors=self._errors,
        )

    def _parse_candidates(
        self, dimension: str, raw: str
    ) -> list[ReviewFindingCandidate]:
        """Parse the canonical review_submit tool result."""
        payload = self._review_submit_payload(raw)
        if payload is None:
            return []
        findings = payload.get("findings")
        if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
            return []
        return [self._dict_to_candidate(item, dimension) for item in findings]

    @classmethod
    def _incomplete_reason(cls, raw: str) -> str:
        text = raw.strip()
        if not text:
            return ""

        payload = cls._review_submit_payload(text, log_diagnostics=False)
        if payload is not None:
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                return "; ".join(str(e) for e in errors)[:300]
            return ""

        lower = text.lower()
        if not any(pattern in lower for pattern in cls._INCOMPLETE_ERROR_PATTERNS):
            return "No structured findings were produced by this reviewer."
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(pattern in stripped.lower() for pattern in cls._INCOMPLETE_ERROR_PATTERNS):
                return stripped[:300]
        return text[:300]

    @staticmethod
    def _review_submit_payload(raw: str, *, log_diagnostics: bool = True) -> dict[str, Any] | None:
        text = raw.strip()
        if not text:
            return None
        if not text.startswith("{"):
            logger.debug("review.finalizer.submit_payload.skip_non_json")
            return None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            if log_diagnostics:
                logger.warning("review.finalizer.submit_payload.invalid_json error={}", e)
            return None
        if not isinstance(data, dict) or data.get("submitted") is not True :
            if log_diagnostics:
                logger.warning("review.finalizer.submit_payload.invalid_schema reason=submitted")
            return None
        errors = data.get("errors")
        if not isinstance(errors, list):
            if log_diagnostics:
                logger.warning("review.finalizer.submit_payload.invalid_schema reason=errors")
            return None
        return data

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

    @staticmethod
    def _subagent_metadata(message: dict[str, Any]) -> dict[str, Any] | None:
        meta = message.get("_metadata")
        if not isinstance(meta, dict):
            meta = message.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
        if message.get("injected_event") == "subagent_result":
            meta = {
                **meta,
                **{
                    key: message[key]
                    for key in (
                        "injected_event",
                        "subagent_task_id",
                        "subagent_label",
                        "subagent_status",
                        "subagent_result",
                    )
                    if key in message
                },
            }
        if meta.get("injected_event") != "subagent_result":
            return None
        return meta

    @staticmethod
    def _subagent_raw_output(message: dict[str, Any], meta: dict[str, Any]) -> str:
        raw = meta.get("subagent_result")
        if isinstance(raw, str):
            return raw
        content = message.get("content", "")
        if not isinstance(content, str):
            return str(content)
        return content.strip()
