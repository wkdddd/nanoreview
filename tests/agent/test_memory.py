from __future__ import annotations

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
    (tmp_path / "SOUL.md").write_text("soul voice", encoding="utf-8")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("old global fact", encoding="utf-8")
    (memory_dir / "history.jsonl").write_text(
        '{"cursor":1,"timestamp":"2026-06-20 10:00","content":"old review"}\n',
        encoding="utf-8",
    )

    prompt = ContextBuilder(tmp_path).build_system_prompt()

    assert "soul voice" in prompt
    assert "old global fact" not in prompt
    assert "old review" not in prompt
    assert "Recent History" not in prompt
    assert "Long-term Memory" not in prompt


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
