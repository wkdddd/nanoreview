"""Pre-plan data normalization for review inputs.

A review input is a GitHub pull request: normalization resolves the selected
reviewers, the review action and the pull request reference, and nothing else.
"""
from __future__ import annotations

from nanoreview.review.input.normalizers import (
    normalize_requested_dimensions,
    normalize_review_action,
    normalize_review_target_type,
)
from nanoreview.review.input.targets import (
    PullRequestReference,
    ReviewTargetError,
    extract_review_target,
    parse_pull_request_reference,
    try_parse_pull_request_reference,
)

__all__ = [
    "PullRequestReference",
    "ReviewTargetError",
    "extract_review_target",
    "normalize_requested_dimensions",
    "normalize_review_action",
    "normalize_review_target_type",
    "parse_pull_request_reference",
    "try_parse_pull_request_reference",
]
