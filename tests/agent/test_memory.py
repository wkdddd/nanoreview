from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.context import ContextBuilder
from nanoreview.agent.memory import Consolidator, MemoryStore
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.session.manager import Session, SessionManager


class SummaryProvider(LLMProvider):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        return LLMResponse(content="session-only summary")

    def get_default_model(self) -> str:
        return "summary-model"


def test_system_prompt_uses_personalization_without_long_term_memory(tmp_path) -> None:
    (tmp_path / "COMMON_RULES.md").write_text("shared rules voice", encoding="utf-8")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("old global fact", encoding="utf-8")
    (memory_dir / "history.jsonl").write_text(
        '{"cursor":1,"timestamp":"2026-06-20 10:00","content":"old review"}\n',
        encoding="utf-8",
    )

    prompt = ContextBuilder(tmp_path).build_system_prompt()

    assert "shared rules voice" in prompt
    assert "old global fact" not in prompt
    assert "old review" not in prompt
    assert "Recent History" not in prompt
    assert "Long-term Memory" not in prompt


def test_bootstrap_files_read_common_rules_instead_of_soul() -> None:
    assert "COMMON_RULES.md" in ContextBuilder.BOOTSTRAP_FILES
    assert not any(name.upper().startswith("SOUL") for name in ContextBuilder.BOOTSTRAP_FILES)


def test_load_common_rules_reads_and_strips_workspace_file(tmp_path) -> None:
    (tmp_path / "COMMON_RULES.md").write_text("  shared rules  \n", encoding="utf-8")

    assert ContextBuilder.load_common_rules(tmp_path) == "shared rules"


def test_load_common_rules_missing_file_returns_empty(tmp_path) -> None:
    # A missing rules file must never raise; callers skip the section.
    assert ContextBuilder.load_common_rules(tmp_path) == ""


@pytest.mark.asyncio
async def test_consolidation_summary_is_session_scoped_metadata(tmp_path) -> None:
    session = Session(key="test:session")
    session.add_message("user", "first request")
    session.add_message("assistant", "first answer")
    sessions = SessionManager(tmp_path)
    consolidator = Consolidator(
        store=MemoryStore(tmp_path),
        provider=SummaryProvider(),
        model="summary-model",
        sessions=sessions,
        context_window_tokens=8192,
        build_messages=lambda **kwargs: [
            {"role": "system", "content": "system"},
            *kwargs["history"],
        ],
        get_tool_definitions=lambda: [],
    )

    summary = await consolidator.archive(session.messages)
    consolidator._persist_last_summary(session, summary)

    assert summary == "session-only summary"
    assert session.metadata["_last_summary"]["text"] == "session-only summary"
    assert not (tmp_path / "memory" / "history.jsonl").exists()
    assert not (tmp_path / "memory" / "MEMORY.md").exists()


def _consolidator(workspace, sessions: SessionManager) -> Consolidator:
    """Real ``build_messages``: the probe must see what the prompt would."""
    return Consolidator(
        store=MemoryStore(workspace),
        provider=SummaryProvider(),
        model="summary-model",
        sessions=sessions,
        context_window_tokens=8192,
        build_messages=ContextBuilder(workspace).build_messages,
        get_tool_definitions=lambda: [],
    )


@pytest.mark.asyncio
async def test_last_summary_survives_a_restart(tmp_path) -> None:
    """The summary is written to JSONL and read back by a fresh manager.

    A restart must not lose it: consolidation hides old turns from the replay
    window, so this text is the only carrier of them into later turns.
    """
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:session")
    session.add_message("user", "first request")
    session.add_message("assistant", "first answer")

    consolidator = _consolidator(tmp_path, sessions)
    summary = await consolidator.archive(session.messages)
    consolidator._persist_last_summary(session, summary)
    assert summary == "session-only summary"

    # Restart: a brand-new manager has an empty cache and must read the JSONL.
    restarted = SessionManager(tmp_path)
    reloaded = restarted.get_or_create("test:session")

    assert reloaded is not session
    assert reloaded.metadata["_last_summary"]["text"] == "session-only summary"
    assert Consolidator.last_summary_text(reloaded) == "session-only summary"


def test_last_summary_text_reads_both_metadata_shapes() -> None:
    session = Session(key="test:session")
    assert Consolidator.last_summary_text(session) is None

    session.metadata["_last_summary"] = {
        "text": "summary body",
        "last_active": "2026-10-06T10:00:00",
    }
    assert Consolidator.last_summary_text(session) == "summary body"

    # Legacy bare-string shape written before the dict form.
    session.metadata["_last_summary"] = "legacy body"
    assert Consolidator.last_summary_text(session) == "legacy body"

    session.metadata["_last_summary"] = {"text": "", "last_active": "2026-10-06"}
    assert Consolidator.last_summary_text(session) is None


def test_reloaded_summary_participates_in_token_estimation(tmp_path) -> None:
    """The probe counts the summary it reloaded from disk."""
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:session")
    session.add_message("user", "hello")
    session.metadata["_last_summary"] = {
        "text": "archived conversation context " * 200,
        "last_active": session.updated_at.isoformat(),
    }
    sessions.save(session)

    restarted = SessionManager(tmp_path)
    reloaded = restarted.get_or_create("test:session")
    consolidator = _consolidator(tmp_path, restarted)

    with_summary, source = consolidator.estimate_session_prompt_tokens(reloaded)
    assert source != "error"

    reloaded.metadata.pop("_last_summary")
    without_summary, _ = consolidator.estimate_session_prompt_tokens(reloaded)

    assert with_summary > without_summary


# --- Repeated consolidation must accumulate, not overwrite, the summary -------

_MARKER = re.compile(r"MARK-\d+")


class MarkerSummaryProvider(LLMProvider):
    """Summarize by echoing every ``MARK-nn`` marker the request carried."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[str] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        body = "\n".join(str(message.get("content", "")) for message in messages)
        self.requests.append(body)
        markers = sorted(set(_MARKER.findall(body)))
        return LLMResponse(content="SUMMARY: " + " ".join(markers))

    def get_default_model(self) -> str:
        return "summary-model"


def _marker_consolidator(
    workspace: Path,
    sessions: SessionManager,
    provider: LLMProvider,
    *,
    context_window_tokens: int = 4000,
) -> Consolidator:
    return Consolidator(
        store=MemoryStore(workspace),
        provider=provider,
        model="summary-model",
        sessions=sessions,
        context_window_tokens=context_window_tokens,
        max_completion_tokens=256,
        build_messages=ContextBuilder(workspace).build_messages,
        get_tool_definitions=lambda: [],
    )


def _seed_turns(session: Session, start: int, count: int) -> None:
    filler = "alpha beta gamma delta " * 6
    for offset in range(start, start + count):
        session.add_message("user", f"MARK-{offset:02d} {filler}")
        session.add_message("assistant", f"reply {offset:02d} {filler}")


@pytest.mark.asyncio
async def test_archive_folds_a_previous_summary_into_the_request(tmp_path) -> None:
    """The earlier summary must be an input, not something the new one replaces."""
    sessions = SessionManager(tmp_path)
    provider = MarkerSummaryProvider()
    consolidator = _marker_consolidator(tmp_path, sessions, provider)

    summary = await consolidator.archive(
        [{"role": "user", "content": "MARK-99 new turn"}],
        previous_summary="SUMMARY: MARK-00 earlier decision",
    )

    assert "MARK-00" in provider.requests[0]
    assert "MARK-99" in provider.requests[0]
    assert "MARK-00" in summary
    assert "MARK-99" in summary


def test_raw_archive_keeps_the_previous_summary(tmp_path) -> None:
    """The degraded path must not drop already-archived turns either."""
    consolidator = _marker_consolidator(
        tmp_path, SessionManager(tmp_path), MarkerSummaryProvider()
    )

    raw = consolidator.raw_archive(
        [{"role": "user", "content": "MARK-99 new turn"}],
        previous_summary="SUMMARY: MARK-00 earlier decision",
    )

    assert raw is not None
    assert "MARK-00" in raw
    assert "MARK-99" in raw


@pytest.mark.asyncio
async def test_consolidation_rounds_accumulate_into_one_summary(tmp_path) -> None:
    """Two rounds in one call: the second must fold the first in, not replace it.

    The replay window trims an early chunk and token pressure trims the rest, so
    a single call archives twice. Without folding, the second summary overwrites
    the first and the earliest turns — already hidden by ``last_consolidated`` —
    disappear from ``_last_summary`` and from every later prompt.
    """
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:session")
    _seed_turns(session, 0, 30)

    provider = MarkerSummaryProvider()
    consolidator = _marker_consolidator(tmp_path, sessions, provider)
    rounds: list[int] = []
    original_archive = consolidator.archive

    async def _recording_archive(messages, **kwargs):
        rounds.append(len(messages))
        return await original_archive(messages, **kwargs)

    consolidator.archive = _recording_archive  # type: ignore[method-assign]

    await consolidator.maybe_consolidate_by_tokens(session, replay_max_messages=30)

    assert len(rounds) >= 2
    first_round_markers = set(_MARKER.findall(provider.requests[0]))
    assert "MARK-00" in first_round_markers
    summary = Consolidator.last_summary_text(session) or ""
    assert first_round_markers <= set(_MARKER.findall(summary))


@pytest.mark.asyncio
async def test_a_later_consolidation_keeps_the_earlier_summary(tmp_path) -> None:
    """Consecutive turns of consolidation must not lose the earlier summary."""
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:session")
    _seed_turns(session, 0, 30)

    provider = MarkerSummaryProvider()
    consolidator = _marker_consolidator(tmp_path, sessions, provider)

    await consolidator.maybe_consolidate_by_tokens(session)
    first = Consolidator.last_summary_text(session) or ""
    assert "MARK-00" in first
    consolidated_once = session.last_consolidated
    assert consolidated_once > 0

    _seed_turns(session, 30, 20)
    await consolidator.maybe_consolidate_by_tokens(session)

    assert session.last_consolidated > consolidated_once
    later = Consolidator.last_summary_text(session) or ""
    assert set(_MARKER.findall(first)) <= set(_MARKER.findall(later))

