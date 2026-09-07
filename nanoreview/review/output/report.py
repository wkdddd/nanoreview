"""Fixed Markdown report renderer for code review results."""
from __future__ import annotations

from nanoreview.review.output.judge import ReviewJudgeStats
from nanoreview.review.profiles import get_reviewer_profile
from nanoreview.review.types import (
    SEVERITY_ORDER,
    FileSkipSummary,
    FindingVerdict,
    ReviewBudgetSkip,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
)


def _clean_text(value: object) -> str:
    """Normalize model-provided text before embedding it in Markdown."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return " ".join(line for line in lines if line).strip()


def _escape_markdown_inline(value: object) -> str:
    text = _clean_text(value)
    for old, new in (
        ("\\", "\\\\"),
        ("`", "\\`"),
        ("*", "\\*"),
        ("_", "\\_"),
        ("[", "\\["),
        ("]", "\\]"),
        ("|", "\\|"),
    ):
        text = text.replace(old, new)
    return text


def _table_cell(value: object) -> str:
    text = _clean_text(value)
    return text.replace("\\", "\\\\").replace("|", "\\|") or "-"


def _location(file: object, line: object | None = None) -> str:
    path = _clean_text(file) or "unknown"
    if line:
        return f"{path}:{line}"
    return path


def render_review_report(
    target_name: str,
    dimensions: list[ReviewDimensionResult],
    *,
    routing_mode: str = "explicit",
    selected_dimensions: tuple[str, ...] | list[str] = (),
    budget_skipped: tuple[ReviewBudgetSkip, ...] | list[ReviewBudgetSkip] = (),
    skipped_files: tuple[FileSkipSummary, ...] | list[FileSkipSummary] = (),
    judge_stats: ReviewJudgeStats | None = None,
) -> str:
    """Render final Markdown report from validated dimension results."""
    all_accepted = _collect_accepted(dimensions)
    all_uncertain = _collect_uncertain(dimensions)
    all_rejected = _collect_rejected(dimensions)

    stats = _severity_stats(all_accepted)
    incomplete = bool(budget_skipped) or bool(skipped_files) or _has_incomplete_checks(dimensions)
    summary = _build_summary(
        stats,
        dimensions,
        uncertain_count=len(all_uncertain),
        rejected_count=len(all_rejected),
        incomplete=incomplete,
    )

    sections: list[str] = []
    sections.append(f"## Code Review Report: {_escape_markdown_inline(target_name)}\n")
    sections.append(f"### Executive Summary\n\n{summary}\n")
    sections.append(_render_selected_reviewers(routing_mode, selected_dimensions))
    if budget_skipped:
        sections.append(_render_budget_skipped(budget_skipped))
    if skipped_files:
        sections.append(_render_skipped_files(skipped_files))
    sections.append(_render_findings(
        all_accepted,
        uncertain_count=len(all_uncertain),
        rejected_count=len(all_rejected),
        incomplete=incomplete,
    ))
    sections.append(_render_checks_performed(dimensions))
    if judge_stats is not None:
        sections.append(_render_judge_stats(judge_stats))
    if all_uncertain:
        sections.append(_render_needs_confirmation(all_uncertain))
    if all_rejected:
        sections.append(_render_rejected_summary(all_rejected))
    sections.append(_render_recommendations(
        all_accepted,
        uncertain_count=len(all_uncertain),
        rejected_count=len(all_rejected),
        incomplete=incomplete,
    ))
    return "\n".join(sections)


def _collect_accepted(dims: list[ReviewDimensionResult]) -> list[ReviewFindingCandidate]:
    findings: list[ReviewFindingCandidate] = []
    for d in dims:
        if d.judged:
            findings.extend(item.candidate for item in d.judged if item.final_verdict == FindingVerdict.ACCEPTED)
        else:
            findings.extend(d.accepted)
    findings.sort(key=lambda f: SEVERITY_ORDER.index(f.severity) if f.severity in SEVERITY_ORDER else 99)
    return findings


def _collect_uncertain(
    dims: list[ReviewDimensionResult],
) -> list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]]:
    items: list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]] = []
    for d in dims:
        if d.judged:
            for item in d.judged:
                if item.final_verdict == FindingVerdict.UNCERTAIN:
                    reason = item.hard_verdict
                    if item.judge_verdict is not None:
                        # Surface why the judge (or judge outage) requires
                        # manual confirmation, not the raw hard verdict.
                        reason = ReviewFindingVerdict(
                            verdict=FindingVerdict.UNCERTAIN,
                            reason=item.judge_verdict.reason,
                        )
                    items.append((item.candidate, reason))
        else:
            items.extend(d.uncertain)
    return items


def _collect_rejected(
    dims: list[ReviewDimensionResult],
) -> list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]]:
    items: list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]] = []
    for d in dims:
        if d.judged:
            for item in d.judged:
                if item.final_verdict == FindingVerdict.REJECTED:
                    reason = item.hard_verdict
                    if item.judge_verdict is not None:
                        reason = ReviewFindingVerdict(
                            verdict=FindingVerdict.REJECTED,
                            reason=f"AI judge rejected: {item.judge_verdict.reason}",
                        )
                    items.append((item.candidate, reason))
        items.extend(d.rejected)
    return items


def _severity_stats(findings: list[ReviewFindingCandidate]) -> dict[str, int]:
    stats = {s: 0 for s in SEVERITY_ORDER}
    for f in findings:
        if f.severity in stats:
            stats[f.severity] += 1
    return stats


def _build_summary(
    stats: dict[str, int],
    dims: list[ReviewDimensionResult],
    *,
    uncertain_count: int = 0,
    rejected_count: int = 0,
    incomplete: bool = False,
) -> str:
    total = sum(stats.values())
    if total == 0:
        if incomplete:
            return "Review incomplete. Some checks could not access enough evidence to produce a reliable result."
        if uncertain_count:
            return (
                f"No confirmed actionable issues found, but {uncertain_count} candidate"
                f"{'s' if uncertain_count != 1 else ''} need confirmation before this review can be considered clean."
            )
        if rejected_count:
            return (
                f"No confirmed actionable issues found. {rejected_count} candidate"
                f"{'s were' if rejected_count != 1 else ' was'} rejected or skipped during validation."
            )
        return "No actionable issues found."
    parts: list[str] = []
    for sev in SEVERITY_ORDER:
        if stats[sev] > 0:
            parts.append(f"{stats[sev]} {sev}")
    dim_names = ", ".join(d.dimension for d in dims if d.status == "validated")
    return f"Found {total} issues ({', '.join(parts)}). Dimensions reviewed: {dim_names}."


def _has_incomplete_checks(dims: list[ReviewDimensionResult]) -> bool:
    return not dims or any(d.status in {"incomplete", "error"} or d.errors for d in dims)


def _render_findings(
    findings: list[ReviewFindingCandidate],
    *,
    uncertain_count: int = 0,
    rejected_count: int = 0,
    incomplete: bool = False,
) -> str:
    if not findings:
        if incomplete:
            return "### Findings\n\nReview incomplete; no reliable finding set was produced.\n"
        if uncertain_count:
            return "### Findings\n\nNo confirmed findings. See Needs Confirmation for candidates requiring verification.\n"
        if rejected_count:
            return "### Findings\n\nNo confirmed findings. Candidate findings were rejected or skipped during validation.\n"
        return "### Findings\n\nNo actionable issues found.\n"
    lines = ["### Findings\n"]
    current_sev = ""
    idx = 0
    for f in findings:
        if f.severity != current_sev:
            current_sev = f.severity
            lines.append(f"#### {current_sev.capitalize()}\n")
            lines.append("| # | Dimension | File | Issue | Impact |")
            lines.append("|---|-----------|------|-------|--------|")
        idx += 1
        loc = _location(f.file, f.line)
        lines.append(
            f"| {idx} | {_table_cell(f.dimension)} | {_table_cell(loc)} | "
            f"{_table_cell(f.title)} | {_table_cell(f.impact)} |"
        )
    lines.append("")
    lines.append("**Details:**\n")
    for i, f in enumerate(findings, 1):
        loc = _location(f.file, f.line)
        lines.append(f"{i}. **{_escape_markdown_inline(f.title)}** (`{_clean_text(loc)}`)")
        lines.append(f"   - Impact: {_escape_markdown_inline(f.impact)}")
        lines.append(f"   - Recommendation: {_escape_markdown_inline(f.recommendation)}")
        profile = get_reviewer_profile(f.dimension)
        if profile is not None:
            for key, label in profile.report_fields:
                value = f.details.get(key)
                if isinstance(value, list):
                    value = ", ".join(str(item) for item in value)
                lines.append(
                    f"   - {_escape_markdown_inline(label)}: {_escape_markdown_inline(value)}"
                )
    lines.append("")
    return "\n".join(lines)


def _render_selected_reviewers(
    routing_mode: str,
    selected_dimensions: tuple[str, ...] | list[str],
) -> str:
    lines = ["### Selected Reviewers\n"]
    lines.append(f"- Routing: {_escape_markdown_inline(routing_mode)}")
    for dimension in selected_dimensions:
        profile = get_reviewer_profile(dimension)
        label = profile.label if profile is not None else dimension
        lines.append(f"- {_escape_markdown_inline(label)}")
    if not selected_dimensions:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def _render_budget_skipped(skipped: tuple[ReviewBudgetSkip, ...] | list[ReviewBudgetSkip]) -> str:
    lines = ["### Skipped Reviewers\n"]
    lines.append("The following selected reviewers were not started because the subagent budget was insufficient:\n")
    for item in skipped:
        profile = get_reviewer_profile(item.dimension)
        label = profile.label if profile is not None else item.dimension
        lines.append(
            f"- {_escape_markdown_inline(label)} - budget-skipped "
            f"(estimated quota: {item.quota_tokens} tokens; input: {item.input_tokens} tokens)"
        )
    lines.append("")
    return "\n".join(lines)


def _render_skipped_files(skipped: tuple[FileSkipSummary, ...] | list[FileSkipSummary]) -> str:
    lines = ["### Scope Not Reviewed\n"]
    lines.append(
        "The following files or line ranges were excluded from the review scope by preprocessing "
        "(oversized units, unsupported code types, missing grammars, parse failures, or budget limits):\n"
    )
    for summary in skipped:
        # describe() renders the path plus unreviewed line ranges and reasons.
        lines.append(f"- {_escape_markdown_inline(summary.describe())}")
    lines.append("")
    return "\n".join(lines)


def _render_checks_performed(dims: list[ReviewDimensionResult]) -> str:
    lines = ["### Checks Performed\n"]
    if not dims:
        lines.append("- [ ] review - incomplete: no review dimension results were produced")
        lines.append("")
        return "\n".join(lines)
    for d in dims:
        if d.status == "validated":
            lines.append(f"- [x] {_escape_markdown_inline(d.dimension)}")
        else:
            reason = "; ".join(_clean_text(error) for error in d.errors if _clean_text(error))
            suffix = f": {reason}" if reason else ""
            lines.append(
                f"- [ ] {_escape_markdown_inline(d.dimension)} - "
                f"{_escape_markdown_inline(d.status)}{_escape_markdown_inline(suffix)}"
            )
    lines.append("")
    return "\n".join(lines)


def _render_judge_stats(stats: ReviewJudgeStats) -> str:
    """Render explicit AI judge statistics.

    ``sent``/``verdicts`` come from the judge run itself (see
    ReviewJudgeStats) — never from ``len(dimension.judged)``, which would
    misreport unjudged candidates as actually sent to the judge.
    """
    lines = ["### AI Judge Statistics\n"]
    lines.append(f"- Candidates: {stats.total_candidates}")
    lines.append(f"- Sent to judge: {stats.sent_candidates}")
    lines.append(f"- Verdicts returned: {stats.returned_verdicts}")
    lines.append(f"- Needs confirmation: {stats.needs_confirmation}")
    lines.append(f"- Batches: {stats.batches}")
    lines.append("")
    return "\n".join(lines)


def _render_needs_confirmation(
    items: list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]],
) -> str:
    lines = ["### Needs Confirmation\n"]
    lines.append("The following items could not be definitively verified:\n")
    for c, v in items:
        loc = _location(c.file, c.line)
        reason = v.reason
        lines.append(
            f"- **{_escape_markdown_inline(c.title)}** (`{_clean_text(loc)}`) - "
            f"Severity: {_escape_markdown_inline(c.severity)} - {_escape_markdown_inline(reason)}"
        )
    lines.append("")
    return "\n".join(lines)


def _render_rejected_summary(
    items: list[tuple[ReviewFindingCandidate, ReviewFindingVerdict]],
) -> str:
    lines = ["### Rejected/Skipped Summary\n"]
    lines.append(f"{len(items)} candidates rejected during validation:\n")
    for c, v in items[:10]:
        lines.append(
            f"- {_escape_markdown_inline(c.title)} (`{_clean_text(c.file)}`) - "
            f"{_escape_markdown_inline(v.reason)}"
        )
    if len(items) > 10:
        lines.append(f"- ... and {len(items) - 10} more")
    lines.append("")
    return "\n".join(lines)


def _render_recommendations(
    findings: list[ReviewFindingCandidate],
    *,
    uncertain_count: int = 0,
    rejected_count: int = 0,
    incomplete: bool = False,
) -> str:
    lines = ["### Recommendations\n"]
    if not findings:
        if incomplete:
            lines.append("1. Re-run the review after the missing evidence or failed checks are resolved.\n")
            return "\n".join(lines)
        if uncertain_count:
            lines.append("1. Verify the items in Needs Confirmation before treating this review as clean.\n")
            return "\n".join(lines)
        if rejected_count:
            lines.append("1. Review the rejected/skipped summary if you expected these candidates to be actionable.\n")
            return "\n".join(lines)
        lines.append("No priority fixes needed.\n")
        return "\n".join(lines)
    critical_high = [f for f in findings if f.severity in ("critical", "high")]
    for i, f in enumerate(critical_high[:5], 1):
        lines.append(f"{i}. {_escape_markdown_inline(f.recommendation)}")
    if not critical_high:
        lines.append("1. Address medium-severity findings when convenient.")
    lines.append("")
    return "\n".join(lines)
