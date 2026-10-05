"""Transport-independent notifications emitted by agent operations.

Mirrors the nanobot ``EventSink`` contract: ``publish`` keeps error semantics
for delivery hooks (a failed publish propagates into the hook chain), while
``emit`` is best-effort and only logs. ``accepts`` lets expensive producers skip
work when the bound consumer cannot use their event type.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger


class AgentEvent:
    """A typed event; only explicitly projected events cross a client boundary."""


@dataclass(frozen=True)
class ProgressEvent(AgentEvent):
    """Incremental progress for one turn: prose, reasoning or tool activity."""

    content: str | None = None
    tool_hint: str | None = None
    reasoning: str | None = None
    reasoning_delta: bool = False
    reasoning_end: bool = False
    stream_id: str | None = None
    tool_events: list[dict[str, Any]] | None = None
    file_edit_events: list[dict[str, Any]] | None = None

    @property
    def text(self) -> str:
        """The event's display text, regardless of which field carries it.

        Progress text reaches this event through two channels: prose and tool
        hints use ``content``, reasoning chunks use ``reasoning``. Transports
        project ``text`` so a reasoning delta is never published as an empty
        message (the pre-migration callbacks passed reasoning text as the
        positional ``content`` argument for exactly this reason).
        """
        return self.content or self.reasoning or ""


@dataclass(frozen=True)
class StreamDeltaEvent(AgentEvent):
    """One streamed content fragment of the current answer segment."""

    content: str
    stream_id: str | None = None


@dataclass(frozen=True)
class StreamEndEvent(AgentEvent):
    """Close the current answer segment."""

    content: str | None = None
    stream_id: str | None = None
    resuming: bool = False


@dataclass(frozen=True)
class FileEditEvent(ProgressEvent):
    """A tracked file-edit lifecycle event (start / end / error)."""


@dataclass(frozen=True)
class EventSink:
    """A thin send callback bound to one operation's outbound route.

    This owns no queue or subscribers. ``publish`` is awaited directly by
    delivery hooks so output failures retain runner error semantics; ``emit``
    swallows and logs. Both propagate cancellation.
    """

    publish: Callable[[AgentEvent], Awaitable[None]] | None = None
    accepts_type: Callable[[type[AgentEvent]], bool] | None = None

    def accepts(self, event_type: type[AgentEvent]) -> bool:
        """Whether producing this event has a consumer in the bound scope."""
        return self.publish is not None and (
            self.accepts_type is None or self.accepts_type(event_type)
        )

    async def emit(self, event: AgentEvent) -> None:
        if self.publish is None:
            return
        try:
            await self.publish(event)
        except Exception:
            logger.exception("Failed to publish {}", type(event).__name__)


NO_EVENTS = EventSink()
