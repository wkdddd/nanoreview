"""SDK-facing runner hooks."""

from __future__ import annotations

from typing import Any

from nanoreview.agent.hooks.lifecycle import (
    AgentHook,
    AgentHookContext,
    AgentRunHookContext,
)


class SDKCaptureHook(AgentHook):
    """Record tool names and the final message list for ``RunResult``.

    The runner mutates ``context.messages`` in place across iterations, so the
    snapshot is refreshed on every ``after_iteration`` call; the last call
    reflects the end-of-turn state the SDK caller cares about. The run-level
    snapshot is authoritative when available and covers paths without a final
    per-iteration callback.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tools_used: list[str] = []
        self.messages: list[dict[str, Any]] = []
        self.usage: dict[str, int] = {}
        self.stop_reason: str | None = None
        self.error: str | None = None
        self.tool_events: list[dict[str, str]] = []
        self.had_injections: bool = False

    async def after_iteration(self, context: AgentHookContext) -> None:
        for call in context.tool_calls:
            self.tools_used.append(call.name)
        self.messages = list(context.messages)
        self.usage = dict(context.usage or {})
        self.stop_reason = context.stop_reason
        self.error = context.error
        self.tool_events = list(context.tool_events)

    async def after_run(self, context: AgentRunHookContext) -> None:
        self.tools_used = list(context.tools_used)
        self.messages = list(context.messages)
        self.usage = dict(context.usage or {})
        self.stop_reason = context.stop_reason
        self.error = context.error
        self.tool_events = list(context.tool_events)
        self.had_injections = context.had_injections
