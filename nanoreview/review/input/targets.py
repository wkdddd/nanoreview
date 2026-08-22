"""Extract and normalize review targets before plan construction."""
from __future__ import annotations

import re

from nanoreview.review.source.utils import (
    GITHUB_PR_URL_RE,
    GITHUB_SCOPED_URL_RE,
    GITHUB_URL_RE,
    parse_repo,
)


def infer_review_target_type(target: str | None) -> str | None:
    if not target:
        return None
    if GITHUB_URL_RE.search(target):
        return "github"
    return "local"


def extract_review_target(text: str) -> tuple[str, str] | None:
    scoped_match = GITHUB_SCOPED_URL_RE.search(text)
    if scoped_match:
        target = scoped_match.group(0).rstrip(".,;:!?)）】]")
        repo = f"{scoped_match.group('owner')}/{scoped_match.group('repo').removesuffix('.git')}"
        return target, repo

    github_match = GITHUB_URL_RE.search(text)
    if github_match:
        owner, repo = github_match.group(1), github_match.group(2).removesuffix(".git")
        target = f"https://github.com/{owner}/{repo}"
        if pr_match := GITHUB_PR_URL_RE.search(text):
            target = f"https://github.com/{pr_match.group(1)}/{pr_match.group(2)}/pull/{pr_match.group(3)}"
        return target, f"{owner}/{repo}"

    local_match = re.search(r"(?i)(?:review|code\s*review|审查|评审)\s+([^\s，。；;\r\n]+)", text)
    if local_match:
        target = local_match.group(1).strip(" `\"'")
        if target:
            return target, target
    path_match = re.search(
        r"(?P<path>(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|~[\\/]|/)[^\s`\"'，。；;]+)",
        text,
    )
    if path_match:
        target = path_match.group("path").rstrip(".,;:!?)）】]")
        if target:
            return target, target
    stripped = text.strip().strip(" `\"'")
    if stripped and not re.search(r"\s", stripped):
        if re.match(r"^[A-Za-z]:[\\/]", stripped) or stripped.startswith(("/", "./", "../", "~")):
            return stripped, stripped
    return None


def parse_repo_target(target: str | None) -> str | None:
    if not target:
        return None
    try:
        owner, repo = parse_repo(target)
    except ValueError:
        return None
    return f"{owner}/{repo}"
