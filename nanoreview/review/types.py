"""Shared code-review data types."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from nanoreview.review.profiles import REVIEWER_PROFILES

SEVERITY_ORDER = ("critical", "high", "medium", "low")


class ReviewMetaKey:
    """Session metadata keys for code review state."""

    MODE = "review_mode"
    TARGET = "review_target"
    TARGET_TYPE = "review_target_type"
    MODE_VARIANT = "review_mode_variant"
    ACTION = "review_action"
    REQUESTED_DIMENSIONS = "review_focus"
    TARGET_REF = "review_target_ref"
    LOCAL_ROOT = "review_local_root"
    LOCAL_TARGET = "review_local_target"
    LOCAL_SCOPE_KIND = "review_local_scope_kind"
    MAX_CONCURRENT_SUBAGENTS = "max_concurrent_subagents"
    ALLOWED_DIMENSIONS = "allowed_review_dimensions"
    EVIDENCE_PROVIDER = "_review_evidence_service"
    GITHUB_PREFETCH_READY = "_review_github_prefetch_ready"
    DIFF_CONTEXT_WINDOW_TOKENS = "_review_diff_context_window_tokens"
    GITHUB_PR_HEAD_REF = "_review_github_pr_head_ref"
    EVIDENCE_BUNDLE = "_review_evidence_bundle"

ReviewTargetType = Literal["auto", "github", "local"]
ReviewDepth = Literal["quick", "full", "deep"]
ReviewScopeKind = Literal["file", "directory", "repo"]
ReviewRoutingMode = Literal["auto", "explicit"]


class ReviewAction(StrEnum):
    REPO = "repo"
    DIFF = "diff"


@dataclass(slots=True)
class GitHubDiffEvidence:
    """Non-RAG evidence collected from one GitHub pull request."""

    snapshot: str
    head_sha: str
    patches: dict[str, str] = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    touched_lines: dict[str, list[int]] = field(default_factory=dict)
    patch_unavailable_files: dict[str, str] = field(default_factory=dict)


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
    mode: str
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
        description=profile.planner_description,
        evidence_required=True,
    )
    for key, profile in REVIEWER_PROFILES.items()
}
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
class LocalReviewScope:
    """Resolved local review boundary derived from the requested target."""

    kind: ReviewScopeKind
    review_root: str
    scope_paths: list[str] = field(default_factory=list)
    target_path: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    target: str | None
    target_name: str | None
    target_type: ReviewTargetType
    action: ReviewAction
    depth: ReviewDepth
    roles: list[ReviewRole]
    routing_mode: ReviewRoutingMode
    user_requirements: str = ""
    target_repo: str | None = None
    pr_number: int | None = None
    target_ref: str | None = None
    target_subpath: str | None = None
    target_subpath_kind: str | None = None
    local_scope: LocalReviewScope | None = None
    prefetch_summary: str | None = None


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


@dataclass(frozen=True, slots=True)
class ReviewEvidenceBundle:
    """Evidence exposed to the coordinator and available for subagent routing."""

    references: tuple[EvidenceReference, ...] = ()
    summary: str = ""
    status: str = "ok"
    reason: str = ""

    def by_id(self) -> dict[str, EvidenceReference]:
        return {reference.id: reference for reference in self.references}


@dataclass(frozen=True, slots=True)
class ReviewAssignment:
    """Validated coordinator guidance for one program-required dimension."""

    dimension: str
    focus: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewFindingCandidate:
    """A candidate finding produced by a dimension subagent."""

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


@dataclass(frozen=True, slots=True)
class ReviewModePolicy:
    """Programmatic behavior policy for a review depth."""

    depth: ReviewDepth
    severities: tuple[str, ...]
    judge_enabled: bool
    evidence_max_results: int
    include_optional_roles: bool = False
    report_style: str = "full"


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
    filtered_count: int = 0
    filtered_severities: tuple[str, ...] = ()


@runtime_checkable
class ReviewEvidenceProvider(Protocol):
    """Structural protocol for evidence retrieval used by prefetch and loop."""

    async def local_context(
        self,
        *,
        review_query: str | None,
        max_results: int,
        include_tests: bool | None,
        local_scope: LocalReviewScope | None = None,
    ) -> str: ...

    async def local_changed_context(
        self,
        *,
        review_query: str | None,
        max_results: int,
        include_tests: bool | None,
        local_scope: LocalReviewScope | None = None,
        context_window_tokens: int | None = None,
    ) -> str: ...

    async def github_context(
        self,
        *,
        repo: str,
        ref: str | None,
        tree_pattern: str | None,
        review_query: str | None,
        max_results: int,
        include_tests: bool | None,
        trace_id: str,
    ) -> str: ...

    async def github_diff_context(
        self,
        *,
        repo: str,
        pr_number: int,
        review_query: str | None,
        max_results: int,
        include_tests: bool | None,
        trace_id: str,
        context_window_tokens: int | None = None,
    ) -> str: ...

    async def dispatch(
        self,
        *,
        target_type: str,
        action: str,
        repo: str = "",
        ref: str | None = None,
        pr_number: int = 0,
        tree_pattern: str | None = None,
        target_subpath: str | None = None,
        target_subpath_kind: str | None = None,
        review_query: str | None = None,
        max_results: int = 5,
        include_tests: bool | None = None,
        local_scope: LocalReviewScope | None = None,
        trace_id: str = "",
        context_window_tokens: int | None = None,
    ) -> str: ...
