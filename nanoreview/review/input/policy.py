"""Programmatic review mode policy."""
from __future__ import annotations

from nanoreview.review.types import (
    ReviewDepth,
    ReviewModePolicy,
)


def policy_for_depth(depth: ReviewDepth, **_: object) -> ReviewModePolicy:
    """Return the execution policy for a review depth."""
    if depth == "quick":
        return ReviewModePolicy(
            depth=depth,
            severities=("critical", "high"),
            judge_enabled=False,
            evidence_max_results=4,
            report_style="quick",
        )
    if depth == "deep":
        return ReviewModePolicy(
            depth=depth,
            severities=("critical", "high", "medium", "low"),
            judge_enabled=True,
            evidence_max_results=12,
            include_optional_roles=True,
            report_style="deep",
        )
    return ReviewModePolicy(
        depth="full",
        severities=("critical", "high", "medium", "low"),
        judge_enabled=True,
        evidence_max_results=8,
        report_style="full",
    )


def apply_policy_to_roles(*, roles: list, **_: object) -> list:
    """Depth controls budgets, never the available reviewer dimensions."""
    return list(roles)
