"""Typed event publication and its projection onto the existing wire format.

The event layer only changed *how* progress travels internally. These tests pin
the contract that matters to transports: the outbound metadata keys, the
``_stream_delta`` marker shape, ``accepts``-driven skipping, the difference
between a delivery ``publish`` (errors propagate) and a best-effort ``emit``
(errors are logged and swallowed), and the fact that event *text* reaches every
transport regardless of which field carries it.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoreview.agent.event_sink import (
    build_bus_event_sink,
    build_callback_event_sink,
    event_text,
    metadata_for_event,
    progress_metadata,
    stream_delta_metadata,
    stream_end_metadata,
)
from nanoreview.bus.events import OutboundMessage
from nanoreview.events import (
    NO_EVENTS,
    AgentEvent,
    EventSink,
    FileEditEvent,
    ProgressEvent,
    StreamDeltaEvent,
    StreamEndEvent,
)


class RecordingSink:
    """Captures published events and the event types asked about."""

    def __init__(self, *, accepts: bool = True) -> None:
        self.events: list[AgentEvent] = []
        self.accepted: list[type[AgentEvent]] = []
        self._accepts = accepts

    async def publish(self, event: AgentEvent) -> None:
        self.events.append(event)

    def accepts_type(self, event_type: type[AgentEvent]) -> bool:
        self.accepted.append(event_type)
        return self._accepts

    @property
    def sink(self) -> EventSink:
        return EventSink(publish=self.publish, accepts_type=self.accepts_type)


# ---------------------------------------------------------------------------
# EventSink contract
# ---------------------------------------------------------------------------


async def test_no_events_sink_accepts_nothing_and_drops_publishes() -> None:
    assert NO_EVENTS.accepts(ProgressEvent) is False
    # ``emit`` on an unbound sink is a no-op, not an error.
    await NO_EVENTS.emit(ProgressEvent(content="x"))


async def test_accepts_follows_the_publish_callback_when_unfiltered() -> None:
    recorder = RecordingSink()
    sink = EventSink(publish=recorder.publish)

    assert sink.accepts(ProgressEvent) is True
    assert sink.accepts(StreamDeltaEvent) is True


async def test_publish_failure_reaches_the_caller() -> None:
    """Delivery keeps error semantics: a failing transport is the run's error."""

    async def failing_publish(event: AgentEvent) -> None:
        raise RuntimeError("transport closed")

    sink = EventSink(publish=failing_publish)

    with pytest.raises(RuntimeError, match="transport closed"):
        await sink.publish(ProgressEvent(content="x"))


async def test_emit_failure_is_logged_and_swallowed() -> None:
    async def failing_publish(event: AgentEvent) -> None:
        raise RuntimeError("transport closed")

    await EventSink(publish=failing_publish).emit(ProgressEvent(content="x"))


# ---------------------------------------------------------------------------
# Metadata projection
# ---------------------------------------------------------------------------


def test_progress_metadata_carries_the_existing_wire_keys() -> None:
    meta = progress_metadata(
        {"_review_id": "r1"},
        ProgressEvent(
            content="calling tools",
            tool_hint="read_file",
            tool_events=[{"name": "read_file", "status": "start"}],
            stream_id="s1",
        ),
        stream_kind="subagent_content",
    )

    assert meta["_progress"] is True
    assert meta["_tool_hint"] is True
    assert meta["_tool_events"] == [{"name": "read_file", "status": "start"}]
    assert meta["_stream_id"] == "s1"
    assert meta["_stream_kind"] == "subagent_content"
    # The caller's own metadata is preserved, never replaced.
    assert meta["_review_id"] == "r1"


def test_progress_metadata_omits_absent_flags() -> None:
    meta = progress_metadata(None, ProgressEvent(content="thinking"))

    assert meta["_progress"] is True
    # No tool hint means the flag is present-and-false, matching the wire format.
    assert meta["_tool_hint"] is False
    for absent in ("_reasoning_delta", "_reasoning_end", "_tool_events", "_stream_id", "_stream_kind"):
        assert absent not in meta


def test_progress_metadata_marks_reasoning_segments() -> None:
    delta = progress_metadata(None, ProgressEvent(reasoning="because", reasoning_delta=True))
    end = progress_metadata(None, ProgressEvent(reasoning_end=True))

    assert delta["_reasoning_delta"] is True
    assert "_reasoning_end" not in delta
    assert end["_reasoning_end"] is True
    assert "_reasoning_delta" not in end


def test_progress_metadata_carries_file_edit_events() -> None:
    payload = [{"version": 1, "phase": "start", "path": "app.py"}]
    meta = progress_metadata(None, FileEditEvent(file_edit_events=payload))

    assert meta["_file_edit_events"] == payload


def test_stream_delta_metadata_is_a_marker_not_the_text() -> None:
    """The delta text travels in ``OutboundMessage.content``; the metadata flag
    only marks the message. Inlining the text here would double it on the wire.
    """
    meta = stream_delta_metadata(None, StreamDeltaEvent(content="partial"))

    assert meta["_stream_delta"] is True
    assert "partial" not in meta.values()


def test_stream_end_metadata_carries_the_resume_flag() -> None:
    assert stream_end_metadata(None, StreamEndEvent())["_resuming"] is False
    assert stream_end_metadata(None, StreamEndEvent(resuming=True))["_resuming"] is True


def test_metadata_for_event_dispatches_and_ignores_unknown_types() -> None:
    assert metadata_for_event(None, StreamDeltaEvent(content="x"))["_stream_delta"] is True
    assert metadata_for_event(None, StreamEndEvent())["_stream_end"] is True
    assert metadata_for_event(None, ProgressEvent(content="x"))["_progress"] is True
    # A file-edit event is a progress event, so it keeps the progress shape.
    assert metadata_for_event(None, FileEditEvent())["_progress"] is True

    class UnknownEvent(AgentEvent):
        pass

    assert metadata_for_event(None, UnknownEvent()) is None


# ---------------------------------------------------------------------------
# Event text
# ---------------------------------------------------------------------------


def test_event_text_reads_prose_from_content_and_reasoning_from_reasoning() -> None:
    """Progress text arrives through two fields; the accessor is the single
    place that knows it, so no projection can drop one of them."""
    assert event_text(ProgressEvent(content="calling tools")) == "calling tools"
    assert event_text(ProgressEvent(reasoning="because the file changed")) == (
        "because the file changed"
    )
    assert event_text(StreamDeltaEvent(content="partial")) == "partial"
    # Content wins when both are set: it is the display text.
    assert event_text(ProgressEvent(content="hint", reasoning="thought")) == "hint"
    # Marker-only events carry no text.
    assert event_text(ProgressEvent(reasoning_end=True)) == ""
    assert event_text(FileEditEvent(file_edit_events=[{"phase": "start"}])) == ""


# ---------------------------------------------------------------------------
# Transport projections
# ---------------------------------------------------------------------------


async def test_bus_sink_routes_reasoning_text_into_content() -> None:
    """Regression: the bus projection read only ``content``, so every reasoning
    delta shipped as an empty message -- the UI showed a reasoning bubble with
    no text even though the hook had passed the text along.
    """
    sent: list[OutboundMessage] = []

    async def publish_outbound(message: OutboundMessage) -> None:
        sent.append(message)

    sink = build_bus_event_sink(publish_outbound, channel="websocket", chat_id="c1")

    await sink.publish(ProgressEvent(reasoning="checking the diff", reasoning_delta=True))
    await sink.publish(ProgressEvent(reasoning_end=True))

    assert sent[0].content == "checking the diff"
    assert sent[0].metadata["_reasoning_delta"] is True
    # The close marker still carries no text.
    assert sent[1].content == ""
    assert sent[1].metadata["_reasoning_end"] is True


async def test_callback_sink_routes_reasoning_text_into_the_content_argument() -> None:
    """The CLI renderer and other callback transports read reasoning text from
    the positional argument, exactly like the pre-migration callbacks."""
    seen: list[tuple[str, dict[str, Any]]] = []

    async def on_progress(content: str, **kwargs: Any) -> None:
        seen.append((content, kwargs))

    sink = build_callback_event_sink(on_progress=on_progress)

    await sink.publish(ProgressEvent(reasoning="checking the diff", reasoning_delta=True))

    assert seen[0][0] == "checking the diff"
    assert seen[0][1]["reasoning"] is True


async def test_bus_sink_keeps_prose_and_tool_hints_unchanged() -> None:
    sent: list[OutboundMessage] = []

    async def publish_outbound(message: OutboundMessage) -> None:
        sent.append(message)

    sink = build_bus_event_sink(publish_outbound, channel="cli", chat_id="c1")

    await sink.publish(ProgressEvent(content="read_file", tool_hint=True))

    assert sent[0].content == "read_file"
    assert sent[0].metadata["_tool_hint"] is True


async def test_bus_sink_publishes_typed_events_as_outbound_messages() -> None:
    sent: list[OutboundMessage] = []

    async def publish_outbound(message: OutboundMessage) -> None:
        sent.append(message)

    sink = build_bus_event_sink(
        publish_outbound,
        channel="websocket",
        chat_id="c1",
        metadata={"_run": "r1"},
        stream_kind="review_thinking",
    )

    await sink.publish(StreamDeltaEvent(content="partial"))
    await sink.publish(ProgressEvent(content="working", tool_hint="read_file"))

    assert sent[0].channel == "websocket"
    assert sent[0].chat_id == "c1"
    assert sent[0].content == "partial"
    assert sent[0].metadata["_stream_delta"] is True
    assert sent[0].metadata["_stream_kind"] == "review_thinking"
    assert sent[0].metadata["_run"] == "r1"

    assert sent[1].content == "working"
    assert sent[1].metadata["_progress"] is True
    assert sent[1].metadata["_tool_hint"] is True


async def test_bus_sink_skips_events_no_projection_consumes() -> None:
    sent: list[OutboundMessage] = []

    async def publish_outbound(message: OutboundMessage) -> None:
        sent.append(message)

    sink = build_bus_event_sink(publish_outbound, channel="cli", chat_id="c1")

    class UnknownEvent(AgentEvent):
        pass

    await sink.publish(UnknownEvent())

    assert sent == []


async def test_callback_sink_routes_each_event_to_its_handler() -> None:
    progress: list[tuple[str, dict[str, Any]]] = []
    deltas: list[str] = []
    ends: list[bool] = []

    async def on_progress(content: str, **kwargs: Any) -> None:
        progress.append((content, kwargs))

    async def on_stream(delta: str) -> None:
        deltas.append(delta)

    async def on_stream_end(*, resuming: bool = False) -> None:
        ends.append(resuming)

    sink = build_callback_event_sink(
        on_progress=on_progress,
        on_stream=on_stream,
        on_stream_end=on_stream_end,
    )

    await sink.publish(ProgressEvent(content="hint", tool_hint="read_file", tool_events=[{"a": 1}]))
    await sink.publish(StreamDeltaEvent(content="partial"))
    await sink.publish(StreamEndEvent(resuming=True))

    assert progress == [("hint", {"tool_hint": True, "tool_events": [{"a": 1}], "reasoning": False, "reasoning_end": False})]
    assert deltas == ["partial"]
    assert ends == [True]


async def test_callback_sink_accepts_only_the_bound_handlers() -> None:
    """``accepts`` is what lets the progress hook skip streaming work for a
    caller that supplied no stream handler."""
    progress_only = build_callback_event_sink(on_progress=lambda *a, **k: _noop())
    assert progress_only.accepts(ProgressEvent) is True
    assert progress_only.accepts(StreamDeltaEvent) is False
    assert progress_only.accepts(StreamEndEvent) is False

    stream_only = build_callback_event_sink(on_stream=lambda *a, **k: _noop())
    assert stream_only.accepts(StreamDeltaEvent) is True
    assert stream_only.accepts(ProgressEvent) is False

    unbound = build_callback_event_sink()
    assert unbound.accepts(ProgressEvent) is False


async def test_callback_sink_tolerates_missing_handlers() -> None:
    """Publishing an event whose handler was never supplied is a no-op, not an
    ``AttributeError`` on the caller's behalf."""
    sink = build_callback_event_sink()

    await sink.publish(StreamDeltaEvent(content="x"))
    await sink.publish(StreamEndEvent())
    await sink.publish(ProgressEvent(content="x"))


async def _noop() -> None:
    return None
