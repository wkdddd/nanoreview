"""WebSocket review submissions go through the shared admission boundary.

The transport must translate an admission rejection into a structured event
and leave session metadata, history, and the pending queue untouched so the
user can correct the target and resubmit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.loop import AgentLoop
from nanoreview.bus.queue import MessageBus
from nanoreview.channels.websocket import WebSocketChannel
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review.types import ReviewMetaKey


class _DummyProvider(LLMProvider):
    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


class _FakeConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(raw) for raw in self.sent]


@pytest.fixture()
def channel(tmp_path: Path) -> WebSocketChannel:
    loop = AgentLoop(MessageBus(), _DummyProvider(), tmp_path)
    return WebSocketChannel({}, MessageBus(), agent_loop=loop)


@pytest.mark.asyncio
async def test_rejected_submission_emits_structured_error_event(
    tmp_path: Path, channel: WebSocketChannel
) -> None:
    connection = _FakeConnection()
    metadata: dict[str, Any] = {}

    approved, content = await channel._admit_review_submission(
        connection,
        "chat-1",
        "relative/app.py",
        "local",
        "repo",
        ["bug"],
        metadata,
    )

    assert approved is False
    assert content == ""
    # Rejection leaves nothing for the handler to deliver.
    assert metadata == {}
    events = connection.events()
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "relative_path_not_allowed"
    assert events[-1]["field"] == "target"
    assert "relative_path_not_allowed" in events[-1]["detail"]

    loop = channel._agent_loop  # type: ignore[attr-defined]
    persisted = loop.sessions.get_or_create("websocket:chat-1")
    assert ReviewMetaKey.RUN_ID not in persisted.metadata


@pytest.mark.asyncio
async def test_accepted_submission_carries_admitted_marker(
    tmp_path: Path, channel: WebSocketChannel
) -> None:
    target = tmp_path / "pkg"
    target.mkdir()
    (target / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    connection = _FakeConnection()
    metadata: dict[str, Any] = {}

    approved, content = await channel._admit_review_submission(
        connection,
        "chat-2",
        str(target),
        "local",
        "repo",
        ["bug"],
        metadata,
    )

    assert approved is True
    assert content
    assert metadata["review_target"] == str(target)
    assert metadata["review_action"] == "repo"
    assert metadata["_review_admitted"]
    assert connection.events() == []
    loop = channel._agent_loop  # type: ignore[attr-defined]
    state = loop._review_runs["websocket:chat-2"]
    assert state.run_id == metadata["_review_admitted"]
