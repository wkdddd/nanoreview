"""Pre-plan data normalization and review mode policy."""
from __future__ import annotations

from nanoreview.review.input.normalizers import (
    normalize_requested_dimensions,
    normalize_mode,
    normalize_review_action,
    normalize_review_target_type,
)
from nanoreview.review.input.policy import apply_policy_to_roles, policy_for_depth
from nanoreview.review.input.targets import (
    extract_review_target,
    infer_review_target_type,
    parse_repo_target,
)

__all__ = [
    "apply_policy_to_roles",
    "extract_review_target",
    "infer_review_target_type",
    "normalize_requested_dimensions",
    "normalize_mode",
    "normalize_review_action",
    "normalize_review_target_type",
    "parse_repo_target",
    "policy_for_depth",
]
