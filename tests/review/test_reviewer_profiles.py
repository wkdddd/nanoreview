from __future__ import annotations

from nanoreview.agent.subagent_profiles import (
    BUG_REVIEWER_SCOPE,
    GENERIC_SUBAGENT_PROFILE,
    MAINTAINABILITY_REVIEWER_SCOPE,
    PERFORMANCE_REVIEWER_SCOPE,
    SECURITY_REVIEWER_SCOPE,
    SUBAGENT_SCOPE,
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
    assert GENERIC_SUBAGENT_PROFILE.scope == SUBAGENT_SCOPE
    profiles = {key: value.execution_profile() for key, value in REVIEWER_PROFILES.items()}
    assert profiles["bug"].scope == BUG_REVIEWER_SCOPE
    assert profiles["security"].scope == SECURITY_REVIEWER_SCOPE
    assert profiles["performance"].scope == PERFORMANCE_REVIEWER_SCOPE
    assert profiles["maintainability"].scope == MAINTAINABILITY_REVIEWER_SCOPE
    assert profiles["bug"].required_tools == frozenset(
        {"read_file", "list_dir", "grep", "review_submit"}
    )


def test_execution_profiles_use_independent_scopes_and_required_tools() -> None:
    assert GENERIC_SUBAGENT_PROFILE.scope == SUBAGENT_SCOPE
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
