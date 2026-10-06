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
    #: Immutable workspace access decision for the active turn. ``None`` means
    #: the caller did not resolve a scope (e.g. review-internal tools that pass
    #: no path guard), in which case tools fall back to their construction-time
    #: configuration.
    workspace_scope: Any | None = None
    #: Whether diagnostics raised while serving this request may include the
    #: request's own content. Callers handling untrusted external payloads (MCP
    #: servers) turn this off so credentials and tool output stay out of logs.
    log_content: bool = True


_current_request_context: ContextVar[RequestContext | None] = ContextVar(
    "current_tool_request_context",
    default=None,
)


def current_request_context() -> RequestContext | None:
    """Return the request context for the current tool execution task."""
    return _current_request_context.get()


def current_workspace_scope() -> Any | None:
    """Return the workspace scope bound to the active tool request, if any."""
    ctx = _current_request_context.get()
    return ctx.workspace_scope if ctx is not None else None


def tool_log_content_allowed() -> bool:
    """Whether diagnostics may include content from the current tool request."""
    ctx = current_request_context()
    return ctx is None or ctx.log_content


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
    review_config: Any | None = None
    bus: Any | None = None
    sessions: Any | None = None
    file_state_store: Any = field(default=None)
    provider_snapshot_loader: Callable[[], Any] | None = None
    timezone: str = "UTC"
