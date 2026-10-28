"""Tests for the stateless ``ReviewFinalizer``.

The finalizer owns no candidate accumulation: the run's validated dimension
results live in the coordinator-owned blackboard. These tests therefore feed a
candidate set in and check what comes out — validation of one reviewer's
``findings`` array, judge application on a *given* set, and report rendering.

Two invariants are pinned throughout: a candidate is never presented as having
passed a judge that did not return a verdict for it, and a run that produced
nothing is ``incomplete`` rather than ``clean``.
"""

from __future__ import annotations

import pytest
from loguru import logger

from nanoreview.review.output.finalizer import (
    ReviewFinalizer,
    reviewer_failure_reason,
)
from nanoreview.review.output.judge import JudgeExecutionResult, ReviewJudgeStats
from nanoreview.review.types import (
    ReviewDimensionResult,
    ReviewJudgeDecision,
    ReviewJudgeVerdict,
    normalize_review_dimension,
)


class FakeJudge:
    """Judge stub returning a ``JudgeExecutionResult`` for the given verdicts."""

    def __init__(self, verdicts, *, stats=None, usage=None, error=None):
        self.verdicts = verdicts
        self.stats = stats
        self.usage = dict(usage or {})
        self.error = error
        self.calls = 0

    async def judge_dimensions(self, dimensions):
        self.calls += 1
        return JudgeExecutionResult(
            verdicts=self.verdicts,
            stats=self.stats,
            usage=dict(self.usage),
            error=self.error,
        )


class ExplodingJudge:
    """Judge stub whose pass raises outright (a provider outage)."""

    def __init__(self, message: str = "provider down") -> None:
        self.message = message

    async def judge_dimensions(self, dimensions):
        raise RuntimeError(self.message)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return str(tmp_path)


_SECURITY_DETAILS = {
    "trust_boundary": "Public HTTP request reaches the query layer",
    "attack_preconditions": "Attacker controls the request parameter",
    "attack_path": "request -> handler -> query builder -> database",
}

_PERFORMANCE_DETAILS = {
    "hot_path": "index.html render path",
    "scale_condition": "Every page load",
    "resource_impact": "Blocking network fetch delays first paint",
}

_MAINTAINABILITY_DETAILS = {
    "violated_boundary": "webui imports gateway internals directly",
    "change_amplification": "schema changes require touching every caller",
    "affected_modules": ["src/app.py"],
}

_BUG_DETAILS = {
    "trigger": "Calling the endpoint with an empty payload",
    "expected_behavior": "The handler rejects empty payloads",
    "actual_behavior": "The handler crashes with an unhandled error",
}


def _findings(
    findings: list[dict[str, object]],
    details: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Build a validated-shape findings array (what the reviewer contract emits)."""
    prepared: list[dict[str, object]] = []
    for finding in findings:
        item = dict(finding)
        item.setdefault(
            "details", dict(details if details is not None else _SECURITY_DETAILS)
        )
        prepared.append(item)
    return prepared


class TestDimensionNormalization:
    def test_normalizes_review_label_suffix(self):
        assert normalize_review_dimension("Security Review") == "security"
        assert normalize_review_dimension("Maintainability Review") == "maintainability"


class TestValidateFindings:
    def test_valid_findings_become_accepted_candidates(self, workspace):
        f = ReviewFinalizer(workspace)

        result = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Injection",
                        "evidence": "line1",
                        "impact": "RCE",
                        "recommendation": "Parameterize",
                    }
                ]
            ),
        )

        assert result.status == "validated"
        assert [candidate.title for candidate in result.accepted] == ["Injection"]

    def test_an_empty_findings_array_is_no_findings(self, workspace):
        f = ReviewFinalizer(workspace)

        assert f.validate_findings("security", []).status == "no_findings"

    def test_normalizes_the_dimension_label(self, workspace):
        f = ReviewFinalizer(workspace, allowed_dimensions={"maintainability"})

        result = f.validate_findings(
            "Maintainability Reviewer",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Boundary violation",
                        "evidence": "line1",
                        "impact": "Change amplification",
                        "recommendation": "Introduce a stable interface",
                    }
                ],
                details=_MAINTAINABILITY_DETAILS,
            ),
        )

        assert result.status == "validated"
        assert result.dimension == "maintainability"

    def test_accepts_spawn_labels_with_task_suffix(self, workspace):
        f = ReviewFinalizer(workspace, allowed_dimensions={"performance"})

        validated = f.validate_findings(
            "Performance Reviewer for review-webui/index.html",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Font loading blocks render",
                        "evidence": "line1",
                        "impact": "Slower first paint",
                        "recommendation": "Use font-display swap",
                    }
                ],
                details=_PERFORMANCE_DETAILS,
            ),
        )
        result = f.finalize((validated,), target_name="review-webui/index.html")

        assert validated.status != "skipped_disallowed"
        assert validated.dimension == "performance"
        assert "Font loading blocks render" in result.report_markdown
        assert not result.errors

    def test_disallowed_dimension_is_skipped_not_reported(self, workspace):
        f = ReviewFinalizer(workspace, allowed_dimensions={"maintainability"})

        skipped = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "critical",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Disallowed finding",
                        "evidence": "line1",
                        "impact": "bad",
                        "recommendation": "fix",
                    }
                ]
            ),
        )
        result = f.finalize((skipped,), target_name="myproject")

        assert skipped.status == "skipped_disallowed"
        assert "Disallowed finding" not in result.report_markdown
        assert "Review incomplete" in result.report_markdown
        assert "No actionable issues found" not in result.report_markdown

    def test_medium_and_low_candidates_are_kept(self, workspace):
        f = ReviewFinalizer(workspace)

        result = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "medium",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Medium issue",
                        "evidence": "line1",
                        "impact": "bad",
                        "recommendation": "fix",
                    },
                    {
                        "severity": "low",
                        "file": "src/app.py",
                        "line": 2,
                        "title": "Low issue",
                        "evidence": "line2",
                        "impact": "bad",
                        "recommendation": "fix",
                    },
                ]
            ),
        )

        assert [candidate.title for candidate in result.accepted] == [
            "Medium issue",
            "Low issue",
        ]


class TestDimensionFailure:
    def test_a_failed_reviewer_is_incomplete_with_a_bounded_reason(self, workspace):
        f = ReviewFinalizer(workspace)

        failed = f.dimension_failure("security", "provider timeout")
        result = f.finalize((failed,), target_name="repo")

        assert failed.status == "incomplete"
        assert failed.errors == ["provider timeout"]
        assert "Review incomplete" in result.report_markdown
        assert "No actionable issues found" not in result.report_markdown

    def test_an_empty_reason_still_explains_the_gap(self, workspace):
        failed = ReviewFinalizer(workspace).dimension_failure("security", "   ")

        assert failed.errors == ["reviewer did not complete"]

    def test_failure_reason_is_bounded_and_labels_the_status(self):
        reason = reviewer_failure_reason("error", "x" * 2_000)

        assert reason.startswith("reviewer status=error: ")
        assert len(reason) <= 500
        assert "\n" not in reason

    def test_failure_reason_without_detail_states_no_output(self):
        assert reviewer_failure_reason("error", "") == (
            "reviewer status=error: no result output"
        )


class TestFinalize:
    def test_finalize_without_dimensions_is_incomplete_not_clean(self, workspace):
        result = ReviewFinalizer(workspace).finalize((), target_name="repo")

        assert result.errors == ["No review dimension results were produced."]
        assert "Review incomplete" in result.report_markdown
        assert "No actionable issues found" not in result.report_markdown

    def test_finalize_produces_report(self, workspace):
        f = ReviewFinalizer(workspace)
        validated = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "critical",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "RCE",
                        "evidence": "line1",
                        "impact": "Full compromise",
                        "recommendation": "Remove exec",
                    }
                ]
            ),
        )

        result = f.finalize((validated,), target_name="myproject")

        assert "## Code Review Report: myproject" in result.report_markdown
        assert "RCE" in result.report_markdown
        assert result.errors == []

    def test_report_is_rendered_from_every_given_dimension(self, workspace):
        """The finalizer renders exactly the set it is handed (no accumulation)."""
        f = ReviewFinalizer(workspace)
        first = f.validate_findings("security", _findings([
            {
                "severity": "high",
                "file": "src/app.py",
                "line": 1,
                "title": "First finding",
                "evidence": "line1",
                "impact": "bad",
                "recommendation": "fix",
            }
        ]))
        second = f.validate_findings("bug", _findings([
            {
                "severity": "high",
                "file": "src/app.py",
                "line": 2,
                "title": "Second finding",
                "evidence": "line2",
                "impact": "bad",
                "recommendation": "fix",
            }
        ], details=_BUG_DETAILS))

        result = f.finalize((first, second), target_name="myproject")

        assert "First finding" in result.report_markdown
        assert "Second finding" in result.report_markdown
        # Re-finalizing the same set is idempotent (no accumulation inside).
        again = f.finalize((first, second), target_name="myproject")
        assert again.report_markdown == result.report_markdown

    def test_a_rendering_failure_is_reported_as_an_error(self, workspace, monkeypatch):
        import nanoreview.review.output.report as report_module

        def _boom(*args, **kwargs):
            raise RuntimeError("render exploded")

        monkeypatch.setattr(report_module, "render_review_report", _boom)
        result = ReviewFinalizer(workspace).finalize((), target_name="repo")

        assert any("render exploded" in error for error in result.errors)
        assert "Report rendering failed" in result.report_markdown


class TestNeedsConfirmation:
    def test_no_confirmation_needed_when_every_candidate_is_clear(self, workspace):
        f = ReviewFinalizer(workspace)
        validated = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Clear bug",
                        "evidence": "line1",
                        "impact": "bad",
                        "recommendation": "fix",
                    }
                ]
            ),
        )

        result = f.finalize((validated,), target_name="test")

        assert result.needs_confirmation == []

    def test_uncertain_items_remain_in_needs_confirmation(self, workspace):
        f = ReviewFinalizer(workspace)
        validated = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Maybe",
                        "evidence": "",
                        "impact": "unknown",
                        "recommendation": "check",
                    }
                ]
            ),
        )

        result = f.finalize((validated,), target_name="test")

        assert len(result.needs_confirmation) == 1
        assert "Maybe" in result.report_markdown
        assert "### Needs Confirmation" in result.report_markdown


class TestJudge:
    @pytest.mark.asyncio
    async def test_ai_judge_can_reject_a_hard_accepted_candidate(self, workspace):
        f = ReviewFinalizer(workspace)
        validated = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "False positive",
                        "evidence": "line1",
                        "impact": "bad",
                        "recommendation": "fix",
                    }
                ]
            ),
        )
        judge = FakeJudge(
            {
                "security:src/app.py:1:false positive": ReviewJudgeVerdict(
                    decision=ReviewJudgeDecision.REJECT,
                    reason="not actionable",
                )
            }
        )

        _, judged = await f.judge(judge, (validated,))
        result = f.finalize(judged, target_name="test")

        assert "No actionable issues found" not in result.report_markdown
        assert "AI judge rejected: not actionable" in result.report_markdown

    @pytest.mark.asyncio
    async def test_a_missing_judge_marks_candidates_needs_confirmation(
        self, workspace
    ):
        """No judge instance: candidates must not silently pass as accepted."""
        f = ReviewFinalizer(workspace)
        validated = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Unjudged issue",
                        "evidence": "line1",
                        "impact": "bad",
                        "recommendation": "fix",
                    }
                ]
            ),
        )

        execution, judged = await f.judge(None, (validated,))
        result = f.finalize(judged, target_name="test", judge_stats=execution.stats)

        assert execution.error is None
        assert "Unjudged issue" in result.report_markdown
        assert "### Needs Confirmation" in result.report_markdown
        assert "AI judge is unavailable" in result.report_markdown
        assert "### AI Judge Statistics" in result.report_markdown
        assert "- Sent to judge: 0" in result.report_markdown
        # result.needs_confirmation must stay consistent with the report.
        assert len(result.needs_confirmation) == 1
        assert result.needs_confirmation[0][0].title == "Unjudged issue"
        assert "AI judge is unavailable" in result.needs_confirmation[0][1].reason

    @pytest.mark.asyncio
    async def test_an_exploding_judge_marks_candidates_needs_confirmation(
        self, workspace
    ):
        """A judge that raises outright must surface candidates, not accept them."""
        f = ReviewFinalizer(workspace)
        validated = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Failed judge issue",
                        "evidence": "line1",
                        "impact": "bad",
                        "recommendation": "fix",
                    }
                ]
            ),
        )

        execution, judged = await f.judge(ExplodingJudge(), (validated,))
        result = f.finalize(judged, target_name="test", judge_stats=execution.stats)

        assert execution.error is not None
        assert "Failed judge issue" in result.report_markdown
        assert "### Needs Confirmation" in result.report_markdown
        assert "AI judge failed" in result.report_markdown
        # result.needs_confirmation must stay consistent with the report.
        assert len(result.needs_confirmation) == 1
        assert result.needs_confirmation[0][0].title == "Failed judge issue"
        assert "AI judge failed" in result.needs_confirmation[0][1].reason

    @pytest.mark.asyncio
    async def test_judge_returns_stats_usage_and_bounded_error(self, workspace):
        """The finalizer passes the judge's immutable result through untouched."""
        f = ReviewFinalizer(workspace)
        validated = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Partial judge issue",
                        "evidence": "line1",
                        "impact": "bad",
                        "recommendation": "fix",
                    }
                ]
            ),
        )
        stats = ReviewJudgeStats(
            total_candidates=1,
            sent_candidates=1,
            returned_verdicts=0,
            needs_confirmation=1,
            batches=1,
        )
        judge = FakeJudge(
            {
                "security:src/app.py:1:partial judge issue": ReviewJudgeVerdict(
                    decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
                    reason="AI judge batch failed; manual verification required",
                )
            },
            stats=stats,
            usage={"total_tokens": 42},
            error="AI judge batch failed (invalid_output): boom",
        )

        returned, judged = await f.judge(judge, (validated,))
        result = f.finalize(judged, target_name="test", judge_stats=returned.stats)

        assert returned.stats is stats
        assert returned.usage == {"total_tokens": 42}
        assert returned.error == "AI judge batch failed (invalid_output): boom"
        # A judge error never silently accepts: the candidate is surfaced.
        assert len(result.needs_confirmation) == 1
        assert result.needs_confirmation[0][0].title == "Partial judge issue"
        assert "batch failed" in result.needs_confirmation[0][1].reason
        assert "### AI Judge Statistics" in result.report_markdown
        assert "- Sent to judge: 1" in result.report_markdown

    @pytest.mark.asyncio
    async def test_an_escalated_failure_reports_a_bounded_error(self, workspace):
        """An escaping judge failure returns a bounded error for the supervisor."""
        f = ReviewFinalizer(workspace)
        validated = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Escaped judge issue",
                        "evidence": "line1",
                        "impact": "bad",
                        "recommendation": "fix",
                    }
                ]
            ),
        )

        returned, _ = await f.judge(
            ExplodingJudge("provider down\nwith a long trace"), (validated,)
        )

        assert returned.error is not None
        assert "provider down" in returned.error
        assert "\n" not in returned.error
        assert len(returned.error) <= 300

    @pytest.mark.asyncio
    async def test_no_verdicts_for_candidates_marks_them_needs_confirmation(
        self, workspace
    ):
        """An empty verdict map over real candidates is a gap, not a clean pass."""
        f = ReviewFinalizer(workspace)
        validated = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Unreturned issue",
                        "evidence": "line1",
                        "impact": "bad",
                        "recommendation": "fix",
                    }
                ]
            ),
        )

        execution, judged = await f.judge(FakeJudge({}), (validated,))
        result = f.finalize(judged, target_name="test", judge_stats=execution.stats)

        assert execution.verdicts == {}
        assert "AI judge returned no verdicts" in result.report_markdown
        assert len(result.needs_confirmation) == 1

    @pytest.mark.asyncio
    async def test_an_empty_candidate_set_needs_no_judge_pass(self, workspace):
        f = ReviewFinalizer(workspace)
        empty = ReviewDimensionResult(dimension="security", status="no_findings")

        execution, judged = await f.judge(FakeJudge({}), (empty,))

        assert execution.verdicts == {}
        assert judged == (empty,)

    @pytest.mark.asyncio
    async def test_judge_applies_verdicts_to_accepted_and_uncertain_candidates(
        self, workspace
    ):
        f = ReviewFinalizer(workspace)
        validated = f.validate_findings(
            "security",
            _findings(
                [
                    {
                        "severity": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "title": "Accepted issue",
                        "evidence": "line1",
                        "impact": "bad",
                        "recommendation": "fix",
                    }
                ]
            ),
        )
        assert validated.accepted
        verdicts = {
            "security:src/app.py:1:accepted issue": ReviewJudgeVerdict(
                decision=ReviewJudgeDecision.ACCEPT, reason="confirmed", confidence="high"
            )
        }

        _, judged = await f.judge(FakeJudge(verdicts), (validated,))

        assert len(judged) == 1
        assert judged[0].judged[0].judge_verdict.decision is ReviewJudgeDecision.ACCEPT
        # The judge pass returns a *new* set; the input is untouched.
        assert validated.judged == []


class TestLogging:
    def test_an_unstructured_answer_does_not_log_a_json_error(self, workspace):
        """A missing structured answer is a normal gap, not an internal error."""
        errors: list[str] = []
        sink_id = logger.add(lambda message: errors.append(str(message)), level="ERROR")
        try:
            # The runner never hands the finalizer an unstructured answer: it
            # rejects it first. What must not happen is a JSON parse error here.
            f = ReviewFinalizer(workspace)
            f.validate_findings("bug-risk", [])
        finally:
            logger.remove(sink_id)

        assert not any("submit json error" in message for message in errors)

    def test_a_disallowed_dimension_logs_a_warning(self, workspace):
        warnings: list[str] = []
        sink_id = logger.add(
            lambda message: warnings.append(str(message)), level="WARNING"
        )
        try:
            ReviewFinalizer(workspace, allowed_dimensions={"bug"}).validate_findings(
                "security", []
            )
        finally:
            logger.remove(sink_id)

        assert any("skip_disallowed" in message for message in warnings)
