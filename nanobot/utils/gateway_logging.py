"""Persistent JSONL logging for the WebUI gateway process."""

from __future__ import annotations

import hashlib
import json
import re
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_logs_dir
from nanobot.utils.log_sanitization import sanitize_persisted_log_text

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LOGURU_COLOR_TAG = re.compile(
    r"</?(?:black|red|green|yellow|blue|magenta|cyan|white|"
    r"bold|dim|n|normal|italic|underline|blink|reverse|hidden|strike)>",
    re.IGNORECASE,
)


def gateway_log_path(workspace: Path, run_id: str | None = None) -> Path:
    """Return the JSONL log path for one gateway run and workspace."""
    workspace_id = hashlib.sha256(
        str(workspace.expanduser().resolve(strict=False)).encode("utf-8")
    ).hexdigest()[:32]
    if run_id is None:
        started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        run_id = f"{started_at}-{uuid.uuid4().hex[:12]}"
    return get_logs_dir() / workspace_id / f"{run_id}.jsonl"


@dataclass(frozen=True)
class GatewayLogHandle:
    """Owns the loguru handler installed for one gateway lifecycle."""

    path: Path
    handler_id: int

    def close(self) -> None:
        """Flush and remove the gateway file sink."""
        logger.remove(self.handler_id)


class _GatewayJsonlSink:
    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, message: Any) -> None:
        record = message.record
        payload = {
            "timestamp": record["time"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record["level"].name,
            "channel": _channel_from_record(record),
            "message": _clean_text(str(record["message"])),
            "exception": _exception_from_record(record.get("exception")),
        }
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def configure_gateway_file_logging(workspace: Path) -> GatewayLogHandle:
    """Add a DEBUG JSONL sink for the lifetime of one WebUI gateway."""
    path = gateway_log_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=False)
    path.chmod(0o600)
    handler_id = logger.add(
        _GatewayJsonlSink(path).write,
        level="DEBUG",
        format="{message}",
        colorize=False,
        backtrace=False,
        diagnose=False,
        enqueue=True,
    )
    return GatewayLogHandle(path=path, handler_id=handler_id)


def _channel_from_record(record: dict[str, Any]) -> str | None:
    channel = record.get("extra", {}).get("channel")
    return channel if isinstance(channel, str) and channel else None


def _exception_from_record(exception: Any) -> str | None:
    if exception is None:
        return None
    return _clean_text(
        "".join(traceback.format_exception(exception.type, exception.value, exception.traceback))
    )


def _clean_text(text: str) -> str:
    return _LOGURU_COLOR_TAG.sub("", _ANSI_ESCAPE.sub("", sanitize_persisted_log_text(text)))
