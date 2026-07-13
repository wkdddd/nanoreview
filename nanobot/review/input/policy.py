"""Programmatic review mode policy."""
from __future__ import annotations

from nanobot.review.types import (
    ALL_REVIEW_ROLES,
    DEFAULT_REVIEW_ROLES,
    OPTIONAL_REVIEW_ROLES,
    ReviewDepth,
    ReviewModePolicy,
    ReviewRole,
)

_QUICK_ROLE_KEYS = ("security", "bug-risk", "tests")
_FULL_ROLE_KEYS = ("security", "tests", "architecture", "performance")


def _roles(keys: tuple[str, ...]) -> list[ReviewRole]:
    return [ALL_REVIEW_ROLES[key] for key in keys if key in ALL_REVIEW_ROLES]


def policy_for_depth(depth: ReviewDepth, *, requested_max_subagents: int = 4) -> ReviewModePolicy:
    """Return the execution policy for a review depth."""
    max_subagents = min(max(int(requested_max_subagents or 1), 1), 10)
    if depth == "quick":
        return ReviewModePolicy(
            depth=depth,
            roles=_roles(_QUICK_ROLE_KEYS),
            max_subagents=max_subagents,
            severities=("critical", "high"),
            judge_enabled=False,
            evidence_max_results=4,
            report_style="quick",
        )
    if depth == "deep":
        return ReviewModePolicy(
            depth=depth,
            roles=[*DEFAULT_REVIEW_ROLES.values(), *OPTIONAL_REVIEW_ROLES.values()],
            max_subagents=max_subagents,
            severities=("critical", "high", "medium", "low"),
            judge_enabled=True,
            evidence_max_results=12,
            include_optional_roles=True,
            report_style="deep",
        )
    return ReviewModePolicy(
        depth="full",
        roles=_roles(_FULL_ROLE_KEYS),
        max_subagents=max_subagents,
        severities=("critical", "high", "medium", "low"),
        judge_enabled=True,
        evidence_max_results=8,
        report_style="full",
    )


def apply_policy_to_roles(
    *,
    roles: list[ReviewRole],
    forced_focus: bool,
    policy: ReviewModePolicy,
) -> list[ReviewRole]:
    """Respect explicit focus, otherwise use mode-selected roles."""
    if forced_focus:
        return roles
    return list(policy.roles)
