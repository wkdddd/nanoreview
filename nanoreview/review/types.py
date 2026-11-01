"""Shared code-review data types."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from nanoreview.review.profiles import REVIEWER_PROFILES

SEVERITY_ORDER = ("critical", "high", "medium", "low")


class ReviewMetaKey:
    """Session metadata keys for code review state.

    Review activation is decided solely by the presence of a valid
    ``review_target``; a target is always a pull request, so the persisted
    metadata names the repository and PR instead of a local path.
    """

    TARGET = "review_target"
    TARGET_TYPE = "review_target_type"
    ACTION = "review_action"
    REQUESTED_DIMENSIONS = "review_focus"
    REPOSITORY_ID = "review_repository_id"
    PULL_REQUEST_ID = "review_pull_request_id"
    PULL_REQUEST_NUMBER = "review_pull_request_number"
    BASE_REPO = "review_base_repo"
    HEAD_REPO = "review_head_repo"
    BASE_SHA = "review_base_sha"
    HEAD_SHA = "review_head_sha"
    MERGE_BASE_SHA = "review_merge_base_sha"
    MAX_CONCURRENT_SUBAGENTS = "max_concurrent_subagents"
    ALLOWED_DIMENSIONS = "allowed_review_dimensions"
    EVIDENCE_PROVIDER = "_review_evidence_service"
    #: In-process handoff of the admitted snapshot's patches, so prefetch
    #: reviews exactly the change frozen at admission. Never persisted to
    #: session metadata and never forwarded to reviewer metadata.
    FROZEN_DIFF = "_review_frozen_diff"
    # One-shot ReviewAgent run state (stable wire contract consumed by
    # WebUI/API and the report artifact API).
    RUN_ID = "review_run_id"
    STATUS = "review_status"
    PHASE = "review_phase"
    REPORT_REF = "review_report_ref"
    INPUT_FINGERPRINT = "review_input_fingerprint"
    #: Wire-safe reference to the immutable input snapshot captured at admission.
    SNAPSHOT_REF = "review_snapshot_ref"
    #: Bounded digest of the produced report, or the failure reason when no
    #: report exists. Persisted with the terminal state so a restart (or a
    #: transport that never owned the run) can explain the outcome without
    #: loading the report artifact.
    SUMMARY = "review_summary"
    #: Bounded failure reason persisted for a non-completed terminal run.
    ERROR = "review_error"
    #: Run whose report has already been injected into the first conversation
    #: turn. Its presence marks the handoff as consumed so a later turn does
    #: not inject the same report twice.
    HANDOFF_RUN_ID = "review_handoff_run_id"

ReviewTargetType = Literal["pull_request"]
#: Canonical wire value of the only supported review target type.
PULL_REQUEST_TARGET_TYPE = "pull_request"


class ReviewAction(StrEnum):
    REPO = "repo"
    DIFF = "diff"


def review_action_values() -> tuple[str, ...]:
    return tuple(action.value for action in ReviewAction)


@dataclass(frozen=True, slots=True)
class ReviewRole:
    name: str
    label: str
    description: str
    evidence_required: bool = False


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str
    file: str
    line: int | None
    title: str
    impact: str
    recommendation: str


@dataclass
class ReviewReport:
    target: str
    dimensions: list[str]
    summary: str
    findings: list[Finding] = field(default_factory=list)
    checks_performed: list[str] = field(default_factory=list)
    checks_skipped: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def max_severity(self) -> str | None:
        if not self.findings:
            return None
        for severity in SEVERITY_ORDER:
            if any(finding.severity == severity for finding in self.findings):
                return severity
        return None


ALL_REVIEW_ROLES: dict[str, ReviewRole] = {
    key: ReviewRole(
        name=profile.id,
        label=profile.label,
        description=profile.description,
        evidence_required=True,
    )
    for key, profile in REVIEWER_PROFILES.items()
}
#: The four specialized profiles. ``general`` is a peer reviewer, not a
#: fallback: it is dispatched like any other selected reviewer and may be
#: combined with any specialized profile.
SPECIAL_REVIEW_ROLE_NAMES: frozenset[str] = frozenset(
    name for name in ALL_REVIEW_ROLES if name != "general"
)
DEFAULT_REVIEW_ROLES = ALL_REVIEW_ROLES
OPTIONAL_REVIEW_ROLES: dict[str, ReviewRole] = {}


def normalize_review_dimension(value: str | None) -> str | None:
    """Normalize a role name or display label to a review dimension key."""
    raw = (value or "").strip().lower()
    if not raw:
        return None
    if raw in ALL_REVIEW_ROLES:
        return raw
    for key, role in ALL_REVIEW_ROLES.items():
        label = role.label.strip().lower()
        short_label = label.removesuffix(" reviewer").strip()
        dashed_label = " ".join(label.split()).replace(" ", "-")
        dashed_short_label = " ".join(short_label.split()).replace(" ", "-")
        if (
            raw == label
            or raw == short_label
            or raw == short_label + " review"
            or raw.startswith(label + " ")
            or raw.startswith(short_label + " review ")
            or raw.startswith(short_label + " reviewer ")
            or raw.replace(" ", "-") == dashed_label
            or raw.replace(" ", "-") == dashed_short_label
        ):
            return key
    return None


@dataclass(frozen=True, slots=True)
class PullRequestTarget:
    """The frozen pull request a review run is bound to.

    Every later read is pinned to ``base_sha``/``head_sha``/``merge_base_sha``;
    no member of this object names a branch, a URL to fetch or a local path.
    """

    repository: str
    number: int
    base_repo: str
    head_repo: str
    base_sha: str
    head_sha: str
    merge_base_sha: str
    snapshot_id: str = ""
    url: str | None = None

    @property
    def label(self) -> str:
        return f"{self.repository}#{self.number}"


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    target: str | None
    target_name: str | None
    target_type: ReviewTargetType
    action: ReviewAction
    roles: list[ReviewRole]
    user_requirements: str = ""
    pull_request: PullRequestTarget | None = None


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """One bounded, program-authorized unit of review evidence."""

    id: str
    path: str
    start_line: int | None = None
    end_line: int | None = None
    source: str = "prefetch"
    tags: tuple[str, ...] = ()
    excerpt: str = ""
    # Semantic unit metadata: file/module/class/function/diff/document/config/
    # syntax_diagnostic. Related chunks supplement the main chunk they link to.
    kind: str = "file"
    parent_id: str | None = None
    token_count: int = 0
    preview: str = ""
    # User review-query hit words found in the unit. Plain metadata: it never
    # routes or orders evidence and the reviewer decides from the diff alone.
    matched: tuple[str, ...] = ()
    # Human-readable statement of which real lines the preview actually covers,
    # e.g. "chunk lines 10-80; preview covers lines 10-16, 40-44".
    preview_coverage: str = ""

    @property
    def is_related(self) -> bool:
        return "related" in self.tags


@dataclass(frozen=True, slots=True)
class SkippedReviewUnit:
    """One file-level or line-range unit excluded from the review pipeline."""

    path: str
    reason: str
    start_line: int | None = None
    end_line: int | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FileSkipSummary:
    """Per-file aggregation of skipped units: one path, merged ranges."""

    path: str
    reasons: tuple[str, ...]
    ranges: tuple[tuple[int, int], ...] = ()
    whole_file: bool = True

    def describe(self) -> str:
        parts = [self.path]
        if not self.whole_file and self.ranges:
            parts.append(" (unreviewed lines " + ", ".join(f"{start}-{end}" for start, end in self.ranges) + ")")
        elif self.whole_file:
            parts.append(" (whole file)")
        parts.append(f" - {', '.join(self.reasons)}")
        return "".join(parts)


@dataclass(frozen=True, slots=True)
class ReviewEvidenceBundle:
    """Evidence exposed to the review pipeline and available for reviewer tasks."""

    references: tuple[EvidenceReference, ...] = ()
    summary: str = ""
    status: str = "ok"
    reason: str = ""
    skipped: tuple[SkippedReviewUnit, ...] = ()

    def by_id(self) -> dict[str, EvidenceReference]:
        return {reference.id: reference for reference in self.references}

    def skipped_by_file(self) -> dict[str, FileSkipSummary]:
        """Aggregate skipped units per file path, merging line ranges."""
        grouped: dict[str, list[SkippedReviewUnit]] = {}
        for unit in self.skipped:
            grouped.setdefault(unit.path, []).append(unit)
        summaries: dict[str, FileSkipSummary] = {}
        for path, units in grouped.items():
            reasons = tuple(dict.fromkeys(unit.reason for unit in units))
            has_range = any(unit.start_line is not None for unit in units)
            ranges = tuple(
                (unit.start_line or 1, unit.end_line or unit.start_line or 1)
                for unit in sorted(units, key=lambda item: item.start_line or 0)
                if unit.start_line is not None
            )
            summaries[path] = FileSkipSummary(
                path=path,
                reasons=reasons,
                ranges=ranges,
                whole_file=not has_range,
            )
        return summaries


@dataclass(frozen=True, slots=True)
class ReviewAssignment:
    """One program-generated reviewer task over the accepted evidence."""

    dimension: str
    focus: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewFindingCandidate:
    """A candidate finding produced by a dimension subagent.

    ``file`` is the repository-relative path on the PR's head side; ``side``
    distinguishes a head-side location from a merge-base one. ``evidence_refs``
    may only name evidence the run actually handed the reviewer — the validator
    rejects references the model invented.
    """

    severity: str
    dimension: str
    file: str
    line: int | None
    title: str
    evidence: str
    impact: str
    recommendation: str
    details: dict[str, Any] = field(default_factory=dict)
    confidence: str = "high"
    source: str = ""
    side: str = "new"
    end_line: int | None = None
    evidence_refs: tuple[str, ...] = ()


class FindingVerdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class ReviewJudgeDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass(frozen=True, slots=True)
class ReviewFindingVerdict:
    """Verdict on a candidate finding after hard validation."""

    verdict: FindingVerdict
    reason: str = ""
    missing_evidence: str = ""
    suggested_verification: str = ""


@dataclass(frozen=True, slots=True)
class ReviewJudgeVerdict:
    """AI judge verdict for a candidate after hard validation."""

    decision: ReviewJudgeDecision
    reason: str = ""
    confidence: str = "medium"
    severity: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewJudgedFinding:
    """Candidate with both hard validation and optional AI judge result."""

    candidate: ReviewFindingCandidate
    hard_verdict: ReviewFindingVerdict
    judge_verdict: ReviewJudgeVerdict | None = None

    @property
    def final_verdict(self) -> FindingVerdict:
        if self.judge_verdict is None:
            return self.hard_verdict.verdict
        if self.judge_verdict.decision == ReviewJudgeDecision.ACCEPT:
            return FindingVerdict.ACCEPTED
        if self.judge_verdict.decision == ReviewJudgeDecision.REJECT:
            return FindingVerdict.REJECTED
        return FindingVerdict.UNCERTAIN


@dataclass
class ReviewDimensionResult:
    """Aggregated result for one review dimension."""

    dimension: str
    status: str = "pending"
    candidates: list[ReviewFindingCandidate] = field(default_factory=list)
    accepted: list[ReviewFindingCandidate] = field(default_factory=list)
    rejected: list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]] = field(
        default_factory=list
    )
    uncertain: list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]] = field(
        default_factory=list
    )
    judged: list[ReviewJudgedFinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@runtime_checkable
class ReviewEvidenceProvider(Protocol):
    """Structural protocol for evidence retrieval used by prefetch and loop.

    Review evidence is the admitted change only: the provider is handed the
    frozen snapshot patches, and no member of this protocol can read a
    working tree, a branch or a default ref.
    """

    async def dispatch(
        self,
        *,
        target_type: str,
        action: str,
        review_query: str | None = None,
        max_results: int = 5,
        include_tests: bool | None = None,
        trace_id: str = "",
        frozen_diff: dict[str, Any] | None = None,
    ) -> str: ...
