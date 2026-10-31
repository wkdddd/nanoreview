"""Long-term memory journal, and the Dream consolidation run around it.

The properties pinned here are the ones that make long-term memory safe:

- the journal is append-only and de-duplicated by cursor, so a failed run
  replays exactly what it never merged;
- ``memory/MEMORY.md`` is written atomically and only after a completed run;
- a run that fails, is cancelled, or simply chooses not to write advances
  nothing (or, for "no change needed", advances the cursor without touching the
  file);
- the automatic run happens inside one weekly window and never catches up.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.dream import (
    MAX_ENTRY_CHARS,
    MAX_MEMORY_CHARS,
    DreamOutcome,
    DreamScheduler,
    DreamService,
    DreamState,
    MemoryJournal,
    ReadMemoryTool,
    WriteMemoryTool,
    shanghai_tz,
    week_key,
)
from nanoreview.agent.runner import AgentRunResult, AgentRunSpec
from nanoreview.providers.base import LLMProvider, LLMResponse


class NoopProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dream"


class ScriptedRunner:
    """Runner double that executes a callback against the run's own tools."""

    def __init__(self, action=None, *, stop_reason: str = "completed", error=None) -> None:
        self.action = action
        self.stop_reason = stop_reason
        self.error = error
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        if self.action is not None:
            await self.action(spec)
        return AgentRunResult(
            final_content="done",
            messages=list(spec.messages),
            stop_reason=self.stop_reason,
            error=self.error,
        )


def _service(tmp_path: Path, runner) -> DreamService:
    return DreamService(
        workspace=tmp_path,
        provider=NoopProvider(),
        model="dream",
        context_window_tokens=64_000,
        runner=runner,
        max_iterations=5,
    )


def _journal(tmp_path: Path, count: int = 1) -> MemoryJournal:
    journal = MemoryJournal(tmp_path)
    for index in range(count):
        journal.append(f"entry {index}")
    return journal


# --- journal ---------------------------------------------------------------


def test_append_assigns_monotonic_cursors(tmp_path: Path) -> None:
    journal = MemoryJournal(tmp_path)

    first = journal.append("first")
    second = journal.append("second")

    assert (first, second) == (1, 2)
    lines = journal.journal_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["cursor"] for line in lines] == [1, 2]


def test_an_empty_summary_is_not_journaled(tmp_path: Path) -> None:
    journal = MemoryJournal(tmp_path)

    assert journal.append("   ") is None
    assert not journal.journal_path.exists()


def test_pending_reads_only_unconsumed_entries(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=3)
    journal.advance(2)

    pending = journal.pending()

    assert [entry.cursor for entry in pending] == [3]


def test_pending_is_bounded_per_read(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=25)

    pending = journal.pending()

    assert len(pending) == 20
    assert pending[0].cursor == 1


def test_a_pending_entry_is_truncated(tmp_path: Path) -> None:
    journal = MemoryJournal(tmp_path)
    journal.append("x" * (MAX_ENTRY_CHARS * 3))

    entry = journal.pending()[0]

    assert entry.content.startswith("x" * MAX_ENTRY_CHARS)
    assert "(truncated)" in entry.content


def test_the_cursor_never_moves_backwards(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=3)

    journal.advance(3)
    journal.advance(1)

    assert journal.read_state().last_cursor == 3


def test_state_survives_a_restart(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=1)
    journal.advance(1)
    journal.mark_week_fired("2026-W44")

    reloaded = MemoryJournal(tmp_path).read_state()

    assert reloaded == DreamState(last_cursor=1, last_fired_week="2026-W44")


def test_a_corrupt_state_file_falls_back_to_defaults(tmp_path: Path) -> None:
    journal = MemoryJournal(tmp_path)
    journal.directory.mkdir(parents=True)
    journal.state_path.write_text("{not json", encoding="utf-8")

    assert journal.read_state() == DreamState()


def test_corrupt_journal_lines_are_skipped(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=1)
    with journal.journal_path.open("a", encoding="utf-8") as handle:
        handle.write("{oops\n")
        handle.write(json.dumps({"cursor": 2, "content": ""}) + "\n")

    assert [entry.cursor for entry in journal.pending()] == [1]
    # The good line still defines the next cursor.
    assert journal.append("next") == 2


# --- tools -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_memory_reports_an_empty_file(tmp_path: Path) -> None:
    tool = ReadMemoryTool(MemoryJournal(tmp_path))

    assert "empty" in str(await tool.execute()).lower()


@pytest.mark.asyncio
async def test_write_memory_only_stages(tmp_path: Path) -> None:
    journal = MemoryJournal(tmp_path)
    tool = WriteMemoryTool(journal)

    await tool.execute(content="new memory")

    assert tool.staged == "new memory"
    assert not journal.memory_path.exists()


@pytest.mark.asyncio
async def test_write_memory_rejects_unusable_content(tmp_path: Path) -> None:
    tool = WriteMemoryTool(MemoryJournal(tmp_path))

    assert "requires a string" in str(await tool.execute(content=None))
    assert "empty" in str(await tool.execute(content="   "))
    oversized = "x" * (MAX_MEMORY_CHARS + 1)
    assert "limit" in str(await tool.execute(content=oversized))
    assert tool.staged is None


def test_the_write_tool_has_no_path_parameter() -> None:
    """The host owns the target; the model cannot name another file."""
    tool = WriteMemoryTool(MemoryJournal(Path(".")))

    assert list(tool.parameters["properties"]) == ["content"]


# --- service ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_without_entries_never_starts(tmp_path: Path) -> None:
    runner = ScriptedRunner()
    service = _service(tmp_path, runner)

    outcome = await service.run()

    assert outcome.status == "idle"
    assert runner.specs == []


@pytest.mark.asyncio
async def test_a_staged_edit_is_committed_and_consumed(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=2)

    async def _write(spec: AgentRunSpec) -> None:
        tool = spec.tools.get("write_memory")
        assert tool is not None
        await tool.execute(content="# Memory\n\n- prefers tabs\n")

    service = _service(tmp_path, ScriptedRunner(_write))

    outcome = await service.run()

    assert outcome == DreamOutcome(
        status="completed", entries=2, applied=True, advanced_to=2
    )
    assert journal.read_memory() == "# Memory\n\n- prefers tabs\n"
    assert journal.read_state().last_cursor == 2
    # Atomic write: no temporary file is left behind.
    assert sorted(p.name for p in journal.directory.iterdir()) == [
        "MEMORY.md",
        "dream-state.json",
        "history.jsonl",
    ]


@pytest.mark.asyncio
async def test_a_run_that_writes_nothing_still_consumes(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=2)
    service = _service(tmp_path, ScriptedRunner())

    outcome = await service.run()

    assert outcome.status == "completed"
    assert outcome.applied is False
    assert outcome.advanced_to == 2
    # Nothing was remembered, so nothing is created.
    assert not journal.memory_path.exists()
    assert journal.read_state().last_cursor == 2


@pytest.mark.asyncio
async def test_a_failed_run_leaves_memory_and_cursor_untouched(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=2)

    async def _write(spec: AgentRunSpec) -> None:
        await spec.tools.get("write_memory").execute(content="# Memory\n")

    runner = ScriptedRunner(_write, stop_reason="context_limit", error="too big")
    outcome = await _service(tmp_path, runner).run()

    assert outcome.status == "failed"
    assert outcome.reason == "too big"
    assert not journal.memory_path.exists()
    assert journal.read_state().last_cursor == 0
    # The entries are still pending, so the next run retries them.
    assert len(journal.pending()) == 2


@pytest.mark.asyncio
async def test_a_cancelled_run_leaves_memory_and_cursor_untouched(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=1)

    async def _write(spec: AgentRunSpec) -> None:
        await spec.tools.get("write_memory").execute(content="# Memory\n")
        raise asyncio.CancelledError()

    service = _service(tmp_path, ScriptedRunner(_write))

    with pytest.raises(asyncio.CancelledError):
        await service.run()

    assert not journal.memory_path.exists()
    assert journal.read_state().last_cursor == 0


@pytest.mark.asyncio
async def test_a_raising_runner_is_reported_not_raised(tmp_path: Path) -> None:
    _journal(tmp_path, count=1)

    class Boom:
        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            raise RuntimeError("provider exploded")

    outcome = await _service(tmp_path, Boom()).run()

    assert outcome.status == "failed"
    assert "provider exploded" in outcome.reason


@pytest.mark.asyncio
async def test_a_second_run_is_rejected_while_one_is_in_flight(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=1)
    started = asyncio.Event()
    release = asyncio.Event()

    class Blocking:
        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            started.set()
            await release.wait()
            return AgentRunResult(final_content="ok", messages=list(spec.messages))

    service = _service(tmp_path, Blocking())
    task = asyncio.create_task(service.run())
    await started.wait()

    second = await service.run()
    release.set()
    first = await task

    assert second.status == "busy"
    assert first.status == "completed"
    # The blocked run consumed nothing (it staged no write).
    assert journal.read_state().last_cursor == 1


@pytest.mark.asyncio
async def test_the_run_gets_only_the_two_memory_tools(tmp_path: Path) -> None:
    _journal(tmp_path, count=1)
    runner = ScriptedRunner()

    await _service(tmp_path, runner).run()

    spec = runner.specs[0]
    assert spec.tools.tool_names == ["read_memory", "write_memory"]
    assert spec.runtime.role == "dream"
    assert spec.runtime.model == "dream"
    # No session, no workspace tools, no checkpointing of its own.
    assert spec.session_key is None
    assert spec.checkpoint_callback is None
    assert spec.max_iterations == 5


@pytest.mark.asyncio
async def test_the_prompt_lists_every_entry_in_order(tmp_path: Path) -> None:
    _journal(tmp_path, count=3)
    runner = ScriptedRunner()

    await _service(tmp_path, runner).run()

    prompt = runner.specs[0].messages[1]["content"]
    assert prompt.index("entry 0") < prompt.index("entry 1") < prompt.index("entry 2")


# --- schedule --------------------------------------------------------------


def _now(**override) -> datetime:
    base = datetime(2026, 11, 1, 3, 0, tzinfo=shanghai_tz())  # a Sunday
    return base.replace(**override)


def test_the_window_opens_on_sunday_at_three(tmp_path: Path) -> None:
    scheduler = DreamScheduler(service=_service(tmp_path, ScriptedRunner()))

    assert scheduler.window_open(_now()) is True
    assert scheduler.window_open(_now(minute=4)) is True


def test_the_window_closes_after_one_tick(tmp_path: Path) -> None:
    """No catch-up: a later start is simply a missed week."""
    scheduler = DreamScheduler(service=_service(tmp_path, ScriptedRunner()))

    assert scheduler.window_open(_now(minute=5)) is False
    assert scheduler.window_open(_now(hour=4)) is False
    assert scheduler.window_open(_now() - timedelta(days=1)) is False  # Saturday
    assert scheduler.window_open(_now(minute=59, hour=2)) is False


def test_a_disabled_scheduler_never_fires(tmp_path: Path) -> None:
    scheduler = DreamScheduler(service=_service(tmp_path, ScriptedRunner()), enabled=False)

    assert scheduler.due(_now()) is False


@pytest.mark.asyncio
async def test_a_week_fires_only_once(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=1)
    scheduler = DreamScheduler(
        service=_service(tmp_path, ScriptedRunner()), clock=_now
    )

    first = await scheduler.tick()
    second = await scheduler.tick()

    assert first is not None and first.status == "completed"
    assert second is None
    assert journal.read_state().last_fired_week == week_key(_now())


@pytest.mark.asyncio
async def test_the_week_is_marked_even_when_the_run_fails(tmp_path: Path) -> None:
    """A failed window is not retried: the next attempt is next week's."""
    journal = _journal(tmp_path, count=1)
    runner = ScriptedRunner(stop_reason="error", error="boom")
    scheduler = DreamScheduler(service=_service(tmp_path, runner), clock=_now)

    outcome = await scheduler.tick()
    again = await scheduler.tick()

    assert outcome is not None and outcome.status == "failed"
    assert again is None
    assert journal.read_state().last_fired_week == week_key(_now())
    assert journal.read_state().last_cursor == 0


@pytest.mark.asyncio
async def test_a_scheduled_tick_skips_a_run_already_in_flight(tmp_path: Path) -> None:
    journal = _journal(tmp_path, count=1)
    service = _service(tmp_path, ScriptedRunner())
    scheduler = DreamScheduler(service=service, clock=_now)

    async with service._lock:  # noqa: SLF001 - simulate a manual run in flight
        outcome = await scheduler.tick()

    assert outcome is not None and outcome.status == "busy"
    # The week is still marked: an automatic run does not queue behind a manual one.
    assert journal.read_state().last_fired_week == week_key(_now())


def test_week_key_is_iso_week_based() -> None:
    assert week_key(datetime(2026, 11, 1, tzinfo=shanghai_tz())) == "2026-W44"
    assert week_key(datetime(2026, 11, 8, tzinfo=shanghai_tz())) == "2026-W45"


def test_the_window_is_anchored_to_asia_shanghai() -> None:
    """The check converts naive datetimes to China Standard Time."""
    scheduler = DreamScheduler(service=_service(Path("."), ScriptedRunner()))

    assert scheduler.window_open(datetime(2026, 11, 1, 3, 0)) is True


@pytest.mark.asyncio
async def test_serve_ticks_and_survives_errors(tmp_path: Path) -> None:
    class Broken:
        calls = 0

        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            Broken.calls += 1
            raise RuntimeError("still broken")

    service = _service(tmp_path, Broken())
    _journal(tmp_path, count=1)
    scheduler = DreamScheduler(service=service, tick_seconds=1)

    task = asyncio.create_task(scheduler.serve())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert Broken.calls <= 1


def test_dream_outcome_reports_only_completion_as_ok() -> None:
    assert DreamOutcome(status="completed").ok is True
    assert DreamOutcome(status="failed").ok is False
    assert DreamOutcome(status="busy").ok is False


def test_a_day_offset_still_belongs_to_its_own_week() -> None:
    sunday = datetime(2026, 11, 1, 3, 0, tzinfo=shanghai_tz())
    monday = sunday + timedelta(days=1)

    assert week_key(sunday) == "2026-W44"
    assert week_key(monday) == "2026-W45"


def test_the_journal_keeps_the_entry_timestamp(tmp_path: Path) -> None:
    journal = MemoryJournal(tmp_path)
    journal.append("first", timestamp="2026-10-31 10:00")

    entry: Any = journal.pending()[0]

    assert entry.timestamp == "2026-10-31 10:00"
