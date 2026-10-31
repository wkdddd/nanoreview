"""Search tool: local ``grep``.

The matching/pagination core lives in :mod:`nanoreview.agent.tools.search_core`
and is shared with the remote PR reader, so identical text yields identical
pages regardless of where the bytes came from. This module owns only the local
concerns: path resolution, workspace scope, directory traversal, read budgets,
binary detection, and the reviewer-run dedup ledger.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable

from nanoreview.agent.tools.base import ToolResult
from nanoreview.agent.tools.filesystem import ListDirTool, _FsTool
from nanoreview.agent.tools.search_core import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_HEAD_LIMIT,
    DEFAULT_LINE_LIMIT,
    DEFAULT_MAX_RESULT_CHARS,
    ContentPage,
    MatchConfig,
    MatchTooLargeError,
    SearchBudget,
    SearchBudgetExceededError,
    SearchCancelledError,
    SearchLine,
    count_matches,
    match_glob,
    matches_type,
    text_page,
)

_DEFAULT_MAX_FILE_BYTES = 2_000_000
_DEFAULT_MAX_SCAN_PATHS = 500_000
_DEFAULT_MAX_SCAN_SECONDS = 30.0
_VALID_OUTPUT_MODES = {"content", "files_with_matches", "count"}


def _is_binary(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    sample = raw[:4096]
    if not sample:
        return False
    non_text = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return (non_text / len(sample)) > 0.2


def _grep_signature(*, config: MatchConfig, target: Path, glob: str | None, file_type: str | None) -> str:
    """Stable identity for one resolved grep request.

    Two calls share the identity only when every parameter that can change the
    returned text matches (including the resolved target path and the
    normalized limit/offset). A different range or file filter therefore never
    collides.
    """
    return repr(
        (
            config.pattern,
            str(target),
            glob or "",
            (file_type or "").lower(),
            bool(config.case_insensitive),
            bool(config.fixed_strings),
            config.output_mode,
            int(config.context_before),
            int(config.context_after),
            config.head_limit,
            int(config.offset),
        )
    )


class _SearchTool(_FsTool):
    _IGNORE_DIRS = set(ListDirTool._IGNORE_DIRS)

    def _display_path(self, target: Path, root: Path) -> str:
        if self._workspace:
            with suppress(ValueError):
                return target.relative_to(self._workspace).as_posix()
        return target.relative_to(root).as_posix()

    def _iter_files(self, root: Path, budget: SearchBudget) -> Iterable[Path]:
        if root.is_file():
            budget.visit_path()
            yield root
            return

        for dirpath, dirnames, filenames in os.walk(root):
            budget.checkpoint()
            for _ in dirnames:
                budget.visit_path()
            dirnames[:] = sorted(d for d in dirnames if d not in self._IGNORE_DIRS)
            current = Path(dirpath)
            for filename in sorted(filenames):
                budget.visit_path()
                yield current / filename


class GrepTool(_SearchTool):
    """Search local file contents via the shared plain-text matcher."""

    _scopes = {
        "core", "subagent",
        "reviewer.bug", "reviewer.security",
        "reviewer.performance", "reviewer.maintainability",
        "reviewer.general",
    }

    _MAX_RESULT_CHARS = DEFAULT_MAX_RESULT_CHARS
    _MAX_RENDERED_LINE_CHARS = DEFAULT_LINE_LIMIT
    _MAX_FILE_BYTES = _DEFAULT_MAX_FILE_BYTES
    _MAX_SCAN_PATHS = _DEFAULT_MAX_SCAN_PATHS
    _MAX_SCAN_SECONDS = _DEFAULT_MAX_SCAN_SECONDS

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents. Returns matches with five context lines by default. "
            "fixed_strings=true treats the pattern literally. "
            "Skips binary files and files over 2 MB; supports glob/type filters."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex, or literal text when fixed_strings=true",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": "Search root (default '.')",
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Root-relative path glob, e.g. 'src/**/*.{ts,tsx}'; "
                        "a bare '*.py' matches at any depth"
                    ),
                },
                "type": {
                    "type": "string",
                    "description": "File type, e.g. 'py', 'ts', 'md', or 'json'",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Ignore case (default false)",
                },
                "fixed_strings": {
                    "type": "boolean",
                    "description": "Treat pattern literally (default false)",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        "content: matches with context (default); "
                        "files_with_matches: paths; count: matches per file"
                    ),
                },
                "context_before": {
                    "type": "integer",
                    "description": "Context lines before a match (default 5)",
                    "minimum": 0,
                    "maximum": 20,
                },
                "context_after": {
                    "type": "integer",
                    "description": "Context lines after a match (default 5)",
                    "minimum": 0,
                    "maximum": 20,
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Maximum matches or file entries (default 250; 0 for all)",
                    "minimum": 0,
                    "maximum": 1000,
                },
                "offset": {
                    "type": "integer",
                    "description": "Matches or file entries to skip before head_limit",
                    "minimum": 0,
                    "maximum": 100000,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        type: str | None = None,
        case_insensitive: bool = False,
        fixed_strings: bool = False,
        output_mode: str = "content",
        context_before: int = DEFAULT_CONTEXT_LINES,
        context_after: int = DEFAULT_CONTEXT_LINES,
        head_limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> Any:
        cancelled = threading.Event()
        try:
            return await asyncio.to_thread(
                self._execute_sync,
                pattern=pattern,
                path=path,
                glob=glob,
                file_type=type,
                case_insensitive=case_insensitive,
                fixed_strings=fixed_strings,
                output_mode=output_mode,
                context_before=context_before,
                context_after=context_after,
                head_limit=head_limit,
                offset=offset,
                cancelled=cancelled,
            )
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def _execute_sync(
        self,
        *,
        pattern: str,
        path: str,
        glob: str | None,
        file_type: str | None,
        case_insensitive: bool,
        fixed_strings: bool,
        output_mode: str,
        context_before: int,
        context_after: int,
        head_limit: int | None,
        offset: int,
        cancelled: threading.Event,
    ) -> Any:
        budget = SearchBudget(
            cancelled=cancelled,
            deadline=time.monotonic() + self._MAX_SCAN_SECONDS,
            max_paths=self._MAX_SCAN_PATHS,
        )
        try:
            budget.checkpoint()
            if output_mode not in _VALID_OUTPUT_MODES:
                return ToolResult.error(
                    f"Error: invalid output_mode {output_mode!r}; "
                    f"use one of {sorted(_VALID_OUTPUT_MODES)}"
                )
            if glob:
                try:
                    match_glob("", "", glob)
                except ValueError as exc:
                    return ToolResult.error(f"Error: {exc}")

            target = self._resolve(path or ".")
            if not target.exists():
                return ToolResult.error(f"Error: Path not found: {path}")
            if not (target.is_dir() or target.is_file()):
                return ToolResult.error(f"Error: Unsupported path: {path}")

            limit = None if head_limit == 0 else (DEFAULT_HEAD_LIMIT if head_limit is None else head_limit)
            config = MatchConfig(
                pattern=pattern,
                case_insensitive=case_insensitive,
                fixed_strings=fixed_strings,
                output_mode=output_mode,
                context_before=max(0, context_before),
                context_after=max(0, context_after),
                head_limit=limit,
                offset=max(0, offset),
                max_chars=self._MAX_RESULT_CHARS,
                line_limit=self._MAX_RENDERED_LINE_CHARS,
            )
            try:
                regex = config.compile()
            except re.error as e:
                return ToolResult.error(f"Error: invalid regex pattern: {e}")

            review_ledger = self._file_states.review_ledger
            search_signature = (
                _grep_signature(config=config, target=target, glob=glob, file_type=file_type)
                if review_ledger is not None
                else ""
            )

            page = ContentPage(
                limit=config.head_limit,
                offset=config.offset,
                max_chars=config.max_chars,
                line_limit=config.line_limit,
            )
            file_mtimes: dict[str, float] = {}
            counts: dict[str, int] = {}
            skipped_binary = 0
            skipped_large = 0
            root = target if target.is_dir() else target.parent

            for file_path in self._iter_files(target, budget):
                rel_path = file_path.relative_to(root).as_posix()
                if glob and not match_glob(rel_path, file_path.name, glob):
                    continue
                if not matches_type(file_path.name, file_type):
                    continue
                display_path = self._display_path(file_path, root)

                try:
                    stat = file_path.stat()
                except OSError:
                    skipped_binary += 1
                    continue
                if stat.st_size > self._MAX_FILE_BYTES:
                    skipped_large += 1
                    continue
                try:
                    raw = file_path.read_bytes()
                except OSError:
                    skipped_binary += 1
                    continue
                if _is_binary(raw):
                    skipped_binary += 1
                    continue
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    skipped_binary += 1
                    continue

                lines = (
                    SearchLine(number, text)
                    for number, text in enumerate(content.splitlines(), start=1)
                )
                checked = self._checked(lines, budget)
                file_had_match = False
                if output_mode == "content":
                    page.scan(display_path, checked, regex, config.context_before, config.context_after)
                    file_had_match = page.returned > 0
                elif output_mode == "count":
                    count = count_matches(checked, regex)
                    if count:
                        counts[display_path] = count
                        file_mtimes[display_path] = stat.st_mtime
                        file_had_match = True
                else:
                    file_had_match = count_matches(checked, regex, stop_after=1) > 0
                    if file_had_match:
                        file_mtimes[display_path] = stat.st_mtime

                if page.stopped:
                    break

            no_matches = f"No matches found for pattern '{pattern}' in {path}"
            notes: list[str] = []
            if output_mode == "content":
                result, note = page.render(no_matches)
                if note:
                    notes.append(note)
            else:
                ordered_files = sorted(
                    file_mtimes, key=lambda name: (-file_mtimes.get(name, 0.0), name)
                )
                entries = (
                    [f"{name}: {counts[name]}" for name in ordered_files]
                    if output_mode == "count"
                    else ordered_files
                )
                paged, truncated = text_page(entries, limit, offset, self._MAX_RESULT_CHARS)
                if not entries:
                    result = no_matches
                else:
                    result = "\n".join(paged)
                    if truncated:
                        notes.append(
                            f"(pagination: limit={limit}, offset={offset}; "
                            f"use offset={offset + len(paged)} to continue)"
                        )
                    elif offset > 0:
                        notes.append(f"(pagination: offset={offset})")

            if skipped_binary:
                notes.append(f"(skipped {skipped_binary} binary/unreadable files)")
            if skipped_large:
                notes.append(f"(skipped {skipped_large} large files)")
            if output_mode == "count" and counts:
                notes.append(f"(total matches: {sum(counts.values())} in {len(counts)} files)")
            if notes:
                result += "\n\n" + "\n".join(notes)

            # Reviewer-run duplicate suppression: an identical request that
            # already produced byte-identical output is answered with a short
            # hint. Errors are never suppressed so a transient failure stays
            # visible.
            if review_ledger is not None:
                if review_ledger.search_repeat(search_signature, result):
                    review_ledger.duplicate_searches += 1
                    return (
                        f"[Duplicate search suppressed: identical grep pattern={pattern!r} "
                        f"path={path!r} mode={output_mode} already returned the same unchanged "
                        "result earlier in this reviewer run. Reuse that result, or change the "
                        "pattern, path, glob or range.]"
                    )
                review_ledger.note_search(search_signature, result)
            return result
        except MatchTooLargeError as exc:
            return ToolResult.error(f"Error: {exc}")
        except SearchBudgetExceededError as exc:
            detail = (
                f"{self._MAX_SCAN_PATHS} paths"
                if str(exc) == "paths"
                else f"{self._MAX_SCAN_SECONDS:g} seconds"
            )
            return ToolResult.error(
                f"Error: grep scan exceeded {detail}; narrow path, glob, or type and retry."
            )
        except SearchCancelledError:
            return ToolResult.error("Error: grep search cancelled")
        except PermissionError as e:
            return ToolResult.error(f"Error: {e}")
        except Exception as e:
            return ToolResult.error(f"Error searching files: {e}")

    @staticmethod
    def _checked(lines: Iterable[SearchLine], budget: SearchBudget) -> Iterable[SearchLine]:
        for line in lines:
            budget.checkpoint()
            yield line


def search_text_lines(
    path: str,
    lines: Iterable[SearchLine],
    config: MatchConfig,
    *,
    budget: SearchBudget | None = None,
) -> tuple[str, bool]:
    """Run one path through the shared matcher.

    Returns ``(rendered_page, has_match)``. Adapters that already fetched the
    text (remote readers) use this to get byte-identical output to the local
    tool without duplicating matcher logic.
    """
    regex = config.compile()
    page = ContentPage(
        limit=config.match_limit,
        offset=config.offset,
        max_chars=config.max_chars,
        line_limit=config.line_limit,
    )
    source = lines if budget is None else GrepTool._checked(lines, budget)
    page.scan(path, source, regex, config.context_before, config.context_after)
    rendered, note = page.render(f"No matches found for pattern '{config.pattern}' in {path}")
    if note:
        rendered += "\n\n" + note
    return rendered, page.returned > 0


__all__ = ["GrepTool", "search_text_lines"]
