from __future__ import annotations

import json

import pytest

from nanoreview.agent.runner import AgentRunResult
from nanoreview.agent.subagent_profiles import (
    BUG_REVIEWER_SCOPE,
    MAINTAINABILITY_REVIEWER_SCOPE,
    PERFORMANCE_REVIEWER_SCOPE,
    SECURITY_REVIEWER_SCOPE,
)
from nanoreview.agent.tools.loader import ToolLoader
from nanoreview.agent.tools.review_plan import ReviewPlanReceiver
from nanoreview.agent.tools.review_submit import review_submit
from nanoreview.review.input.normalizers import normalize_requested_dimensions
from nanoreview.review.profiles import (
    REVIEWER_PROFILES,
    public_reviewer_profiles,
    reviewer_execution_profiles,
)


def test_registry_exposes_only_four_reviewer_profiles() -> None:
    assert set(REVIEWER_PROFILES) == {"bug", "security", "performance", "maintainability"}
    assert [item["id"] for item in public_reviewer_profiles()] == list(REVIEWER_PROFILES)


def test_execution_profiles_use_dedicated_scopes_and_require_shared_tools() -> None:
    profiles = {key: value.execution_profile() for key, value in REVIEWER_PROFILES.items()}
    assert profiles["bug"].scope == BUG_REVIEWER_SCOPE
    assert profiles["security"].scope == SECURITY_REVIEWER_SCOPE
    assert profiles["performance"].scope == PERFORMANCE_REVIEWER_SCOPE
    assert profiles["maintainability"].scope == MAINTAINABILITY_REVIEWER_SCOPE
    assert profiles["bug"].required_tools == frozenset(
        {"read_file", "list_dir", "grep", "review_submit"}
    )


def test_execution_profiles_use_independent_scopes_and_required_tools() -> None:
    profiles = reviewer_execution_profiles()
    assert {profile.scope for profile in profiles.values()} == {
        BUG_REVIEWER_SCOPE,
        SECURITY_REVIEWER_SCOPE,
        PERFORMANCE_REVIEWER_SCOPE,
        MAINTAINABILITY_REVIEWER_SCOPE,
    }
    expected_required = frozenset({"read_file", "list_dir", "grep", "review_submit"})
    assert all(profile.required_tools == expected_required for profile in profiles.values())


def test_tool_loader_rejects_unknown_scope() -> None:
    loader = ToolLoader()
    try:
        loader.validate_scope("reviewer.unknown")
    except ValueError as exc:
        assert "Unknown tool scope" in str(exc)
    else:
        raise AssertionError("unknown scope should fail validation")


def test_empty_dimensions_are_auto_and_explicit_dimensions_are_explicit() -> None:
    _, auto_mode = normalize_requested_dimensions(None)
    roles, explicit_mode = normalize_requested_dimensions("security,bug")
    assert auto_mode == "auto"
    assert explicit_mode == "explicit"
    assert [role.name for role in roles] == ["security", "bug"]


def test_plan_allows_evidence_reuse_across_reviewers() -> None:
    receiver = ReviewPlanReceiver({"bug", "security"}, {"ev-1"}, "explicit")
    accepted, _ = receiver.submit([
        {"dimension": "security", "focus": "trust boundary", "evidence_ids": ["ev-1"]},
        {"dimension": "bug", "focus": "exception path", "evidence_ids": ["ev-1"]},
    ])
    assert accepted


def test_reviewer_profiles_preserve_review_submit_result() -> None:
    """Reviewer profiles declare the accepted submission as a preserved result."""
    profiles = reviewer_execution_profiles()
    assert all(
        profile.preserve_tool_result_tools == frozenset({"review_submit"})
        for profile in profiles.values()
    )


def test_reviewer_profiles_declare_their_own_terminal_contract() -> None:
    """Every registered reviewer profile declares its tools explicitly.

    The manager has no built-in default profile (the generic fallback was
    removed), so each profile must carry its own terminal/preserved contract;
    a profile that forgot to would otherwise run with the wrong boundary.
    """
    profiles = reviewer_execution_profiles()
    assert all(
        profile.terminal_tools == frozenset({"review_submit"})
        for profile in profiles.values()
    )
    assert all(
        profile.preserve_tool_result_tools == frozenset({"review_submit"})
        for profile in profiles.values()
    )


def _submit_payload(*, marker: str) -> str:
    return json.dumps(
        {
            "submitted": True,
            "findings": [
                {
                    "severity": "high",
                    "file": "src/app.py",
                    "line": 1,
                    "title": marker,
                    "evidence": "x" * 400,
                    "impact": "bad",
                    "recommendation": "fix",
                }
            ],
            "errors": [],
        }
    )


@pytest.mark.asyncio
async def test_reviewer_handler_parses_full_submission() -> None:
    handler = reviewer_execution_profiles()["bug"].result_handler
    assert handler is not None
    raw_result = _submit_payload(marker="Issue")
    result = AgentRunResult(
        final_content=None,
        messages=[
            {"role": "tool", "name": "read_file", "content": "evidence"},
            {"role": "tool", "name": "review_submit", "content": raw_result},
        ],
        tool_events=[
            {"name": "read_file", "status": "ok", "detail": "evidence"},
            {
                "name": "review_submit",
                "status": "ok",
                "detail": raw_result[:120] + "...",
                "raw_result": raw_result,
            },
        ],
    )

    completion = await handler(result=result, target_type="local")

    assert completion.status == "ok"
    assert json.loads(completion.content)["findings"][0]["title"] == "Issue"
    assert completion.stop_reason == "completed"


@pytest.mark.asyncio
async def test_reviewer_handler_truncated_tool_message_uses_raw_result() -> None:
    """The bounded tool message must not shadow the untruncated raw result."""
    handler = reviewer_execution_profiles()["bug"].result_handler
    assert handler is not None
    raw_result = _submit_payload(marker="FullFinding")
    truncated = raw_result[:60] + "\n... (truncated)"
    result = AgentRunResult(
        final_content=None,
        messages=[
            {"role": "tool", "name": "review_submit", "content": truncated},
        ],
        tool_events=[
            {
                "name": "review_submit",
                "status": "ok",
                "detail": raw_result[:120] + "...",
                "raw_result": raw_result,
            },
        ],
    )

    completion = await handler(result=result, target_type="local")

    assert completion.status == "ok"
    parsed = json.loads(completion.content)
    assert parsed["findings"][0]["title"] == "FullFinding"
    assert parsed["findings"][0]["evidence"] == "x" * 400


@pytest.mark.asyncio
async def test_reviewer_handler_rejects_unprocessed_submission() -> None:
    """A review_submit call without a processed result is not a valid submission."""
    handler = reviewer_execution_profiles()["bug"].result_handler
    assert handler is not None
    result = AgentRunResult(
        final_content=None,
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "review_submit",
                            "arguments": _submit_payload(marker="Issue"),
                        },
                    }
                ],
            }
        ],
        tool_events=[
            {"name": "review_submit", "status": "error", "detail": "failed"},
        ],
    )

    completion = await handler(result=result, target_type="local")

    assert completion.status == "error"
    assert "No structured findings submitted" in completion.content


def test_review_submit_preserves_details() -> None:
    result = review_submit([
        {
            "severity": "high",
            "file": "src/app.py",
            "line": 4,
            "title": "Unsafe path",
            "evidence": "`path = request.path`",
            "impact": "Traversal",
            "recommendation": "Validate the path",
            "details": {
                "trust_boundary": "HTTP request to filesystem",
                "attack_preconditions": "Attacker controls path",
                "attack_path": "request -> path join -> read",
            },
        }
    ])
    assert result.submitted
    assert result.findings[0]["details"]["trust_boundary"].startswith("HTTP")


def _reviewer_prompt(profile_id: str, metadata: dict, workspace) -> str:
    profile = REVIEWER_PROFILES[profile_id]
    builder = profile.execution_profile().prompt_builder
    return builder(metadata, workspace)


def test_reviewer_prompt_injects_common_rules_from_workspace(tmp_path) -> None:
    rules_root = tmp_path / "nanoreview-workspace"
    rules_root.mkdir()
    (rules_root / "COMMON_RULES.md").write_text("shared reviewer rule", encoding="utf-8")

    prompt = _reviewer_prompt(
        "bug", {"common_rules_workspace": str(rules_root)}, tmp_path
    )

    assert "## Shared Rules" in prompt
    assert "shared reviewer rule" in prompt


def test_reviewer_prompt_omits_shared_rules_without_workspace(tmp_path) -> None:
    prompt = _reviewer_prompt("bug", {}, tmp_path)

    assert "## Shared Rules" not in prompt


def test_reviewer_prompt_omits_shared_rules_when_file_missing(tmp_path) -> None:
    rules_root = tmp_path / "empty-workspace"
    rules_root.mkdir()

    prompt = _reviewer_prompt(
        "bug", {"common_rules_workspace": str(rules_root)}, tmp_path
    )

    assert "## Shared Rules" not in prompt
