"""Long-term memory wiring: journal appends, the turn snapshot, ``/dream``.

The pieces are tested on their own in ``test_dream.py`` and
``test_context_governance.py``; this module pins how they are connected:

- only the conversation turn appends to the journal;
- the turn reads ``memory/MEMORY.md`` once, as a snapshot, and the review roles
  sharing the same context builder never see it;
- ``/dream`` runs the consolidation on demand and reports what happened, and
  ``dream.enabled`` only gates the automatic schedule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.dream import MemoryJournal
from nanoreview.agent.runner import AgentRunResult, AgentRunSpec
from nanoreview.bus.events import InboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.command.builtin import cmd_dream
from nanoreview.command.router import CommandContext
from nanoreview.config.schema import Config, DreamConfig
from nanoreview.providers.base import LLMProvider, LLMResponse


class DummyProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


class CapturingRunner:
    """Records the spec and echoes the transcript, like the real runner."""

    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return AgentRunResult(final_content="ok", messages=list(spec.messages))


def _coordinator(tmp_path: Path, **kwargs: Any) -> SessionCoordinator:
    return SessionCoordinator(MessageBus(), DummyProvider(), tmp_path, **kwargs)


def _msg(text: str = "hello") -> InboundMessage:
    return InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=text)


# --- journal wiring --------------------------------------------------------


def test_the_conversation_loop_appends_archived_summaries(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)

    coordinator._record_conversation_summary("cli:direct", "earlier decisions")

    entries = MemoryJournal(tmp_path).pending()
    assert len(entries) == 1
    assert "cli:direct" in entries[0].content
    assert "earlier decisions" in entries[0].content


def test_an_empty_summary_is_not_journaled(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)

    coordinator._record_conversation_summary("cli:direct", "   ")

    assert MemoryJournal(tmp_path).pending() == []


@pytest.mark.asyncio
async def test_the_turn_reads_the_memory_snapshot(tmp_path: Path) -> None:
    memory = MemoryJournal(tmp_path)
    memory.directory.mkdir(parents=True)
    memory.memory_path.write_text("- the user prefers tabs\n", encoding="utf-8")
    coordinator = _coordinator(tmp_path)
    runner = CapturingRunner()
    coordinator.conversation_loop._runner = runner

    await coordinator.conversation_loop.process_message(
        _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
    )

    system = runner.specs[0].messages[0]["content"]
    assert "# Long-term Memory" in system
    assert "the user prefers tabs" in system


@pytest.mark.asyncio
async def test_a_turn_without_a_memory_file_has_no_memory_section(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    runner = CapturingRunner()
    coordinator.conversation_loop._runner = runner

    await coordinator.conversation_loop.process_message(
        _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
    )

    assert "# Long-term Memory" not in runner.specs[0].messages[0]["content"]


@pytest.mark.asyncio
async def test_the_review_roles_never_see_the_memory_snapshot(tmp_path: Path) -> None:
    """Review prompts share the context builder, so the snapshot is opt-in only."""
    memory = MemoryJournal(tmp_path)
    memory.directory.mkdir(parents=True)
    memory.memory_path.write_text("- SECRET-MEMORY-FACT\n", encoding="utf-8")
    coordinator = _coordinator(tmp_path)

    review_prompt = "\n".join(
        str(message.get("content"))
        for message in coordinator.context.build_messages(
            history=[], current_message="review this diff"
        )
    )

    assert "SECRET-MEMORY-FACT" not in review_prompt


# --- /dream ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_dream_reports_an_idle_journal(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)

    response = await cmd_dream(
        CommandContext(
            msg=_msg("/dream"),
            session=None,
            key="cli:direct",
            raw="/dream",
            args="",
            loop=coordinator,
        )
    )

    assert "no new entries" in response.content


@pytest.mark.asyncio
async def test_dream_applies_a_rewrite_and_reports_it(tmp_path: Path) -> None:
    journal = MemoryJournal(tmp_path)
    journal.append("we chose ruff over flake8")

    class WritingRunner:
        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            await spec.tools.get("write_memory").execute(content="- uses ruff\n")
            return AgentRunResult(final_content="done", messages=list(spec.messages))

    coordinator = _coordinator(tmp_path)
    coordinator.dream._runner = WritingRunner()

    response = await cmd_dream(
        CommandContext(
            msg=_msg("/dream"),
            session=None,
            key="cli:direct",
            raw="/dream",
            args="",
            loop=coordinator,
        )
    )

    assert "into memory/MEMORY.md" in response.content
    assert journal.read_memory() == "- uses ruff\n"
    assert journal.read_state().last_cursor == 1


@pytest.mark.asyncio
async def test_dream_works_even_when_the_schedule_is_disabled(tmp_path: Path) -> None:
    """``dream.enabled`` gates the automatic run only."""
    coordinator = _coordinator(tmp_path, dream_config=DreamConfig(enabled=False))
    MemoryJournal(tmp_path).append("something worth keeping")

    outcome = await coordinator.run_dream()

    assert outcome.status == "completed"
    assert coordinator.dream_scheduler.enabled is False


def test_the_schedule_is_enabled_by_default(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path, dream_config=DreamConfig())

    assert coordinator.dream_scheduler.enabled is True


def test_the_schedule_is_disabled_by_config(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path, dream_config=DreamConfig(enabled=False))

    assert coordinator.dream_scheduler.enabled is False


def test_from_config_reads_the_dream_block(tmp_path: Path) -> None:
    config = Config()
    config.dream = DreamConfig(enabled=False)
    config.agents.defaults.workspace = str(tmp_path)

    coordinator = SessionCoordinator.from_config(config, provider=DummyProvider())

    assert coordinator.dream_scheduler.enabled is False
