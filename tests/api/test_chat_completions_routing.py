"""Direct-entry routing over the HTTP API.

The API is a transport over ``SessionCoordinator.process_direct``: it must not
keep its own session lock (the coordinator owns serialisation and cancellation)
and it must honour the caller's ``session_id`` so distinct conversations stay
isolated instead of collapsing onto one ``channel:chat_id`` session. It also
must treat a ``/stop``-cancelled request as a deliberate terminal answer, never
as an empty-response failure to retry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.hooks import AgentHookContext
from nanoreview.agent.runner import AgentRunResult, AgentRunSpec
from nanoreview.api.server import create_app
from nanoreview.bus.events import OutboundMessage
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


class _BlockingRunner:
    """Turn that never finishes on its own, so ``/stop`` must end it."""

    def __init__(self, *, stream: bool = False) -> None:
        self.stream = stream
        self.runs = 0
        self.started = asyncio.Event()

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.runs += 1
        if self.stream:
            assert spec.hook is not None
            context = AgentHookContext(
                iteration=0, messages=[*spec.frozen_messages, *spec.working_messages]
            )
            await spec.hook.on_stream(context, "partial ")
        self.started.set()
        await asyncio.sleep(10)
        return AgentRunResult(
            final_content="late",
            messages=[*spec.frozen_messages, *spec.working_messages],
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


async def _post_stop(client: TestClient, session_id: str) -> Any:
    return await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "/stop"}],
            "session_id": session_id,
        },
    )


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


@pytest.mark.asyncio
async def test_stopped_non_streaming_request_is_not_retried(
    loop: SessionCoordinator,
) -> None:
    """A ``/stop`` answer is terminal: the API must not retry it as empty.

    Without the stop flag the empty-reply guard would re-issue the whole turn,
    so a stop would silently run the model a second time.
    """
    runner = _BlockingRunner()
    loop.conversation_loop._runner = runner
    client = await _client(loop)
    try:
        pending = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "long job"}],
                    "session_id": "stop-a",
                },
            )
        )
        await asyncio.wait_for(runner.started.wait(), timeout=2.0)
        stop = await _post_stop(client, "stop-a")
        stopped = await asyncio.wait_for(pending, timeout=2.0)
        body = await stopped.json()
    finally:
        await client.close()

    assert stop.status == 200
    assert stopped.status == 200
    assert body["choices"][0]["message"]["content"] == "Stopped."
    assert runner.runs == 1  # no empty-response retry behind the stop


@pytest.mark.asyncio
async def test_stopped_streaming_request_appends_the_note_once(
    loop: SessionCoordinator,
) -> None:
    """A stopped SSE request keeps its deltas, adds one note and closes."""
    runner = _BlockingRunner(stream=True)
    loop.conversation_loop._runner = runner
    client = await _client(loop)
    try:
        pending = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "long job"}],
                    "session_id": "stop-b",
                    "stream": True,
                },
            )
        )
        await asyncio.wait_for(runner.started.wait(), timeout=2.0)
        await _post_stop(client, "stop-b")
        response = await asyncio.wait_for(pending, timeout=2.0)
        text = await response.text()
    finally:
        await client.close()

    assert response.status == 200
    assert "partial" in text
    assert text.count("Stopped.") == 1  # the explanation is sent exactly once
    assert text.rstrip().endswith("data: [DONE]")  # stream was closed


@pytest.mark.asyncio
async def test_stop_reply_without_text_is_not_retried(
    loop: SessionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop flag alone suppresses the empty-reply retry, not the text."""
    calls: list[str] = []

    async def _fake_process_direct(content: str, **_kwargs: Any) -> OutboundMessage:
        calls.append(content)
        return OutboundMessage(
            channel="api",
            chat_id="x",
            content="",
            metadata={"stop_reason": "stopped"},
        )

    monkeypatch.setattr(loop, "process_direct", _fake_process_direct)
    client = await _client(loop)
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "long job"}],
                "session_id": "stop-empty",
            },
        )
    finally:
        await client.close()

    assert response.status == 200
    assert calls == ["long job"]  # exactly one attempt: no retry behind the stop


