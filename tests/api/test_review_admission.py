"""Structured review entry over the HTTP API.

The API is a *transport*: it must not re-implement review validation. These
tests pin the contract the WebUI/CLI share — a rejected submission returns a
4xx with a stable code and leaves the session untouched, and an accepted one
hands the pre-registered run to the executor.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.review_loop import ReviewLoopOutcome
from nanoreview.agent.review_state import ReviewRunState
from nanoreview.api.server import create_app
from nanoreview.bus.queue import MessageBus
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review.types import ReviewMetaKey


class _DummyProvider(LLMProvider):
    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _make_reviewable_repo(root: Path, rel: str = "pkg/mod.py") -> Path:
    """Init a repo with a committed baseline, then leave one file changed."""
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    path.write_text("VALUE = 2\n", encoding="utf-8")
    return path.parent


@pytest.fixture()
def loop(tmp_path: Path) -> SessionCoordinator:
    return SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)


async def _client(loop: SessionCoordinator) -> TestClient:
    client = TestClient(TestServer(create_app(loop, model_name="dummy")))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_relative_target_is_rejected_with_structured_code(
    tmp_path: Path, loop: SessionCoordinator
) -> None:
    client = await _client(loop)
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": ""}], "review": {"target": "app.py"}},
        )
        assert response.status == 400
        payload = await response.json()
    finally:
        await client.close()

    assert payload["error"] == "review_not_admitted"
    assert payload["code"] == "relative_path_not_allowed"
    # A rejected submission creates no session metadata on the API session.
    assert ReviewMetaKey.RUN_ID not in loop.sessions.get_or_create("api:default").metadata


@pytest.mark.asyncio
async def test_missing_target_returns_404(tmp_path: Path, loop: SessionCoordinator) -> None:
    client = await _client(loop)
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": ""}],
                "review": {"target": str(tmp_path / "missing")},
            },
        )
        assert response.status == 404
        payload = await response.json()
    finally:
        await client.close()

    assert payload["code"] == "target_not_found"


@pytest.mark.asyncio
async def test_accepted_review_delegates_the_registered_run(
    tmp_path: Path, loop: SessionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_reviewable_repo(tmp_path)

    observed: dict[str, Any] = {}

    async def _fake_execute(request):
        state: ReviewRunState | None = loop._review_runs.get("api:default")
        observed["run_id"] = state.run_id if state else None
        observed["metadata"] = dict(request.msg.metadata)
        return ReviewLoopOutcome(report_markdown="ok", produces_report=False)

    monkeypatch.setattr(loop.review_loop, "execute", _fake_execute)
    client = await _client(loop)
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": ""}],
                "review": {"target": str(target), "action": "diff"},
            },
        )
        assert response.status == 200
    finally:
        await client.close()

    assert observed["run_id"]
    assert observed["metadata"]["_review_admitted"] == observed["run_id"]
    assert observed["metadata"]["review_target"] == str(target)
    assert observed["metadata"]["review_action"] == "diff"
