"""Reviewer-run duplicate read/search suppression.

Reviewers may not silently re-inflate their context with repeated reads. The
ledger lives on the run's ``FileStates``, so reviewer mode is on only for the
four ``reviewer.*`` profiles; every other agent keeps the pre-existing
read-dedup behaviour unchanged.
"""

from __future__ import annotations

import pytest

from nanoreview.agent.tools.file_state import FileStates, normalize_read_range
from nanoreview.agent.tools.filesystem import ReadFileTool
from nanoreview.agent.tools.search import GrepTool

_LINES = "\n".join(f"line {i}" for i in range(1, 51))


def _review_states() -> FileStates:
    return FileStates(review_dedup=True)


# ---------------------------------------------------------------------------
# normalize_read_range
# ---------------------------------------------------------------------------


def test_normalize_read_range_treats_default_limit_as_explicit() -> None:
    assert normalize_read_range(1, None, 2000) == normalize_read_range(1, 2000, 2000)
    assert normalize_read_range(0, None, 2000) == (1, 2000)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_repeat_read_is_suppressed(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(_LINES, encoding="utf-8")
    states = _review_states()
    tool = ReadFileTool(workspace=tmp_path, file_states=states)

    first = await tool.execute(path=str(target), offset=1, limit=10)
    second = await tool.execute(path=str(target), offset=1, limit=10)

    assert "line 1" in first
    assert second.startswith("[Duplicate read suppressed:")
    assert "line 1" not in second
    assert states.review_ledger is not None
    assert states.review_ledger.duplicate_reads == 1


@pytest.mark.asyncio
async def test_reviewer_dedup_cannot_be_bypassed_by_force(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(_LINES, encoding="utf-8")
    states = _review_states()
    tool = ReadFileTool(workspace=tmp_path, file_states=states)

    await tool.execute(path=str(target), offset=1, limit=10)
    forced = await tool.execute(path=str(target), offset=1, limit=10, force=True)

    assert forced.startswith("[Duplicate read suppressed:")
    assert states.review_ledger is not None
    assert states.review_ledger.duplicate_reads == 1


@pytest.mark.asyncio
async def test_reviewer_normalizes_default_and_explicit_limit(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(_LINES, encoding="utf-8")
    states = _review_states()
    tool = ReadFileTool(workspace=tmp_path, file_states=states)

    await tool.execute(path=str(target), offset=1)
    repeat = await tool.execute(path=str(target), offset=1, limit=2000)

    assert repeat.startswith("[Duplicate read suppressed:")


@pytest.mark.asyncio
async def test_reviewer_allows_a_different_range(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(_LINES, encoding="utf-8")
    states = _review_states()
    tool = ReadFileTool(workspace=tmp_path, file_states=states)

    await tool.execute(path=str(target), offset=1, limit=5)
    other = await tool.execute(path=str(target), offset=10, limit=5)

    assert "line 10" in other
    assert not other.startswith("[Duplicate read suppressed:")
    assert states.review_ledger is not None
    assert states.review_ledger.duplicate_reads == 0


@pytest.mark.asyncio
async def test_reviewer_allows_a_changed_file(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(_LINES, encoding="utf-8")
    states = _review_states()
    tool = ReadFileTool(workspace=tmp_path, file_states=states)

    await tool.execute(path=str(target), offset=1, limit=10)
    target.write_text(_LINES.replace("line 3", "line three"), encoding="utf-8")
    changed = await tool.execute(path=str(target), offset=1, limit=10)

    assert "line three" in changed
    assert not changed.startswith("[Duplicate read suppressed:")


@pytest.mark.asyncio
async def test_non_review_session_keeps_legacy_dedup(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(_LINES, encoding="utf-8")
    states = FileStates()  # review_dedup defaults to False
    tool = ReadFileTool(workspace=tmp_path, file_states=states)

    await tool.execute(path=str(target), offset=1, limit=10)
    forced = await tool.execute(path=str(target), offset=1, limit=10, force=True)

    # ``force`` still bypasses the legacy path, and no reviewer ledger exists.
    assert states.review_ledger is None
    assert "line 1" in forced


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_repeat_search_is_suppressed(tmp_path) -> None:
    (tmp_path / "a.py").write_text("TODO: fix\nx = 1\n", encoding="utf-8")
    states = _review_states()
    tool = GrepTool(workspace=tmp_path, file_states=states)

    first = await tool.execute(pattern="TODO", path=".", output_mode="content")
    second = await tool.execute(pattern="TODO", path=".", output_mode="content")

    assert "a.py:1" in first
    assert second.startswith("[Duplicate search suppressed:")
    assert states.review_ledger is not None
    assert states.review_ledger.duplicate_searches == 1


@pytest.mark.asyncio
async def test_reviewer_search_allows_changed_tree(tmp_path) -> None:
    (tmp_path / "a.py").write_text("TODO: fix\n", encoding="utf-8")
    states = _review_states()
    tool = GrepTool(workspace=tmp_path, file_states=states)

    await tool.execute(pattern="TODO", path=".", output_mode="content")
    (tmp_path / "b.py").write_text("TODO: also\n", encoding="utf-8")
    second = await tool.execute(pattern="TODO", path=".", output_mode="content")

    assert not second.startswith("[Duplicate search suppressed:")
    assert "b.py" in second


@pytest.mark.asyncio
async def test_reviewer_search_allows_a_different_pattern(tmp_path) -> None:
    (tmp_path / "a.py").write_text("TODO: fix\nFIXME: later\n", encoding="utf-8")
    states = _review_states()
    tool = GrepTool(workspace=tmp_path, file_states=states)

    await tool.execute(pattern="TODO", path=".", output_mode="content")
    other = await tool.execute(pattern="FIXME", path=".", output_mode="content")

    assert "FIXME" in other
    assert not other.startswith("[Duplicate search suppressed:")


def test_reviewer_ledgers_are_not_shared_across_runs() -> None:
    first = _review_states()
    second = _review_states()
    assert first.review_ledger is not second.review_ledger
    assert first.review_ledger is not None
    first.review_ledger.duplicate_reads = 3
    assert second.review_ledger is not None
    assert second.review_ledger.duplicate_reads == 0
