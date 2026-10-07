"""Deterministic review-quality evaluation (golden diff replay).

Review quality must be measurable without an LLM in the loop: a fixed golden
diff case is replayed with a deterministic script of tool calls, and the
resulting findings are scored against the case's expected findings.

This module deliberately holds only plain data and pure functions — the replay
is driven by an explicit script of tool results (no provider, no runner, no
disk) so two runs of the same case always produce the same score. It exists to
compare quality and cost across configurations (model, reasoning effort,
Planner/reviewer settings) while holding the case fixed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def normalize_review_path(path: str) -> str:
    """Normalize a finding/evidence path for comparison.

    Findings may arrive as ``./src/a.py``, ``src\\a.py`` or with redundant
    separators; all of those must compare equal.
    """
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        parts.append(part)
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class ExpectedFinding:
    """One issue the golden case expects a correct review to report."""

    file: str
    line: int | None
    title: str
    severity: str = "medium"


@dataclass(frozen=True, slots=True)
class ActualFinding:
    """One issue a replayed reviewer run actually produced."""

    file: str
    line: int | None
    title: str
    dimension: str = ""
    severity: str = "medium"


@dataclass(frozen=True, slots=True)
class MatchDetail:
    expected: ExpectedFinding
    actual: ActualFinding
    line_delta: int | None
    title_similarity: float


def _title_similarity(left: str, right: str) -> float:
    left_norm = " ".join(str(left or "").lower().split())
    right_norm = " ".join(str(right or "").lower().split())
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _finding_matches(
    expected: ExpectedFinding,
    actual: ActualFinding,
    *,
    line_tolerance: int,
    title_similarity: float,
) -> MatchDetail | None:
    if normalize_review_path(expected.file) != normalize_review_path(actual.file):
        return None
    if expected.line is not None and actual.line is not None:
        delta = abs(int(expected.line) - int(actual.line))
        if delta > line_tolerance:
            return None
    similarity = _title_similarity(expected.title, actual.title)
    if similarity < title_similarity:
        return None
    delta = (
        abs(int(expected.line) - int(actual.line))
        if expected.line is not None and actual.line is not None
        else None
    )
    return MatchDetail(
        expected=expected,
        actual=actual,
        line_delta=delta,
        title_similarity=similarity,
    )


def match_findings(
    expected: Sequence[ExpectedFinding],
    actual: Sequence[ActualFinding],
    *,
    line_tolerance: int = 3,
    title_similarity: float = 0.5,
) -> tuple[tuple[MatchDetail, ...], tuple[ExpectedFinding, ...], tuple[ActualFinding, ...]]:
    """Greedy best-match pairing of expected vs. actual findings.

    Returns ``(matched, missing, spurious)``. Each actual finding is consumed at
    most once, and for every expected finding the highest-similarity unused
    actual finding is chosen — deterministic given stable input order.
    """
    used: set[int] = set()
    matched: list[MatchDetail] = []
    missing: list[ExpectedFinding] = []

    for want in expected:
        best_index = -1
        best_detail: MatchDetail | None = None
        for index, got in enumerate(actual):
            if index in used:
                continue
            detail = _finding_matches(
                want,
                got,
                line_tolerance=line_tolerance,
                title_similarity=title_similarity,
            )
            if detail is None:
                continue
            if best_detail is None or detail.title_similarity > best_detail.title_similarity:
                best_detail = detail
                best_index = index
        if best_detail is None:
            missing.append(want)
            continue
        used.add(best_index)
        matched.append(best_detail)

    spurious = tuple(got for index, got in enumerate(actual) if index not in used)
    return tuple(matched), tuple(missing), spurious


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualityScore:
    """One golden case's quality and cost summary."""

    case_id: str
    precision: float
    recall: float
    f1: float
    matched: tuple[MatchDetail, ...]
    missing: tuple[ExpectedFinding, ...]
    spurious: tuple[ActualFinding, ...]
    incomplete_rate: float
    coverage: float
    tool_calls: int
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    undecidable: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "matched": [
                {
                    "expected": detail.expected.title,
                    "actual": detail.actual.title,
                    "file": detail.expected.file,
                    "line_delta": detail.line_delta,
                    "title_similarity": round(detail.title_similarity, 4),
                }
                for detail in self.matched
            ],
            "missing": [want.title for want in self.missing],
            "spurious": [got.title for got in self.spurious],
            "incomplete_rate": round(self.incomplete_rate, 4),
            "coverage": round(self.coverage, 4),
            "tool_calls": self.tool_calls,
            "usage": dict(self.usage),
            "elapsed_ms": round(self.elapsed_ms, 3),
            "undecidable": self.undecidable,
        }


def _ratio(numerator: int, denominator: int) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def precision_recall_f1(
    matched: int, expected_total: int, actual_total: int
) -> tuple[float, float, float]:
    precision = _ratio(matched, actual_total)
    recall = _ratio(matched, expected_total)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


def _incomplete_rate(dimension_statuses: Mapping[str, str]) -> float:
    if not dimension_statuses:
        return 0.0
    incomplete = sum(
        1 for status in dimension_statuses.values() if str(status) != "completed"
    )
    return _ratio(incomplete, len(dimension_statuses))


# ---------------------------------------------------------------------------
# Golden cases
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """A fixed diff plus the findings a correct review should report."""

    case_id: str
    diff: str
    changed_files: tuple[str, ...]
    expected: tuple[ExpectedFinding, ...]
    #: Tool-replay coverage the case considers sufficient to judge quality.
    #: A run that never read a changed file cannot be scored fairly, so it is
    #: marked undecidable instead of counting as a model miss.
    required_paths: tuple[str, ...] = ()


_GOLDEN_SQL_AND_OFF_BY_ONE_DIFF = "\n".join(
    [
        "diff --git a/app/users.py b/app/users.py",
        "--- a/app/users.py",
        "+++ b/app/users.py",
        "@@ -1,6 +1,10 @@",
        " import sqlite3",
        " ",
        " ",
        " def find_user(conn, name):",
        '-    return conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()',
        '+    query = "SELECT * FROM users WHERE name = \'" + name + "\'"',
        "+    return conn.execute(query).fetchone()",
        "+",
        "+",
        "+def page_bounds(page, size):",
        "+    start = page * size",
        "+    return start, start + size",
        "",
    ]
)

GOLDEN_CASES: dict[str, GoldenCase] = {
    "sql_and_off_by_one": GoldenCase(
        case_id="sql_and_off_by_one",
        diff=_GOLDEN_SQL_AND_OFF_BY_ONE_DIFF,
        changed_files=("app/users.py",),
        expected=(
            ExpectedFinding(
                file="app/users.py",
                line=6,
                title="SQL injection via string concatenation in find_user",
                severity="critical",
            ),
            ExpectedFinding(
                file="app/users.py",
                line=11,
                title="Off-by-one in page_bounds end offset",
                severity="medium",
            ),
        ),
        required_paths=("app/users.py",),
    ),
}


def load_golden_cases() -> dict[str, GoldenCase]:
    """Return the registered golden cases, keyed by case id."""
    return dict(GOLDEN_CASES)


# ---------------------------------------------------------------------------
# Deterministic tool replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """One recorded tool interaction.

    ``result`` is the exact text the recorded run received, so replay never
    touches the filesystem or the network.
    """

    name: str
    arguments: dict[str, object]
    result: str
    duration_ms: float = 0.0
    #: Paths this step is considered to have read (for coverage accounting).
    targets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SubmitStep:
    """The recorded terminal ``review_submit`` payload."""

    findings: tuple[ActualFinding, ...]
    duration_ms: float = 0.0
    dimension: str = ""


@dataclass
class ReplayOutcome:
    """Deterministic result of replaying one case's script."""

    findings: list[ActualFinding]
    tool_calls: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    dimension_statuses: dict[str, str] = field(default_factory=dict)
    read_paths: set[str] = field(default_factory=set)

    @property
    def coverage(self) -> set[str]:
        return set(self.read_paths)


def replay_case(
    case: GoldenCase,
    script: Sequence[ReplayStep | SubmitStep],
    *,
    usage: Mapping[str, int] | None = None,
    dimension_statuses: Mapping[str, str] | None = None,
) -> ReplayOutcome:
    """Replay a scripted run and collect its findings, cost and coverage."""
    outcome = ReplayOutcome(findings=[], usage=dict(usage or {}))
    for step in script:
        if isinstance(step, SubmitStep):
            outcome.findings.extend(step.findings)
            outcome.elapsed_ms += step.duration_ms
            if step.dimension:
                outcome.dimension_statuses.setdefault(step.dimension, "completed")
            continue
        outcome.tool_calls += 1
        outcome.elapsed_ms += step.duration_ms
        for target in step.targets:
            outcome.read_paths.add(normalize_review_path(target))
    if dimension_statuses:
        outcome.dimension_statuses.update(dict(dimension_statuses))
    return outcome


def evaluate_case(
    case: GoldenCase,
    outcome: ReplayOutcome,
    *,
    line_tolerance: int = 3,
    title_similarity: float = 0.5,
) -> QualityScore:
    """Score a replayed outcome against the golden case's expectations.

    A run that never read the case's ``required_paths`` is marked
    ``undecidable``: the missing tool coverage — not the model — caused the
    result, so it must not be counted as a model miss.
    """
    matched, missing, spurious = match_findings(
        case.expected,
        outcome.findings,
        line_tolerance=line_tolerance,
        title_similarity=title_similarity,
    )
    precision, recall, f1 = precision_recall_f1(
        len(matched), len(case.expected), len(outcome.findings)
    )
    required = {normalize_review_path(path) for path in case.required_paths}
    covered = required & outcome.read_paths
    coverage = _ratio(len(covered), len(required)) if required else 1.0
    return QualityScore(
        case_id=case.case_id,
        precision=precision,
        recall=recall,
        f1=f1,
        matched=matched,
        missing=missing,
        spurious=spurious,
        incomplete_rate=_incomplete_rate(outcome.dimension_statuses),
        coverage=coverage,
        tool_calls=outcome.tool_calls,
        usage=dict(outcome.usage),
        elapsed_ms=outcome.elapsed_ms,
        undecidable=bool(required) and not required.issubset(outcome.read_paths),
    )


def format_quality_report(score: QualityScore) -> str:
    """Render a human-readable, copy-pasteable quality report."""
    lines = [
        f"case: {score.case_id}",
        f"precision={score.precision:.3f} recall={score.recall:.3f} f1={score.f1:.3f}",
        f"matched={len(score.matched)} missing={len(score.missing)} spurious={len(score.spurious)}",
        f"coverage={score.coverage:.3f} incomplete_rate={score.incomplete_rate:.3f}",
        f"tool_calls={score.tool_calls} elapsed_ms={score.elapsed_ms:.1f}",
        f"usage={dict(sorted(score.usage.items()))}",
    ]
    if score.undecidable:
        lines.append("verdict: UNDECIDABLE (tool replay coverage insufficient)")
    for detail in score.matched:
        lines.append(
            "  match: {title!r} (line_delta={delta}, similarity={sim:.2f})".format(
                title=detail.expected.title,
                delta=detail.line_delta,
                sim=detail.title_similarity,
            )
        )
    for want in score.missing:
        lines.append(f"  missed: {want.title!r} at {want.file}:{want.line}")
    for got in score.spurious:
        lines.append(f"  spurious: {got.title!r} at {got.file}:{got.line}")
    return "\n".join(lines)
