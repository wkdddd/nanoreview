"""Shared helpers for code-review evidence collection."""

from __future__ import annotations

import re

from loguru import logger


def clean_scope_paths(paths: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for path in paths or []:
        if not isinstance(path, str):
            continue
        value = path.strip().replace("\\", "/")
        value = value.rstrip("/")
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def path_matches_scope(path: str, scopes: list[str]) -> bool:
    if not scopes:
        return True
    normalized = path.strip().replace("\\", "/").lstrip("/")
    return any(normalized == scope or normalized.startswith(f"{scope}/") for scope in scopes)


def changed_line_ranges(lines: list[int]) -> tuple[tuple[int, int], ...]:
    """Collapse changed line numbers into inclusive ranges.

    A finding may only point at a line the pull request actually touched, so
    the validator needs ranges rather than the full line set.
    """
    ordered = sorted({line for line in lines if line > 0})
    if not ordered:
        return ()
    ranges: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for line in ordered[1:]:
        if line == previous + 1:
            previous = line
            continue
        ranges.append((start, previous))
        start = previous = line
    ranges.append((start, previous))
    return tuple(ranges)


def line_in_ranges(line: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= line <= end for start, end in ranges)


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
