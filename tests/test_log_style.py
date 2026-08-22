from __future__ import annotations

import json
import logging
from pathlib import Path

from loguru import logger

import nanoreview.utils.gateway_logging as gateway_logging
from nanoreview.utils.gateway_logging import configure_gateway_file_logging, gateway_log_path
from nanoreview.utils.log_style import event_message
from nanoreview.utils.logging_bridge import redirect_lib_logging


class _LogSink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message: str) -> None:
        self.messages.append(message)

    @property
    def text(self) -> str:
        return "".join(self.messages)


def test_log_event_message_uses_status_symbol_and_key_values() -> None:
    message = event_message(
        "review.evidence.local.done",
        status="success",
        trace_id="trace-1",
        hits=2,
    )

    assert message.startswith("<green>✓</green> review.evidence.local.done")
    assert "status=success" in message
    assert "trace_id=trace-1" in message
    assert "hits=2" in message


def test_logging_bridge_routes_stdlib_once_with_structured_event() -> None:
    name = "nanoreview.test.bridge"
    lib_logger = logging.getLogger(name)
    lib_logger.handlers = []
    lib_logger.propagate = True
    sink = _LogSink()
    handler_id = logger.add(sink, level="INFO", format="{message}")
    try:
        redirect_lib_logging(name, level="WARNING")
        redirect_lib_logging(name, level="WARNING")
        lib_logger.warning("socket closed")
    finally:
        logger.remove(handler_id)
        lib_logger.handlers = []
        lib_logger.propagate = True

    assert sink.text.count("lib.log") == 1
    assert "status=warning" in sink.text
    assert f"lib={name}" in sink.text
    assert "message='socket closed'" in sink.text


def test_gateway_log_path_is_workspace_scoped(monkeypatch, tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(gateway_logging, "get_logs_dir", lambda: logs_dir)

    first = gateway_log_path(tmp_path / "workspace-a", run_id="run-a")
    second = gateway_log_path(tmp_path / "workspace-b", run_id="run-a")

    assert first.parent.parent == logs_dir
    assert first.parent != second.parent
    assert first.name == "run-a.jsonl"


def test_gateway_file_logging_writes_sanitized_jsonl(monkeypatch, tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(gateway_logging, "get_logs_dir", lambda: logs_dir)
    handle = configure_gateway_file_logging(tmp_path / "workspace")
    try:
        logger.bind(channel="websocket").debug(
            "<green>debug</green> api_key=super-secret-value Authorization: Bearer abcdefghijklmnop"
        )
        try:
            raise RuntimeError("token=exception-secret")
        except RuntimeError:
            logger.bind(channel="websocket").exception("gateway request failed")
    finally:
        handle.close()

    records = [json.loads(line) for line in handle.path.read_text(encoding="utf-8").splitlines()]

    assert len(records) == 2
    assert records[0] == {
        "timestamp": records[0]["timestamp"],
        "level": "DEBUG",
        "channel": "websocket",
        "message": "debug api_key=***REDACTED*** Authorization: Bearer ***REDACTED***",
        "exception": None,
    }
    assert records[1]["level"] == "ERROR"
    assert records[1]["channel"] == "websocket"
    assert records[1]["exception"] is not None
    assert "exception-secret" not in records[1]["exception"]
    assert "***REDACTED***" in records[1]["exception"]
