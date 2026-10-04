"""Direct-entry routing over the HTTP API.

The API is a transport over ``SessionCoordinator.process_direct``: it must not
keep its own session lock (the coordinator owns serialisation and cancellation)
and it must honour the caller's ``session_id`` so distinct conversations stay
isolated instead of collapsing onto one ``channel:chat_id`` session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.runner import AgentRunResult, AgentRunSpec
from nanoreview.api.server import create_app
from nanoreview.bus.queue import MessageBus
from nanoreview.providers.base import LLMProvider, LLMResponse


class _DummyProvider(LLMProvider):
    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


class _StubRunner:
    """Deterministic single-turn runner: user in, assistant ``ok`` out."""

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        return AgentRunResult(
            final_content="ok",
            messages=[
                *spec.frozen_messages,
                *spec.working_messages,
                {"role": "assistant", "content": "ok"},
            ],
        )


@pytest.fixture()
def loop(tmp_path: Path) -> SessionCoordinator:
    coordinator = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    coordinator.conversation_loop._runner = _StubRunner()
    return coordinator


async def _client(loop: SessionCoordinator) -> TestClient:
    client = TestClient(TestServer(create_app(loop, model_name="dummy")))
    await client.start_server()
    return client


def _user_texts(loop: SessionCoordinator, key: str) -> list[str]:
    session = loop.sessions.get_or_create(key)
    return [
        m.get("content")
        for m in session.messages
        if m.get("role") == "user"
    ]


@pytest.mark.asyncio
async def test_distinct_session_ids_keep_separate_history(
    loop: SessionCoordinator,
) -> None:
    """Two ``session_id`` values must not share one session's history."""
    client = await _client(loop)
    try:
        for session_id, text in (("conv-a", "hello A"), ("conv-b", "hello B")):
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": text}],
                    "session_id": session_id,
                },
            )
            assert response.status == 200
    finally:
        await client.close()

    assert _user_texts(loop, "api:conv-a") == ["hello A"]
    assert _user_texts(loop, "api:conv-b") == ["hello B"]


def test_api_does_not_keep_its_own_session_lock(loop: SessionCoordinator) -> None:
    """The coordinator owns serialisation; the transport keeps no lock table."""
    app = create_app(loop, model_name="dummy")

    assert "session_locks" not in app
