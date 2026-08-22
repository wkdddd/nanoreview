"""Runtime context for tool construction."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class RequestContext:
    """Per-request context injected into tools at message-processing time."""
    channel: str
    chat_id: str
    message_id: str | None = None
    session_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


_current_request_context: ContextVar[RequestContext | None] = ContextVar(
    "current_tool_request_context",
    default=None,
)


def current_request_context() -> RequestContext | None:
    """Return the request context for the current tool execution task."""
    return _current_request_context.get()


def set_current_request_context(ctx: RequestContext) -> Token[RequestContext | None]:
    return _current_request_context.set(ctx)


def reset_current_request_context(token: Token[RequestContext | None]) -> None:
    _current_request_context.reset(token)


@runtime_checkable
class ContextAware(Protocol):
    def set_context(self, ctx: RequestContext) -> None:
        ...


@dataclass
class ToolContext:
    config: Any
    workspace: str
    provider: Any | None = None
    model: str | None = None
    rag_config: Any | None = None
    review_config: Any | None = None
    embedding_config: Any | None = None
    rerank_config: Any | None = None
    qdrant_config: Any | None = None
    bus: Any | None = None
    subagent_manager: Any | None = None
    sessions: Any | None = None
    file_state_store: Any = field(default=None)
    provider_snapshot_loader: Callable[[], Any] | None = None
    timezone: str = "UTC"
