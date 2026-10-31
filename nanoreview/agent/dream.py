"""Long-term memory: the conversation journal and the Dream consolidation run.

Two things live here, and they are deliberately separate from
``agent/memory.py`` (which owns *session* checkpoints):

* :class:`MemoryJournal` appends one bounded line per compacted conversation
  chunk to ``memory/history.jsonl`` and tracks the consumption cursor. Only the
  conversation agent ever appends: review roles have their own transcripts and
  must never leak into long-term memory.
* :class:`DreamService` replays the un-consumed entries through one temporary
  agent run and lets it rewrite ``memory/MEMORY.md``. The run has exactly two
  tools (:class:`ReadMemoryTool`, :class:`WriteMemoryTool`); the write is only
  *staged* while the model runs and the host commits it atomically afterwards.
  A failed, cancelled or unfitting run therefore leaves both the memory file and
  the cursor untouched, so the same entries are retried next time.

The scheduler is a fixed weekly window (Sunday 03:00 Asia/Shanghai) with no
catch-up: a missed window is dropped rather than replayed, which is why the
fired week is persisted in ``memory/dream-state.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoreview.agent.llm_runtime import ROLE_DREAM, LLMRuntime
from nanoreview.agent.runner import AgentRunner, AgentRunSpec
from nanoreview.agent.tools.base import Tool
from nanoreview.agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from nanoreview.providers.base import LLMProvider

#: Workspace directory holding every long-term memory file.
MEMORY_DIR = "memory"
#: The one file Dream may rewrite.
MEMORY_FILE = "MEMORY.md"
#: Append-only journal of compacted conversation chunks.
JOURNAL_FILE = "history.jsonl"
#: Cursor + weekly trigger marker.
STATE_FILE = "dream-state.json"

#: Entries a single Dream run may consume.
MAX_ENTRIES_PER_RUN = 20
#: Characters kept from one journal entry.
MAX_ENTRY_CHARS = 1_000
#: Upper bound on the memory file Dream may write back.
MAX_MEMORY_CHARS = 64_000

#: Fixed automatic window: Sunday 03:00 Asia/Shanghai.
DREAM_WEEKDAY = 6
DREAM_HOUR = 3
DREAM_MINUTE = 0
#: The window stays open for one tick, so a late start never catches up.
DREAM_TICK_SECONDS = 300

DREAM_SYSTEM_PROMPT = """You maintain the long-term memory file of a coding agent.

You receive journal entries: short summaries of conversation turns that were already
compacted out of the live session. Merge what is durable into the memory file and drop
what is not.

Keep: stable facts about the user and the workspace, decisions with their reasons,
conventions and preferences, unresolved problems, and pointers that will still matter
weeks from now.
Drop: one-off questions, transient tool output, restatements of the code, and anything
already recorded.

Rules:
- Read the current memory file with read_memory before deciding.
- Write the COMPLETE new file content with write_memory, not a diff. Preserve the
  existing structure and wording where it is still correct.
- If nothing in the new entries deserves to be remembered, do not call write_memory at
  all; that is a valid outcome.
- Never invent facts, and never record secrets, tokens or credentials.
- Do not mention the journal, this instruction, or that a consolidation happened.
"""

_SHANGHAI_FALLBACK = timezone(timedelta(hours=8), name="Asia/Shanghai")


def shanghai_tz() -> "timezone | Any":
    """Asia/Shanghai, with a fixed +08:00 fallback when tzdata is unavailable."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:  # pragma: no cover - depends on the host's tz database
        # Mainland China has no DST, so a fixed offset is exact.
        return _SHANGHAI_FALLBACK


def week_key(moment: datetime) -> str:
    """ISO ``YYYY-Www`` key of ``moment``, used to fire at most once a week."""
    year, week, _weekday = moment.isocalendar()
    return f"{year}-W{week:02d}"


@dataclass(frozen=True, slots=True)
class DreamJournalEntry:
    """One archived conversation chunk."""

    cursor: int
    timestamp: str
    content: str


@dataclass(slots=True)
class DreamState:
    """Persisted journal cursor and weekly trigger marker."""

    last_cursor: int = 0
    last_fired_week: str | None = None


class MemoryJournal:
    """``memory/history.jsonl`` plus its consumption cursor.

    Appends are one line each and never rewrite history; the cursor lives in a
    separate state file so a failed Dream cannot consume entries it never
    actually merged.
    """

    def __init__(self, workspace: "Path | str") -> None:
        self.workspace = Path(workspace)

    # -- paths --------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self.workspace / MEMORY_DIR

    @property
    def journal_path(self) -> Path:
        return self.directory / JOURNAL_FILE

    @property
    def state_path(self) -> Path:
        return self.directory / STATE_FILE

    @property
    def memory_path(self) -> Path:
        return self.directory / MEMORY_FILE

    # -- state --------------------------------------------------------------

    def read_state(self) -> DreamState:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return DreamState()
        except OSError:
            logger.warning("dream.state.read_failed path={}", self.state_path)
            return DreamState()
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("dream.state.corrupt path={}", self.state_path)
            return DreamState()
        if not isinstance(payload, dict):
            return DreamState()
        cursor = payload.get("last_cursor")
        week = payload.get("last_fired_week")
        return DreamState(
            last_cursor=cursor if isinstance(cursor, int) and cursor >= 0 else 0,
            last_fired_week=week if isinstance(week, str) and week else None,
        )

    def write_state(self, state: DreamState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_cursor": max(0, int(state.last_cursor)),
            "last_fired_week": state.last_fired_week,
        }
        _atomic_write(self.state_path, json.dumps(payload, ensure_ascii=False, indent=2))

    # -- journal ------------------------------------------------------------

    def append(self, content: str, *, timestamp: str | None = None) -> int | None:
        """Append one entry and return its cursor, or ``None`` when empty.

        The cursor is derived from the last journal line (not from the state
        file) so a lost or rolled-back state file can never make two entries
        share a cursor.
        """
        text = (content or "").strip()
        if not text:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        cursor = self.last_entry_cursor() + 1
        entry = {
            "cursor": cursor,
            "timestamp": timestamp or datetime.now().isoformat(timespec="seconds"),
            "content": text,
        }
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("memory.journal.appended cursor={} chars={}", cursor, len(text))
        return cursor

    def last_entry_cursor(self) -> int:
        """Cursor of the newest journal line, or 0 when the journal is empty."""
        try:
            with self.journal_path.open("r", encoding="utf-8") as handle:
                for line in reversed(handle.readlines()):
                    cursor = _entry_cursor(line)
                    if cursor is not None:
                        return cursor
        except FileNotFoundError:
            return 0
        except OSError:
            logger.warning("memory.journal.read_failed path={}", self.journal_path)
        return 0

    def pending(self, *, limit: int = MAX_ENTRIES_PER_RUN) -> list[DreamJournalEntry]:
        """Entries after the stored cursor, oldest first, bounded per read."""
        state = self.read_state()
        if limit <= 0:
            return []
        entries: list[DreamJournalEntry] = []
        try:
            with self.journal_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    entry = _parse_entry(line)
                    if entry is None or entry.cursor <= state.last_cursor:
                        continue
                    entries.append(entry)
                    if len(entries) >= limit:
                        break
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("memory.journal.read_failed path={}", self.journal_path)
            return []
        return entries

    def advance(self, cursor: int) -> DreamState:
        """Move the cursor forward (never backwards) and persist the state."""
        state = self.read_state()
        state.last_cursor = max(state.last_cursor, int(cursor))
        self.write_state(state)
        return state

    def mark_week_fired(self, week: str) -> DreamState:
        state = self.read_state()
        state.last_fired_week = week
        self.write_state(state)
        return state

    def read_memory(self) -> str:
        """Current ``memory/MEMORY.md`` text, or an empty string when absent."""
        try:
            return self.memory_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def write_memory(self, content: str) -> None:
        """Atomically replace ``memory/MEMORY.md``."""
        self.directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.memory_path, content)


def _entry_cursor(line: str) -> int | None:
    entry = _parse_entry(line, truncate=False)
    return entry.cursor if entry is not None else None


def _parse_entry(line: str, *, truncate: bool = True) -> DreamJournalEntry | None:
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    cursor = payload.get("cursor")
    if not isinstance(cursor, int) or cursor < 0:
        return None
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    if truncate:
        content = (
            content
            if len(content) <= MAX_ENTRY_CHARS
            else f"{content[:MAX_ENTRY_CHARS]}\n... (truncated)"
        )
    timestamp = payload.get("timestamp")
    return DreamJournalEntry(
        cursor=cursor,
        timestamp=timestamp if isinstance(timestamp, str) else "",
        content=content,
    )


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a temp file and ``os.replace``.

    A half-written memory file would be worse than no update at all, and the
    journal cursor must never be advanced past content that was not durably
    stored.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


# --- tools -----------------------------------------------------------------


class ReadMemoryTool(Tool):
    """Read the long-term memory file Dream maintains."""

    def __init__(self, journal: MemoryJournal) -> None:
        self._journal = journal

    @property
    def name(self) -> str:
        return "read_memory"

    @property
    def description(self) -> str:
        return "Read the current long-term memory file (memory/MEMORY.md)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        _ = kwargs
        content = self._journal.read_memory()
        return content if content.strip() else "(the memory file is empty)"


class WriteMemoryTool(Tool):
    """Stage a full replacement of the long-term memory file.

    There is no path parameter: the host owns the target, so a Dream run cannot
    write anywhere else. The content is only staged here — the service commits it
    after the run finishes successfully.
    """

    def __init__(self, journal: MemoryJournal) -> None:
        self._journal = journal
        self.staged: str | None = None

    @property
    def name(self) -> str:
        return "write_memory"

    @property
    def description(self) -> str:
        return (
            "Replace the whole long-term memory file with the given content. "
            "Pass the complete file, not a diff. Omit the call to leave memory unchanged."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Complete new content of memory/MEMORY.md.",
                }
            },
            "required": ["content"],
        }

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, content: Any = None, **kwargs: Any) -> Any:
        _ = kwargs
        if not isinstance(content, str):
            return "Error: write_memory requires a string 'content'."
        if len(content) > MAX_MEMORY_CHARS:
            return (
                f"Error: memory content is {len(content)} characters; the limit is "
                f"{MAX_MEMORY_CHARS}. Shorten it and call write_memory again."
            )
        if not content.strip():
            return "Error: refusing to write an empty memory file."
        self.staged = content
        return "Memory update staged. The host writes it after this run succeeds."


# --- service ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DreamOutcome:
    """Result of one Dream attempt."""

    status: str
    reason: str = ""
    entries: int = 0
    applied: bool = False
    advanced_to: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"


class DreamService:
    """Run one Dream consolidation per workspace at a time."""

    def __init__(
        self,
        *,
        workspace: "Path | str",
        provider: "LLMProvider",
        model: str,
        context_window_tokens: int,
        runner: AgentRunner,
        max_iterations: int,
        journal: MemoryJournal | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self._provider = provider
        self._model = model
        self._context_window_tokens = context_window_tokens
        self._runner = runner
        self._max_iterations = max(1, int(max_iterations))
        self.journal = journal or MemoryJournal(self.workspace)
        self._lock = asyncio.Lock()

    # -- introspection ------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def runtime(self) -> LLMRuntime:
        """The Dream runtime: the conversation model, its own role tag."""
        return LLMRuntime.capture(
            self._provider,
            model=self._model,
            context_window_tokens=self._context_window_tokens,
            role=ROLE_DREAM,
        )

    # -- execution ----------------------------------------------------------

    async def run(self, *, trigger: str = "manual") -> DreamOutcome:
        """Consume one bounded batch of journal entries.

        Only a completed run that produced a staged edit commits it; the cursor
        advances just after the write. A run that legitimately changed nothing
        still advances, because its entries have been considered.
        """
        if self._lock.locked():
            logger.info("dream.skip trigger={} reason=running", trigger)
            return DreamOutcome(status="busy", reason="a dream run is already in progress")

        async with self._lock:
            try:
                return await self._run_locked(trigger=trigger)
            except asyncio.CancelledError:
                logger.warning("dream.cancelled trigger={}", trigger)
                raise
            except Exception as exc:
                logger.warning(
                    "dream.failed trigger={} reason={}", trigger, f"{type(exc).__name__}: {exc}"
                )
                return DreamOutcome(status="failed", reason=str(exc))

    async def _run_locked(self, *, trigger: str) -> DreamOutcome:
        entries = self.journal.pending()
        if not entries:
            logger.info("dream.skip trigger={} reason=no_entries", trigger)
            return DreamOutcome(status="idle", reason="no unconsumed journal entries")

        reader = ReadMemoryTool(self.journal)
        writer = WriteMemoryTool(self.journal)
        tools = ToolRegistry()
        tools.register(reader)
        tools.register(writer)
        spec = AgentRunSpec(
            messages=[
                {"role": "system", "content": DREAM_SYSTEM_PROMPT},
                {"role": "user", "content": _render_entries(entries)},
            ],
            tools=tools,
            runtime=self.runtime(),
            max_iterations=self._max_iterations,
            max_tool_result_chars=MAX_MEMORY_CHARS,
            accepted_message_count=0,
            session_key=None,
            error_message=None,
            concurrent_tools=False,
        )
        logger.info(
            "dream.start trigger={} entries={} first={} last={} model={}",
            trigger,
            len(entries),
            entries[0].cursor,
            entries[-1].cursor,
            self._model,
        )
        result = await self._runner.run(spec)
        last_cursor = entries[-1].cursor
        if result.stop_reason != "completed":
            logger.warning(
                "dream.failed trigger={} stop_reason={} reason={}",
                trigger,
                result.stop_reason,
                result.error,
            )
            return DreamOutcome(
                status="failed",
                reason=result.error or f"run stopped with {result.stop_reason}",
                entries=len(entries),
            )
        if writer.staged is None:
            self.journal.advance(last_cursor)
            logger.info(
                "dream.done trigger={} entries={} applied=false advanced_to={}",
                trigger,
                len(entries),
                last_cursor,
            )
            return DreamOutcome(
                status="completed",
                entries=len(entries),
                applied=False,
                advanced_to=last_cursor,
            )
        self.journal.write_memory(writer.staged)
        self.journal.advance(last_cursor)
        logger.info(
            "dream.done trigger={} entries={} applied=true chars={} advanced_to={}",
            trigger,
            len(entries),
            len(writer.staged),
            last_cursor,
        )
        return DreamOutcome(
            status="completed",
            entries=len(entries),
            applied=True,
            advanced_to=last_cursor,
        )


def _render_entries(entries: list[DreamJournalEntry]) -> str:
    lines = ["Journal entries to consolidate (oldest first):", ""]
    for entry in entries:
        lines.append(f"[cursor {entry.cursor} | {entry.timestamp}]")
        lines.append(entry.content)
        lines.append("")
    return "\n".join(lines).strip()


# --- schedule --------------------------------------------------------------


@dataclass(slots=True)
class DreamScheduler:
    """Fire Dream inside a fixed weekly window, never as a catch-up run."""

    service: DreamService
    enabled: bool = True
    tick_seconds: int = DREAM_TICK_SECONDS
    clock: "Callable[[], datetime]" = field(default=lambda: datetime.now(shanghai_tz()))

    def window_open(self, now: datetime) -> bool:
        """Whether ``now`` sits inside this week's (single-tick) window."""
        local = now if now.tzinfo is not None else now.replace(tzinfo=shanghai_tz())
        if local.isocalendar()[2] != 7:  # ISO weekday: 7 == Sunday
            return False
        start = local.replace(hour=DREAM_HOUR, minute=DREAM_MINUTE, second=0, microsecond=0)
        return timedelta(0) <= (local - start) < timedelta(seconds=self.tick_seconds)

    def due(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        if not self.window_open(now):
            return False
        return self.service.journal.read_state().last_fired_week != week_key(now)

    async def tick(self) -> DreamOutcome | None:
        """Fire once per week when the window is open; ``None`` when idle."""
        now = self.clock()
        if not self.due(now):
            return None
        week = week_key(now)
        # Marked before the run: a failure must not re-trigger every tick, and a
        # missed window is explicitly not replayed.
        self.service.journal.mark_week_fired(week)
        if self.service.running:
            logger.info("dream.skip trigger=schedule reason=running week={}", week)
            return DreamOutcome(status="busy", reason="a dream run is already in progress")
        return await self.service.run(trigger="schedule")

    async def serve(self) -> None:
        """Tick until cancelled (started by the coordinator's run loop)."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive: keep the loop alive
                logger.exception("dream.schedule.tick_failed")
            await asyncio.sleep(max(1, self.tick_seconds))
