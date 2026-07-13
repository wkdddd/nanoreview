"""Subagent execution hooks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.hooks.lifecycle import AgentHook, AgentHookContext
from nanobot.utils.helpers import IncrementalThinkExtractor, strip_think

if TYPE_CHECKING:
    from nanobot.agent.tools.registry import ToolRegistry


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float
    phase: str = "initializing"
    iteration: int = 0
    tool_events: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    stop_reason: str | None = None
    error: str | None = None


class SubagentHook(AgentHook):
    """Hook for subagent execution: log tool calls, update status, and stream
    reasoning/output to the frontend via bus callbacks.

    Mirrors :class:`AgentProgressHook`'s streaming logic —
    :class:`IncrementalThinkExtractor` splits ``<think>`` blocks from answer
    text so reasoning chunks and content deltas can be emitted through
    separate callbacks.
    """

    def __init__(
        self,
        task_id: str,
        status: SubagentStatus | None = None,
        *,
        tools: "ToolRegistry | None" = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream_cb: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end_cb: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status
        self._stream_buf = ""
        self._tools = tools
        self._origin_channel = origin_channel
        self._origin_chat_id = origin_chat_id
        self._session_key = session_key
        self._origin_message_id = origin_message_id
        self._metadata = dict(metadata or {})
        self._on_progress = on_progress
        self._on_stream_cb = on_stream_cb
        self._on_stream_end_cb = on_stream_end_cb
        self._think_extractor = IncrementalThinkExtractor()
        self._reasoning_open = False

    def wants_streaming(self) -> bool:
        # Force review subagents onto the provider streaming path so they avoid
        # long non-stream request timeouts while keeping the execution flow local.
        return True

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        if not delta:
            return
        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean):]

        if await self._think_extractor.feed(self._stream_buf, self.emit_reasoning):
            context.streamed_reasoning = True

        if incremental:
            # Answer text has started; close the reasoning segment so the UI
            # can lock the bubble before the answer renders below it.
            await self.emit_reasoning_end()
            if self._on_stream_cb:
                await self._on_stream_cb(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self.emit_reasoning_end()
        if self._on_stream_end_cb:
            await self._on_stream_end_cb(resuming=resuming)
        self._stream_buf = ""
        self._think_extractor.reset()

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        """Publish a reasoning chunk via the progress callback."""
        if self._on_progress and reasoning_content:
            self._reasoning_open = True
            await self._on_progress(reasoning_content, reasoning=True)

    async def emit_reasoning_end(self) -> None:
        """Close the current reasoning stream segment, if open."""
        if self._reasoning_open and self._on_progress:
            self._reasoning_open = False
            await self._on_progress("", reasoning_end=True)
        else:
            self._reasoning_open = False

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        # Propagate review metadata into the subagent's ContextAware tools
        # and the current-request context var so that local_review,
        # github_review, and read_file can make correct target-type and
        # workspace-boundary decisions.
        from nanobot.agent.tools.context import (
            ContextAware,
            RequestContext,
            set_current_request_context,
        )

        if self._tools is not None:
            request_ctx = RequestContext(
                channel=self._origin_channel,
                chat_id=self._origin_chat_id,
                message_id=self._origin_message_id,
                session_key=self._session_key,
                metadata=dict(self._metadata),
            )
            for name in self._tools.tool_names:
                tool = self._tools.get(name)
                if tool and isinstance(tool, ContextAware):
                    tool.set_context(request_ctx)
            set_current_request_context(request_ctx)

        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.info(
                "Subagent [{}] tool call: {}({})",
                self._task_id,
                tool_call.name,
                args_str[:200],
            )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)
