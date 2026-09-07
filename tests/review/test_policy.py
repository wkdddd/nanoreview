"""Tests for the programmatic review mode policy."""
from __future__ import annotations

from nanoreview.review.input import apply_policy_to_roles, policy_for_depth
from nanoreview.review.types import ALL_REVIEW_ROLES


def test_quick_policy_limits_severities_and_disables_judge() -> None:
    policy = policy_for_depth("quick")

    assert policy.depth == "quick"
    assert policy.severities == ("critical", "high")
    assert policy.judge_enabled is False
    assert policy.evidence_max_results == 4
    assert policy.include_optional_roles is False


def test_full_policy_enables_judge_and_all_severities() -> None:
    policy = policy_for_depth("full")

    assert policy.depth == "full"
    assert policy.severities == ("critical", "high", "medium", "low")
    assert policy.judge_enabled is True
    assert policy.evidence_max_results == 8


def test_deep_policy_adds_optional_roles_and_wider_evidence() -> None:
    policy = policy_for_depth("deep")

    assert policy.depth == "deep"
    assert policy.include_optional_roles is True
    assert policy.evidence_max_results == 12
    assert policy.judge_enabled is True


def test_policy_never_controls_reviewer_dimensions() -> None:
    """Depth controls budgets only; the requested reviewer set is preserved."""
    roles = list(ALL_REVIEW_ROLES.values())
    for depth in ("quick", "full", "deep"):
        policy = policy_for_depth(depth)  # type: ignore[arg-type]
        assert apply_policy_to_roles(
            roles=roles, routing_mode="auto", policy=policy
        ) == roles
