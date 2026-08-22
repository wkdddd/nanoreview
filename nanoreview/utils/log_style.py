"""Small helpers for consistent internal log event styling."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_STYLE_BY_STATUS: Mapping[str, tuple[str, str]] = {
    "success": ("✓", "green"),
    "done": ("✓", "green"),
    "ok": ("✓", "green"),
    "start": ("▶", "blue"),
    "running": ("▶", "blue"),
    "retry": ("⚠", "yellow"),
    "fallback": ("⚠", "yellow"),
    "warning": ("⚠", "yellow"),
    "warn": ("⚠", "yellow"),
    "error": ("✗", "red"),
    "failed": ("✗", "red"),
    "failure": ("✗", "red"),
}

_NEUTRAL_STATUSES = {
    "skip",
    "skipped",
    "no_hits",
    "empty",
    "empty_query",
    "empty_files",
    "no_terms",
    "neutral",
    "missing",
}


def event_message(event: str, *, status: str | None = None, **fields: Any) -> str:
    """Build a consistent log event message with a colored status symbol."""
    marker = _status_marker(status)
    parts = [marker, event]
    if status:
        parts.append(f"status={_format_value(status)}")
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_format_value(value)}")
    return " ".join(parts)


def log_event(log: Any, level: str, event: str, *, status: str | None = None, **fields: Any) -> None:
    """Log a styled event using loguru color tags when the sink supports them."""
    getattr(log.opt(colors=True), level)(
        event_message(event, status=status, **fields)
    )


def _status_marker(status: str | None) -> str:
    normalized = (status or "neutral").lower()
    symbol, color = _STYLE_BY_STATUS.get(normalized, ("·", "cyan"))
    if normalized in _NEUTRAL_STATUSES:
        symbol, color = "·", "cyan"
    return f"<{color}>{symbol}</{color}>"


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if not text:
        return '""'
    if any(ch.isspace() for ch in text):
        return repr(text)
    return text
