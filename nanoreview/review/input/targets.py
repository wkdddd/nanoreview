"""Parse the pull request a review turn refers to.

A review target is a GitHub pull request. Nothing in this module resolves a
local path, a branch or a repository snapshot: it only turns the target text
into an ``owner/name#number`` reference that admission can resolve through the
GitHub API.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PR_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)"
    r"/pull/(?P<number>\d+)/?$",
    re.IGNORECASE,
)
_PR_SLUG_RE = re.compile(
    r"^(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?#(?P<number>\d+)$",
    re.IGNORECASE,
)


class ReviewTargetError(ValueError):
    """The target text is not a pull request reference."""


@dataclass(frozen=True, slots=True)
class PullRequestReference:
    owner: str
    name: str
    number: int

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.name}"

    def __str__(self) -> str:
        return f"{self.repository}#{self.number}"


def parse_pull_request_reference(value: str) -> PullRequestReference:
    """Accept a PR URL or ``owner/name#number``; reject everything else."""
    text = (value or "").strip()
    match = _PR_URL_RE.match(text) or _PR_SLUG_RE.match(text)
    if match is None:
        raise ReviewTargetError(
            "A review target must be a GitHub pull request URL "
            "(https://github.com/owner/name/pull/123) or owner/name#123."
        )
    return PullRequestReference(
        owner=match.group("owner"),
        name=match.group("name"),
        number=int(match.group("number")),
    )


def try_parse_pull_request_reference(value: str) -> PullRequestReference | None:
    try:
        return parse_pull_request_reference(value)
    except ReviewTargetError:
        return None


def extract_review_target(text: str) -> tuple[str, str] | None:
    """Find a pull request reference inside free text, if the text holds one."""
    match = re.search(
        r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+/pull/\d+",
        text or "",
        re.IGNORECASE,
    )
    if match is None:
        return None
    target = match.group(0)
    return target, target


__all__ = [
    "PullRequestReference",
    "ReviewTargetError",
    "extract_review_target",
    "parse_pull_request_reference",
    "try_parse_pull_request_reference",
]
