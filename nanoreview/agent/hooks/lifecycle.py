"""Shared lifecycle hook primitives for agent runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.events import NO_EVENTS, EventSink
from nanoreview.providers.base import LLMResponse, ToolCallRequest


@dataclass(slots=True)
class AgentHookContext:
    """Mutable per-iteration state exposed to runner hooks."""

    iteration: int
    messages: list[dict[str, Any]]
    response: LLMResponse | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    streamed_content: bool = False
    streamed_reasoning: bool = False
    stream_continues_current_message: bool = False
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    session_key: str | None = None


@dataclass(slots=True)
class AgentRunHookContext:
    """Run-level state snapshot exposed to runner hooks.

    ``usage`` keeps the NanoReview ``dict[str, int]`` representation instead of
    the provider-level usage dataclass.
    """

    messages: list[dict[str, Any]]
    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    exception: BaseException | None = None


@dataclass(slots=True)
class AgentTurnHookContext:
    """Turn-local inputs available when constructing per-turn hooks."""

    events: EventSink = NO_EVENTS
    workspace: Path | None = None
    channel: str = "cli"
    chat_id: str = "direct"
    message_id: str | None = None
    session_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    ephemeral: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)


class AgentHook:
    """Minimal lifecycle surface for shared runner customization."""

    def __init__(self, reraise: bool = False) -> None:
        self._reraise = reraise

    def wants_streaming(self) -> bool:
        return False

    async def before_run(self, context: AgentRunHookContext) -> None:
        pass

    async def after_run(self, context: AgentRunHookContext) -> None:
        pass

    async def on_error(self, context: AgentRunHookContext) -> None:
        pass

    async def on_finally(self, context: AgentRunHookContext) -> None:
        pass

    async def before_iteration(self, context: AgentHookContext) -> None:
        pass

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        pass

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        pass

    async def on_provider_tool_event(
        self,
        context: AgentHookContext,
        event: dict[str, Any],
    ) -> None:
        """Observe a provider-hosted tool lifecycle event."""
        pass

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        pass

    async def before_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
    ) -> None:
        pass

    async def after_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        pass

    async def on_execute_tool_error(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
        error: Any,
    ) -> None:
        pass

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        pass

    async def emit_reasoning_end(self) -> None:
        """Mark the end of an in-flight reasoning stream.

        Hooks that buffer ``emit_reasoning`` chunks (for in-place UI updates)
        flush and freeze the rendered group here. One-shot hooks ignore.
        """
        pass

    async def after_iteration(self, context: AgentHookContext) -> None:
        pass

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return content

    def resolve_final_content(
        self,
        context: AgentHookContext,
        content: str | None,
    ) -> FinalizeContentResult | None:
        """Return an explicit final-content replacement, or ``None`` to defer.

        ``finalize_content`` is the cleaning pipeline; this is the opt-in
        channel for hooks that mean to *replace* the answer (review reports,
        terminal-tool prose). Only a non-``None`` result stops pending-injection
        drain and terminal-tool retries.
        """
        return None


AgentTurnHookFactory = Callable[[AgentTurnHookContext], AgentHook | None]


def finalize_content_result(
    hook: AgentHook,
    context: AgentHookContext,
    content: str | None,
) -> FinalizeContentResult:
    """Run the cleaning pipeline then the explicit-replacement channel."""
    cleaned = hook.finalize_content(context, content)
    replacement = hook.resolve_final_content(context, cleaned)
    if replacement is not None:
        return replacement
    return FinalizeContentResult(cleaned)


class CompositeHook(AgentHook):
    """Fan-out hook that delegates to an ordered list of hooks.

    Error isolation: async methods catch and log per-hook exceptions
    so a faulty custom hook cannot crash the agent loop. Hooks constructed with
    ``reraise=True`` (delivery hooks) propagate instead.
    ``finalize_content`` is a pipeline (no isolation - bugs should surface).
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)

    def wants_streaming(self) -> bool:
        return any(h.wants_streaming() for h in self._hooks)

    async def _for_each_hook_safe(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        for h in self._hooks:
            if getattr(h, "_reraise", False):
                await getattr(h, method_name)(*args, **kwargs)
                continue

            try:
                await getattr(h, method_name)(*args, **kwargs)
            except Exception:
                logger.exception("AgentHook.{} error in {}", method_name, type(h).__name__)

    async def before_run(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("before_run", context)

    async def after_run(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("after_run", context)

    async def on_error(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("on_error", context)

    async def on_finally(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("on_finally", context)

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_iteration", context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._for_each_hook_safe("on_stream", context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self._for_each_hook_safe("on_stream_end", context, resuming=resuming)

    async def on_provider_tool_event(
        self,
        context: AgentHookContext,
        event: dict[str, Any],
    ) -> None:
        await self._for_each_hook_safe("on_provider_tool_event", context, event)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_execute_tools", context)

    async def before_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
    ) -> None:
        await self._for_each_hook_safe("before_execute_tool", context, tool_call, tool, params)

    async def after_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        await self._for_each_hook_safe(
            "after_execute_tool",
            context,
            tool_call,
            tool,
            params,
            result,
        )

    async def on_execute_tool_error(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
        error: Any,
    ) -> None:
        await self._for_each_hook_safe(
            "on_execute_tool_error",
            context,
            tool_call,
            tool,
            params,
            error,
        )

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        await self._for_each_hook_safe("emit_reasoning", reasoning_content)

    async def emit_reasoning_end(self) -> None:
        await self._for_each_hook_safe("emit_reasoning_end")

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("after_iteration", context)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        for h in self._hooks:
            content = h.finalize_content(context, content)
        return content

    def resolve_final_content(
        self,
        context: AgentHookContext,
        content: str | None,
    ) -> FinalizeContentResult | None:
        for h in self._hooks:
            if getattr(h, "_reraise", False):
                result = h.resolve_final_content(context, content)
            else:
                try:
                    result = h.resolve_final_content(context, content)
                except Exception:
                    logger.exception(
                        "AgentHook.resolve_final_content error in {}", type(h).__name__
                    )
                    continue
            if result is not None:
                return result
        return None


class FinalizeContentResult:
    """Runner-internal outcome of the ``finalize_content`` pipeline.

    Separates "what the final content is" from "did a hook explicitly replace
    it". Cleaning (for example DSML stripping) yields content without setting
    ``is_replaced``; only an explicit replacement stops pending-injection drain
    and terminal-tool prose retries. This state never enters the public hook
    context.
    """

    __slots__ = ("content", "is_replaced")

    def __init__(self, content: str | None, *, is_replaced: bool = False) -> None:
        self.content = content
        self.is_replaced = is_replaced

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FinalizeContentResult(content={self.content!r}, is_replaced={self.is_replaced})"
