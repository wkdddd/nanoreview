"""Tests for the fixed Markdown report renderer."""
from __future__ import annotations

from nanobot.review.output.report import render_review_report
from nanobot.review.types import (
    FindingVerdict,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
)


def _candidate(severity="high", file="src/main.py", line=10, title="Bug", **kw):
    defaults = {
        "dimension": "security",
        "evidence": "some code",
        "impact": "Bad things",
        "recommendation": "Fix it",
    }
    defaults.update(kw)
    return ReviewFindingCandidate(
        severity=severity, file=file, line=line, title=title, **defaults
    )


def _dim(name, accepted=None, uncertain=None, rejected=None):
    return ReviewDimensionResult(
        dimension=name,
        status="validated",
        accepted=accepted or [],
        uncertain=uncertain or [],
        rejected=rejected or [],
    )


class TestReportStructure:
    def test_no_dimensions_is_incomplete_not_clean(self):
        report = render_review_report("repo", [])

        assert "Review incomplete" in report
        assert "No actionable issues found" not in report
        assert "- [ ] review - incomplete: no review dimension results were produced" in report

    def test_empty_findings(self):
        report = render_review_report("myproject", [_dim("security")])
        assert "## Code Review Report: myproject" in report
        assert "No actionable issues found" in report

    def test_incomplete_checks_do_not_report_clean_result(self):
        dims = [
            ReviewDimensionResult(
                dimension="tests",
                status="incomplete",
                errors=["GitHub API rate limited"],
            )
        ]
        report = render_review_report("repo", dims)

        assert "Review incomplete" in report
        assert "No actionable issues found" not in report
        assert "GitHub API rate limited" in report

    def test_findings_sorted_by_severity(self):
        dims = [_dim("security", accepted=[
            _candidate(severity="medium", title="Med issue"),
            _candidate(severity="critical", title="Crit issue"),
        ])]
        report = render_review_report("repo", dims)
        crit_pos = report.index("Critical")
        med_pos = report.index("Medium")
        assert crit_pos < med_pos

    def test_checks_performed_listed(self):
        dims = [_dim("security"), _dim("performance")]
        report = render_review_report("repo", dims)
        assert "- [x] security" in report
        assert "- [x] performance" in report

    def test_finding_dimension_is_rendered_in_table(self):
        report = render_review_report("repo", [_dim("security", accepted=[_candidate()])])
        row = next(line for line in report.splitlines() if line.startswith("| 1 |"))
        assert row.startswith("| 1 | security |")

    def test_needs_confirmation_section(self):
        uncertain = [(_candidate(title="Maybe bug"), ReviewFindingVerdict(
            verdict=FindingVerdict.UNCERTAIN, reason="unclear evidence"
        ))]
        dims = [_dim("security", uncertain=uncertain)]
        report = render_review_report("repo", dims)
        assert "### Needs Confirmation" in report
        assert "Maybe bug" in report
        assert "Severity: high" in report
        assert "No actionable issues found" not in report
        assert "No priority fixes needed" not in report
        assert "Verify the items in Needs Confirmation" in report

    def test_rejected_summary_section(self):
        rejected = [(_candidate(title="False positive"), ReviewFindingVerdict(
            verdict=FindingVerdict.REJECTED, reason="file not found"
        ))]
        dims = [_dim("security", rejected=rejected)]
        report = render_review_report("repo", dims)
        assert "### Rejected/Skipped Summary" in report
        assert "False positive" in report
        assert "No actionable issues found" not in report
        assert "No priority fixes needed" not in report

    def test_recommendations_from_critical_high(self):
        dims = [_dim("security", accepted=[
            _candidate(severity="critical", recommendation="Fix auth"),
            _candidate(severity="low", title="Minor", recommendation="Whatever"),
        ])]
        report = render_review_report("repo", dims)
        assert "Fix auth" in report

    def test_severity_stats_in_summary(self):
        dims = [_dim("security", accepted=[
            _candidate(severity="critical"),
            _candidate(severity="critical", title="Another", file="b.py"),
            _candidate(severity="high", title="High one", file="c.py"),
        ])]
        report = render_review_report("repo", dims)
        assert "2 critical" in report
        assert "1 high" in report

    def test_markdown_table_cells_are_escaped_and_flattened(self):
        dims = [_dim("security", accepted=[
            _candidate(
                file="src/a|b.py",
                title="Pipe | issue\nsecond line",
                impact="Breaks | table\nand layout",
                recommendation="Use parser | not split\nthen validate",
            )
        ])]

        report = render_review_report("repo|name", dims)

        assert "## Code Review Report: repo\\|name" in report
        assert "src/a\\|b.py:10" in report
        assert "Pipe \\| issue second line" in report
        assert "Breaks \\| table and layout" in report
        assert "Use parser \\| not split then validate" in report
        finding_rows = [line for line in report.splitlines() if line.startswith("| 1 |")]
        assert len(finding_rows) == 1
        assert "| security |" in finding_rows[0]
