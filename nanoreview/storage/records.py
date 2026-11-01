"""Immutable records exchanged between storage and the review domain.

ORM rows never leave this package: repositories copy what they read into the
dataclasses below, so a closed ``AsyncSession`` cannot leak into agent code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class RepositoryRecord:
    id: int
    provider: str
    owner: str
    name: str
    url: str
    default_branch: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class PullRequestRecord:
    id: int
    repository_id: int
    number: int
    title: str
    state: str
    draft: bool
    author: str | None
    base_ref: str
    head_ref: str
    head_sha: str | None
    base_sha: str | None
    html_url: str | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChangedFile:
    """One entry of a PR file listing at a pinned SHA pair."""

    path: str
    old_path: str | None
    status: str
    additions: int = 0
    deletions: int = 0
    patch: str | None = None
    patch_available: bool = True

    def as_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
            "patch": self.patch,
            "patch_available": self.patch_available,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ChangedFile":
        return cls(
            path=str(payload.get("path") or ""),
            old_path=payload.get("old_path"),
            status=str(payload.get("status") or "modified"),
            additions=int(payload.get("additions") or 0),
            deletions=int(payload.get("deletions") or 0),
            patch=payload.get("patch"),
            patch_available=bool(payload.get("patch_available", True)),
        )


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    """Frozen input for one PR review run.

    ``base_sha``/``head_sha``/``merge_base_sha`` pin every read: the old side
    is always read at the merge base and the new side at the head commit, so a
    later push to the PR cannot change what was reviewed.
    """

    id: str
    run_id: str
    repository_id: int
    pr_number: int
    base_repo: str
    head_repo: str
    base_sha: str
    head_sha: str
    merge_base_sha: str
    metadata: dict[str, Any] = field(default_factory=dict)
    changed_files: tuple[ChangedFile, ...] = ()
    input_fingerprint: str = ""
    gaps: tuple[str, ...] = ()
    created_at: datetime | None = None
    schema_version: int = 1

    @property
    def patch_map(self) -> dict[str, str]:
        """Ordered ``path -> unified diff`` mapping for the preprocessor."""
        return {
            item.path: item.patch
            for item in self.changed_files
            if item.patch
        }

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.changed_files)

    def file_for(self, path: str) -> ChangedFile | None:
        for item in self.changed_files:
            if item.path == path or item.old_path == path:
                return item
        return None


@dataclass(frozen=True, slots=True)
class ReviewRunRecord:
    id: str
    session_id: str
    repository_id: int
    pull_request_id: int
    status: str
    phase: str
    request_id: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    merge_base_sha: str | None = None
    snapshot_id: str | None = None
    error: str | None = None
    summary: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReviewReportRecord:
    run_id: str
    markdown: str
    findings: tuple[dict[str, Any], ...]
    coverage: dict[str, Any]
    created_at: datetime | None = None


__all__ = [
    "ChangedFile",
    "PullRequestRecord",
    "RepositoryRecord",
    "ReviewReportRecord",
    "ReviewRunRecord",
    "ReviewSnapshot",
]
