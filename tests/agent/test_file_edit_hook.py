"""File-edit activity tracking for the Conversation Agent.

``FileEditActivityHook`` observes the per-tool lifecycle and turns it into
progress events the WebUI/CLI already know how to render. The contract these
tests pin: start/end pairs for a successful edit, an error event when the tool
fails or raises, an interruption event when a turn is cancelled mid-edit, and
the fact that review / planner / Judge paths never assemble the hook at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.hooks import (
    AgentHookContext,
    AgentRunHookContext,
    AgentTurnHookContext,
    AgentTurnHookSpec,
    FileEditActivityHook,
    build_agent_turn_hook,
    create_file_edit_activity_hook,
)
from nanoreview.agent.runner import AgentRunner, AgentRunSpec
from nanoreview.agent.tools.filesystem import EditFileTool, WriteFileTool
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.events import (
    NO_EVENTS,
    AgentEvent,
    EventSink,
    FileEditEvent,
    ProgressEvent,
)
from nanoreview.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class FailingWriteTool(WriteFileTool):
    async def execute(self, path: str | None = None, content: str | None = None, **kwargs: Any) -> str:
        raise OSError("disk on fire")


class ExplodingWriteTool(WriteFileTool):
    async def execute(self, path: str | None = None, content: str | None = None, **kwargs: Any) -> str:
        raise RuntimeError("tool crashed")


class ScriptedProvider(LLMProvider):
    """Replays scripted replies; the last one repeats once exhausted."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[index]

    def get_default_model(self) -> str:
        return "dummy"


class CollectingSink:
    """Captures every published event, keeping file-edit payloads flat."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def publish(self, event: AgentEvent) -> None:
        self.events.append(event)

    @property
    def sink(self) -> EventSink:
        return EventSink(publish=self.publish)

    @property
    def file_edits(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for event in self.events:
            if isinstance(event, FileEditEvent) and event.file_edit_events:
                payloads.extend(event.file_edit_events)
        return payloads

    def phases(self) -> list[str]:
        return [payload["phase"] for payload in self.file_edits]


def make_hook(workspace: Path, sink: EventSink) -> FileEditActivityHook:
    return FileEditActivityHook(events=sink, workspace=workspace)


def hook_context() -> AgentHookContext:
    return AgentHookContext(iteration=0, messages=[])


def write_call(call_id: str = "c1", path: str = "app.py") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="write_file", arguments={"path": path, "content": "x"})


def edit_call(call_id: str = "c1", path: str = "app.py") -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="edit_file",
        arguments={"path": path, "old_text": "a", "new_text": "b"},
    )


def write_tool(workspace: Path) -> WriteFileTool:
    return WriteFileTool(workspace=workspace, allowed_dir=workspace)


# ---------------------------------------------------------------------------
# Factory gating
# ---------------------------------------------------------------------------


def test_factory_creates_the_hook_only_when_file_edits_are_consumed() -> None:
    collector = CollectingSink()

    created = create_file_edit_activity_hook(
        AgentTurnHookContext(events=collector.sink, workspace=Path("."))
    )
    assert isinstance(created, FileEditActivityHook)

    # A transport with no file-edit consumer must not pay for tracking.
    assert create_file_edit_activity_hook(AgentTurnHookContext(events=NO_EVENTS)) is None
    assert (
        create_file_edit_activity_hook(
            AgentTurnHookContext(
                events=EventSink(
                    publish=collector.publish,
                    accepts_type=lambda t: t is ProgressEvent,
                )
            )
        )
        is None
    )


def test_file_edit_events_still_satisfy_the_progress_consumer() -> None:
    """``FileEditEvent`` extends ``ProgressEvent``, so a progress-only
    consumer accepts it -- the ``accepts`` check must not exclude it by
    accident."""
    collector = CollectingSink()
    sink = EventSink(
        publish=collector.publish,
        accepts_type=lambda event_type: issubclass(event_type, ProgressEvent),
    )

    hook = create_file_edit_activity_hook(AgentTurnHookContext(events=sink, workspace=Path(".")))

    assert hook is not None


# ---------------------------------------------------------------------------
# Per-tool lifecycle
# ---------------------------------------------------------------------------


async def test_write_file_emits_a_start_event_before_the_tool_runs(tmp_path) -> None:
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    tool = write_tool(tmp_path)
    call = write_call()

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, tool, call.arguments)

    assert collector.phases() == ["start"]
    assert collector.file_edits[0]["path"] == "app.py"
    assert collector.file_edits[0]["tool"] == "write_file"
    assert collector.file_edits[0]["call_id"] == "c1"


async def test_completed_write_reports_line_stats_and_a_diff(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("a\nb\n", encoding="utf-8")
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    tool = write_tool(tmp_path)
    call = ToolCallRequest(
        id="c1", name="write_file", arguments={"path": "app.py", "content": "a\nc\n"}
    )

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, tool, call.arguments)
    result = await tool.execute(**call.arguments)
    await hook.after_execute_tool(hook_context(), call, tool, call.arguments, result)

    assert collector.phases() == ["start", "end"]
    end = collector.file_edits[1]
    assert end["status"] == "ok"
    # One line replaced by another: one added, one deleted.
    assert end["added"] == 1
    assert end["deleted"] == 1
    assert "+c" in end["diff"]
    assert "-b" in end["diff"]


async def test_completed_edit_file_reports_a_diff_too(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("a\n", encoding="utf-8")
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    tool = EditFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    call = edit_call()

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, tool, call.arguments)
    result = await tool.execute(**call.arguments)
    await hook.after_execute_tool(hook_context(), call, tool, call.arguments, result)

    assert collector.phases() == ["start", "end"]
    assert collector.file_edits[1]["status"] == "ok"


async def test_untracked_tools_produce_no_events(tmp_path) -> None:
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    call = ToolCallRequest(id="c1", name="read_file", arguments={"path": "app.py"})

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, write_tool(tmp_path), call.arguments)

    assert collector.events == []


async def test_a_path_outside_the_workspace_produces_no_start_event(tmp_path) -> None:
    """Tracking reuses the tool's own path resolution, so a rejected path
    never opens a tracker -- the tool error event reports it instead."""
    collector = CollectingSink()
    hook = make_hook(tmp_path / "workspace", collector.sink)
    tool = WriteFileTool(workspace=tmp_path / "workspace", allowed_dir=tmp_path / "workspace")
    call = write_call(path="../escape.py")

    await hook.before_iteration(hook_context())
    with pytest.raises(Exception):
        tool._resolve(call.arguments["path"])
    await hook.before_execute_tool(hook_context(), call, tool, call.arguments)

    assert collector.events == []


async def test_a_tool_returning_an_error_result_reports_status_error(tmp_path) -> None:
    """NanoReview tools report failures as an ``Error: ...`` string rather than
    raising; that is still a failed edit, not a clean one."""
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    tool = write_tool(tmp_path)
    call = write_call(path="app.py")

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, tool, call.arguments)
    await hook.after_execute_tool(
        hook_context(), call, tool, call.arguments, "Error: PermissionError: denied"
    )

    assert collector.phases() == ["start", "end"]
    end = collector.file_edits[1]
    assert end["status"] == "error"
    assert "denied" in end["error"]


async def test_a_raising_tool_reports_an_error_event(tmp_path) -> None:
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    tool = FailingWriteTool(workspace=tmp_path, allowed_dir=tmp_path)
    call = write_call()

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, tool, call.arguments)
    try:
        await tool.execute(**call.arguments)
    except OSError as exc:
        await hook.on_execute_tool_error(hook_context(), call, tool, call.arguments, exc)

    assert collector.phases() == ["start", "error"]
    assert "disk on fire" in collector.file_edits[1]["error"]


async def test_an_error_closes_the_tracker_so_no_end_event_follows(tmp_path) -> None:
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    tool = ExplodingWriteTool(workspace=tmp_path, allowed_dir=tmp_path)
    call = write_call()
    params = call.arguments

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, tool, params)
    await hook.on_execute_tool_error(hook_context(), call, tool, params, RuntimeError("boom"))
    await hook.after_execute_tool(hook_context(), call, tool, params, "ignored")

    assert collector.phases() == ["start", "error"]


async def test_a_deleted_file_is_reported_as_a_delete(tmp_path) -> None:
    target = tmp_path / "gone.py"
    target.write_text("a\n", encoding="utf-8")
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    tool = write_tool(tmp_path)
    call = write_call(path="gone.py")

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, tool, call.arguments)
    target.unlink()
    await hook.after_execute_tool(hook_context(), call, tool, call.arguments, "ok")

    assert collector.file_edits[1]["operation"] == "delete"


async def test_a_new_iteration_drops_an_unfinished_tracker(tmp_path) -> None:
    """Trackers are iteration-scoped: a start with no terminal event must not
    leak into the next iteration's report."""
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    tool = write_tool(tmp_path)
    call = write_call()

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, tool, call.arguments)
    await hook.before_iteration(hook_context())
    await hook.after_execute_tool(hook_context(), call, tool, call.arguments, "ok")

    assert collector.phases() == ["start"]


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_a_cancelled_turn_closes_open_edits_with_an_interruption(tmp_path) -> None:
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    tool = write_tool(tmp_path)
    call = write_call()

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, tool, call.arguments)
    await hook.on_finally(AgentRunHookContext(messages=[], stop_reason="cancelled"))

    assert collector.phases() == ["start", "error"]
    assert "interrupted" in collector.file_edits[1]["error"].lower()


async def test_a_finished_turn_leaves_no_interruption_event(tmp_path) -> None:
    collector = CollectingSink()
    hook = make_hook(tmp_path, collector.sink)
    tool = write_tool(tmp_path)
    call = write_call()

    await hook.before_iteration(hook_context())
    await hook.before_execute_tool(hook_context(), call, tool, call.arguments)
    await hook.on_finally(AgentRunHookContext(messages=[], stop_reason="completed"))

    assert collector.phases() == ["start"]


# ---------------------------------------------------------------------------
# Runner integration
# ---------------------------------------------------------------------------


def make_spec(tools: ToolRegistry, hook: Any, **overrides: Any) -> AgentRunSpec:
    values: dict[str, Any] = {
        "frozen_messages": [],
        "working_messages": [],
        "tools": tools,
        "model": "dummy",
        "max_iterations": 3,
        "max_tool_result_chars": 1000,
        "hook": hook,
    }
    values.update(overrides)
    return AgentRunSpec(**values)


async def test_a_conversation_turn_reports_its_file_edits(tmp_path) -> None:
    collector = CollectingSink()
    tools = ToolRegistry()
    tools.register(write_tool(tmp_path))
    provider = ScriptedProvider([
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="c1",
                    name="write_file",
                    arguments={"path": "app.py", "content": "hello\n"},
                )
            ],
        ),
        LLMResponse(content="done"),
    ])

    await AgentRunner(provider).run(
        make_spec(tools, make_hook(tmp_path, collector.sink))
    )

    assert collector.phases() == ["start", "end"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "hello\n"


async def test_review_and_judge_turns_report_no_file_edits(tmp_path) -> None:
    """Reviewer, planner and Judge specs are built without the registered
    factory, so a file edit there is invisible to the activity stream."""
    collector = CollectingSink()
    tools = ToolRegistry()
    tools.register(write_tool(tmp_path))
    provider = ScriptedProvider([
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="c1",
                    name="write_file",
                    arguments={"path": "app.py", "content": "hello\n"},
                )
            ],
        ),
        LLMResponse(content="report"),
    ])

    review_hook = build_agent_turn_hook(AgentTurnHookSpec(workspace=tmp_path, ephemeral=True))
    await AgentRunner(provider).run(make_spec(tools, review_hook))

    assert collector.phases() == []
    # The edit itself still happened: only the observation is absent.
    assert (tmp_path / "app.py").exists()


async def test_the_builder_registers_the_hook_for_conversation_turns(tmp_path) -> None:
    collector = CollectingSink()
    hook = build_agent_turn_hook(
        AgentTurnHookSpec(
            events=collector.sink,
            workspace=tmp_path,
            registered_hook_factories=[create_file_edit_activity_hook],
        )
    )
    tools = ToolRegistry()
    tools.register(write_tool(tmp_path))
    provider = ScriptedProvider([
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="c1",
                    name="write_file",
                    arguments={"path": "app.py", "content": "hello\n"},
                )
            ],
        ),
        LLMResponse(content="done"),
    ])

    await AgentRunner(provider).run(make_spec(tools, hook))

    assert collector.phases() == ["start", "end"]
