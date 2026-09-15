"""Tests for the read-only review report API on the WebSocket channel."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanoreview.agent.review_state import (
    ReviewArtifactStore,
    ReviewRunState,
    ReviewRunStatus,
    build_report_artifact,
)
from nanoreview.bus.queue import MessageBus
from nanoreview.channels.websocket import WebSocketChannel
from nanoreview.review.types import ReviewMetaKey
from nanoreview.session.manager import SessionManager

API_TOKEN = "test-api-token"
RUN_ID = "run-abc123def456"
FINGERPRINT = "fp-" + "a" * 60
SESSION_KEY = "websocket:chat-1"


def _make_channel(tmp_path: Path) -> WebSocketChannel:
    workspace = tmp_path / "workspace"
    channel = WebSocketChannel(
        {"enabled": True, "host": "127.0.0.1", "path": "/"},
        MessageBus(),
        session_manager=SessionManager(workspace),
    )
    channel._api_tokens[API_TOKEN] = time.monotonic() + 60.0
    return channel


def _artifact_payload() -> dict[str, Any]:
    state = ReviewRunState(
        run_id=RUN_ID,
        session_key=SESSION_KEY,
        input_fingerprint=FINGERPRINT,
        status=ReviewRunStatus.COMPLETED,
    )
    return build_report_artifact(state, report_markdown="# Code Review Report")


def _seed_review_session(
    tmp_path: Path,
    *,
    metadata_extra: dict[str, Any] | None = None,
    skip_report_ref: bool = False,
    artifact_overrides: dict[str, Any] | None = None,
    write_artifact: bool = True,
) -> None:
    """Persist a websocket session with review run metadata and artifact."""
    workspace = tmp_path / "workspace"
    manager = SessionManager(workspace)
    session = manager.get_or_create(SESSION_KEY)
    session.metadata[ReviewMetaKey.RUN_ID] = RUN_ID
    session.metadata[ReviewMetaKey.STATUS] = "completed"
    session.metadata[ReviewMetaKey.PHASE] = "done"
    session.metadata[ReviewMetaKey.INPUT_FINGERPRINT] = FINGERPRINT

    if write_artifact:
        artifact = {**_artifact_payload(), **(artifact_overrides or {})}
        store = ReviewArtifactStore(workspace)
        ref = store.write(artifact)
        if not skip_report_ref and ref is not None:
            session.metadata[ReviewMetaKey.REPORT_REF] = ref
    session.metadata.update(metadata_extra or {})
    manager.save(session)


def _report_url(key: str = SESSION_KEY) -> str:
    from urllib.parse import quote

    return f"/api/sessions/{quote(key, safe='')}/review-report"


@pytest.mark.asyncio
async def test_review_report_requires_api_token(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    client = TestClient(TestServer(channel._build_aiohttp_app()))
    await client.start_server()
    try:
        response = await client.get(_report_url())
        assert response.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_review_report_returns_404_for_missing_session(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    client = TestClient(TestServer(channel._build_aiohttp_app()))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        response = await client.get(_report_url(), headers=headers)
        assert response.status == 404
        assert (await response.json())["error"] == "session not found"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_review_report_rejects_non_websocket_sessions(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    manager = SessionManager(tmp_path / "workspace")
    session = manager.get_or_create("cli:chat-1")
    manager.save(session)

    client = TestClient(TestServer(channel._build_aiohttp_app()))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        response = await client.get(_report_url("cli:chat-1"), headers=headers)
        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_review_report_returns_404_without_review_metadata(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    manager = SessionManager(tmp_path / "workspace")
    session = manager.get_or_create(SESSION_KEY)
    manager.save(session)

    client = TestClient(TestServer(channel._build_aiohttp_app()))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        response = await client.get(_report_url(), headers=headers)
        assert response.status == 404
        assert "no review report" in (await response.json())["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_review_report_returns_404_when_report_not_generated(tmp_path: Path) -> None:
    # Run finished in error without an artifact: no review_report_ref is set.
    _seed_review_session(
        tmp_path,
        metadata_extra={ReviewMetaKey.STATUS: "error"},
        skip_report_ref=True,
        write_artifact=False,
    )
    channel = _make_channel(tmp_path)
    client = TestClient(TestServer(channel._build_aiohttp_app()))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        response = await client.get(_report_url(), headers=headers)
        assert response.status == 404
        assert "not generated" in (await response.json())["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_review_report_returns_409_on_reference_mismatch(tmp_path: Path) -> None:
    _seed_review_session(
        tmp_path,
        metadata_extra={ReviewMetaKey.REPORT_REF: "review-artifacts/run-spoofed99999.json"},
    )
    channel = _make_channel(tmp_path)
    client = TestClient(TestServer(channel._build_aiohttp_app()))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        response = await client.get(_report_url(), headers=headers)
        assert response.status == 409
        assert "reference mismatch" in (await response.json())["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_review_report_returns_409_on_fingerprint_mismatch(tmp_path: Path) -> None:
    _seed_review_session(tmp_path, artifact_overrides={"input_fingerprint": "fp-other"})
    channel = _make_channel(tmp_path)
    client = TestClient(TestServer(channel._build_aiohttp_app()))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        response = await client.get(_report_url(), headers=headers)
        assert response.status == 409
        assert "fingerprint mismatch" in (await response.json())["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_review_report_returns_409_on_corrupt_artifact(tmp_path: Path) -> None:
    _seed_review_session(tmp_path)
    workspace = tmp_path / "workspace"
    artifact_path = ReviewArtifactStore(workspace).path_for(RUN_ID)
    artifact_path.write_text("{corrupt", encoding="utf-8")

    channel = _make_channel(tmp_path)
    client = TestClient(TestServer(channel._build_aiohttp_app()))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        response = await client.get(_report_url(), headers=headers)
        assert response.status == 409
        assert "corrupt" in (await response.json())["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_review_report_returns_404_when_artifact_missing(tmp_path: Path) -> None:
    _seed_review_session(tmp_path)
    workspace = tmp_path / "workspace"
    ReviewArtifactStore(workspace).path_for(RUN_ID).unlink()

    channel = _make_channel(tmp_path)
    client = TestClient(TestServer(channel._build_aiohttp_app()))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        response = await client.get(_report_url(), headers=headers)
        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_review_report_success_returns_verified_artifact(tmp_path: Path) -> None:
    _seed_review_session(tmp_path)
    channel = _make_channel(tmp_path)
    client = TestClient(TestServer(channel._build_aiohttp_app()))
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        response = await client.get(_report_url(), headers=headers)
        assert response.status == 200
        payload = await response.json()
        assert payload["run_id"] == RUN_ID
        assert payload["status"] == "completed"
        artifact = payload["artifact"]
        assert artifact["report_markdown"] == "# Code Review Report"
        assert artifact["input_fingerprint"] == FINGERPRINT

        # The payload must not leak server filesystem layout.
        raw = json.dumps(payload)
        assert str(tmp_path.resolve()) not in raw
        assert "workspace" not in artifact
    finally:
        await client.close()
