"""Contract tests for the real ``ConversationLoop``.

``ConversationLoop`` owns one complete conversation turn: session/history,
handoff consumption, frozen/working context, a core-only ``ToolRegistry``, one
``AgentRunner`` run, history persistence and reply assembly. These tests pin the
boundary the coordinator and the review side rely on:

* the conversation core never sees the review-only tools (``local_review`` /
  ``github_review``);
* one turn runs exactly one runner and persists the user/assistant history;
* the review handoff is consumed *once*, as a replayable history message, and
  the report artifact is never copied into session metadata;
* ``set_runtime_model`` moves the model id and context window for later turns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.conversation_loop import (
    MAX_PENDING_CONVERSATION_MESSAGES,
    ConversationLoop,
)
from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.handoff import REVIEW_HANDOFF_EVENT, ReviewHandoff
from nanoreview.agent.review_state import ReviewRunStatus
from nanoreview.agent.runner import AgentRunResult, AgentRunSpec
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.bus.events import InboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review.result import ReviewHandoffState, ReviewResult
from nanoreview.review.types import ReviewMetaKey

REPORT_REF = "review-artifacts/run-a.json"


class DummyProvider(LLMProvider):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        _ = (tools, model, max_tokens, temperature, reasoning_effort, tool_choice)
        _ = response_format
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


class SpecCapturingRunner:
    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return AgentRunResult(
            final_content="ok",
            messages=[*spec.frozen_messages, *spec.working_messages],
        )


def _loop(tmp_path: Path) -> ConversationLoop:
    return SessionCoordinator(MessageBus(), DummyProvider(), tmp_path).conversation_loop


def _msg(content: str = "hello", *, chat_id: str = "direct") -> InboundMessage:
    return InboundMessage(
        channel="cli", sender_id="user", chat_id=chat_id, content=content
    )


def _handoff() -> ReviewHandoff:
    result = ReviewResult(
        run_id="run-a",
        session_key="cli:review",
        status=ReviewRunStatus.COMPLETED,
        handoff=ReviewHandoffState.COMPLETE,
        report_ref=REPORT_REF,
        coverage=("security",),
    )
    return ReviewHandoff(
        result=result, report_markdown="## Report\n\nNo issues.", fits=True
    )


def test_the_pending_cap_is_the_documented_bound() -> None:
    assert MAX_PENDING_CONVERSATION_MESSAGES == 20


@pytest.mark.asyncio
async def test_review_tools_are_excluded_from_the_conversation_turn(tmp_path) -> None:
    loop = _loop(tmp_path)
    runner = SpecCapturingRunner()
    loop._runner = runner

    await loop.process_message(
        _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
    )

    assert len(runner.specs) == 1
    tools = runner.specs[0].tools
    assert isinstance(tools, ToolRegistry)
    assert not tools.has("local_review")
    assert not tools.has("github_review")
    # The ordinary conversation tools stay available.
    assert tools.has("read_file")
    # Sub-agent dispatch is review-only now: the conversation turn must not
    # expose a spawn tool.
    assert not tools.has("spawn")


@pytest.mark.asyncio
async def test_a_conversation_turn_persists_history_and_returns_a_reply(
    tmp_path,
) -> None:
    loop = _loop(tmp_path)

    response = await loop.process_message(
        _msg("hello there"),
        session_key="cli:direct",
        turn_id="t1",
        target_root=tmp_path,
    )

    assert response is not None
    assert response.content == "ok"
    session = loop._sessions.get_or_create("cli:direct")
    roles = [message.get("role") for message in session.messages]
    assert roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_the_handoff_is_consumed_once_and_by_reference(tmp_path) -> None:
    loop = _loop(tmp_path)
    handoff = _handoff()

    await loop.process_message(
        _msg("follow-up"),
        session_key="cli:review",
        turn_id="t1",
        target_root=tmp_path,
        handoff=handoff,
    )

    session = loop._sessions.get_or_create("cli:review")
    injected = [
        message
        for message in session.messages
        if message.get("injected_event") == REVIEW_HANDOFF_EVENT
    ]
    assert len(injected) == 1
    assert injected[0]["review_run_id"] == "run-a"
    assert session.metadata[ReviewMetaKey.HANDOFF_RUN_ID] == "run-a"
    # The report artifact is referenced, never copied into session metadata.
    assert "report_markdown" not in str(session.metadata)


def test_set_runtime_model_moves_model_and_window(tmp_path) -> None:
    loop = _loop(tmp_path)

    loop.set_runtime_model(DummyProvider(), "switched-model", 131_072)

    assert loop._model == "switched-model"
    assert loop._context_window_tokens == 131_072


def _handoff_with_report(report: str) -> ReviewHandoff:
    handoff = _handoff()
    return ReviewHandoff(result=handoff.result, report_markdown=report, fits=True)


_REPORT_WITH_IDS = (
    "## Code Review Report: repo\n\n### Findings\n\n"
    "| ID | Dimension | File | Issue | Impact |\n"
    "|----|-----------|------|-------|--------|\n"
    "| F001 | bug | a.py:1 | Null deref | crash |\n"
)


def _capture_finding_refs(loop: ConversationLoop) -> dict[str, Any]:
    """Spy on the transient reference collection without changing behaviour."""
    captured: dict[str, Any] = {}
    original = loop._collect_finding_refs

    def spy(ctx) -> None:
        original(ctx)
        captured["valid"] = set(ctx.valid_finding_ids)
        captured["refs"] = list(ctx.finding_refs)

    loop._collect_finding_refs = spy
    return captured


@pytest.mark.asyncio
async def test_turn_collects_the_finding_ids_the_user_named(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop._runner = SpecCapturingRunner()
    captured = _capture_finding_refs(loop)

    await loop.process_message(
        _msg("please fix F001, and ignore F999"),
        session_key="cli:review",
        turn_id="t1",
        target_root=tmp_path,
        handoff=_handoff_with_report(_REPORT_WITH_IDS),
    )

    assert captured["valid"] == {"F001"}
    # Only IDs present in the report survive; the phantom F999 is dropped.
    assert captured["refs"] == ["F001"]


@pytest.mark.asyncio
async def test_turn_without_a_report_collects_no_references(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop._runner = SpecCapturingRunner()
    captured = _capture_finding_refs(loop)

    await loop.process_message(
        _msg("fix F001"),
        session_key="cli:direct",
        turn_id="t1",
        target_root=tmp_path,
    )

    assert captured["refs"] == []
