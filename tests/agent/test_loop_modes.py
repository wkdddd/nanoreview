from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.hooks import AgentHookContext
from nanobot.agent.hooks.subagent import SubagentHook, SubagentStatus
from nanobot.agent.loop import AgentLoop, TurnContext, TurnState, _is_consumed_subagent_result
from nanobot.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config, ToolsConfig, _resolve_tool_config_refs
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.review.planning.planner import ReviewPreparation
from nanobot.review.types import ReviewMetaKey
from nanobot.session.manager import Session


class DummyProvider(LLMProvider):
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
        _ = response_format
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


class CapturingRunner:
    def __init__(self) -> None:
        self.initial_messages: list[dict[str, Any]] | None = None

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.initial_messages = list(spec.initial_messages)
        return AgentRunResult(final_content="ok", messages=spec.initial_messages)


class SpawnExecutingRunner:
    def __init__(self) -> None:
        self.result: Any | None = None

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        from nanobot.agent.hooks import AgentHookContext

        call = ToolCallRequest(
            id="call_spawn",
            name="spawn",
            arguments={"task": "review performance", "label": "Performance Reviewer"},
        )
        context = AgentHookContext(
            iteration=0,
            messages=list(spec.initial_messages),
            response=LLMResponse(content="spawning reviewer", tool_calls=[call]),
            tool_calls=[call],
        )
        assert spec.hook is not None
        await spec.hook.before_execute_tools(context)
        tool = spec.tools.get("spawn")
        assert tool is not None
        self.result = await tool.execute(**call.arguments)
        return AgentRunResult(
            final_content="ok", messages=spec.initial_messages, tools_used=["spawn"]
        )


class InjectionRunner:
    def __init__(self) -> None:
        self.injected: list[dict[str, Any]] | None = None

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        assert spec.injection_callback is not None
        self.injected = await spec.injection_callback(limit=3)
        return AgentRunResult(
            final_content="ok",
            messages=list(spec.initial_messages) + list(self.injected),
            had_injections=bool(self.injected),
        )


class ReviewSubmitRetryRunner:
    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        if len(self.specs) == 1:
            return AgentRunResult(
                final_content="review prose without tool call",
                messages=list(spec.initial_messages),
                stop_reason="completed",
                tool_events=[
                    {"name": "read_file", "status": "ok", "detail": "file content"},
                ],
            )
        return AgentRunResult(
            final_content=None,
            messages=list(spec.initial_messages),
            tool_events=[
                {
                    "name": "review_submit",
                    "status": "ok",
                    "detail": '{"submitted": true, "findings": [], "errors": []}',
                    "raw_result": '{"submitted":true,"findings":[],"errors":[]}',
                }
            ],
        )


class BlockingSubmitRunner:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        await self.release.wait()
        return AgentRunResult(
            final_content=None,
            messages=list(spec.initial_messages),
            tool_events=[
                {"name": "read_file", "status": "ok", "detail": "file content"},
                {
                    "name": "review_submit",
                    "status": "ok",
                    "detail": '{"submitted": true, "findings": [], "errors": []}',
                    "raw_result": '{"submitted":true,"findings":[],"errors":[]}',
                },
            ],
        )


class StreamingChoiceProvider(DummyProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.calls.append("chat")
        return LLMResponse(content="non-stream")

    async def chat_stream_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.calls.append("stream")
        on_content_delta = kwargs.get("on_content_delta")
        if callable(on_content_delta):
            await on_content_delta("partial")
        return LLMResponse(content="streamed final")


class RunningSubagents:
    def __init__(self, running: int = 1) -> None:
        self.running = running
        self.results: asyncio.Queue = asyncio.Queue()

    def get_running_count_by_session(self, session_key: str) -> int:
        return self.running

    def publish(self, msg: Any) -> None:
        self.results.put_nowait(msg)

    def drain_session_results(self, session_key: str, *, limit: int) -> list[Any]:
        items: list[Any] = []
        while len(items) < limit:
            try:
                items.append(self.results.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    async def wait_for_session_result(self, session_key: str, *, timeout: float = 0.1) -> Any:
        try:
            return await asyncio.wait_for(self.results.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


class MultiDrainRunner:
    def __init__(self, pending: asyncio.Queue, subagents: RunningSubagents) -> None:
        self.pending = pending
        self.subagents = subagents
        self.injected_batches: list[list[dict[str, Any]]] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        assert spec.injection_callback is not None

        await self.pending.put(_inbound("user interjection", sender_id="user"))
        first_wait = asyncio.create_task(spec.injection_callback(limit=3))
        await asyncio.sleep(0)
        assert not first_wait.done()

        self.subagents.publish(_subagent_result("first", "security"))
        first = await asyncio.wait_for(first_wait, timeout=0.5)
        self.injected_batches.append(first)
        assert len(first) == 1
        assert first[0]["_metadata"]["subagent_task_id"] == "first"

        second_wait = asyncio.create_task(spec.injection_callback(limit=3))
        await asyncio.sleep(0)
        assert not second_wait.done()

        self.subagents.running = 0
        self.subagents.publish(_subagent_result("second", "tests"))
        second = await asyncio.wait_for(second_wait, timeout=0.5)
        self.injected_batches.append(second)
        task_ids = [item.get("_metadata", {}).get("subagent_task_id") for item in second]
        assert "second" in task_ids
        assert any(item.get("content") == "user interjection" for item in second)
        assert any(
            item.get("_metadata", {}).get("injected_event") == "subagent_barrier" for item in second
        )

        messages = list(spec.initial_messages)
        for batch in self.injected_batches:
            messages.extend(batch)
        return AgentRunResult(final_content="ok", messages=messages, had_injections=True)


class ManagerQueueDrainRunner:
    def __init__(self, subagents: RunningSubagents) -> None:
        self.subagents = subagents
        self.injected: list[dict[str, Any]] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        assert spec.injection_callback is not None
        wait = asyncio.create_task(spec.injection_callback(limit=3))
        await asyncio.sleep(0)
        assert not wait.done()

        self.subagents.running = 0
        self.subagents.publish(_subagent_result("direct", "security"))
        self.injected = await asyncio.wait_for(wait, timeout=0.5)
        return AgentRunResult(
            final_content="ok",
            messages=list(spec.initial_messages) + list(self.injected),
            had_injections=bool(self.injected),
        )


async def _review_fallback_preparation() -> ReviewPreparation:
    return ReviewPreparation(None, "review system prompt")


async def _review_coordinator_preparation() -> ReviewPreparation:
    return ReviewPreparation(
        None,
        "You are the planning coordinator for a read-only code review.\n"
        "- Name: test/repo\nsubmit_review_plan",
    )


def _inbound(
    content: str,
    *,
    sender_id: str = "subagent",
    metadata: dict[str, Any] | None = None,
) -> Any:
    from nanobot.bus.events import InboundMessage

    return InboundMessage(
        channel="system" if sender_id == "subagent" else "websocket",
        sender_id=sender_id,
        chat_id="test:pending",
        content=content,
        session_key_override="test:pending",
        metadata=metadata or {},
    )


def _subagent_result(task_id: str, label: str) -> Any:
    return _inbound(
        f"subagent {task_id} result",
        metadata={
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
            "subagent_label": label,
            "subagent_status": "ok",
            "subagent_result": '{"submitted": true, "findings": [], "errors": []}',
        },
    )


def _config(data: dict[str, Any]) -> Config:
    _resolve_tool_config_refs()
    return Config.model_validate(data)


def test_agent_loop_applies_configured_subagent_concurrency(tmp_path) -> None:
    config = _config(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path),
                    "maxConcurrentSubagents": 5,
                }
            }
        }
    )
    loop = AgentLoop.from_config(config, bus=MessageBus(), provider=DummyProvider())

    assert loop.subagents.max_concurrent_subagents == 5


def test_config_accepts_subagent_concurrency_below_five() -> None:
    config = _config(
        {
            "agents": {
                "defaults": {
                    "maxConcurrentSubagents": 2,
                }
            }
        }
    )

    assert config.agents.defaults.max_concurrent_subagents == 2


def test_agent_loop_state_machine_has_no_review_finalize_state() -> None:
    assert "FINALIZE" not in TurnState.__members__
    assert all(
        state.name != "FINALIZE"
        for transition in AgentLoop._TRANSITIONS.values()
        for state in [transition]
    )


@pytest.mark.asyncio
async def test_agent_loop_always_injects_review_context(
    tmp_path,
    monkeypatch,
) -> None:
    """Review context is always resolved regardless of session metadata."""

    async def mock_review(*args: Any, **kwargs: Any) -> ReviewPreparation:
        return ReviewPreparation(None, "review system prompt")

    monkeypatch.setattr("nanobot.agent.loop.prepare_code_review_context", mock_review)

    loop = AgentLoop(MessageBus(), DummyProvider(), tmp_path)
    runner = CapturingRunner()
    loop.runner = runner
    session = Session(key="test:plain")

    await loop._run_agent_loop(
        [{"role": "user", "content": "hello"}],
        session=session,
        session_key=session.key,
    )

    assert runner.initial_messages[0] == {"role": "system", "content": "review system prompt"}
    assert runner.initial_messages[1] == {"role": "user", "content": "hello"}


@pytest.mark.asyncio
async def test_agent_loop_ignores_legacy_math_qa_mode_metadata(tmp_path) -> None:
    """Legacy math_qa_mode metadata does not alter behavior — review context is still injected."""
    loop = AgentLoop(MessageBus(), DummyProvider(), tmp_path)
    runner = CapturingRunner()
    loop.runner = runner
    session = Session(key="test:math")
    session.metadata["math_qa_mode"] = True

    await loop._run_agent_loop(
        [{"role": "user", "content": "求极限"}],
        session=session,
        session_key=session.key,
    )

    # Review context is always injected as the first system message
    assert runner.initial_messages[0]["role"] == "system"
    assert runner.initial_messages[1] == {"role": "user", "content": "求极限"}


@pytest.mark.asyncio
async def test_agent_loop_pending_drain_waits_for_running_subagent_results(tmp_path, monkeypatch) -> None:
    loop = AgentLoop(MessageBus(), DummyProvider(), tmp_path)
    subagents = RunningSubagents(running=1)
    loop.subagents = subagents
    session = Session(key="test:pending")
    session.metadata["review_target"] = "target"
    monkeypatch.setattr(
        "nanobot.agent.loop.prepare_code_review_context",
        lambda *_args, **_kwargs: _review_fallback_preparation(),
    )
    pending: asyncio.Queue = asyncio.Queue()
    runner = MultiDrainRunner(pending, subagents)
    loop.runner = runner

    await loop._run_agent_loop(
        [{"role": "user", "content": "hello"}],
        session=session,
        session_key=session.key,
        pending_queue=pending,
    )

    assert [batch[0]["_metadata"]["subagent_task_id"] for batch in runner.injected_batches] == [
        "first",
        "second",
    ]
    assert runner.injected_batches[1][-1]["_metadata"]["injected_event"] == "subagent_barrier"


@pytest.mark.asyncio
async def test_agent_loop_drain_waits_on_subagent_manager_result_queue(tmp_path, monkeypatch) -> None:
    loop = AgentLoop(MessageBus(), DummyProvider(), tmp_path)
    subagents = RunningSubagents(running=1)
    loop.subagents = subagents
    session = Session(key="test:pending")
    session.metadata["review_target"] = "target"
    monkeypatch.setattr(
        "nanobot.agent.loop.prepare_code_review_context",
        lambda *_args, **_kwargs: _review_fallback_preparation(),
    )
    pending: asyncio.Queue = asyncio.Queue()
    runner = ManagerQueueDrainRunner(subagents)
    loop.runner = runner

    await loop._run_agent_loop(
        [{"role": "user", "content": "hello"}],
        session=session,
        session_key=session.key,
        pending_queue=pending,
    )

    assert len(runner.injected) == 2
    assert runner.injected[0]["_metadata"]["subagent_task_id"] == "direct"
    assert runner.injected[1]["_metadata"]["injected_event"] == "subagent_barrier"


def test_invalid_max_concurrent_requests_falls_back_to_default(monkeypatch) -> None:
    warnings: list[str] = []

    def capture_warning(message: str, raw: str) -> None:
        warnings.append(message.format(raw))

    monkeypatch.setenv("NANOBOT_MAX_CONCURRENT_REQUESTS", "not-an-int")
    monkeypatch.setattr("nanobot.agent.loop.logger.warning", capture_warning)

    assert AgentLoop._parse_max_concurrent_requests() == 3
    assert warnings == ["Invalid NANOBOT_MAX_CONCURRENT_REQUESTS='not-an-int'; using default 3"]


def test_cleanup_session_lock_removes_idle_lock() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    lock = asyncio.Lock()
    loop._session_locks = {"session": lock}
    loop._pending_queues = {}
    loop._active_tasks = {}

    loop._cleanup_session_lock("session", lock)

    assert loop._session_locks == {}


def test_sanitize_persisted_blocks_converts_non_dict_blocks() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop.max_tool_result_chars = 20

    result = loop._sanitize_persisted_blocks(["hello", b"raw", 123])

    assert result == [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "[binary content omit\n... (truncated)"},
        {"type": "text", "text": "123"},
    ]


def test_session_history_preserves_subagent_result_metadata() -> None:
    raw_result = '{"submitted":true,"findings":[],"errors":[]}'
    session = Session(key="test:review")
    session.add_message(
        "assistant",
        "wrapped result",
        injected_event="subagent_result",
        subagent_task_id="task",
        subagent_label="security",
        subagent_status="ok",
        subagent_result=raw_result,
    )

    history = session.get_history()

    assert history == [
        {
            "role": "assistant",
            "content": "wrapped result",
            "_metadata": {
                "injected_event": "subagent_result",
                "subagent_task_id": "task",
                "subagent_label": "security",
                "subagent_status": "ok",
                "subagent_result": raw_result,
            },
        }
    ]


def test_consumed_subagent_result_helper_matches_consumed_task() -> None:
    msg = _subagent_result("task-1", "security")

    assert _is_consumed_subagent_result(msg, {"task-1"}) is True
    assert _is_consumed_subagent_result(msg, {"other"}) is False
    assert _is_consumed_subagent_result(msg, set()) is False


def test_review_subagent_tools_include_structured_submitter(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )

    tools = manager._build_tools()

    assert tools.has("review_submit")
    assert not tools.has("spawn")
    assert not tools.has("message")


def test_review_subagent_inherits_subagent_tool_config(tmp_path) -> None:
    tools_config = ToolsConfig()
    tools_config.exec.timeout = 123
    tools_config.restrict_to_workspace = False
    tools_config.github_repo.token = "gh-test-token"
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        tools_config=tools_config,
        restrict_to_workspace=True,
    )

    ctx = manager._build_tool_context()

    assert ctx.config.exec.timeout == 123
    assert ctx.config.restrict_to_workspace is True
    assert ctx.config.github_repo.token == "gh-test-token"


@pytest.mark.asyncio
async def test_subagent_hook_uses_streaming_request_path() -> None:
    provider = StreamingChoiceProvider()
    runner = AgentRunner(provider)
    hook = SubagentHook("task-1")
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "review this"}],
        tools=ToolRegistry(),
        model="dummy",
        max_iterations=1,
        max_tool_result_chars=1000,
        hook=hook,
    )
    context = AgentHookContext(iteration=0, messages=list(spec.initial_messages))

    response = await runner._request_model(spec, spec.initial_messages, hook, context)

    assert provider.calls == ["stream"]
    assert response.content == "streamed final"
    assert context.streamed_content is True


def test_review_subagent_extracts_review_submit_tool_result() -> None:
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "review_submit",
            "content": (
                '{"submitted":true,"findings":[{"severity":"high","file":"src/app.py",'
                '"line":1,"title":"Issue","evidence":"line1",'
                '"impact":"bad","recommendation":"fix"}],"errors":[]}'
            ),
        }
    ]

    result = SubagentManager._extract_review_submit_result(messages)

    assert result is not None
    assert '"Issue"' in result
    assert result.startswith("{")
    assert '"submitted": true' in result


def test_review_subagent_extracts_untruncated_review_submit_event() -> None:
    raw_result = (
        '{"submitted":true,"findings":[{"severity":"high","file":"src/app.py",'
        '"line":1,"title":"Issue","evidence":"line1",'
        '"impact":"bad","recommendation":"fix"}],"errors":[]}'
    )
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "review_submit",
            "content": raw_result[:60] + "\n... (truncated)",
        }
    ]
    tool_events = [
        {
            "name": "review_submit",
            "status": "ok",
            "detail": raw_result[:120] + "...",
            "raw_result": raw_result,
        }
    ]

    result = SubagentManager._extract_review_submit_result(messages, tool_events)

    assert result is not None
    assert '"Issue"' in result
    assert result.startswith("{")
    assert '"submitted": true' in result


def test_review_subagent_ignores_unprocessed_review_submit_arguments() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "review_submit",
                        "arguments": (
                            '{"findings":[{"severity":"high","file":"src/app.py",'
                            '"line":1,"title":"Issue","evidence":"line1",'
                            '"impact":"bad","recommendation":"fix"}]}'
                        ),
                    },
                }
            ],
        }
    ]

    assert SubagentManager._extract_review_submit_result(messages) is None


@pytest.mark.asyncio
async def test_review_subagent_finalization_retry_forces_review_submit(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )
    runner = ReviewSubmitRetryRunner()
    manager.runner = runner  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task1",
        label="security",
        task_description="review security",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task1",
        "review security",
        "security",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
    )

    assert len(runner.specs) == 2
    assert runner.specs[0].tool_choice is None
    assert "read_file" in runner.specs[0].soft_tool_error_tools
    assert "review_submit" not in runner.specs[0].soft_tool_error_tools
    assert runner.specs[1].tool_choice == {
        "type": "function",
        "function": {"name": "review_submit"},
    }
    assert runner.specs[1].response_format == {"type": "json_object"}
    retry_content = str(runner.specs[1].initial_messages[-1]["content"])
    assert "JSON" in retry_content
    assert runner.specs[1].tools.has("review_submit")
    assert status.phase == "done"
    assert status.stop_reason == "completed"
    assert manager._dimension_state("cli:direct", "security") == "completed"


class MaxIterationsThenSubmitRunner:
    """Runner that hits max_iterations first, then submits on forced retry."""

    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        if len(self.specs) == 1:
            return AgentRunResult(
                final_content="review prose without tool call",
                messages=list(spec.initial_messages),
                stop_reason="max_iterations",
                tool_events=[
                    {"name": "read_file", "status": "ok", "detail": "file content"},
                ],
            )
        return AgentRunResult(
            final_content=None,
            messages=list(spec.initial_messages),
            tool_events=[
                {
                    "name": "review_submit",
                    "status": "ok",
                    "detail": '{"submitted": true, "findings": [], "errors": []}',
                    "raw_result": '{"submitted":true,"findings":[],"errors":[]}',
                }
            ],
        )


class AlwaysNoSubmitRunner:
    """Runner that never produces a review_submit result."""

    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return AgentRunResult(
            final_content="review prose without tool call",
            messages=list(spec.initial_messages),
            stop_reason="max_iterations" if len(self.specs) == 1 else "completed",
        )


@pytest.mark.asyncio
async def test_review_subagent_max_iterations_triggers_forced_review_submit(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )
    runner = MaxIterationsThenSubmitRunner()
    manager.runner = runner  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task1",
        label="security",
        task_description="review security",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task1",
        "review security",
        "security",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
    )

    assert len(runner.specs) == 2
    assert runner.specs[0].tool_choice is None
    assert runner.specs[1].tool_choice == {
        "type": "function",
        "function": {"name": "review_submit"},
    }
    assert runner.specs[1].response_format == {"type": "json_object"}
    assert status.phase == "done"
    assert status.stop_reason == "completed"
    assert manager._dimension_state("cli:direct", "security") == "completed"

    # The announced subagent_result should be canonical JSON
    results = manager.drain_session_results("cli:direct", limit=1)
    assert len(results) == 1
    msg = results[0]
    assert msg.metadata["subagent_status"] == "ok"
    result_json = json.loads(msg.metadata["subagent_result"])
    assert result_json == {"submitted": True, "findings": [], "errors": []}


@pytest.mark.asyncio
async def test_review_subagent_retry_failure_announces_error_not_success(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )
    runner = AlwaysNoSubmitRunner()
    manager.runner = runner  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task1",
        label="security",
        task_description="review security",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task1",
        "review security",
        "security",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
    )

    assert len(runner.specs) == 2
    assert runner.specs[1].tool_choice == {
        "type": "function",
        "function": {"name": "review_submit"},
    }
    # Should NOT be announced as "completed successfully"
    assert status.phase == "done"
    assert manager._dimension_state("cli:direct", "security") == "failed"

    results = manager.drain_session_results("cli:direct", limit=1)
    assert len(results) == 1
    msg = results[0]
    assert msg.metadata["subagent_status"] == "error"
    assert "no structured findings submitted" in msg.metadata["subagent_result"].lower()


@pytest.mark.asyncio
async def test_subagent_manager_rejects_duplicate_dimension_lifecycle(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )
    runner = BlockingSubmitRunner()
    manager.runner = runner  # type: ignore[assignment]

    first = await manager.spawn(
        "review security",
        "security",
        origin_channel="cli",
        origin_chat_id="direct",
        session_key="cli:direct",
    )
    running_duplicate = await manager.spawn(
        "review security again",
        "security",
        origin_channel="cli",
        origin_chat_id="direct",
        session_key="cli:direct",
    )

    assert "started" in first
    assert "already running" in running_duplicate
    assert manager._dimension_state("cli:direct", "security") == "running"
    for _ in range(50):
        if runner.specs:
            break
        await asyncio.sleep(0.01)
    assert len(runner.specs) == 1

    runner.release.set()
    msg = await manager.wait_for_session_result("cli:direct", timeout=0.5)
    assert msg is not None
    if manager._running_tasks:
        await asyncio.gather(*list(manager._running_tasks.values()))

    completed_duplicate = await manager.spawn(
        "review security after completion",
        "security",
        origin_channel="cli",
        origin_chat_id="direct",
        session_key="cli:direct",
    )

    assert "already completed" in completed_duplicate
    assert manager._dimension_state("cli:direct", "security") == "completed"
    assert len(runner.specs) == 1


# ---------------------------------------------------------------------------
# Review metadata propagation & subagent tool context alignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_workspace_uses_local_root(tmp_path) -> None:
    """Subagent tools resolve paths relative to review_local_root, not project root."""
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "target.py").write_text("x = 1\n", encoding="utf-8")

    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )

    captured: list[ToolRegistry] = []

    class WorkspaceCaptureRunner:
        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            captured.append(spec.tools)
            return AgentRunResult(
                final_content=None,
                messages=list(spec.initial_messages),
                tool_events=[
                    {"name": "read_file", "status": "ok", "detail": "content"},
                    {
                        "name": "review_submit",
                        "status": "ok",
                        "detail": '{"submitted": true, "findings": [], "errors": []}',
                        "raw_result": '{"submitted":true,"findings":[],"errors":[]}',
                    },
                ],
            )

    manager.runner = WorkspaceCaptureRunner()  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task1",
        label="security",
        task_description="review",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task1",
        "review",
        "security",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        origin_metadata={
            ReviewMetaKey.TARGET_TYPE: "local",
            ReviewMetaKey.LOCAL_ROOT: str(subdir),
        },
    )

    assert status.phase == "done"
    assert len(captured) == 1
    read_tool = captured[0].get("read_file")
    assert read_tool is not None
    tool_workspace = Path(read_tool._workspace).resolve()  # type: ignore[attr-defined]
    assert tool_workspace == subdir.resolve()


@pytest.mark.asyncio
async def test_subagent_hook_sets_request_context(tmp_path) -> None:
    """SubagentHook.before_execute_tools sets current_request_context with metadata."""
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )
    tools = manager._build_tools()

    hook = SubagentHook(
        "task1",
        None,
        tools=tools,
        origin_channel="websocket",
        origin_chat_id="chat",
        session_key="websocket:chat",
        metadata={
            ReviewMetaKey.TARGET_TYPE: "local",
            ReviewMetaKey.LOCAL_ROOT: str(tmp_path),
        },
    )

    context = AgentHookContext(
        iteration=0,
        messages=[],
        tool_calls=[
            ToolCallRequest(id="call1", name="read_file", arguments={"path": "test.py"}),
        ],
    )

    await hook.before_execute_tools(context)

    ctx = current_request_context()
    assert ctx is not None
    assert ctx.metadata.get(ReviewMetaKey.TARGET_TYPE) == "local"
    assert ctx.metadata.get(ReviewMetaKey.LOCAL_ROOT) == str(tmp_path)


@pytest.mark.asyncio
async def test_subagent_read_file_blocked_for_github_target(tmp_path) -> None:
    """read_file blocks local workspace reads when target_type is github."""
    (tmp_path / "local.py").write_text("x = 1\n", encoding="utf-8")

    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )
    tools = manager._build_tools()

    hook = SubagentHook(
        "task1",
        None,
        tools=tools,
        origin_channel="websocket",
        origin_chat_id="chat",
        metadata={ReviewMetaKey.TARGET_TYPE: "github"},
    )

    context = AgentHookContext(
        iteration=0,
        messages=[],
        tool_calls=[
            ToolCallRequest(id="call1", name="read_file", arguments={"path": "local.py"}),
        ],
    )

    await hook.before_execute_tools(context)

    read_tool = tools.get("read_file")
    assert read_tool is not None
    result = await read_tool.execute(path="local.py")
    assert "cannot read local workspace files" in str(result).lower()


# ---------------------------------------------------------------------------
# Evidence-less empty findings guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_findings_without_evidence_is_incomplete(tmp_path) -> None:
    """findings:[] with no evidence reads → error/incomplete, not clean."""
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )

    class NoEvidenceSubmitRunner:
        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            return AgentRunResult(
                final_content=None,
                messages=list(spec.initial_messages),
                tool_events=[
                    {
                        "name": "review_submit",
                        "status": "ok",
                        "detail": '{"submitted": true, "findings": [], "errors": []}',
                        "raw_result": '{"submitted":true,"findings":[],"errors":[]}',
                    },
                ],
            )

    manager.runner = NoEvidenceSubmitRunner()  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task1",
        label="security",
        task_description="review",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task1",
        "review",
        "security",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        origin_metadata={ReviewMetaKey.TARGET_TYPE: "local"},
    )

    assert status.phase == "error"
    results = manager.drain_session_results("cli:direct", limit=1)
    assert len(results) == 1
    assert results[0].metadata["subagent_status"] == "error"
    assert "no target evidence" in results[0].metadata["subagent_result"].lower()


@pytest.mark.asyncio
async def test_empty_findings_with_local_evidence_allows_no_findings(tmp_path) -> None:
    """findings:[] backed by successful read_file → still ok (no_findings)."""
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )

    class EvidenceSubmitRunner:
        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            return AgentRunResult(
                final_content=None,
                messages=list(spec.initial_messages),
                tool_events=[
                    {"name": "read_file", "status": "ok", "detail": "content"},
                    {
                        "name": "review_submit",
                        "status": "ok",
                        "detail": '{"submitted": true, "findings": [], "errors": []}',
                        "raw_result": '{"submitted":true,"findings":[],"errors":[]}',
                    },
                ],
            )

    manager.runner = EvidenceSubmitRunner()  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task1",
        label="security",
        task_description="review",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task1",
        "review",
        "security",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        origin_metadata={ReviewMetaKey.TARGET_TYPE: "local"},
    )

    assert status.phase == "done"
    results = manager.drain_session_results("cli:direct", limit=1)
    assert len(results) == 1
    assert results[0].metadata["subagent_status"] == "ok"


@pytest.mark.asyncio
async def test_empty_findings_with_github_evidence_allows_no_findings(tmp_path) -> None:
    """For GitHub targets, successful github_review counts as evidence."""
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )

    class GithubEvidenceRunner:
        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            return AgentRunResult(
                final_content=None,
                messages=list(spec.initial_messages),
                tool_events=[
                    {"name": "github_review", "status": "ok", "detail": "repo content"},
                    {
                        "name": "review_submit",
                        "status": "ok",
                        "detail": '{"submitted": true, "findings": [], "errors": []}',
                        "raw_result": '{"submitted":true,"findings":[],"errors":[]}',
                    },
                ],
            )

    manager.runner = GithubEvidenceRunner()  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task1",
        label="security",
        task_description="review",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task1",
        "review",
        "security",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        origin_metadata={ReviewMetaKey.TARGET_TYPE: "github"},
    )

    assert status.phase == "done"
    results = manager.drain_session_results("cli:direct", limit=1)
    assert len(results) == 1
    assert results[0].metadata["subagent_status"] == "ok"


@pytest.mark.asyncio
async def test_agent_loop_review_mode_injects_code_review_context(tmp_path, monkeypatch) -> None:
    loop = AgentLoop(MessageBus(), DummyProvider(), tmp_path)
    runner = CapturingRunner()
    loop.runner = runner
    monkeypatch.setattr(
        "nanobot.agent.loop.prepare_code_review_context",
        lambda *_args, **_kwargs: _review_coordinator_preparation(),
    )
    session = Session(key="test:review")
    session.metadata["review_mode"] = True

    await loop._run_agent_loop(
        [{"role": "user", "content": "please review https://github.com/test/repo"}],
        session=session,
        session_key=session.key,
    )

    assert runner.initial_messages is not None
    assert runner.initial_messages[0]["role"] == "system"
    assert "planning coordinator" in runner.initial_messages[0]["content"]
    assert "submit_review_plan" in runner.initial_messages[0]["content"]


@pytest.mark.asyncio
async def test_agent_loop_review_message_metadata_is_visible_same_turn(tmp_path, monkeypatch) -> None:
    from nanobot.bus.events import InboundMessage

    loop = AgentLoop(MessageBus(), DummyProvider(), tmp_path)
    runner = CapturingRunner()
    loop.runner = runner
    async def mock_review(
        _messages: list[dict[str, Any]],
        session_meta: dict[str, Any],
        **_kwargs: Any,
    ) -> ReviewPreparation:
        assert session_meta["review_target"] == "https://github.com/test/repo"
        assert session_meta["review_focus"] == ["dependency"]
        session_meta[ReviewMetaKey.ALLOWED_DIMENSIONS] = ["dependency"]
        return ReviewPreparation(
            None,
            "- Name: https://github.com/test/repo\n- Type: github\nDependency Reviewer",
        )

    monkeypatch.setattr("nanobot.agent.loop.prepare_code_review_context", mock_review)
    session = Session(key="websocket:review")
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="review",
        content="请审查登录逻辑",
        metadata={
            "review_target": "https://github.com/test/repo",
            "review_target_type": "github",
            "review_focus": ["dependency"],
            "review_mode_variant": "quick",
        },
    )
    ctx = TurnContext(
        msg=msg,
        session_key=session.key,
        state=TurnState.RESTORE,
        turn_id="turn",
        session=session,
    )

    await loop._state_restore(ctx)
    await loop._state_compact(ctx)
    await loop._state_build(ctx)
    await loop._state_run(ctx)

    assert runner.initial_messages is not None
    assert runner.initial_messages[0]["role"] == "system"
    assert "- Name: https://github.com/test/repo" in runner.initial_messages[0]["content"]
    assert "- Type: github" in runner.initial_messages[0]["content"]
    assert session.metadata["allowed_review_dimensions"] == ["dependency"]
    assert "Dependency Reviewer" in runner.initial_messages[0]["content"]
    assert "Security Reviewer" not in runner.initial_messages[0]["content"]


@pytest.mark.asyncio
async def test_agent_loop_keeps_manual_spawn_tool_registered(tmp_path) -> None:
    loop = AgentLoop(MessageBus(), DummyProvider(), tmp_path)

    assert loop.tools.has("spawn")


@pytest.mark.asyncio
async def test_process_system_message_accepts_agent_loop_return_shape(tmp_path) -> None:
    from nanobot.bus.events import InboundMessage

    loop = AgentLoop(MessageBus(), DummyProvider(), tmp_path)
    runner = CapturingRunner()
    loop.runner = runner
    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="websocket:parent",
        content="subagent result",
    )

    response = await loop._process_system_message(msg)

    assert response is not None
    assert response.content == "ok"
