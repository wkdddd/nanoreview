from nanoreview.review.input import apply_policy_to_roles, policy_for_depth
from nanoreview.review.types import ALL_REVIEW_ROLES


def test_quick_policy_preserves_requested_subagent_limit() -> None:
    policy = policy_for_depth("quick", requested_max_subagents=6)

    assert policy.max_subagents == 6
    assert policy.severities == ("critical", "high")
    assert policy.judge_enabled is False
    assert [role.name for role in policy.roles] == ["security", "bug-risk", "tests"]


def test_full_policy_enables_judge_and_default_roles() -> None:
    policy = policy_for_depth("full", requested_max_subagents=4)

    assert policy.max_subagents == 4
    assert policy.judge_enabled is True
    assert [role.name for role in policy.roles] == [
        "security",
        "tests",
        "architecture",
        "performance",
    ]


def test_deep_policy_adds_optional_roles_and_preserves_subagent_limit() -> None:
    policy = policy_for_depth("deep", requested_max_subagents=4)

    assert policy.max_subagents == 4
    assert policy.include_optional_roles is True
    assert "dependency" in {role.name for role in policy.roles}


def test_forced_focus_is_not_replaced_by_quick_policy() -> None:
    policy = policy_for_depth("quick", requested_max_subagents=6)
    roles = apply_policy_to_roles(
        roles=[ALL_REVIEW_ROLES["dependency"]],
        forced_dimensions=True,
        policy=policy,
    )

    assert [role.name for role in roles] == ["dependency"]
