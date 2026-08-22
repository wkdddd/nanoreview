"""Shared helpers for code-review evidence collection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from loguru import logger

GITHUB_URL_RE = re.compile(r"(?:https?://)?github\.com/([^/\s]+)/([^/\s.,;!?)#]+)", re.I)
GITHUB_PR_URL_RE = re.compile(
    r"(?:https?://)?github\.com/([^/\s]+)/([^/\s.,;!?)#]+)/pull/(\d+)",
    re.I,
)
GITHUB_SCOPED_URL_RE = re.compile(
    r"(?:https?://)?github\.com/"
    r"(?P<owner>[^/\s]+)/(?P<repo>[^/\s.,;!?)#]+)/"
    r"(?P<kind>blob|tree)/(?P<ref>[^/\s]+)/(?P<path>[^\s`\"'，。；;]+)",
    re.I,
)


@dataclass(frozen=True, slots=True)
class GitHubScopedTarget:
    repo: str
    ref: str
    path: str
    kind: str


def parse_pr_target(target: str | None) -> tuple[str | None, int | None]:
    if not target:
        return None, None
    match = GITHUB_PR_URL_RE.search(target.strip())
    if not match:
        return None, None
    owner, repo, pr_number = match.group(1), match.group(2).removesuffix(".git"), int(match.group(3))
    return f"{owner}/{repo}", pr_number


def parse_github_scoped_target(target: str | None) -> GitHubScopedTarget | None:
    if not target:
        return None
    match = GITHUB_SCOPED_URL_RE.search(target.strip())
    if not match:
        return None
    repo = f"{match.group('owner')}/{match.group('repo').removesuffix('.git')}"
    path = unquote(match.group("path")).rstrip(".,;:!?)）】]")
    return GitHubScopedTarget(
        repo=repo,
        ref=unquote(match.group("ref")),
        path=path.strip("/"),
        kind=match.group("kind").lower(),
    )


def parse_repo(repo: str) -> tuple[str, str]:
    repo = repo.strip().rstrip("/")
    match = GITHUB_URL_RE.search(repo)
    if match:
        return match.group(1), match.group(2).removesuffix(".git")
    parts = repo.split("/")
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1].removesuffix(".git")
    raise ValueError(f"Cannot parse GitHub repo: '{repo}'. Use 'owner/repo' or a GitHub URL.")


def clean_scope_paths(paths: list[str] | None, *, remote: bool = False) -> list[str]:
    cleaned: list[str] = []
    for path in paths or []:
        if not isinstance(path, str):
            continue
        value = path.strip().replace("\\", "/")
        if remote:
            value = value.lstrip("/")
        value = value.rstrip("/")
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def path_matches_scope(path: str, scopes: list[str]) -> bool:
    if not scopes:
        return True
    normalized = path.strip().replace("\\", "/").lstrip("/")
    return any(normalized == scope or normalized.startswith(f"{scope}/") for scope in scopes)


def changed_lines_from_patch(filename: str, patch: str) -> list[int]:
    if not patch:
        return []
    try:
        from unidiff import PatchSet  # type: ignore

        parsed = PatchSet(f"diff --git a/{filename} b/{filename}\n--- a/{filename}\n+++ b/{filename}\n{patch}")
        lines: list[int] = []
        for patched_file in parsed:
            for hunk in patched_file:
                for line in hunk:
                    if line.is_added and line.target_line_no is not None:
                        lines.append(int(line.target_line_no))
        return sorted(set(lines))
    except Exception as exc:
        logger.debug("review.patch.unidiff_fallback filename={} reason={}", filename, exc)

    lines = []
    current = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            current = int(match.group(1)) if match else current
            continue
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(current)
            current += 1
        elif not line.startswith("-"):
            current += 1
    return sorted(set(line for line in lines if line > 0))
