"""Contract tests for the shared plain-text grep core and the local tool.

The matcher was ported from nanobot ``tests/tools/test_search_tools.py``. These
tests keep the ported behaviour honest: default context, merged blocks, size
pages that can always advance, long-line visibility, and cooperative scan
budgets. The local tool feeds the same core the remote PR reader uses, so
identical text must render identically.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from pathlib import Path

import pytest

from nanoreview.agent.tools.context import RequestContext, reset_current_request_context, set_current_request_context
from nanoreview.agent.tools.search import GrepTool, search_text_lines
from nanoreview.agent.tools.search_core import (
    MatchConfig,
    SearchBudget,
    SearchBudgetExceededError,
    SearchCancelledError,
    SearchLine,
    build_regex,
    match_glob,
    matches_type,
    text_page,
)
from nanoreview.agent.tools.workspace_scope import review_workspace_scope


def _lines(text: str) -> list[SearchLine]:
    return [SearchLine(number, line) for number, line in enumerate(text.splitlines(), 1)]


# ---------------------------------------------------------------------------
# core matcher
# ---------------------------------------------------------------------------


def test_fixed_strings_and_regex_modes() -> None:
    lines = _lines("a.b literal\naxb other\nA.B upper")
    literal = build_regex("a.b", fixed_strings=True)
    regex = build_regex("a.b", fixed_strings=False)

    assert [line.number for line in lines if literal.search(line.text)] == [1]
    assert [line.number for line in lines if regex.search(line.text)] == [1, 2]


def test_case_insensitive_is_opt_in() -> None:
    lines = _lines("Needle\nneedle")

    assert [line.number for line in lines if build_regex("needle").search(line.text)] == [2]
    assert [
        line.number
        for line in lines
        if build_regex("needle", case_insensitive=True).search(line.text)
    ] == [1, 2]


def test_glob_matching_spans_zero_or_more_directories() -> None:
    assert match_glob("tests/test_a.py", "test_a.py", "tests/**/test_*.py")
    assert match_glob("tests/unit/test_a.py", "test_a.py", "tests/**/test_*.py")
    assert match_glob("src/main.py", "main.py", "*.py")
    assert not match_glob("src/main.js", "main.js", "*.py")


def test_glob_rejects_unbalanced_braces() -> None:
    with pytest.raises(ValueError):
        match_glob("a.py", "a.py", "*.{py,js")


def test_type_filter_uses_extension_map() -> None:
    assert matches_type("a.ts", "ts") is True
    assert matches_type("a.tsx", "ts") is True
    assert matches_type("a.md", "py") is False
    assert matches_type("a.unknown", "unknown") is True


def test_text_page_keeps_one_entry_when_budget_is_tiny() -> None:
    with pytest.raises(ValueError):
        text_page(["x" * 200], limit=None, offset=0, max_chars=10)


def test_search_budget_reports_time_and_path_limits() -> None:
    budget = SearchBudget(cancelled=threading.Event(), deadline=0.0, max_paths=10)
    with pytest.raises(SearchBudgetExceededError, match="time"):
        budget.checkpoint()

    limited = SearchBudget(cancelled=threading.Event(), deadline=1e12, max_paths=1)
    limited.visit_path()
    with pytest.raises(SearchBudgetExceededError, match="paths"):
        limited.visit_path()

    cancelled = SearchBudget(cancelled=threading.Event(), deadline=1e12, max_paths=10)
    cancelled.cancelled.set()
    with pytest.raises(SearchCancelledError):
        cancelled.checkpoint()


def test_core_renders_merged_blocks_and_pages_by_match() -> None:
    text = "before\nneedle one\nneedle two\nneedle three\nafter"
    rendered, has_match = search_text_lines(
        "source.txt",
        _lines(text),
        MatchConfig(pattern="needle", head_limit=2),
    )

    assert has_match is True
    assert rendered.count("source.txt:") == 1
    assert "> 2| needle one" in rendered and "> 3| needle two" in rendered
    assert "use offset=2 to continue" in rendered

    second, _ = search_text_lines(
        "source.txt",
        _lines(text),
        MatchConfig(pattern="needle", head_limit=2, offset=2),
    )
    assert re.findall(r"^> (\d+)\|", second, re.M) == ["4"]


# ---------------------------------------------------------------------------
# local tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_respects_glob_filter_and_context(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "alpha\nbeta\nmatch_here\ngamma\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("match_here\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)

    result = await tool.execute(
        pattern="match_here",
        path=".",
        glob="*.py",
        context_before=1,
        context_after=1,
    )

    assert "src/main.py:3" in result
    assert "  2| beta" in result
    assert "> 3| match_here" in result
    assert "  4| gamma" in result
    assert "README.md" not in result


@pytest.mark.asyncio
async def test_grep_defaults_to_five_context_lines(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 6))
        + "\nmatch_here\n"
        + "\n".join(f"line {line}" for line in range(7, 13)),
        encoding="utf-8",
    )
    tool = GrepTool(workspace=tmp_path)

    result = await tool.execute(pattern="match_here", path="src")

    assert "src/main.py:6" in result
    assert "  1| line 1" in result
    assert "> 6| match_here" in result
    assert "  11| line 11" in result
    assert "line 12" not in result


@pytest.mark.asyncio
async def test_grep_keeps_an_oversized_matching_line_visible(tmp_path: Path) -> None:
    long_line = "x" * 130_000 + "needle" + "y" * 10_000
    (tmp_path / "huge-line.txt").write_text(long_line, encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)

    result = await tool.execute(
        pattern="needle",
        path="huge-line.txt",
        fixed_strings=True,
        context_before=0,
        context_after=0,
    )

    assert "huge-line.txt:1" in result
    assert "needle" in result
    assert "No matches found" not in result
    assert len(result) < GrepTool._MAX_RESULT_CHARS + 200


@pytest.mark.asyncio
async def test_grep_size_limit_returns_a_resumable_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "\n".join(f"needle-{line}-" + "x" * 80 for line in range(1, 11))
    (tmp_path / "many.txt").write_text(content, encoding="utf-8")
    monkeypatch.setattr(GrepTool, "_MAX_RESULT_CHARS", 350)
    tool = GrepTool(workspace=tmp_path)

    first = await tool.execute(
        pattern="needle",
        path="many.txt",
        fixed_strings=True,
        context_before=0,
        context_after=0,
        head_limit=10,
    )
    continuation = re.search(r"use offset=(\d+) to continue", first)
    assert continuation is not None
    next_offset = int(continuation.group(1))

    second = await tool.execute(
        pattern="needle",
        path="many.txt",
        fixed_strings=True,
        context_before=0,
        context_after=0,
        head_limit=10,
        offset=next_offset,
    )
    first_headers = {line for line in first.splitlines() if line.startswith("many.txt:")}
    second_headers = {line for line in second.splitlines() if line.startswith("many.txt:")}
    assert second_headers
    assert first_headers.isdisjoint(second_headers)


@pytest.mark.asyncio
async def test_grep_files_with_matches_is_unique_and_paged(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("needle\nneedle\n", encoding="utf-8")
        os.utime(tmp_path / name, (1, 1))
    tool = GrepTool(workspace=tmp_path)

    all_files = await tool.execute(pattern="needle", output_mode="files_with_matches")
    assert all_files.splitlines() == ["a.txt", "b.txt", "c.txt"]

    page = await tool.execute(
        pattern="needle", output_mode="files_with_matches", head_limit=2
    )
    assert "a.txt" in page and "b.txt" in page and "c.txt" not in page
    assert "use offset=2 to continue" in page

    tail = await tool.execute(
        pattern="needle", output_mode="files_with_matches", head_limit=2, offset=2
    )
    assert tail.splitlines()[0] == "c.txt"


@pytest.mark.asyncio
async def test_grep_count_mode_reports_counts_per_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle\nx\nneedle\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)

    result = await tool.execute(pattern="needle", output_mode="count")

    assert "a.txt: 2" in result
    assert "b.txt: 1" in result
    assert "(total matches: 3 in 2 files)" in result


@pytest.mark.asyncio
async def test_grep_skips_binary_files(tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"\x00needle")
    (tmp_path / "text.txt").write_text("needle", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)

    result = await tool.execute(pattern="needle", output_mode="files_with_matches")

    assert "text.txt" in result
    assert "bin.dat" not in result
    assert "binary/unreadable" in result


@pytest.mark.asyncio
async def test_grep_skips_large_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "big.txt").write_text("needle", encoding="utf-8")
    monkeypatch.setattr(GrepTool, "_MAX_FILE_BYTES", 2)
    tool = GrepTool(workspace=tmp_path)

    result = await tool.execute(pattern="needle", output_mode="files_with_matches")

    assert "No matches found" in result
    assert "large files" in result


@pytest.mark.asyncio
async def test_grep_scan_budget_is_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "source.txt").write_text("ordinary", encoding="utf-8")
    monkeypatch.setattr(GrepTool, "_MAX_SCAN_SECONDS", 0.0)
    tool = GrepTool(workspace=tmp_path)

    result = await tool.execute(pattern="absent")

    assert result.startswith("Error: grep scan exceeded 0 seconds")


@pytest.mark.asyncio
async def test_grep_rejects_unknown_output_mode(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)

    result = await tool.execute(pattern="needle", output_mode="bogus")

    assert result.startswith("Error: invalid output_mode")


@pytest.mark.asyncio
async def test_grep_worker_preserves_restricted_scope(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "inside.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("needle", encoding="utf-8")
    tool = GrepTool(workspace=project)
    token = set_current_request_context(
        RequestContext(
            channel="test",
            chat_id="test",
            workspace_scope=review_workspace_scope(project),
        )
    )
    try:
        result = await tool.execute(pattern="needle", output_mode="files_with_matches")
    finally:
        reset_current_request_context(token)

    assert result == "inside.txt"


@pytest.mark.asyncio
async def test_grep_cancellation_stops_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = GrepTool(workspace=tmp_path)
    started, stopped = threading.Event(), threading.Event()

    def blocking_iter_files(root: Path, budget: SearchBudget):
        started.set()
        try:
            if not budget.cancelled.wait(timeout=2):
                raise TimeoutError("worker did not receive cancellation")
            budget.checkpoint()
            yield root / "unreachable.txt"
        finally:
            stopped.set()

    monkeypatch.setattr(tool, "_iter_files", blocking_iter_files)
    task = asyncio.create_task(tool.execute(pattern="needle"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    assert await asyncio.to_thread(stopped.wait, 1)


@pytest.mark.asyncio
async def test_local_tool_and_shared_core_render_identical_text(tmp_path: Path) -> None:
    text = "\n".join(
        ["header", "needle a", "context1", "context2", "needle b", "tail", "noise"]
    )
    (tmp_path / "a.py").write_text(text, encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)

    local = await tool.execute(pattern="needle", path="a.py")
    shared, _ = search_text_lines("a.py", _lines(text), MatchConfig(pattern="needle"))

    assert local == shared
