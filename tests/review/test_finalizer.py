"""Tests for ReviewFinalizer parsing and orchestration."""
from __future__ import annotations

import json

import pytest
from loguru import logger

from nanoreview.agent.hooks import AgentHookContext, ReviewFinalizerHook
from nanoreview.review.input import policy_for_depth
from nanoreview.review.output.finalizer import ReviewFinalizer
from nanoreview.review.types import (
    ReviewJudgeDecision,
    ReviewJudgeVerdict,
    normalize_review_dimension,
)


class FakeJudge:
    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.calls = 0

    async def judge_dimensions(self, dimensions):
        self.calls += 1
        return self.verdicts


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


def _submit(
    findings: list[dict[str, object]],
    details: dict[str, object] | None = None,
) -> str:
    prepared: list[dict[str, object]] = []
    for finding in findings:
        item = dict(finding)
        item.setdefault(
            "details", dict(details if details is not None else _SECURITY_DETAILS)
        )
        prepared.append(item)
    return json.dumps(
        {"submitted": True, "findings": prepared, "errors": []},
        ensure_ascii=False,
    )


class TestParsingReviewSubmit:
    def test_normalizes_review_label_suffix(self):
        assert normalize_review_dimension("Security Review") == "security"
        assert normalize_review_dimension("Maintainability Review") == "maintainability"

    def test_parse_review_submit_result(self, workspace):
        raw = _submit([{
            "severity": "high",
            "file": "src/app.py",
            "line": 1,
            "title": "Injection",
            "evidence": "line1",
            "impact": "RCE",
            "recommendation": "Parameterize",
        }])
        f = ReviewFinalizer(workspace)
        result = f.ingest_subagent_output("security", raw)
        assert len(result.accepted) == 1
        assert result.accepted[0].title == "Injection"

    def test_parse_empty_review_submit_result(self, workspace):
        f = ReviewFinalizer(workspace)
        result = f.ingest_subagent_output("security", _submit([]))
        assert result.status == "no_findings"


class TestStrictProtocol:
    def test_rejects_bare_json_array(self, workspace):
        raw = json.dumps([{
            "severity": "high",
            "file": "src/app.py",
            "line": 1,
            "title": "Legacy array",
            "evidence": "line1",
            "impact": "bad",
            "recommendation": "fix",
        }])
        f = ReviewFinalizer(workspace)
        result = f.ingest_subagent_output("security", raw)

        assert result.status == "incomplete"
        assert result.accepted == []
        assert result.errors == ["No structured findings were produced by this reviewer."]

    def test_rejects_jsonl_lines(self, workspace):
        raw = "\n".join([
            json.dumps({
                "severity": "medium",
                "file": "src/app.py",
                "line": 2,
                "title": "Issue1",
                "evidence": "line2",
                "impact": "y",
                "recommendation": "z",
            }),
            json.dumps({
                "severity": "low",
                "file": "src/app.py",
                "line": 3,
                "title": "Issue2",
                "evidence": "line3",
                "impact": "b",
                "recommendation": "c",
            }),
        ])
        f = ReviewFinalizer(workspace)
        result = f.ingest_subagent_output("tests", raw)

        assert result.status == "incomplete"
        assert result.accepted == []

    def test_rejects_markdown_table(self, workspace):
        raw = (
            "| severity | file | line | title | evidence | impact | recommendation |\n"
            "|---|---|---|---|---|---|---|\n"
            "| high | src/app.py | 1 | Table issue | line1 | bad | fix |"
        )
        f = ReviewFinalizer(workspace)
        result = f.ingest_subagent_output("tests", raw)

        assert result.status == "incomplete"
        assert result.accepted == []


class TestSemanticVerdicts:
    def test_no_confirmation_needed_when_no_uncertain(self, workspace):
        raw = _submit([{
            "severity": "high", "file": "src/app.py", "line": 1,
            "title": "Clear bug", "evidence": "line1", "impact": "bad", "recommendation": "fix",
        }])
        f = ReviewFinalizer(workspace)
        f.ingest_subagent_output("security", raw)
        assert f.get_needs_confirmation() == []

    def test_uncertain_items_remain_in_needs_confirmation(self, workspace):
        raw = _submit([{
            "severity": "high", "file": "src/app.py", "line": 1,
            "title": "Maybe", "evidence": "", "impact": "unknown", "recommendation": "check",
        }])
        f = ReviewFinalizer(workspace)
        f.ingest_subagent_output("security", raw)
        needs = f.get_needs_confirmation()
        assert len(needs) == 1
        result = f.finalize("test")
        assert "Maybe" in result.report_markdown
        assert "### Needs Confirmation" in result.report_markdown

    @pytest.mark.asyncio
    async def test_ai_judge_can_reject_hard_accepted_candidate(self, workspace):
        raw = _submit([{
            "severity": "high", "file": "src/app.py", "line": 1,
            "title": "False positive", "evidence": "line1", "impact": "bad", "recommendation": "fix",
        }])
        f = ReviewFinalizer(workspace, policy=policy_for_depth("full"))
        f.ingest_subagent_output("security", raw)
        judge = FakeJudge({
            "security:src/app.py:1:false positive": ReviewJudgeVerdict(
                decision=ReviewJudgeDecision.REJECT,
                reason="not actionable",
            )
        })

        await f.apply_judge(judge)
        result = f.finalize("test")

        assert "No actionable issues found" not in result.report_markdown
        assert "AI judge rejected: not actionable" in result.report_markdown

    def test_quick_policy_filters_medium_and_low_candidates(self, workspace):
        raw = _submit([
            {
                "severity": "medium", "file": "src/app.py", "line": 1,
                "title": "Medium issue", "evidence": "line1", "impact": "bad", "recommendation": "fix",
            },
            {
                "severity": "high", "file": "src/app.py", "line": 2,
                "title": "High issue", "evidence": "line2", "impact": "bad", "recommendation": "fix",
            },
        ])
        f = ReviewFinalizer(workspace, policy=policy_for_depth("quick"))
        result = f.ingest_subagent_output("security", raw)

        assert [candidate.title for candidate in result.accepted] == ["High issue"]


class TestFinalize:
    def test_finalize_without_dimensions_is_incomplete_not_clean(self, workspace):
        f = ReviewFinalizer(workspace)
        result = f.finalize("repo")

        assert result.errors == ["No review dimension results were produced."]
        assert "Review incomplete" in result.report_markdown
        assert "No actionable issues found" not in result.report_markdown

    def test_finalize_produces_report(self, workspace):
        raw = _submit([{
            "severity": "critical", "file": "src/app.py", "line": 1,
            "title": "RCE", "evidence": "line1", "impact": "Full compromise",
            "recommendation": "Remove exec",
        }])
        f = ReviewFinalizer(workspace)
        f.ingest_subagent_output("security", raw)
        result = f.finalize("myproject")
        assert "## Code Review Report: myproject" in result.report_markdown
        assert "RCE" in result.report_markdown
        assert result.errors == []

    def test_tool_context_failure_renders_incomplete_not_clean(self, workspace):
        raw = (
            "Error: failed to fetch GitHub repository context: "
            "Error: GitHub API rate limited. Set GITHUB_TOKEN for higher limits."
        )
        f = ReviewFinalizer(workspace)
        result = f.ingest_subagent_output("tests", raw)
        report = f.finalize("repo").report_markdown

        assert result.status == "incomplete"
        assert result.errors
        assert "Review incomplete" in report
        assert "No actionable issues found" not in report

    def test_unstructured_subagent_output_renders_incomplete_not_clean(self, workspace):
        raw = "I inspected the files but did not call review_submit."
        f = ReviewFinalizer(workspace)
        result = f.ingest_subagent_output("architecture", raw)
        report = f.finalize("repo").report_markdown

        assert result.status == "incomplete"
        assert result.errors == ["No structured findings were produced by this reviewer."]
        assert "Review incomplete" in report
        assert "No actionable issues found" not in report

    def test_unstructured_subagent_output_does_not_log_json_error(self, workspace):
        errors: list[str] = []
        sink_id = logger.add(lambda message: errors.append(str(message)), level="ERROR")
        try:
            f = ReviewFinalizer(workspace)
            result = f.ingest_subagent_output("bug-risk", "Plain review summary, no tool call.")
        finally:
            logger.remove(sink_id)

        assert result.status == "incomplete"
        assert not any("submit json error" in message for message in errors)

    def test_finalizer_skips_disallowed_dimensions(self, workspace):
        maintainability_raw = _submit([{
            "severity": "high",
            "file": "src/app.py",
            "line": 1,
            "title": "Boundary violation",
            "evidence": "line1",
            "impact": "Change amplification",
            "recommendation": "Introduce a stable interface",
        }], details=_MAINTAINABILITY_DETAILS)
        security_raw = _submit([{
            "severity": "high",
            "file": "src/app.py",
            "line": 2,
            "title": "Security finding",
            "evidence": "line2",
            "impact": "Bad",
            "recommendation": "Fix",
        }])
        f = ReviewFinalizer(workspace, allowed_dimensions={"maintainability"})

        skipped = f.ingest_subagent_output("security", security_raw)
        accepted = f.ingest_subagent_output("Maintainability Reviewer", maintainability_raw)
        result = f.finalize("myproject")

        assert skipped.status == "skipped_disallowed"
        assert len(accepted.accepted) == 1
        assert "Boundary violation" in result.report_markdown
        assert "Security finding" not in result.report_markdown
        assert [dimension.dimension for dimension in result.dimensions] == ["maintainability"]

    def test_finalizer_accepts_spawn_labels_with_task_suffix(self, workspace):
        raw = _submit([{
            "severity": "high",
            "file": "src/app.py",
            "line": 1,
            "title": "Font loading blocks render",
            "evidence": "line1",
            "impact": "Slower first paint",
            "recommendation": "Use font-display swap",
        }], details=_PERFORMANCE_DETAILS)
        f = ReviewFinalizer(workspace, allowed_dimensions={"performance"})

        accepted = f.ingest_subagent_output(
            "Performance Reviewer for review-webui/index.html",
            raw,
        )
        result = f.finalize("review-webui/index.html")

        assert accepted.status != "skipped_disallowed"
        assert accepted.dimension == "performance"
        assert "Font loading blocks render" in result.report_markdown
        assert not result.errors

    def test_finalizer_accepts_dimension_key_spawn_label(self, workspace):
        """The orchestrator spawns reviewers with the raw dimension key as label."""
        raw = _submit([{
            "severity": "high",
            "file": "src/app.py",
            "line": 1,
            "title": "Blocking external stylesheet",
            "evidence": "line1",
            "impact": "Slower first paint",
            "recommendation": "Preconnect or self-host critical font assets",
        }], details=_PERFORMANCE_DETAILS)
        f = ReviewFinalizer(workspace, allowed_dimensions={"performance"})

        accepted = f.ingest_subagent_output("performance", raw)
        result = f.finalize("review-webui/index.html")

        assert accepted.status != "skipped_disallowed"
        assert accepted.dimension == "performance"
        assert "Blocking external stylesheet" in result.report_markdown
        assert not result.errors

    def test_ingest_runner_message_metadata_prefers_raw_result(self, workspace):
        raw_result = _submit([{
            "severity": "high",
            "file": "src/app.py",
            "line": 2,
            "title": "Wrapped finding",
            "evidence": "line2",
            "impact": "bad",
            "recommendation": "fix",
        }])
        message = {
            "role": "user",
            "content": "[Subagent 'security' completed]\n\nResult:\n[]",
            "_metadata": {
                "injected_event": "subagent_result",
                "subagent_label": "security",
                "subagent_result": raw_result,
            },
        }
        f = ReviewFinalizer(workspace)
        assert f.ingest_messages([message]) == 1
        result = f.finalize("myproject")
        assert "Wrapped finding" in result.report_markdown

    def test_ingest_persisted_subagent_message_top_level_metadata(self, workspace):
        raw_result = _submit([{
            "severity": "high",
            "file": "src/app.py",
            "line": 2,
            "title": "Persisted finding",
            "evidence": "line2",
            "impact": "bad",
            "recommendation": "fix",
        }])
        message = {
            "role": "assistant",
            "content": "[Subagent 'security' completed]\n\nResult:\n[]",
            "injected_event": "subagent_result",
            "subagent_label": "security",
            "subagent_result": raw_result,
        }
        f = ReviewFinalizer(workspace)

        assert f.ingest_messages([message]) == 1
        result = f.finalize("myproject")

        assert "Persisted finding" in result.report_markdown

    def test_ingest_runner_message_requires_raw_submit_result_metadata(self, workspace):
        raw_result = _submit([{
            "severity": "medium",
            "file": "src/app.py",
            "line": 3,
            "title": "Announced finding",
            "evidence": "line3",
            "impact": "bad",
            "recommendation": "fix",
        }])
        message = {
            "role": "user",
            "content": (
                "[Subagent 'tests' completed]\n\n"
                "Result:\n"
                "```json\n"
                f"{raw_result}\n"
                "```\n\n"
                "Summarize this naturally for the user."
            ),
            "_metadata": {
                "injected_event": "subagent_result",
                "subagent_label": "tests",
            },
        }
        f = ReviewFinalizer(workspace)

        assert f.ingest_messages([message]) == 1
        result = f.finalize("myproject")

        assert "Announced finding" not in result.report_markdown
        assert "Review incomplete" in result.report_markdown

    def test_hook_finalizes_content_from_subagent_messages(self, workspace):
        raw_result = _submit([{
            "severity": "critical",
            "file": "src/app.py",
            "line": 1,
            "title": "Hook finding",
            "evidence": "line1",
            "impact": "bad",
            "recommendation": "fix",
        }])
        context = AgentHookContext(
            iteration=1,
            messages=[{
                "role": "user",
                "content": "wrapper",
                "_metadata": {
                    "injected_event": "subagent_result",
                    "subagent_label": "security",
                    "subagent_result": raw_result,
                },
            }],
        )
        hook = ReviewFinalizerHook(workspace=workspace, target_name="myproject")

        content = hook.finalize_content(context, "raw assistant prose")

        assert "## Code Review Report: myproject" in content
        assert "Hook finding" in content

    def test_hook_replaces_prose_when_no_subagent_results(self, workspace):
        context = AgentHookContext(
            iteration=1,
            messages=[{"role": "user", "content": "review this repo"}],
        )
        hook = ReviewFinalizerHook(workspace=workspace, target_name="myproject")

        content = hook.finalize_content(context, "Looks fine from an architecture perspective.")

        assert "## Code Review Report: myproject" in (content or "")
        assert "Review incomplete" in (content or "")
        assert "Looks fine from an architecture perspective" not in (content or "")
        assert context.content_replaced is True

    def test_hook_keeps_rendered_report_authoritative(self, workspace):
        context = AgentHookContext(
            iteration=1,
            messages=[{"role": "user", "content": "review this repo"}],
        )
        hook = ReviewFinalizerHook(workspace=workspace, target_name="myproject")

        first = hook.finalize_content(context, "raw assistant prose")
        second_context = AgentHookContext(
            iteration=2,
            messages=context.messages,
        )
        second = hook.finalize_content(second_context, "later coordinator summary")

        assert first == second
        assert "## Code Review Report: myproject" in (second or "")
        assert "later coordinator summary" not in (second or "")
        assert second_context.content_replaced is True

    def test_hook_allowed_dimensions_can_be_set_after_construction(self, workspace):
        raw_result = _submit([{
            "severity": "critical",
            "file": "src/app.py",
            "line": 1,
            "title": "Disallowed finding",
            "evidence": "line1",
            "impact": "bad",
            "recommendation": "fix",
        }])
        context = AgentHookContext(
            iteration=1,
            messages=[{
                "role": "user",
                "content": "wrapper",
                "_metadata": {
                    "injected_event": "subagent_result",
                    "subagent_label": "security",
                    "subagent_result": raw_result,
                },
            }],
        )
        hook = ReviewFinalizerHook(workspace=workspace, target_name="myproject")
        hook.set_allowed_dimensions(["maintainability"])

        content = hook.finalize_content(context, "raw assistant prose")

        assert "Disallowed finding" not in (content or "")
        assert "Review incomplete" in (content or "")
        assert "No actionable issues found" not in (content or "")

    @pytest.mark.asyncio
    async def test_hook_ingests_incremental_subagent_results(self, workspace):
        first = _submit([{
            "severity": "high",
            "file": "src/app.py",
            "line": 1,
            "title": "First finding",
            "evidence": "line1",
            "impact": "bad",
            "recommendation": "fix",
        }])
        second = _submit([{
            "severity": "high",
            "file": "src/app.py",
            "line": 2,
            "title": "Second finding",
            "evidence": "line2",
            "impact": "bad",
            "recommendation": "fix",
        }], details=_BUG_DETAILS)
        context = AgentHookContext(
            iteration=1,
            messages=[{
                "role": "user",
                "content": "wrapper",
                "_metadata": {
                    "injected_event": "subagent_result",
                    "subagent_task_id": "first",
                    "subagent_label": "security",
                    "subagent_result": first,
                },
            }],
        )
        hook = ReviewFinalizerHook(workspace=workspace, target_name="myproject", depth="quick")

        await hook.after_iteration(context)
        context.messages.append({
            "role": "user",
            "content": "wrapper",
            "_metadata": {
                "injected_event": "subagent_result",
                "subagent_task_id": "second",
                "subagent_label": "bug",
                "subagent_result": second,
            },
        })
        await hook.after_iteration(context)
        content = hook.finalize_content(context, "raw assistant prose")

        assert "First finding" in content
        assert "Second finding" in content
