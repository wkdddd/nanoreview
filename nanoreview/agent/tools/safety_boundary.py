"""guard tool execution results into safety-boundary ."""

from __future__ import annotations

from typing import Any

from loguru import logger

from nanoreview.providers.base import ToolCallRequest
from nanoreview.utils.runtime import repeated_workspace_violation_error

_SSRF_MARKERS: tuple[str, ...] = (
    "internal/private url detected",
    "private/internal address",
    "private address",
)

_SSRF_BOUNDARY_NOTE: str = (
    "This is a non-bypassable security boundary. Stop trying to access "
    "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
    "alternate DNS, redirects, proxies, or another tool. Ask the user for "
    "local files, logs, screenshots, or an explicit safe public URL instead. "
    "If the user explicitly trusts this private URL, ask them to whitelist "
    "the exact IP/CIDR via tools.ssrfWhitelist."
)

_WORKSPACE_VIOLATION_MARKERS: tuple[str, ...] = (
    "outside the configured workspace",
    "outside allowed directory",
    "working_dir is outside",
    "working_dir could not be resolved",
    "path outside working dir",
    "path traversal detected",
)


def is_ssrf_violation(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _SSRF_MARKERS)


def is_workspace_violation(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    if is_ssrf_violation(lowered):
        return True
    return any(marker in lowered for marker in _WORKSPACE_VIOLATION_MARKERS)


def ssrf_soft_payload(raw_text: str) -> str:
    text = raw_text.strip() or "Error: request blocked by SSRF guard"
    return f"{text}\n\n{_SSRF_BOUNDARY_NOTE}"


def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
    return (prefix + text.replace("\n", " ").strip())[:limit]


def classify_violation(
    *,
    raw_text: str,
    soft_payload: str,
    event: dict[str, str],
    tool_call: ToolCallRequest,
    workspace_violation_counts: dict[str, int],
) -> tuple[Any, dict[str, str], BaseException | None] | None:
    """Classify safety-boundary failures, or return ``None`` to pass through."""
    if is_ssrf_violation(raw_text):
        logger.warning(
            "Tool {} blocked by SSRF guard; returning non-retryable tool error: {}",
            tool_call.name,
            raw_text.replace("\n", " ").strip()[:200],
        )
        event["detail"] = _event_detail("ssrf_violation: ", raw_text)
        return ssrf_soft_payload(raw_text), event, None

    if is_workspace_violation(raw_text):
        escalation = repeated_workspace_violation_error(
            tool_call.name,
            tool_call.arguments,
            workspace_violation_counts,
        )
        event["detail"] = _event_detail("workspace_violation: ", raw_text)
        if escalation is not None:
            logger.warning(
                "Tool {} hit workspace boundary repeatedly; escalating hint",
                tool_call.name,
            )
            event["detail"] = _event_detail(
                "workspace_violation_escalated: ",
                raw_text,
            )
            return escalation, event, None
        return soft_payload, event, None

    return None
