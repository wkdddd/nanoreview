from __future__ import annotations

import pytest

from nanoreview.agent.runner import AgentRunResult
from nanoreview.agent.subagent_profiles import (
    BUG_REVIEWER_SCOPE,
    GENERAL_REVIEWER_SCOPE,
    MAINTAINABILITY_REVIEWER_SCOPE,
    PERFORMANCE_REVIEWER_SCOPE,
    SECURITY_REVIEWER_SCOPE,
)
from nanoreview.agent.tools.loader import ToolLoader
from nanoreview.review.input.normalizers import normalize_requested_dimensions
from nanoreview.review.profiles import (
    REVIEWER_PROFILES,
    public_reviewer_profiles,
    reviewer_execution_profiles,
)


def test_registry_exposes_four_specialized_profiles_plus_general() -> None:
    assert set(REVIEWER_PROFILES) == {
        "bug",
        "security",
        "performance",
        "maintainability",
        "general",
    }
    assert [item["id"] for item in public_reviewer_profiles()] == list(REVIEWER_PROFILES)


def test_execution_profiles_use_dedicated_scopes_and_require_shared_tools() -> None:
    profiles = {key: value.execution_profile() for key, value in REVIEWER_PROFILES.items()}
    assert profiles["bug"].scope == BUG_REVIEWER_SCOPE
    assert profiles["security"].scope == SECURITY_REVIEWER_SCOPE
    assert profiles["performance"].scope == PERFORMANCE_REVIEWER_SCOPE
    assert profiles["maintainability"].scope == MAINTAINABILITY_REVIEWER_SCOPE
    assert profiles["general"].scope == GENERAL_REVIEWER_SCOPE
    assert profiles["bug"].required_tools == frozenset({"read_file", "list_dir", "grep"})


def test_execution_profiles_use_independent_scopes_and_required_tools() -> None:
    profiles = reviewer_execution_profiles()
    assert {profile.scope for profile in profiles.values()} == {
        BUG_REVIEWER_SCOPE,
        SECURITY_REVIEWER_SCOPE,
        PERFORMANCE_REVIEWER_SCOPE,
        MAINTAINABILITY_REVIEWER_SCOPE,
        GENERAL_REVIEWER_SCOPE,
    }
    expected_required = frozenset({"read_file", "list_dir", "grep"})
    assert all(profile.required_tools == expected_required for profile in profiles.values())


def test_tool_loader_rejects_unknown_scope() -> None:
    loader = ToolLoader()
    try:
        loader.validate_scope("reviewer.unknown")
    except ValueError as exc:
        assert "Unknown tool scope" in str(exc)
    else:
        raise AssertionError("unknown scope should fail validation")


def test_default_selection_is_general_and_explicit_selection_is_verbatim() -> None:
    default_roles = normalize_requested_dimensions(None)
    roles = normalize_requested_dimensions("security,bug")
    assert [role.name for role in default_roles] == ["general"]
    assert [role.name for role in roles] == ["security", "bug"]


def test_general_may_be_combined_with_specialized_reviewers() -> None:
    """``general`` is a peer reviewer, not an exclusive mode."""
    roles = normalize_requested_dimensions("general,security")

    assert [role.name for role in roles] == ["general", "security"]


def test_all_five_reviewers_can_be_selected_at_once() -> None:
    roles = normalize_requested_dimensions(
        ["bug", "security", "performance", "maintainability", "general"]
    )

    assert len(roles) == 5
    assert {role.name for role in roles} == {
        "bug",
        "security",
        "performance",
        "maintainability",
        "general",
    }


def test_reviewer_profiles_declare_their_own_output_contract() -> None:
    """Every registered reviewer profile declares its own output contract.

    The manager has no built-in default profile (the generic fallback was
    removed), so a profile that forgot to declare its contract would otherwise
    run with no structured result to hand back to the review loop.
    """
    profiles = reviewer_execution_profiles()
    assert all(profile.output_contract_factory is not None for profile in profiles.values())
    assert all(
        profile.output_contract_factory().name == f"reviewer.{profile.id}"
        for profile in profiles.values()
    )
    # No review tool exists any more, so no profile preserves a tool result.
    assert all(
        profile.preserve_tool_result_tools == frozenset()
        for profile in profiles.values()
    )


def _finding(marker: str) -> dict[str, object]:
    return {
        "severity": "high",
        "file": "src/app.py",
        "line": 1,
        "title": marker,
        "evidence": "x" * 400,
        "impact": "bad",
        "recommendation": "fix",
        "details": {"trigger": "input", "expected_behavior": "a", "actual_behavior": "b"},
    }


def _completed_result(findings: list[dict[str, object]], **extra: object) -> AgentRunResult:
    """An AgentRunResult whose final answer already passed the output contract."""
    payload: dict[str, object] = {"dimension": "bug", "findings": findings}
    payload.update(extra)
    return AgentRunResult(
        final_content="reviewer finished",
        messages=[
            {"role": "tool", "name": "read_file", "content": "evidence"},
            {"role": "assistant", "content": "reviewer finished"},
        ],
        tool_events=[
            {"name": "read_file", "status": "ok", "detail": "evidence"},
        ],
        structured_output=payload,
        stop_reason="completed",
    )


@pytest.mark.asyncio
async def test_reviewer_handler_accepts_the_structured_findings() -> None:
    handler = reviewer_execution_profiles()["bug"].result_handler
    assert handler is not None

    completion = await handler(result=_completed_result([_finding("Issue")]))

    assert completion.status == "ok"
    assert completion.payload["findings"][0]["title"] == "Issue"
    assert completion.payload["findings"][0]["evidence"] == "x" * 400
    assert completion.stop_reason == "completed"
    assert "1 finding(s)" in completion.content


@pytest.mark.asyncio
async def test_reviewer_handler_accepts_no_findings_when_evidence_was_assigned() -> None:
    """An empty list is a legitimate "nothing found" answer when evidence existed."""
    handler = reviewer_execution_profiles()["bug"].result_handler
    assert handler is not None

    completion = await handler(
        result=_completed_result([]), metadata={"assigned_evidence": 2}
    )

    assert completion.status == "ok"
    assert completion.payload["findings"] == []


@pytest.mark.asyncio
async def test_reviewer_handler_rejects_no_findings_without_any_evidence() -> None:
    """Reporting no findings without reading anything is not a completed review."""
    handler = reviewer_execution_profiles()["bug"].result_handler
    assert handler is not None
    result = _completed_result([])
    result = AgentRunResult(
        final_content=result.final_content,
        messages=result.messages,
        tool_events=[],
        structured_output=result.structured_output,
        stop_reason=result.stop_reason,
    )

    completion = await handler(result=result)

    assert completion.status == "error"
    assert "no target evidence" in completion.content


@pytest.mark.asyncio
async def test_reviewer_handler_rejects_a_run_without_structured_output() -> None:
    """A run whose answer never satisfied the contract cannot be read as success."""
    handler = reviewer_execution_profiles()["bug"].result_handler
    assert handler is not None
    result = AgentRunResult(
        final_content="I could not decide.",
        messages=[{"role": "assistant", "content": "I could not decide."}],
        tool_events=[],
        stop_reason="invalid_output",
    )

    completion = await handler(result=result)

    assert completion.status == "error"
    assert "no structured result" in completion.content
    assert completion.stop_reason == "invalid_output"


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


def test_general_profile_declares_its_own_contract(tmp_path) -> None:
    profile = REVIEWER_PROFILES["general"]
    # Its own details schema, not a specialized profile's fields.
    required = set(profile.details_schema["required"])
    assert required == {"concern", "symptom", "affected_behavior", "recommended_fix"}
    assert profile.report_fields and all(
        key in required for key, _ in profile.report_fields
    )
    # Its own prompt fragment, injected into the shared reviewer system prompt.
    prompt = _reviewer_prompt("general", {}, tmp_path)
    assert "General Reviewer Contract" in prompt
    assert "details.concern" in prompt
    assert "Bug Reviewer Contract" not in prompt


def test_general_is_a_peer_reviewer_not_a_specialized_one() -> None:
    from nanoreview.review.types import ALL_REVIEW_ROLES, SPECIAL_REVIEW_ROLE_NAMES

    assert "general" in ALL_REVIEW_ROLES
    assert "general" not in SPECIAL_REVIEW_ROLE_NAMES
    # It is dispatched like any other selected reviewer.
    roles = normalize_requested_dimensions(None)
    assert [role.name for role in roles] == ["general"]
