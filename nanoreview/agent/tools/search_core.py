"""Shared plain-text search core for local and remote (PR) readers.

Adapted from nanobot ``agent/tools/_search_content.py`` and the matching
helpers of ``agent/tools/search.py`` at commit
``a4f2e98d8a4a7f0670a2f17ddb967755404d2934``.

MIT License, Copyright (c) 2025-present Xubin Ren and the nanobot
contributors. The full license text is distributed with the nanobot
reference checkout used to port this file.

The core is deliberately dependency free: it takes a path label plus a stream
of already-decoded ``(line number, text)`` pairs and a match configuration. It
never walks directories, performs network access, resolves SHAs, checks
permissions, or touches persistent state. Directory traversal, workspace
scopes, ledgers, and remote fetches stay in the adapters that call it.
"""

from __future__ import annotations

import fnmatch
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import chain, islice
from pathlib import PurePosixPath
from typing import Iterable, Literal

DEFAULT_HEAD_LIMIT = 250
DEFAULT_CONTEXT_LINES = 5
DEFAULT_MAX_RESULT_CHARS = 12_000
DEFAULT_LINE_LIMIT = 2_000

TYPE_GLOB_MAP: dict[str, tuple[str, ...]] = {
    "py": ("*.py", "*.pyi"),
    "python": ("*.py", "*.pyi"),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "tsx": ("*.tsx",),
    "jsx": ("*.jsx",),
    "json": ("*.json",),
    "md": ("*.md", "*.mdx"),
    "markdown": ("*.md", "*.mdx"),
    "go": ("*.go",),
    "rs": ("*.rs",),
    "rust": ("*.rs",),
    "java": ("*.java",),
    "sh": ("*.sh", "*.bash"),
    "yaml": ("*.yaml", "*.yml"),
    "yml": ("*.yaml", "*.yml"),
    "toml": ("*.toml",),
    "sql": ("*.sql",),
    "html": ("*.html", "*.htm"),
    "css": ("*.css", "*.scss", "*.sass"),
}


class MatchTooLargeError(ValueError):
    """A match and its source locator cannot fit in an empty page."""


class SearchCancelledError(Exception):
    """Stop a worker scan after its owning async task was cancelled."""


class SearchBudgetExceededError(Exception):
    """Stop an unbounded filesystem scan at its configured budget."""


@dataclass(slots=True)
class SearchBudget:
    """Cooperative scan budget shared by the local and remote adapters.

    Cancellation and limits are checked between files and between lines; a
    single long-running :mod:`re` call cannot be interrupted, so this is a
    cooperative budget rather than a hard timeout.
    """

    cancelled: threading.Event
    deadline: float
    max_paths: int
    scanned_paths: int = 0

    def checkpoint(self) -> None:
        if self.cancelled.is_set():
            raise SearchCancelledError
        if time.monotonic() >= self.deadline:
            raise SearchBudgetExceededError("time")

    def visit_path(self) -> None:
        self.checkpoint()
        self.scanned_paths += 1
        if self.scanned_paths > self.max_paths:
            raise SearchBudgetExceededError("paths")


@dataclass(frozen=True, slots=True)
class SearchLine:
    """One decoded source line: 1-based line number plus text without newline."""

    number: int
    text: str


@dataclass(frozen=True, slots=True)
class MatchConfig:
    """Everything that decides which lines match and how a page is shaped."""

    pattern: str
    case_insensitive: bool = False
    fixed_strings: bool = False
    output_mode: str = "content"
    context_before: int = DEFAULT_CONTEXT_LINES
    context_after: int = DEFAULT_CONTEXT_LINES
    head_limit: int | None = DEFAULT_HEAD_LIMIT
    offset: int = 0
    max_chars: int = DEFAULT_MAX_RESULT_CHARS
    line_limit: int = DEFAULT_LINE_LIMIT

    def compile(self) -> re.Pattern[str]:
        return build_regex(
            self.pattern,
            fixed_strings=self.fixed_strings,
            case_insensitive=self.case_insensitive,
        )

    @property
    def match_limit(self) -> int | None:
        return self.head_limit


def normalize_pattern(pattern: str) -> str:
    return pattern.strip().replace("\\", "/")


@lru_cache(maxsize=128)
def glob_patterns(pattern: str) -> tuple[str, ...]:
    """Expand bounded brace alternatives before matching path segments."""
    pending = [normalize_pattern(pattern)]
    expanded: list[str] = []
    total = 0
    while pending:
        current = pending.pop()
        braces = re.search(r"\{([^{}]*)\}", current)
        if braces is None:
            if "{" in current or "}" in current:
                raise ValueError("Invalid glob: unbalanced braces")
            expanded.append(current)
            continue
        options = braces[1].split(",")
        if len(options) < 2:
            raise ValueError("Invalid glob: braces require comma-separated alternatives")
        total += len(pending) + len(options)
        if total > 64:
            raise ValueError("Invalid glob: at most 64 brace alternatives are supported")
        pending.extend(
            current[: braces.start()] + part + current[braces.end() :] for part in options
        )
    return tuple(expanded)


def match_glob(rel_path: str, name: str, pattern: str) -> bool:
    """Match a repo-relative path against a glob.

    A bare pattern such as ``*.py`` matches the file name at any depth; a
    pattern containing ``/`` is matched segment by segment so ``**`` can span
    zero or more directories.
    """
    return any(_match_path_glob(rel_path, name, item) for item in glob_patterns(pattern))


def _match_path_glob(rel_path: str, name: str, normalized: str) -> bool:
    if not normalized:
        return False
    if "/" in normalized:
        pattern_parts = PurePosixPath(normalized).parts
        path_parts = PurePosixPath(rel_path).parts
        matched = [True] + [False] * len(path_parts)
        for part in pattern_parts:
            if part == "**":
                # A globstar consumes zero or more complete path segments.
                for index in range(1, len(matched)):
                    matched[index] = matched[index] or matched[index - 1]
            else:
                matched = [False] + [
                    matched[index] and fnmatch.fnmatchcase(path_part, part)
                    for index, path_part in enumerate(path_parts)
                ]
        return matched[-1]
    return fnmatch.fnmatch(name, normalized)


def matches_type(name: str, file_type: str | None) -> bool:
    if not file_type:
        return True
    lowered = file_type.strip().lower()
    if not lowered:
        return True
    patterns = TYPE_GLOB_MAP.get(lowered, (f"*.{lowered}",))
    return any(fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in patterns)


def build_regex(
    pattern: str,
    *,
    fixed_strings: bool = False,
    case_insensitive: bool = False,
) -> re.Pattern[str]:
    """Compile the pattern used by both adapters; raises ``re.error``."""
    flags = re.IGNORECASE if case_insensitive else 0
    needle = re.escape(pattern) if fixed_strings else pattern
    return re.compile(needle, flags)


def paginate(items: list[str], limit: int | None, offset: int) -> tuple[list[str], bool]:
    if limit is None:
        return items[offset:], False
    sliced = items[offset : offset + limit]
    truncated = len(items) > offset + limit
    return sliced, truncated


def text_page(
    items: list[str], limit: int | None, offset: int, max_chars: int
) -> tuple[list[str], bool]:
    """Page already-rendered path entries under both count and size limits."""
    page, truncated = paginate(items, limit, offset)
    size = 0
    for index, item in enumerate(page):
        size += len(item) + (1 if index else 0)
        if size > max_chars:
            if index == 0:
                raise MatchTooLargeError(
                    "Search entry exceeds output budget; narrow the search path"
                )
            return page[:index], True
    return page, truncated


def pagination_note(limit: int | None, offset: int, truncated: bool) -> str | None:
    if truncated:
        if limit is None:
            return f"(pagination: offset={offset})"
        return f"(pagination: limit={limit}, offset={offset})"
    if offset > 0:
        return f"(pagination: offset={offset})"
    return None


def _clip_line(text: str, limit: int, match_start: int | None) -> str:
    """Clip a long line but keep the matching column visible."""
    if len(text) <= limit:
        return text
    marker = "..."
    available = limit - len(marker)
    if match_start is None:
        return text[:available] + marker
    start = min(max(0, match_start - available // 3), len(text) - available)
    end = start + available
    prefix = marker if start else ""
    suffix = marker if end < len(text) else ""
    visible = text[start:end]
    if prefix and suffix:
        visible = visible[: available - len(marker)]
    return prefix + visible + suffix


def _format_line(line: SearchLine, start: int | None, limit: int) -> str:
    marker = ">" if start is not None else " "
    return f"{marker} {line.number}| {_clip_line(line.text, limit, start)}"


def _format_header(path: str, line: SearchLine) -> str:
    return f"{path}:{line.number}"


@dataclass
class ContentPage:
    """Select matches and append only context not already present in the page."""

    limit: int | None
    offset: int
    max_chars: int
    line_limit: int
    blocks: list[list[str]] = field(default_factory=list, init=False)
    seen: int = field(default=0, init=False)
    returned: int = field(default=0, init=False)
    chars: int = field(default=0, init=False)
    stopped: Literal["limit", "size"] | None = field(default=None, init=False)

    def scan(
        self,
        path: str,
        lines: Iterable[SearchLine],
        regex: re.Pattern[str],
        before: int,
        after: int,
    ) -> None:
        history: deque[SearchLine] = deque(maxlen=before)
        source = iter(lines)
        following = deque(islice(source, after + 1))
        positions: dict[int, int] = {}
        while following:
            match = regex.search(following[0].text)
            if match is not None:
                self.seen += 1
                if self.seen > self.offset:
                    if self.limit is not None and self.returned >= self.limit:
                        self.stopped = "limit"
                        return
                    self._append_match(path, history, following, match.start(), positions)
                    if self.stopped:
                        return
            history.append(following.popleft())
            if (line := next(source, None)) is not None:
                following.append(line)

    def _append_match(
        self,
        path: str,
        history: deque[SearchLine],
        following: deque[SearchLine],
        start: int,
        positions: dict[int, int],
    ) -> None:
        match = following[0]
        last_line = next(reversed(positions), 0)
        first_line = history[0] if history else match
        merge = bool(positions and first_line.number <= last_line)
        header = "" if merge else _format_header(path, match)
        replacement = _format_line(match, start, self.line_limit)
        additions = [
            (
                line.number,
                replacement if line is match else _format_line(line, None, self.line_limit),
            )
            for line in chain(history, following)
            if not merge or line.number > last_line
        ]
        replace_at = positions.get(match.number) if merge else None
        replacement_chars = (
            len(replacement) - len(self.blocks[-1][replace_at]) if replace_at is not None else 0
        )
        header_chars = len(header) + (2 if self.blocks else 0) if not merge else 0
        added_chars = header_chars + sum(len(text) + 1 for _, text in additions) + replacement_chars
        if self.chars + added_chars > self.max_chars:
            self.stopped = "size"
            if self.blocks:
                return
            # Even an oversized first context must return a match and advance the offset.
            additions = [(match.number, replacement)]
            added_chars = header_chars + len(replacement) + 1
            if added_chars > self.max_chars:
                raise MatchTooLargeError("match exceeds output budget; narrow path or pattern")
        if not merge:
            self.blocks.append([header])
            positions.clear()
        block = self.blocks[-1]
        if replace_at is not None:
            block[replace_at] = replacement
        for line_number, text in additions:
            positions[line_number] = len(block)
            block.append(text)
        self.chars += added_chars
        self.returned += 1

    def render(self, no_matches: str) -> tuple[str, str | None]:
        result = "\n\n".join("\n".join(block) for block in self.blocks) or no_matches
        if self.stopped == "limit":
            note = f"pagination: limit={self.limit}, offset={self.offset}"
        elif self.stopped == "size":
            note = "output truncated due to size"
        else:
            note = f"(pagination: offset={self.offset})" if self.offset and self.blocks else None
            return result, note
        return result, f"({note}; use offset={self.offset + self.returned} to continue)"


def count_matches(lines: Iterable[SearchLine], regex: re.Pattern[str], *, stop_after: int | None = None) -> int:
    """Count matching lines, optionally stopping once ``stop_after`` is reached."""
    total = 0
    for line in lines:
        if regex.search(line.text) is not None:
            total += 1
            if stop_after is not None and total >= stop_after:
                break
    return total


def clip_for_page(text: str, line_limit: int, match: re.Match[str] | None) -> str:
    """Expose the clipping rule so adapters can render one-line previews."""
    return _clip_line(text, line_limit, match.start() if match else None)


__all__ = [
    "ContentPage",
    "DEFAULT_CONTEXT_LINES",
    "DEFAULT_HEAD_LIMIT",
    "DEFAULT_LINE_LIMIT",
    "DEFAULT_MAX_RESULT_CHARS",
    "MatchConfig",
    "MatchTooLargeError",
    "SearchBudget",
    "SearchBudgetExceededError",
    "SearchCancelledError",
    "SearchLine",
    "TYPE_GLOB_MAP",
    "build_regex",
    "clip_for_page",
    "count_matches",
    "glob_patterns",
    "match_glob",
    "matches_type",
    "paginate",
    "pagination_note",
    "text_page",
]
