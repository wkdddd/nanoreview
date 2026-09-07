from __future__ import annotations

import pytest

from nanoreview.agent.loop import AgentLoop
from nanoreview.bus.queue import MessageBus
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review import (
    ALL_REVIEW_ROLES,
    DEFAULT_REVIEW_ROLES,
    OPTIONAL_REVIEW_ROLES,
    SEVERITY_ORDER,
    Finding,
    ReviewReport,
    build_code_review_context,
    build_review_plan,
    normalize_requested_dimensions,
    normalize_review_action,
    resolve_code_review_context,
)


class DummyProvider(LLMProvider):
    async def chat(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


def test_normalize_requested_dimensions_defaults_to_all_roles_in_auto_mode() -> None:
    roles, mode = normalize_requested_dimensions(None)

    assert [role.name for role in roles] == list(ALL_REVIEW_ROLES)
    assert mode == "auto"


def test_normalize_requested_dimensions_selects_requested_roles_and_deduplicates() -> None:
    roles, mode = normalize_requested_dimensions("security, bug, security")

    assert [role.name for role in roles] == ["security", "bug"]
    assert mode == "explicit"


def test_normalize_requested_dimensions_rejects_unknown_dimension() -> None:
    with pytest.raises(ValueError, match="Unknown review dimension 'bogus'"):
        normalize_requested_dimensions("security,bogus")


def test_review_role_sets() -> None:
    assert len(DEFAULT_REVIEW_ROLES) == 4
    assert len(OPTIONAL_REVIEW_ROLES) == 0
    assert len(ALL_REVIEW_ROLES) == 4


def test_build_code_review_context_mentions_all_severity_levels() -> None:
    """Unified strategy: the prompt demands coverage of every severity."""
    prompt = build_code_review_context(
        target="https://github.com/test/repo",
        max_subagents=4,
    )

    assert "Cover all severity levels (critical, high, medium, low)" in prompt
    assert "The AI judge is enabled" in prompt


def test_build_code_review_context_includes_subagent_candidate_schema() -> None:
    prompt = build_code_review_context(
        target="https://github.com/test/repo",
        max_subagents=4,
    )
    assert "Review Finding Schema" in prompt
    assert "using `spawn`" in prompt
    assert "review_submit" in prompt
    assert '"severity"' in prompt
    assert '"evidence"' in prompt


def test_build_code_review_context_prohibits_coordinator_review_submit() -> None:
    prompt = build_code_review_context(
        target="https://github.com/test/repo",
        max_subagents=4,
    )
    assert "must NEVER call `review_submit`" in prompt
    assert "subagent-only tool" in prompt


def test_build_code_review_context_spawn_task_requires_target_and_evidence() -> None:
    prompt = build_code_review_context(
        target="https://github.com/test/repo",
        max_subagents=4,
        focus="security",
    )
    assert "target path" in prompt.lower()
    assert "line range" in prompt.lower()
    assert "evidence source" in prompt.lower()
    assert "subagent-only tool" in prompt


def test_build_code_review_context_matches_system_finalizer_boundary() -> None:
    prompt = build_code_review_context(
        target="https://github.com/test/repo",
        max_subagents=4,
    )

    assert "Do not write the final report yourself" in prompt
    assert "Needs Confirmation" in prompt
    assert "respond with accept/reject/uncertain" not in prompt


def test_build_code_review_context_extracts_github_target() -> None:
    prompt = build_code_review_context(
        user_content="please review https://github.com/test/repo.",
    )

    assert "- Name: test/repo" in prompt
    assert "- URL/Path: https://github.com/test/repo" in prompt
    assert "- Type: github" in prompt


def test_build_code_review_context_uses_local_target_type() -> None:
    prompt = build_code_review_context(
        target=r"C:\work\repo\src\app.py",
        target_type="local",
    )

    assert "- Type: local" in prompt
    assert "- URL/Path: C:\\work\\repo\\src\\app.py" in prompt
    assert "Action repo" in prompt


def test_build_code_review_context_extracts_local_path_inside_prompt() -> None:
    prompt = build_code_review_context(
        user_content=r"请审查 C:\work\repo\src\app.py 的登录逻辑",
    )

    assert "- URL/Path: C:\\work\\repo\\src\\app.py" in prompt
    assert "- Type: local" in prompt


def test_build_code_review_context_uses_github_target_type() -> None:
    prompt = build_code_review_context(
        target="https://github.com/test/repo",
        target_type="github",
    )

    assert "- Type: github" in prompt
    assert "github_review(action='repo'" in prompt
    assert "Do NOT clone repositories" in prompt
    assert "repo_review" not in prompt
    assert "github_repo_read" not in prompt


def test_build_code_review_context_requires_local_review_without_prefetch() -> None:
    prompt = build_code_review_context(
        target=r"C:\work\repo\src\app.py",
        target_type="local",
        action="diff",
    )

    assert "No prefetched evidence" in prompt
    assert "use precise reader calls before spawning reviewers" in prompt
    assert "Action diff: review current local git changes" in prompt
    assert "repo_review" not in prompt


@pytest.mark.asyncio
async def test_resolved_github_prompt_does_not_refetch_after_empty_prefetch() -> None:
    class EmptyEvidence:
        async def dispatch(self, **kwargs: object) -> str:
            return ""

    prompt = await resolve_code_review_context(
        [{"role": "user", "content": "review https://github.com/test/repo"}],
        {
            "review_target": "https://github.com/test/repo",
            "review_target_type": "github",
            "_review_evidence_service": EmptyEvidence(),
        },
    )

    assert "submit_review_plan" in prompt
    assert "no program-authorized evidence references" in prompt
    assert "spawn" in prompt


def test_build_code_review_context_uses_user_requirements() -> None:
    prompt = build_code_review_context(
        target="https://github.com/test/repo",
        target_type="github",
        user_content="重点检查鉴权和回归风险",
    )

    assert "User requirements: 重点检查鉴权和回归风险" in prompt


def test_dimension_contract_uses_forced_focus_dimensions() -> None:
    prompt = build_code_review_context(
        target="https://github.com/test/repo",
        target_type="github",
        focus="security,maintainability",
    )

    assert "Cover ONLY these dimensions" in prompt
    assert "Security Reviewer" in prompt
    assert "Maintainability Reviewer" in prompt
    assert "Bug Reviewer" not in prompt
    assert "Performance Reviewer" not in prompt


def test_forced_performance_focus_keeps_performance_dimension() -> None:
    plan = build_review_plan(
        target="https://github.com/test/repo",
        target_type="github",
        focus="performance",
    )

    assert plan is not None
    assert [role.name for role in plan.roles] == ["performance"]

    prompt = build_code_review_context(
        target="https://github.com/test/repo",
        target_type="github",
        focus="performance",
    )

    assert "Performance Reviewer" in prompt
    assert "Security Reviewer" not in prompt
    assert "Bug Reviewer" not in prompt
    assert "Maintainability Reviewer" not in prompt


def test_review_plan_resolves_pr_url_to_diff() -> None:
    plan = build_review_plan(target="https://github.com/test/repo/pull/42", target_type="github")

    assert plan is not None
    assert plan.action == "diff"
    assert plan.target_repo == "test/repo"
    assert plan.pr_number == 42


@pytest.mark.parametrize("action", ["full_repo", "pr_diff", "local_changed"])
def test_review_action_rejects_old_values(action: str) -> None:
    with pytest.raises(ValueError, match=f"Unknown review action '{action}'"):
        normalize_review_action(action)


def test_build_code_review_context_returns_fallback_without_target() -> None:
    prompt = build_code_review_context(user_content="how should I do a review?")

    assert "Code review workflow is active" in prompt


def test_max_severity_and_order() -> None:
    report = ReviewReport(
        target="repo",
        dimensions=[],
        summary="",
        findings=[
            Finding("low", "a.py", None, "t", "i", "r"),
            Finding("high", "b.py", None, "t", "i", "r"),
            Finding("medium", "c.py", None, "t", "i", "r"),
        ],
    )

    assert report.max_severity() == "high"
    assert SEVERITY_ORDER == ("critical", "high", "medium", "low")


def test_code_review_is_not_registered_as_a_tool(tmp_path) -> None:
    loop = AgentLoop(MessageBus(), DummyProvider(), tmp_path)

    assert "code_review" not in loop.tool_names
    assert "github_repo_read" not in loop.tool_names
    assert "repo_review" not in loop.tool_names
    assert "review_judge" not in loop.tool_names
    assert "local_review" in loop.tool_names
    assert "github_review" in loop.tool_names
