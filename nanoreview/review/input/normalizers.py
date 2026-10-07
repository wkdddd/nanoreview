"""Normalize review inputs before building a review plan."""
from __future__ import annotations

from nanoreview.review.input.targets import infer_review_target_type
from nanoreview.review.types import (
    ALL_REVIEW_ROLES,
    ReviewAction,
    ReviewRole,
    review_action_values,
)


def normalize_requested_dimensions(
    raw: str | list[str] | None,
) -> tuple[list[ReviewRole], str]:
    if not raw:
        return list(ALL_REVIEW_ROLES.values()), "auto"

    selected: list[ReviewRole] = []
    items = raw if isinstance(raw, list) else raw.split(",")
    normalized_items = [str(item).strip().lower() for item in items if str(item).strip()]
    if "auto" in normalized_items:
        if len(normalized_items) != 1:
            raise ValueError("Review dimension 'auto' cannot be combined with explicit dimensions")
        return list(ALL_REVIEW_ROLES.values()), "auto"
    for key in normalized_items:
        if not key:
            continue
        role = ALL_REVIEW_ROLES.get(key)
        if role is None:
            allowed = ", ".join(sorted(ALL_REVIEW_ROLES))
            raise ValueError(
                f"Unknown review dimension '{key}'. Available dimensions: {allowed}"
            )
        if role not in selected:
            selected.append(role)
    return (selected, "explicit") if selected else (list(ALL_REVIEW_ROLES.values()), "auto")


def normalize_review_target_type(raw: str | None, target: str | None = None) -> str | None:
    value = (raw or "").strip().lower()
    if value in {"auto", "local"}:
        return value
    return infer_review_target_type(target)


def normalize_review_action(raw: str | None) -> ReviewAction:
    """Resolve the review action; only local ``diff`` review is supported.

    Repo-wide review has been removed as a product entry point: a missing
    action defaults to ``diff`` and an explicit ``repo`` request is rejected so
    no transport can reach the removed entry. ``ReviewAction.REPO`` stays as the
    canonical name of that rejected action (error message and recognition of
    previously persisted metadata) even though nothing produces it any more.
    """
    value = (raw or ReviewAction.DIFF.value).strip().lower()
    if value == ReviewAction.DIFF.value:
        return ReviewAction.DIFF
    if value == ReviewAction.REPO.value:
        raise ValueError(
            "Repo-wide review is not supported; only action 'diff' review is available."
        )
    allowed = ", ".join(review_action_values())
    raise ValueError(f"Unknown review action '{value}'. Available action values: {allowed}")
