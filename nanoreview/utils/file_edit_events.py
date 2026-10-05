"""File-edit activity tracking for the Conversation Agent.

Only ``write_file`` and ``edit_file`` are tracked, matching the tools the
Conversation Agent registers. Path resolution reuses each tool's existing
workspace validation so tracked activity can never diverge from what the tool
actually allowed.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRACKED_FILE_EDIT_TOOLS = frozenset({"write_file", "edit_file"})

# Above this size a unified diff is skipped: the payload goes straight to the
# WebUI, and huge diffs stall rendering more than they inform.
MAX_DIFF_CHARS = 200_000


@dataclass
class FileEditTracker:
    """One in-flight file edit, from ``before_execute_tool`` to its terminal event."""

    call_id: str
    tool: str
    path: str
    absolute_path: Path
    existed_before: bool
    before_text: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _line_stats(before: str | None, after: str | None) -> tuple[int, int]:
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines)
    added = deleted = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            deleted += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, deleted


def _unified_diff(tracker: FileEditTracker, after_text: str | None) -> str | None:
    if after_text is None:
        return None
    before_text = tracker.before_text or ""
    diff = "\n".join(
        difflib.unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile=f"a/{tracker.path}",
            tofile=f"b/{tracker.path}",
            lineterm="",
        )
    )
    if len(diff) > MAX_DIFF_CHARS:
        return None
    return diff or None


def prepare_file_edit_trackers(
    *,
    call_id: str,
    tool_name: str,
    tool: Any,
    params: dict[str, Any],
) -> list[FileEditTracker]:
    """Build trackers for one tool call, or ``[]`` when it edits nothing."""
    if tool_name not in TRACKED_FILE_EDIT_TOOLS:
        return []
    raw_path = params.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return []
    resolve = getattr(tool, "_resolve", None)
    if not callable(resolve):
        return []
    try:
        absolute_path = resolve(raw_path)
    except Exception:
        # Path validation already failed; the tool call reports the error and
        # the per-tool error hook emits the matching failure event.
        return []
    before_text = _read_text(absolute_path) if absolute_path.is_file() else None
    return [
        FileEditTracker(
            call_id=call_id,
            tool=tool_name,
            path=raw_path,
            absolute_path=absolute_path,
            existed_before=before_text is not None,
            before_text=before_text,
        )
    ]


def build_file_edit_start_event(tracker: FileEditTracker) -> dict[str, Any]:
    return {
        "version": 1,
        "call_id": tracker.call_id,
        "tool": tracker.tool,
        "path": tracker.path,
        "absolute_path": str(tracker.absolute_path),
        "phase": "start",
    }


def build_file_edit_end_event(
    tracker: FileEditTracker, result: Any
) -> dict[str, Any]:
    after_text = _read_text(tracker.absolute_path) if tracker.absolute_path.is_file() else None
    added, deleted = _line_stats(tracker.before_text, after_text)
    payload: dict[str, Any] = {
        "version": 1,
        "call_id": tracker.call_id,
        "tool": tracker.tool,
        "path": tracker.path,
        "absolute_path": str(tracker.absolute_path),
        "phase": "end",
        "added": added,
        "deleted": deleted,
        "status": "ok" if not _is_error_result(result) else "error",
    }
    if after_text is None:
        payload["operation"] = "delete"
    else:
        diff = _unified_diff(tracker, after_text)
        if diff:
            payload["diff"] = diff
    if _is_error_result(result):
        payload["error"] = _error_text(result)
    return payload


def build_file_edit_error_event(tracker: FileEditTracker, error: str) -> dict[str, Any]:
    return {
        "version": 1,
        "call_id": tracker.call_id,
        "tool": tracker.tool,
        "path": tracker.path,
        "absolute_path": str(tracker.absolute_path),
        "phase": "error",
        "status": "error",
        "error": error,
    }


def _is_error_result(result: Any) -> bool:
    """NanoReview tools report failures as an ``Error: ...`` string result."""
    return isinstance(result, str) and result.startswith("Error")


def _error_text(result: Any) -> str:
    return result if isinstance(result, str) else str(result)
