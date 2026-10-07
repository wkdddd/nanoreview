"""Local Git net-change collection for review admission.

A local ``diff`` review always examines the *net* workspace change relative to
``HEAD``: staged, unstaged, and untracked files collapse into one snapshot per
path so a review never sees the same change twice (once as staged, once as
unstaged). Callers receive plain data — no repository handles, no locks.
"""

from __future__ import annotations

import difflib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from nanoreview.review.file_filter import review_file_filter_reason
from nanoreview.review.source.utils import clean_scope_paths, path_matches_scope


class GitUnavailableError(RuntimeError):
    """Raised when Git cannot be queried for a local diff review."""


class GitDiffUnavailableError(GitUnavailableError):
    """Raised when the root is a worktree but its diff cannot be read."""


@dataclass(slots=True)
class NetDiff:
    """Net workspace change relative to ``HEAD``, bounded to one scope."""

    head_sha: str
    patches: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    outside_scope_files: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """Whether the workspace differs from ``HEAD`` at all (any scope)."""
        return bool(self.changed_files) or bool(self.outside_scope_files)

    @property
    def is_empty(self) -> bool:
        """Whether the *selected* scope contributes no reviewable patch."""
        return not self.patches


def find_git_root(path: Path) -> Path | None:
    """Return the enclosing Git worktree root for *path*, or ``None``."""
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def git_head_sha(root: Path) -> str | None:
    """Return the current ``HEAD`` commit sha, or ``None`` when unresolvable."""
    try:
        return _git("rev-parse", "HEAD", cwd=root).strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def _relative_to_root(path: Path, *, worktree: Path, review_root: Path) -> str | None:
    """Map a worktree-relative path into *review_root* coordinates."""
    try:
        return (worktree / path).resolve().relative_to(review_root).as_posix()
    except ValueError:
        return None


def _synthesize_untracked_patch(root: Path, path: str) -> str:
    target = (root / path).resolve()
    try:
        target.relative_to(root)
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return ""
    return "".join(
        difflib.unified_diff(
            [],
            content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def collect_net_diff(
    review_root: Path,
    *,
    scope_paths: list[str] | None = None,
) -> NetDiff:
    """Collect the net change relative to ``HEAD`` for one review scope.

    ``review_root`` is the Git worktree (or a subdirectory of it) that the
    review targets; ``scope_paths`` are paths relative to it. Raises
    :class:`GitUnavailableError` when the root is not inside a Git worktree.
    """
    root = review_root.expanduser().resolve()
    git_root = find_git_root(root)
    if git_root is None:
        raise GitUnavailableError(f"not a git worktree: {root}")

    try:
        head_sha = _git("rev-parse", "HEAD", cwd=git_root).strip()
    except (subprocess.SubprocessError, OSError) as exc:
        raise GitDiffUnavailableError(f"cannot resolve HEAD: {exc}") from exc

    try:
        tracked = set(_git("diff", "--name-only", "HEAD", cwd=git_root).splitlines())
        untracked = set(
            _git("ls-files", "--others", "--exclude-standard", cwd=git_root).splitlines()
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise GitDiffUnavailableError(f"cannot read workspace diff: {exc}") from exc

    scopes = clean_scope_paths(scope_paths)
    diff = NetDiff(head_sha=head_sha)
    worktree = git_root
    for raw_path in sorted(tracked | untracked):
        rel_path = _relative_to_root(raw_path, worktree=worktree, review_root=root)
        if rel_path is None:
            # Change lives outside the review root (e.g. reviewing a subdirectory).
            continue
        diff.changed_files.append(rel_path)
        if scopes and not path_matches_scope(rel_path, scopes):
            diff.outside_scope_files.append(rel_path)
            continue
        reason = review_file_filter_reason(rel_path)
        if reason:
            diff.skipped[rel_path] = reason
            continue
        if raw_path in untracked:
            patch = _synthesize_untracked_patch(worktree, raw_path)
        else:
            try:
                patch = _git(
                    "diff", "--no-ext-diff", "--unified=3", "HEAD", "--", raw_path,
                    cwd=worktree,
                )
            except (subprocess.SubprocessError, OSError):
                diff.skipped[rel_path] = "patch_unavailable"
                continue
        if not patch.strip():
            diff.skipped[rel_path] = "patch_unavailable"
            continue
        filtered = review_file_filter_reason(rel_path, patch)
        if filtered is not None:
            diff.skipped[rel_path] = filtered
            continue
        diff.patches[rel_path] = patch

    logger.info(
        "review.admission.net_diff root={} head={} changed={} scoped={} outside_scope={} skipped={}",
        root,
        head_sha[:12],
        len(diff.changed_files),
        len(diff.patches),
        len(diff.outside_scope_files),
        len(diff.skipped),
    )
    return diff
