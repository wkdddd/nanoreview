"""Hard validation for review finding candidates.

Validation is a pure function of the frozen pull-request snapshot: a candidate
is checked against the PR's changed paths, the line ranges the diff actually
touched, the evidence ids the run handed that reviewer, and the dimension's
detail schema. There is no filesystem access here, so a finding can never be
"confirmed" by reading a file the PR did not change, and no location is silently
rewritten to a different one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from loguru import logger

from nanoreview.agent.tools.base import Schema
from nanoreview.review.profiles import get_reviewer_profile
from nanoreview.review.source.utils import line_in_ranges
from nanoreview.review.types import (
    SEVERITY_ORDER,
    FindingVerdict,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
)

#: Sides a finding can point at. ``new`` is the head commit, ``old`` the merge
#: base; both are pinned by the snapshot the run was admitted with.
_VALID_SIDES = frozenset({"new", "old"})


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """The frozen PR boundary a candidate is validated against."""

    #: Repository-relative paths changed by the pull request (new side).
    changed_files: tuple[str, ...] = ()
    #: New-side line ranges the diff touched, per changed path.
    changed_ranges: dict[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    #: Evidence ids this run authorized for the reviewer that produced the
    #: candidate (prefetch units plus the reviewer's own pr_context reads).
    evidence_ids: frozenset[str] = frozenset()

    def normalized_changed_files(self) -> set[str]:
        return {_normalize_rel_path(path) for path in self.changed_files}


class ReviewValidator:
    """Validates candidate findings against the frozen PR boundary."""

    def __init__(
        self, ctx: ValidationContext, *, seen_fingerprints: set[str] | None = None
    ) -> None:
        self._ctx = ctx
        self._changed_files = ctx.normalized_changed_files()
        # Shared across every dimension of one run so the same finding cannot be
        # accepted twice by two reviewers.
        self._seen_fingerprints = (
            seen_fingerprints if seen_fingerprints is not None else set()
        )
        self.stats = {"accepted": 0, "rejected": 0, "needs_confirmation": 0}

    def validate_candidates(
        self, candidates: list[ReviewFindingCandidate], dimension: str
    ) -> ReviewDimensionResult:
        result = ReviewDimensionResult(dimension=dimension, status="validated")
        validated: list[ReviewFindingCandidate] = []
        for candidate in candidates:
            verdict = self._validate_one(candidate, dimension)
            validated.append(candidate)
            if verdict.verdict == FindingVerdict.ACCEPTED:
                result.accepted.append(candidate)
                self.stats["accepted"] += 1
            elif verdict.verdict == FindingVerdict.REJECTED:
                result.rejected.append((candidate, verdict))
                self.stats["rejected"] += 1
            else:
                result.uncertain.append((candidate, verdict))
                self.stats["needs_confirmation"] += 1
        result.candidates = validated
        logger.debug(
            "validator dimension={} accepted={} rejected={} needs_confirmation={}",
            dimension,
            len(result.accepted),
            len(result.rejected),
            len(result.uncertain),
        )
        return result

    def _validate_one(
        self, candidate: ReviewFindingCandidate, dimension: str
    ) -> ReviewFindingVerdict:
        if candidate.severity not in SEVERITY_ORDER:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED,
                reason=f"invalid severity: {candidate.severity}",
            )
        if not candidate.file or not candidate.title:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED,
                reason="missing required field (file or title)",
            )
        if candidate.side not in _VALID_SIDES:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED,
                reason=f"invalid side: {candidate.side}",
            )
        fingerprint = self._fingerprint(candidate)
        if fingerprint in self._seen_fingerprints:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED, reason="duplicate finding"
            )
        self._seen_fingerprints.add(fingerprint)

        profile = get_reviewer_profile(dimension)
        if profile is None:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED,
                reason=f"unknown reviewer profile: {dimension}",
            )
        detail_errors = Schema.validate_json_schema_value(
            candidate.details, profile.details_schema, "details"
        )
        if detail_errors:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.REJECTED,
                reason="invalid reviewer details: " + "; ".join(detail_errors),
            )

        path = _normalize_rel_path(candidate.file)
        if path not in self._changed_files:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.UNCERTAIN,
                reason="file not in changed set",
                missing_evidence=(
                    "the pull request does not change this file, so the finding "
                    "points outside the reviewed diff"
                ),
            )
        if candidate.line is not None and candidate.side != "old":
            # ``changed_ranges`` holds new-side line numbers only; an old-side
            # (merge base) line number cannot be checked against them, so the
            # boundary check stays on the changed-file set for that side.
            ranges = self._ctx.changed_ranges.get(path, ())
            if ranges and not line_in_ranges(candidate.line, ranges):
                return ReviewFindingVerdict(
                    verdict=FindingVerdict.UNCERTAIN,
                    reason=f"line {candidate.line} is not part of the diff",
                    missing_evidence=(
                        "the reported line is unchanged by this pull request; "
                        "cite a line the diff touched"
                    ),
                )
        unknown_refs = [
            ref for ref in candidate.evidence_refs if ref not in self._ctx.evidence_ids
        ]
        if unknown_refs:
            return ReviewFindingVerdict(
                verdict=FindingVerdict.UNCERTAIN,
                reason="unknown evidence reference: " + ", ".join(unknown_refs[:5]),
                missing_evidence=(
                    "findings may only cite evidence ids returned by this run's "
                    "evidence or its pr_context reads"
                ),
            )
        if not candidate.evidence.strip():
            return ReviewFindingVerdict(
                verdict=FindingVerdict.UNCERTAIN,
                reason="no evidence provided",
                missing_evidence="candidate lacks supporting evidence snippet",
            )
        # Cited evidence ids are checked above (every ref must exist); a finding
        # that cites nothing is judged on its inline evidence, so an extra
        # "must cite" rule would not add information here.
        return ReviewFindingVerdict(verdict=FindingVerdict.ACCEPTED)

    @staticmethod
    def _fingerprint(candidate: ReviewFindingCandidate) -> str:
        key = f"{candidate.file}:{candidate.line or 0}:{candidate.title.lower().strip()}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


def _normalize_rel_path(raw: str) -> str:
    value = (raw or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.lstrip("/")


__all__ = ["ReviewValidator", "ValidationContext"]
