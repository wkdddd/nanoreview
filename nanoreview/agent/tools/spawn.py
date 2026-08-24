"""Spawn tool for profile-driven subagents."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from nanoreview.agent.tools.base import Tool, tool_parameters
from nanoreview.agent.tools.context import ContextAware, RequestContext
from nanoreview.agent.tools.schema import StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from nanoreview.agent.subagent import SubagentManager

@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The focused task for the subagent."),
        label=StringSchema("A short task label."),
        required=["task", "label"],
    )
)
class SpawnTool(Tool, ContextAware):
    """Forward a focused task to the configured subagent manager."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel: ContextVar[str] = ContextVar(
            "review_spawn_origin_channel",
            default="cli",
        )
        self._origin_chat_id: ContextVar[str] = ContextVar(
            "review_spawn_origin_chat_id",
            default="direct",
        )
        self._session_key: ContextVar[str] = ContextVar(
            "review_spawn_session_key",
            default="cli:direct",
        )
        self._origin_message_id: ContextVar[str | None] = ContextVar(
            "review_spawn_origin_message_id",
            default=None,
        )
        self._metadata: ContextVar[dict[str, Any]] = ContextVar(
            "review_spawn_metadata",
            default={},
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    def set_context(self, ctx: RequestContext) -> None:
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")
        self._origin_message_id.set(ctx.message_id)
        self._metadata.set(dict(ctx.metadata or {}))

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a focused subagent using the execution profile supplied by the runtime."
        )

    async def execute(self, task: str, label: str, **kwargs: Any) -> str:
        metadata = dict(self._metadata.get())
        running = self._manager.get_running_count()
        limit = self._manager.max_concurrent_subagents
        if running >= limit:
            return (
                f"Error: Cannot spawn review subagent: concurrency limit reached "
                f"({running}/{limit} running). Wait for a running subagent "
                f"to complete before spawning a new one."
            )
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel.get(),
            origin_chat_id=self._origin_chat_id.get(),
            session_key=self._session_key.get(),
            origin_message_id=self._origin_message_id.get(),
            origin_metadata=metadata,
        )
