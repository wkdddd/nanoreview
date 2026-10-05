"""Project typed agent events onto NanoReview outbound messages.

The external wire format is unchanged: every projection writes the same
metadata keys the WebSocket channel, CLI renderer and SDK already read
(``_progress``, ``_tool_hint``, ``_tool_events``, ``_stream_delta``,
``_stream_end``, ``_resuming``, ``_reasoning_delta``, ``_reasoning_end``,
``_stream_id``, ``_stream_kind``). Only the internal delivery path changes --
hooks publish typed events, transports own the key mapping.

Event text always goes through :func:`event_text`, because a progress event
carries it in ``content`` (prose, tool hints) or in ``reasoning`` (reasoning
chunks); a projection that read ``content`` alone would publish every reasoning
delta as an empty message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from nanoreview.bus.events import OutboundMessage
from nanoreview.events import (
    AgentEvent,
    EventSink,
    ProgressEvent,
    StreamDeltaEvent,
    StreamEndEvent,
)

# Legacy progress-callback shape kept as the transport-facing adapter contract.
ProgressHandler = Callable[..., Awaitable[None]]
StreamHandler = Callable[..., Awaitable[None]]
OutboundPublisher = Callable[[OutboundMessage], Awaitable[None]]

__all__ = [
    "OutboundPublisher",
    "ProgressHandler",
    "StreamHandler",
    "build_bus_event_sink",
    "build_callback_event_sink",
    "event_text",
    "metadata_for_event",
    "progress_metadata",
    "stream_delta_metadata",
    "stream_end_metadata",
]


def progress_metadata(
    template: dict[str, Any] | None,
    event: ProgressEvent,
    *,
    stream_kind: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map a progress event onto outbound metadata."""
    meta = dict(template or {})
    meta["_progress"] = True
    meta["_tool_hint"] = bool(event.tool_hint)
    if event.reasoning_delta:
        meta["_reasoning_delta"] = True
    if event.reasoning_end:
        meta["_reasoning_end"] = True
    if event.tool_events:
        meta["_tool_events"] = event.tool_events
    if event.file_edit_events:
        meta["_file_edit_events"] = event.file_edit_events
    if event.stream_id:
        meta["_stream_id"] = event.stream_id
    if stream_kind:
        meta["_stream_kind"] = stream_kind
    if extra:
        meta.update(extra)
    return meta


def stream_delta_metadata(
    template: dict[str, Any] | None,
    event: StreamDeltaEvent,
    *,
    stream_kind: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(template or {})
    # Marker only: the delta text travels in ``OutboundMessage.content`` so the
    # existing wire format (and channel coalescing) is unchanged.
    meta["_stream_delta"] = True
    if event.stream_id:
        meta["_stream_id"] = event.stream_id
    if stream_kind:
        meta["_stream_kind"] = stream_kind
    if extra:
        meta.update(extra)
    return meta


def stream_end_metadata(
    template: dict[str, Any] | None,
    event: StreamEndEvent,
    *,
    stream_kind: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(template or {})
    meta["_stream_end"] = True
    meta["_resuming"] = event.resuming
    if event.stream_id:
        meta["_stream_id"] = event.stream_id
    if stream_kind:
        meta["_stream_kind"] = stream_kind
    if extra:
        meta.update(extra)
    return meta


def metadata_for_event(
    template: dict[str, Any] | None,
    event: AgentEvent,
    *,
    stream_kind: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Dispatch one typed event to its metadata projection.

    Returns ``None`` for event types no outbound projection consumes.
    """
    if isinstance(event, StreamDeltaEvent):
        return stream_delta_metadata(template, event, stream_kind=stream_kind, extra=extra)
    if isinstance(event, StreamEndEvent):
        return stream_end_metadata(template, event, stream_kind=stream_kind, extra=extra)
    if isinstance(event, ProgressEvent):
        return progress_metadata(template, event, stream_kind=stream_kind, extra=extra)
    return None


def event_text(event: AgentEvent) -> str:
    """The event's outbound text.

    ``ProgressEvent`` carries text in ``content`` (prose, tool hints) or
    ``reasoning`` (reasoning chunks); ``StreamDeltaEvent`` carries it in
    ``content``. Reading only ``content`` would publish a reasoning delta as an
    empty message, so projections go through this single accessor.
    """
    text = getattr(event, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(event, "content", None)
    return content if isinstance(content, str) else ""


def build_bus_event_sink(
    publish_outbound: OutboundPublisher,
    *,
    channel: str,
    chat_id: str,
    metadata: dict[str, Any] | None = None,
    stream_kind: str | None = None,
    extra: dict[str, Any] | None = None,
) -> EventSink:
    """Build an ``EventSink`` publishing every consumed event to the bus."""

    async def _publish(event: AgentEvent) -> None:
        meta = metadata_for_event(metadata, event, stream_kind=stream_kind, extra=extra)
        if meta is None:
            return
        await publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=event_text(event),
                metadata=meta,
            )
        )

    return EventSink(publish=_publish)


def build_callback_event_sink(
    *,
    on_progress: ProgressHandler | None = None,
    on_stream: StreamHandler | None = None,
    on_stream_end: StreamHandler | None = None,
) -> EventSink:
    """Adapt typed events onto the transport-local progress/stream handlers.

    Used by API SSE, CLI and SDK entry points, whose public surface still
    accepts separate handlers. ``accepts_type`` lets producers skip streaming
    work when no stream consumer was supplied.
    """

    async def _publish(event: AgentEvent) -> None:
        if isinstance(event, StreamDeltaEvent):
            if on_stream is not None:
                await on_stream(event.content)
            return
        if isinstance(event, StreamEndEvent):
            if on_stream_end is not None:
                await on_stream_end(resuming=event.resuming)
            return
        if isinstance(event, ProgressEvent) and on_progress is not None:
            await on_progress(
                event.text,
                tool_hint=bool(event.tool_hint),
                tool_events=event.tool_events,
                reasoning=event.reasoning_delta,
                reasoning_end=event.reasoning_end,
            )

    def _accepts(event_type: type[AgentEvent]) -> bool:
        if issubclass(event_type, StreamDeltaEvent):
            return on_stream is not None
        if issubclass(event_type, StreamEndEvent):
            return on_stream_end is not None
        if issubclass(event_type, ProgressEvent):
            return on_progress is not None
        return False

    return EventSink(publish=_publish, accepts_type=_accepts)
