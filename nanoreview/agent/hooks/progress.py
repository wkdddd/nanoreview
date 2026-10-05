"""Agent hook that publishes runner events onto an ``EventSink``."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from loguru import logger

from nanoreview.agent.hooks.lifecycle import AgentHook, AgentHookContext
from nanoreview.events import (
    NO_EVENTS,
    EventSink,
    ProgressEvent,
    StreamDeltaEvent,
    StreamEndEvent,
)
from nanoreview.utils.helpers import IncrementalThinkExtractor, strip_think
from nanoreview.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
)
from nanoreview.utils.tool_hints import format_tool_hints

_SKILL_PATH_RE = re.compile(r"[/\\]skills[/\\]([^/\\]+)[/\\]SKILL\.md$", re.IGNORECASE)


class AgentProgressHook(AgentHook):
    """Publish typed progress/stream events for one turn.

    This is a delivery hook: it uses ``reraise=True`` so a failing transport
    surfaces as a run error instead of silently dropping user-visible output.
    """

    def __init__(
        self,
        events: EventSink = NO_EVENTS,
        *,
        streaming: bool = True,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        tool_hint_max_length: int = 40,
        set_tool_context: Callable[..., None] | None = None,
        on_iteration: Callable[[int], None] | None = None,
        suppress_content_progress: bool = False,
    ) -> None:
        super().__init__(reraise=True)
        self._events = events
        self._streaming = streaming
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id
        self._metadata = metadata or {}
        self._session_key = session_key
        self._tool_hint_max_length = tool_hint_max_length
        self._set_tool_context = set_tool_context
        self._on_iteration = on_iteration
        self._suppress_content_progress = suppress_content_progress
        self._stream_buf = ""
        self._think_extractor = IncrementalThinkExtractor()
        self._reasoning_open = False

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        self._metadata = dict(metadata)

    def wants_streaming(self) -> bool:
        return self._streaming and self._events.accepts(StreamDeltaEvent)

    @property
    def events(self) -> EventSink:
        return self._events

    def _publishes_progress(self) -> bool:
        return self._events.accepts(ProgressEvent)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        if not text:
            return None
        return strip_think(text) or None

    def _tool_hint(self, tool_calls: list[Any]) -> str:
        return format_tool_hints(tool_calls, max_length=self._tool_hint_max_length)

    async def _publish(self, event: Any) -> None:
        publish = self._events.publish
        if publish is None:
            return
        await publish(event)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean) :]

        if await self._think_extractor.feed(self._stream_buf, self.emit_reasoning):
            context.streamed_reasoning = True

        if incremental:
            # Answer text has started; close the reasoning segment so the UI can
            # lock the bubble before the answer renders below it.
            await self.emit_reasoning_end()
            if self._streaming and self._events.accepts(StreamDeltaEvent):
                await self._publish(StreamDeltaEvent(content=incremental))

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self.emit_reasoning_end()
        context.stream_continues_current_message = resuming
        if self._events.accepts(StreamEndEvent):
            await self._publish(StreamEndEvent(resuming=resuming))
        self._stream_buf = ""
        self._think_extractor.reset()

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self._on_iteration:
            self._on_iteration(context.iteration)
        logger.debug(
            "Starting agent loop iteration {} for session {}",
            context.iteration,
            self._session_key,
        )

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self._publishes_progress():
            if (
                not self._suppress_content_progress
                and not self._streaming
                and not context.streamed_content
            ):
                thought = self._strip_think(context.response.content if context.response else None)
                if thought:
                    await self._publish(ProgressEvent(content=thought))
            tool_hint = self._strip_think(self._tool_hint(context.tool_calls))
            tool_events = [build_tool_event_start_payload(tc) for tc in context.tool_calls]
            await self._publish(
                ProgressEvent(content=tool_hint, tool_hint=True, tool_events=tool_events)
            )
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])
        if self._set_tool_context:
            self._set_tool_context(
                self._channel,
                self._chat_id,
                self._message_id,
                self._metadata,
                session_key=self._session_key,
            )

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        """Publish a reasoning chunk; channel plugins decide whether to render."""
        if reasoning_content and self._publishes_progress():
            self._reasoning_open = True
            await self._publish(ProgressEvent(reasoning=reasoning_content, reasoning_delta=True))

    async def emit_reasoning_end(self) -> None:
        """Close the current reasoning stream segment, if any was open."""
        if self._reasoning_open and self._publishes_progress():
            self._reasoning_open = False
            await self._publish(ProgressEvent(reasoning_end=True))
        else:
            self._reasoning_open = False

    async def after_iteration(self, context: AgentHookContext) -> None:
        if context.tool_calls and context.tool_events and self._publishes_progress():
            tool_events = build_tool_event_finish_payloads(context)
            if tool_events:
                await self._publish(ProgressEvent(tool_events=tool_events))
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0),
            u.get("cached_tokens", 0),
        )
        if context.tool_calls and context.tool_results:
            for tc, result in zip(context.tool_calls, context.tool_results):
                if tc.name != "read_file":
                    continue
                if not isinstance(result, str) or result.startswith(("Error", "[File unchanged")):
                    continue
                path = tc.arguments.get("path") or tc.arguments.get("file_path") or ""
                m = _SKILL_PATH_RE.search(path)
                if m:
                    logger.info("using the skill {}", m.group(1))

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        # Cleaning is not an explicit replacement: pending injections and
        # terminal-tool prose retries must still see this as the model's answer.
        return self._strip_think(content)
