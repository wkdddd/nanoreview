"""Pre-plan data normalization for review inputs.

The review pipeline uses a single unified strategy (all severity levels, AI
judge enabled). There is no longer a quick/full/deep depth distinction.
"""
from __future__ import annotations

from nanoreview.review.input.normalizers import (
    normalize_requested_dimensions,
    normalize_review_action,
    normalize_review_target_type,
)
from nanoreview.review.input.targets import (
    extract_review_target,
    infer_review_target_type,
    parse_repo_target,
)

__all__ = [
    "extract_review_target",
    "infer_review_target_type",
    "normalize_requested_dimensions",
    "normalize_review_action",
    "normalize_review_target_type",
    "parse_repo_target",
]
