"""Normalize review inputs before building a review plan."""
from __future__ import annotations

from nanoreview.review.input.targets import infer_review_target_type
from nanoreview.review.types import (
    ALL_REVIEW_ROLES,
    SPECIAL_REVIEW_ROLE_NAMES,
    ReviewAction,
    ReviewMode,
    ReviewRole,
    review_action_values,
)

#: Reviewer modes are mutually exclusive; the mode is decided once here so the
#: planner, receiver and report never re-interpret the raw ``focus`` value.
REVIEW_MODES: tuple[ReviewMode, ...] = ("auto", "special", "general")

#: Assignment ceiling shared by every mode; the product fixes it at four.
MAX_ASSIGNMENTS = 4

#: Legacy explicit value that addressed the generic reviewer before the mode
#: split. It is accepted on input and normalized to ``general``; it is not a
#: specialized dimension and never appears in ``auto``/``special``.
_GENERAL_ALIASES = frozenset({"general"})


def normalize_requested_dimensions(
    raw: str | list[str] | None,
) -> tuple[list[ReviewRole], ReviewMode]:
    """Resolve the raw ``focus`` value into roles plus one reviewer mode.

    Three mutually exclusive modes are produced:

    * ``auto`` — no selection: the planner may choose 1–4 specialized reviewers.
    * ``special`` — one or more specialized dimensions: every selected
      dimension must be dispatched.
    * ``general`` — exactly the generic reviewer, never combined with a
      specialized dimension.

    Legacy input is still accepted: a missing/empty value and the literal
    ``auto`` both normalize to ``auto``; the alias ``general`` normalizes to the
    ``general`` mode. Invalid combinations raise ``ValueError`` with a message
    that distinguishes an unknown dimension, an empty selection, a mixed
    general/special selection and an oversized selection.
    """
    if not raw:
        return _auto_roles(), "auto"

    items = raw if isinstance(raw, list) else raw.split(",")
    normalized_items = [str(item).strip().lower() for item in items if str(item).strip()]
    if not normalized_items:
        return _auto_roles(), "auto"

    if "auto" in normalized_items:
        if len(normalized_items) != 1:
            raise ValueError(
                "Review mode 'auto' cannot be combined with explicit dimensions"
            )
        return _auto_roles(), "auto"

    wants_general = any(item in _GENERAL_ALIASES for item in normalized_items)
    specialized: list[ReviewRole] = []
    for key in normalized_items:
        if key in _GENERAL_ALIASES:
            continue
        role = ALL_REVIEW_ROLES.get(key)
        if role is None:
            allowed = ", ".join(sorted(ALL_REVIEW_ROLES))
            raise ValueError(
                f"Unknown review dimension '{key}'. Available dimensions: {allowed}"
            )
        if role not in specialized:
            specialized.append(role)

    if wants_general:
        if specialized:
            raise ValueError(
                "The general reviewer cannot be combined with specialized dimensions; "
                "run general alone or select specialized dimensions only"
            )
        return [ALL_REVIEW_ROLES["general"]], "general"

    if not specialized:
        raise ValueError(
            "The special review mode requires at least one specialized dimension: "
            f"{', '.join(sorted(SPECIAL_REVIEW_ROLE_NAMES))}"
        )
    if len(specialized) > MAX_ASSIGNMENTS:
        raise ValueError(
            f"The special review mode accepts at most {MAX_ASSIGNMENTS} dimensions"
        )
    return specialized, "special"


def _auto_roles() -> list[ReviewRole]:
    """Every specialized role; the generic reviewer is never auto-selected."""
    return [ALL_REVIEW_ROLES[name] for name in ALL_REVIEW_ROLES if name in SPECIAL_REVIEW_ROLE_NAMES]


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
