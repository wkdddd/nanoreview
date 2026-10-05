"""Agent hook that observes file-editing tools and emits file-edit activity."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from nanoreview.agent.hooks.lifecycle import (
    AgentHook,
    AgentHookContext,
    AgentRunHookContext,
    AgentTurnHookContext,
)
from nanoreview.events import EventSink, FileEditEvent
from nanoreview.providers.base import ToolCallRequest
from nanoreview.utils.file_edit_events import (
    FileEditTracker,
    build_file_edit_end_event,
    build_file_edit_error_event,
    build_file_edit_start_event,
    prepare_file_edit_trackers,
)


class FileEditActivityHook(AgentHook):
    """Translate file-editing tool lifecycle events into progress events."""

    def __init__(
        self,
        *,
        events: EventSink,
        workspace: Path | None,
    ) -> None:
        super().__init__()
        self._publish = events.publish if events.accepts(FileEditEvent) else None
        self._workspace = workspace
        self._trackers_by_call: dict[str, list[FileEditTracker]] = {}

    async def before_iteration(self, context: AgentHookContext) -> None:
        self._trackers_by_call.clear()

    async def before_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
    ) -> None:
        if self._publish is None or not isinstance(params, dict):
            return
        typed_params = cast(dict[str, Any], params)
        trackers = prepare_file_edit_trackers(
            call_id=self._tool_call_id(tool_call),
            tool_name=tool_call.name,
            tool=tool,
            params=typed_params,
        )
        if not trackers:
            return
        self._trackers_by_call[self._tool_call_key(tool_call)] = trackers
        await self._emit([build_file_edit_start_event(tracker) for tracker in trackers])

    async def after_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        key = self._tool_call_key(tool_call)
        trackers = self._trackers_by_call.pop(key, [])
        if trackers:
            await self._emit([build_file_edit_end_event(tracker, result) for tracker in trackers])

    async def on_execute_tool_error(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
        error: Any,
    ) -> None:
        key = self._tool_call_key(tool_call)
        trackers = self._trackers_by_call.pop(key, [])
        if trackers:
            await self._emit(
                [build_file_edit_error_event(tracker, str(error)) for tracker in trackers]
            )

    async def on_finally(self, context: AgentRunHookContext) -> None:
        if context.stop_reason != "cancelled" or not self._trackers_by_call:
            return
        trackers = [
            tracker
            for trackers in self._trackers_by_call.values()
            for tracker in trackers
        ]
        self._trackers_by_call.clear()
        await self._emit(
            [
                build_file_edit_error_event(
                    tracker,
                    "Task interrupted before this tool finished.",
                )
                for tracker in trackers
            ]
        )

    async def _emit(self, events: list[dict[str, Any]]) -> None:
        if self._publish is not None:
            await self._publish(FileEditEvent(file_edit_events=events))

    @staticmethod
    def _tool_call_id(tool_call: ToolCallRequest) -> str:
        return getattr(tool_call, "id", "") or ""

    @classmethod
    def _tool_call_key(cls, tool_call: ToolCallRequest) -> str:
        call_id = cls._tool_call_id(tool_call)
        return f"{call_id}|{tool_call.name}" if call_id else f"{id(tool_call)}|{tool_call.name}"


def create_file_edit_activity_hook(context: AgentTurnHookContext) -> AgentHook | None:
    """Create the default file-edit observer for one agent turn.

    Registered only in the Conversation Agent tool registry; review, planner
    and Judge paths never assemble it.
    """
    if not context.events.accepts(FileEditEvent):
        return None
    return FileEditActivityHook(
        events=context.events,
        workspace=context.workspace,
    )
