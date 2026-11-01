"""Prepare and freeze the review input for one pull request.

The snapshot is the authoritative review input: base/head repository, PR
number, and the three commit SHAs (base, head, merge base) that every later
read is pinned to. Files are listed through the GitHub API at those SHAs; no
clone and no working tree are involved.

Version safety: the PR is re-read after its file list is fetched. If the head
moved in between, the snapshot is rebuilt once; a second change fails the run
rather than mixing two versions.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from nanoreview.review.input.github import (
    GitHubClient,
    GitHubError,
    GitHubFileContent,
    GitHubPullRequest,
    GitHubPullRequestFile,
)
from nanoreview.storage.records import ChangedFile, ReviewSnapshot

#: GitHub's hard cap on ``GET /pulls/{n}/files``.
_GITHUB_FILE_LIST_CAP = 3000


class SnapshotPreparationError(RuntimeError):
    """The PR could not be frozen into a consistent snapshot."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class PrTarget:
    owner: str
    name: str
    number: int


def compute_pr_fingerprint(
    *,
    repository: str,
    number: int,
    base_sha: str,
    head_sha: str,
    merge_base_sha: str,
    files: list[ChangedFile],
) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "repository": repository,
                "number": number,
                "base_sha": base_sha,
                "head_sha": head_sha,
                "merge_base_sha": merge_base_sha,
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    for item in files:
        digest.update(f"\0{item.path}\0{item.status}".encode("utf-8"))
        if item.patch:
            digest.update(hashlib.sha256(item.patch.encode("utf-8")).digest())
    return digest.hexdigest()


def rebuild_patch(
    old_text: str, new_text: str, *, old_path: str, new_path: str
) -> str:
    """Reconstruct a unified diff from two pinned texts.

    Used when GitHub omits ``patch`` (binary detection, size limits). The
    result is a normal unified diff so the preprocessor's hunk parsing works
    unchanged.
    """
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{old_path}",
            tofile=f"b/{new_path}",
            n=3,
        )
    )


class PullRequestSnapshotService:
    """Turn a PR reference into a frozen :class:`ReviewSnapshot`."""

    def __init__(self, github: GitHubClient) -> None:
        self._github = github

    async def prepare(
        self,
        *,
        run_id: str,
        owner: str,
        name: str,
        number: int,
        base_repo: str | None = None,
    ) -> ReviewSnapshot:
        pull = await self._github.get_pull_request(owner, name, number)
        base_owner, base_name = _split_repo(base_repo or pull.base_repo, owner, name)
        head_owner, head_name = _split_repo(pull.head_repo, owner, name)

        files, merge_base_sha, pull = await self._freeze(
            pull, base_owner, base_name, head_owner, head_name
        )
        gaps: list[str] = []
        changed: list[ChangedFile] = []
        for item in files:
            patch, gap = await self._patch_for(
                item,
                base_owner=base_owner,
                base_name=base_name,
                head_owner=head_owner,
                head_name=head_name,
                merge_base_sha=merge_base_sha,
                head_sha=pull.head_sha,
            )
            if gap:
                gaps.append(gap)
            changed.append(
                ChangedFile(
                    path=item.path,
                    old_path=item.previous_path,
                    status=item.status,
                    additions=item.additions,
                    deletions=item.deletions,
                    patch=patch,
                    patch_available=patch is not None,
                )
            )
        if len(files) >= _GITHUB_FILE_LIST_CAP:
            gaps.append(
                f"file_list_truncated: GitHub reports at most {_GITHUB_FILE_LIST_CAP} files per PR"
            )

        snapshot = ReviewSnapshot(
            id=f"snap_{uuid.uuid4().hex[:16]}",
            run_id=run_id,
            repository_id=0,
            pr_number=pull.number,
            base_repo=f"{base_owner}/{base_name}",
            head_repo=f"{head_owner}/{head_name}",
            base_sha=pull.base_sha,
            head_sha=pull.head_sha,
            merge_base_sha=merge_base_sha,
            metadata={
                "title": pull.title,
                "state": pull.state,
                "draft": pull.draft,
                "author": pull.author,
                "base_ref": pull.base_ref,
                "head_ref": pull.head_ref,
                "html_url": pull.html_url,
                "updated_at": pull.updated_at.isoformat() if pull.updated_at else None,
                "file_count": len(changed),
            },
            changed_files=tuple(changed),
            gaps=tuple(gaps),
            created_at=datetime.now(timezone.utc),
        )
        fingerprint = compute_pr_fingerprint(
            repository=f"{base_owner}/{base_name}",
            number=pull.number,
            base_sha=snapshot.base_sha,
            head_sha=snapshot.head_sha,
            merge_base_sha=snapshot.merge_base_sha,
            files=list(snapshot.changed_files),
        )
        snapshot = ReviewSnapshot(
            **{**_snapshot_fields(snapshot), "input_fingerprint": fingerprint}
        )
        logger.info(
            "review.snapshot.prepared run_id={} pr={}/{}#{} files={} gaps={}",
            run_id,
            base_owner,
            base_name,
            pull.number,
            len(changed),
            len(gaps),
        )
        return snapshot

    async def _freeze(
        self,
        pull: GitHubPullRequest,
        base_owner: str,
        base_name: str,
        head_owner: str,
        head_name: str,
    ) -> tuple[list[GitHubPullRequestFile], str, GitHubPullRequest]:
        """List files and re-check the head SHA; retry once when it moved."""
        for attempt in (1, 2):
            files = await self._github.list_pull_request_files(
                base_owner, base_name, pull.number
            )
            current = await self._github.get_pull_request(
                base_owner, base_name, pull.number
            )
            if current.head_sha == pull.head_sha:
                merge_base = await self._github.get_merge_base(
                    base_owner, base_name, current.base_sha, current.head_sha
                )
                return files, merge_base, current
            logger.warning(
                "review.snapshot.head_moved attempt={} before={} after={}",
                attempt,
                pull.head_sha,
                current.head_sha,
            )
            pull = current
        raise SnapshotPreparationError(
            "pull_request_changed",
            "The pull request changed while its snapshot was being prepared; retry the review.",
        )

    async def _patch_for(
        self,
        item: GitHubPullRequestFile,
        *,
        base_owner: str,
        base_name: str,
        head_owner: str,
        head_name: str,
        merge_base_sha: str,
        head_sha: str,
    ) -> tuple[str | None, str | None]:
        """Return ``(patch, gap)``; missing patches are rebuilt at pinned SHAs."""
        if item.patch:
            return item.patch, None
        if item.status == "removed":
            old_path = item.previous_path or item.path
            old = await self._read(base_owner, base_name, old_path, merge_base_sha)
            if old.text is None:
                return None, f"patch_missing: {item.path} (old side unavailable)"
            rebuilt = rebuild_patch(old.text, "", old_path=old_path, new_path=item.path)
            return rebuilt, None
        old_path = item.previous_path or item.path
        old = await self._read(base_owner, base_name, old_path, merge_base_sha)
        new = await self._read(head_owner, head_name, item.path, head_sha)
        if old.text is None and item.status != "added":
            return None, f"patch_missing: {item.path} (old side unavailable)"
        if new.text is None:
            return None, f"patch_missing: {item.path} (new side unavailable)"
        rebuilt = rebuild_patch(
            old.text or "", new.text, old_path=old_path, new_path=item.path
        )
        return rebuilt, None

    async def _read(self, owner: str, name: str, path: str, ref: str) -> GitHubFileContent:
        try:
            return await self._github.get_file(owner, name, path, ref)
        except GitHubError as exc:
            logger.warning(
                "review.snapshot.read_failed repo={}/{} path={} ref={} error={}",
                owner,
                name,
                path,
                ref,
                exc.code,
            )
            return GitHubFileContent(path=path, sha=None, text=None)


def _split_repo(value: str | None, default_owner: str, default_name: str) -> tuple[str, str]:
    if not value or "/" not in value:
        return default_owner, default_name
    owner, name = value.split("/", 1)
    return owner, name


def _snapshot_fields(snapshot: ReviewSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "run_id": snapshot.run_id,
        "repository_id": snapshot.repository_id,
        "pr_number": snapshot.pr_number,
        "base_repo": snapshot.base_repo,
        "head_repo": snapshot.head_repo,
        "base_sha": snapshot.base_sha,
        "head_sha": snapshot.head_sha,
        "merge_base_sha": snapshot.merge_base_sha,
        "metadata": snapshot.metadata,
        "changed_files": snapshot.changed_files,
        "input_fingerprint": snapshot.input_fingerprint,
        "gaps": snapshot.gaps,
        "created_at": snapshot.created_at,
        "schema_version": snapshot.schema_version,
    }


__all__ = [
    "PrTarget",
    "PullRequestSnapshotService",
    "SnapshotPreparationError",
    "compute_pr_fingerprint",
    "rebuild_patch",
]
