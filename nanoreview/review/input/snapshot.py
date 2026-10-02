"""Immutable input snapshot for one admitted review run.

A review executes against the snapshot captured at admission time, so later
edits in the workspace cannot retroactively change what was reviewed. The
snapshot is a JSON artifact stored under ``review-snapshots/`` in the
workspace and referenced from session metadata via ``review_snapshot_ref``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.review.file_filter import review_file_filter_reason
from nanoreview.utils.helpers import safe_filename

# Directory (relative to the workspace) that stores review input snapshots.
REVIEW_SNAPSHOTS_DIR_NAME = "review-snapshots"

# Serialized snapshot size limit, mirroring the report artifact guard.
REVIEW_SNAPSHOT_MAX_BYTES = 10 * 1024 * 1024

# Inline repository content budget for ``repo`` reviews. Diff reviews rely on
# the captured patches instead and keep this budget unused.
REPO_CONTENT_MAX_BYTES = 512 * 1024
REPO_CONTENT_MAX_FILES = 200
REPO_CONTENT_MAX_FILE_BYTES = 128 * 1024

# run_id charset: keeps snapshot filenames filesystem-safe on every platform.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ReviewSnapshotError(RuntimeError):
    """Raised when a snapshot cannot be captured or persisted."""


@dataclass(slots=True)
class RepoContent:
    """Bounded inline file contents captured for a ``repo`` review."""

    files: dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    skipped: dict[str, str] = field(default_factory=dict)


def _resolve_within(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def collect_repo_content(
    review_root: Path,
    *,
    scope_paths: list[str] | None = None,
    max_bytes: int = REPO_CONTENT_MAX_BYTES,
    max_files: int = REPO_CONTENT_MAX_FILES,
) -> RepoContent:
    """Capture text file contents for a repository review, bounded by budget.

    Only files inside *review_root* are read; oversized or undecodable files
    are recorded as skipped rather than silently dropped. When the byte or
    file budget is exhausted the result is marked ``truncated`` so downstream
    consumers can state the limitation instead of pretending full coverage.
    """
    root = review_root.expanduser().resolve()
    scopes = [path.strip().replace("\\", "/").rstrip("/") for path in (scope_paths or []) if path]
    result = RepoContent()
    used = 0

    candidates: list[Path] = []
    for rel in scopes or ["."]:
        target = _resolve_within(root, rel) if rel != "." else root
        if target is None:
            result.skipped[rel] = "outside_review_root"
            continue
        if target.is_file():
            candidates.append(target)
        elif target.is_dir():
            candidates.extend(
                path for path in sorted(target.rglob("*")) if path.is_file()
            )

    for path in dict.fromkeys(candidates):
        if len(result.files) >= max_files:
            result.truncated = True
            break
        try:
            rel_path = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        reason = review_file_filter_reason(rel_path)
        if reason:
            result.skipped[rel_path] = reason
            continue
        try:
            size = path.stat().st_size
        except OSError:
            result.skipped[rel_path] = "unreadable_file"
            continue
        if size > REPO_CONTENT_MAX_FILE_BYTES or used + size > max_bytes:
            result.truncated = True
            result.skipped[rel_path] = "budget_exceeded"
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            result.skipped[rel_path] = "unreadable_file"
            continue
        result.files[rel_path] = text
        used += size

    logger.info(
        "review.snapshot.repo_content root={} files={} bytes={} truncated={} skipped={}",
        root,
        len(result.files),
        used,
        result.truncated,
        len(result.skipped),
    )
    return result


def build_snapshot(
    *,
    run_id: str,
    session_key: str,
    action: str,
    target_type: str,
    target: str,
    input_fingerprint: str,
    local_scope: dict[str, Any] | None = None,
    git_head: str | None = None,
    net_diff: dict[str, str] | None = None,
    changed_files: list[str] | None = None,
    scope_files: list[str] | None = None,
    repo_content: RepoContent | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the JSON snapshot payload for one admitted review run."""
    return {
        "run_id": run_id,
        "session_key": session_key,
        "action": action,
        "target_type": target_type,
        "target": target,
        "input_fingerprint": input_fingerprint,
        "local_scope": local_scope or {},
        "git_head": git_head,
        "net_diff": net_diff or {},
        "changed_files": list(changed_files or []),
        "scope_files": list(scope_files or []),
        "repo_content": (repo_content.files if repo_content else {}),
        "repo_content_truncated": bool(repo_content.truncated) if repo_content else False,
        "repo_content_skipped": (repo_content.skipped if repo_content else {}),
        "extra": extra_metadata or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


class ReviewSnapshotStore:
    """Persist review input snapshots inside the workspace."""

    def __init__(self, workspace: Path, *, max_bytes: int = REVIEW_SNAPSHOT_MAX_BYTES) -> None:
        self._workspace = Path(workspace)
        self._max_bytes = max_bytes

    @property
    def directory(self) -> Path:
        return self._workspace / REVIEW_SNAPSHOTS_DIR_NAME

    def path_for(self, run_id: str) -> Path:
        if not _RUN_ID_RE.match(str(run_id or "")):
            raise ReviewSnapshotError("invalid run id")
        return self.directory / f"{safe_filename(run_id)}.json"

    def reference_for(self, run_id: str) -> str:
        return f"{REVIEW_SNAPSHOTS_DIR_NAME}/{safe_filename(run_id)}.json"

    def write(self, snapshot: dict[str, Any]) -> str:
        """Atomically persist *snapshot* and return its wire reference.

        Raises :class:`ReviewSnapshotError` on serialization failure or size
        limit — admission must fail loudly rather than admit a run whose
        frozen input was never stored.
        """
        run_id = str(snapshot.get("run_id") or "")
        if not _RUN_ID_RE.match(run_id):
            raise ReviewSnapshotError("snapshot is missing a valid run id")
        try:
            payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise ReviewSnapshotError(f"snapshot is not serializable: {exc}") from exc
        size = len(payload.encode("utf-8"))
        if size > self._max_bytes:
            raise ReviewSnapshotError(
                f"snapshot exceeds size limit ({size} > {self._max_bytes} bytes)"
            )
        path = self.path_for(run_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        except OSError as exc:
            raise ReviewSnapshotError(f"cannot persist snapshot: {exc}") from exc
        logger.info(
            "review.snapshot.written run_id={} ref={} bytes={}",
            run_id,
            self.reference_for(run_id),
            size,
        )
        return self.reference_for(run_id)
