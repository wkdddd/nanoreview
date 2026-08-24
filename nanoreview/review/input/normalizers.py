"""Normalize review inputs before building a review plan."""
from __future__ import annotations

from typing import Any

from nanoreview.review.input.targets import infer_review_target_type
from nanoreview.review.types import (
    ALL_REVIEW_ROLES,
    ReviewAction,
    ReviewDepth,
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
    if value in {"auto", "local", "github"}:
        return value
    return infer_review_target_type(target)


def normalize_review_action(raw: str | None) -> ReviewAction:
    value = (raw or ReviewAction.REPO.value).strip().lower()
    try:
        return ReviewAction(value)
    except ValueError:
        pass
    allowed = ", ".join(review_action_values())
    raise ValueError(f"Unknown review action '{value}'. Available action values: {allowed}")


def normalize_mode(raw: Any) -> ReviewDepth:
    value = str(raw or "full").strip().lower()
    if value in {"quick", "full", "deep"}:
        return value  # type: ignore[return-value]
    return "full"

