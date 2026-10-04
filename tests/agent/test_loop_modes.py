from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.hooks import AgentHookContext
from nanoreview.agent.hooks.subagent import SubagentHook, SubagentStatus
from nanoreview.agent.conversation_loop import _is_consumed_subagent_result
from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.review_loop import ReviewLoopOutcome, ReviewTurnRequest
from nanoreview.agent.review_state import ReviewPhase, ReviewRunState, ReviewRunStatus
from nanoreview.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanoreview.agent.subagent import SubagentManager
from nanoreview.agent.subagent_profiles import SubagentExecutionLimits
from nanoreview.agent.tools.context import current_request_context
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.bus.events import InboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.config.schema import Config, ToolsConfig, _resolve_tool_config_refs
from nanoreview.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanoreview.review.profiles import reviewer_execution_profiles
from nanoreview.review.types import (
    ReviewMetaKey,
)
from nanoreview.session.manager import Session


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
        self.initial_messages = [*spec.frozen_messages, *spec.working_messages]
        return AgentRunResult(final_content="ok", messages=[*spec.frozen_messages, *spec.working_messages])


class SpecCapturingRunner:
    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return AgentRunResult(final_content="ok", messages=[*spec.frozen_messages, *spec.working_messages])


class SlowRunner:
    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        await asyncio.sleep(0.05)
        return AgentRunResult(final_content="late", messages=[*spec.frozen_messages, *spec.working_messages])


class SpawnExecutingRunner:
    def __init__(self) -> None:
        self.result: Any | None = None

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        from nanoreview.agent.hooks import AgentHookContext

        call = ToolCallRequest(
            id="call_spawn",
            name="spawn",
            arguments={"task": "review performance", "label": "Performance Reviewer"},
        )
        context = AgentHookContext(
            iteration=0,
            messages=[*spec.frozen_messages, *spec.working_messages],
            response=LLMResponse(content="spawning reviewer", tool_calls=[call]),
            tool_calls=[call],
        )
        assert spec.hook is not None
        await spec.hook.before_execute_tools(context)
        tool = spec.tools.get("spawn")
        assert tool is not None
        self.result = await tool.execute(**call.arguments)
        return AgentRunResult(
            final_content="ok", messages=[*spec.frozen_messages, *spec.working_messages], tools_used=["spawn"]
        )


class InjectionRunner:
    def __init__(self) -> None:
        self.injected: list[dict[str, Any]] | None = None

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        assert spec.injection_callback is not None
        self.injected = await spec.injection_callback(limit=3)
        return AgentRunResult(
            final_content="ok",
            messages=[*spec.frozen_messages, *spec.working_messages] + list(self.injected),
            had_injections=bool(self.injected),
        )


class ReviewSubmitRunner:
    """Runner whose single run ends with a successful review_submit.

    Terminal-tool forcing (prose answers, failed submissions) is handled inside
    ``AgentRunner`` via ``terminal_retry_limit``; the manager itself never
    starts a compensation run.
    """

    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return AgentRunResult(
            final_content=None,
            messages=[*spec.frozen_messages, *spec.working_messages],
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


class BlockingSubmitRunner:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        await self.release.wait()
        return AgentRunResult(
            final_content=None,
            messages=[*spec.frozen_messages, *spec.working_messages],
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
        assert all(
            item.get("_metadata", {}).get("injected_event") != "subagent_barrier"
            for item in second
        )

        messages = [*spec.frozen_messages, *spec.working_messages]
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
            messages=[*spec.frozen_messages, *spec.working_messages] + list(self.injected),
            had_injections=bool(self.injected),
        )


REVIEW_TURN_RUN_ID = "run-test-review"


def _admit_review_run(
    coordinator: SessionCoordinator,
    session_key: str,
    *,
    run_id: str = REVIEW_TURN_RUN_ID,
) -> dict[str, Any]:
    """Register a live running review run and return the admitted-turn metadata.

    Only the admitted turn of the *live* run enters the review pipeline: a
    session keeps its ``review_target`` metadata after the review ends, so the
    persisted target alone must never be enough (a later conversation turn
    would otherwise silently re-run the review it is meant to discuss).

    Bypassing admission is deliberate here — these tests exercise the turn-time
    behaviour of the review pipeline, not the admission boundary, which has its
    own coverage in ``tests/review/test_admission.py``.
    """
    coordinator.review_loop.runs[session_key] = ReviewRunState(
        run_id=run_id,
        session_key=session_key,
        input_fingerprint="fp",
        status=ReviewRunStatus.RUNNING,
        phase=ReviewPhase.REVIEW,
    )
    return {"_review_admitted": run_id}


def _stub_review_execution(
    coordinator: SessionCoordinator,
    *,
    report_markdown: str = "## Code Review Report\n\nNo actionable issues found.",
    produces_report: bool = True,
    stop_reason: str = "",
    error: str | None = None,
) -> list[ReviewTurnRequest]:
    """Replace ``ReviewLoop.execute`` and record the delegated requests.

    These cases pin the *loop* half of the boundary — which turns are delegated
    and how the produced report is delivered. The review lifecycle itself is
    covered by ``tests/agent/test_review_loop.py``.
    """
    requests: list[ReviewTurnRequest] = []

    async def _fake_execute(request: ReviewTurnRequest) -> ReviewLoopOutcome:
        requests.append(request)
        return ReviewLoopOutcome(
            report_markdown=report_markdown,
            stop_reason=stop_reason,
            error=error,
            produces_report=produces_report,
        )

    coordinator.review_loop.execute = _fake_execute  # type: ignore[method-assign]
    return requests


def _inbound(
    content: str,
    *,
    sender_id: str = "subagent",
    metadata: dict[str, Any] | None = None,
) -> Any:
    from nanoreview.bus.events import InboundMessage

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
    loop = SessionCoordinator.from_config(
        config, bus=MessageBus(), provider=DummyProvider()
    )

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


def test_the_coordinator_has_no_turn_state_machine() -> None:
    """The old ``TurnState``/``TurnContext`` phase machine was removed.

    The conversation turn is now a straight-line ``ConversationLoop`` and the
    review lifecycle lives in ``ReviewLoop``; nothing reintroduces a FINALIZE
    turn state.
    """
    from nanoreview.agent import conversation_loop as conversation_module
    from nanoreview.agent import coordinator as coordinator_module

    assert not hasattr(conversation_module, "TurnState")
    assert not hasattr(coordinator_module, "TurnState")


@pytest.mark.asyncio
async def test_admitted_review_turn_is_delegated_to_review_loop(tmp_path) -> None:
    """An admitted review turn is handed to ``ReviewLoop`` as a whole turn.

    The coordinator keeps turn semantics (bus, queues, cancel, routing); the
    review lifecycle — preparation, planning, dispatch, report, cleanup — lives
    in ``ReviewLoop``. So the conversation loop must not run for this turn.
    """

    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    runner = CapturingRunner()
    coordinator.conversation_loop._runner = runner
    requests = _stub_review_execution(
        coordinator, report_markdown="## Code Review Report\n\nNo actionable issues found."
    )
    session_key = "cli:review"
    admitted = _admit_review_run(coordinator, session_key)
    session = coordinator.sessions.get_or_create(session_key)
    session.add_message("user", "hello")
    session.add_message("assistant", "prior reply")
    coordinator.sessions.save(session)

    response = await coordinator.process_direct(
        "hello",
        session_key=session_key,
        channel="cli",
        chat_id="review",
        metadata=admitted,
    )

    assert runner.initial_messages is None
    assert len(requests) == 1
    assert requests[0].session_key == session_key
    assert requests[0].msg.content == "hello"
    assert response is not None
    assert response.content == "## Code Review Report\n\nNo actionable issues found."


@pytest.mark.asyncio
async def test_conversation_turn_never_delegates_to_review_loop(tmp_path) -> None:
    """A session that owns a review run must not re-enter review from metadata.

    The review target stays on the session after the review ends, so a
    conversation turn in the same session must run as a plain agent turn —
    otherwise it would silently re-run the review it is meant to discuss.
    """
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    runner = CapturingRunner()
    coordinator.conversation_loop._runner = runner
    requests = _stub_review_execution(coordinator)
    session_key = "cli:conversation"
    session = coordinator.sessions.get_or_create(session_key)
    session.metadata[ReviewMetaKey.TARGET] = "https://github.com/test/repo"
    coordinator.sessions.save(session)

    await coordinator.process_direct(
        "one more question",
        session_key=session_key,
        channel="cli",
        chat_id="conversation",
    )

    assert requests == []
    assert runner.initial_messages is not None
    joined = "\n".join(
        str(message.get("content")) for message in runner.initial_messages
    )
    assert "one more question" in joined


@pytest.mark.asyncio
async def test_conversation_pending_drain_waits_for_running_subagent_results(
    tmp_path,
) -> None:
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    subagents = RunningSubagents(running=1)
    coordinator.conversation_loop._subagents = subagents
    session_key = "cli:pending"
    pending: asyncio.Queue = asyncio.Queue()
    runner = MultiDrainRunner(pending, subagents)
    coordinator.conversation_loop._runner = runner
    msg = InboundMessage(
        channel="cli", sender_id="user", chat_id="pending", content="hello"
    )

    await coordinator.conversation_loop.process_message(
        msg,
        session_key=session_key,
        turn_id="turn",
        target_root=tmp_path,
        pending_queue=pending,
    )

    assert [
        batch[0]["_metadata"]["subagent_task_id"] for batch in runner.injected_batches
    ] == ["first", "second"]


@pytest.mark.asyncio
async def test_conversation_drain_waits_on_subagent_manager_result_queue(
    tmp_path,
) -> None:
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    subagents = RunningSubagents(running=1)
    coordinator.conversation_loop._subagents = subagents
    session_key = "cli:pending"
    pending: asyncio.Queue = asyncio.Queue()
    runner = ManagerQueueDrainRunner(subagents)
    coordinator.conversation_loop._runner = runner
    msg = InboundMessage(
        channel="cli", sender_id="user", chat_id="pending", content="hello"
    )

    await coordinator.conversation_loop.process_message(
        msg,
        session_key=session_key,
        turn_id="turn",
        target_root=tmp_path,
        pending_queue=pending,
    )

    assert len(runner.injected) == 1
    assert runner.injected[0]["_metadata"]["subagent_task_id"] == "direct"


def test_invalid_max_concurrent_requests_falls_back_to_default(monkeypatch) -> None:
    warnings: list[str] = []

    def capture_warning(message: str, raw: str) -> None:
        warnings.append(message.format(raw))

    monkeypatch.setenv("NANOBOT_MAX_CONCURRENT_REQUESTS", "not-an-int")
    monkeypatch.setattr("nanoreview.agent.coordinator.logger.warning", capture_warning)

    assert SessionCoordinator._parse_max_concurrent_requests() == 3
    assert warnings == ["Invalid NANOBOT_MAX_CONCURRENT_REQUESTS='not-an-int'; using default 3"]


def test_cleanup_session_lock_removes_idle_lock() -> None:
    loop = SessionCoordinator.__new__(SessionCoordinator)
    lock = asyncio.Lock()
    loop._session_locks = {"session": lock}
    loop._pending_queues = {}
    loop._active_tasks = {}

    loop._cleanup_session_lock("session", lock)

    assert loop._session_locks == {}


def test_sanitize_persisted_blocks_converts_non_dict_blocks() -> None:
    from nanoreview.agent.conversation_loop import ConversationLoop

    conv = ConversationLoop.__new__(ConversationLoop)
    conv._max_tool_result_chars = 20

    result = conv._sanitize_persisted_blocks(["hello", b"raw", 123])

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


def test_generic_subagent_tools_are_read_only(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        execution_profiles=reviewer_execution_profiles(),
    )

    tools = manager._build_tools()

    assert tools.tool_names == ["grep", "list_dir", "read_file"]
    assert not tools.has("review_submit")
    assert not tools.has("spawn")
    assert not tools.has("message")


def test_subagent_profiles_authorize_tools_by_scope(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        execution_profiles=reviewer_execution_profiles(),
    )

    generic = manager._build_tools()
    reviewer_profile = manager.resolve_profile({"profile_id": "security"})
    reviewer = manager.build_tools(reviewer_profile, tmp_path, target_type="local")
    github_reviewer = manager.build_tools(reviewer_profile, tmp_path, target_type="github")

    assert generic.tool_names == ["grep", "list_dir", "read_file"]
    assert reviewer.tool_names == [
        "grep",
        "list_dir",
        "local_review",
        "read_file",
        "review_submit",
    ]
    assert github_reviewer.tool_names == [
        "github_review",
        "grep",
        "list_dir",
        "read_file",
        "review_submit",
    ]
    assert not reviewer.has("github_review")
    assert not github_reviewer.has("local_review")
    assert not reviewer.has("shell")
    assert not reviewer.has("write_file")
    assert not reviewer.has("edit_file")
    assert not reviewer.has("spawn")


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
async def test_subagent_execution_limits_are_forwarded_to_runner(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )
    runner = SpecCapturingRunner()
    manager.runner = runner  # type: ignore[assignment]
    limits = SubagentExecutionLimits(
        max_iterations=13,
        max_tokens=777,
        timeout_seconds=30,
    )
    status = SubagentStatus(
        task_id="task-limits",
        label="generic",
        task_description="bounded task",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task-limits",
        "bounded task",
        "generic",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        execution_limits=limits,
    )

    assert len(runner.specs) == 1
    assert runner.specs[0].max_iterations == 13
    assert runner.specs[0].max_tokens == 777
    result = manager.drain_session_results("cli:direct", limit=1)[0]
    assert result.metadata["subagent_max_tokens"] == 777
    assert result.metadata["subagent_timeout_seconds"] == 30


@pytest.mark.asyncio
async def test_subagent_timeout_announces_error_with_budget_metadata(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )
    manager.runner = SlowRunner()  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task-timeout",
        label="generic",
        task_description="slow task",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task-timeout",
        "slow task",
        "generic",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        execution_limits=SubagentExecutionLimits(
            max_iterations=10,
            max_tokens=100,
            timeout_seconds=0.001,
        ),
    )

    assert status.phase == "error"
    assert status.stop_reason == "timeout"
    result = manager.drain_session_results("cli:direct", limit=1)[0]
    assert result.metadata["subagent_status"] == "error"
    assert "timed out" in result.metadata["subagent_result"]


class CompressionStopRunner:
    """Runner stub whose reviewer run is stopped by run-level compression."""

    def __init__(self, stop_reason: str, error: str) -> None:
        self.stop_reason = stop_reason
        self.error = error

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        return AgentRunResult(
            final_content=None,
            messages=list([*spec.frozen_messages, *spec.working_messages]),
            stop_reason=self.stop_reason,
            error=self.error,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["compression_failed", "compression_limit"])
async def test_subagent_compression_stop_marks_reviewer_error(
    tmp_path, stop_reason: str
) -> None:
    """A compression-stopped reviewer run is an error, so its dimension is incomplete."""
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
    )
    manager.runner = CompressionStopRunner(  # type: ignore[assignment]
        stop_reason, "sync compression failed after 2 attempts: no content"
    )
    status = SubagentStatus(
        task_id="task-compress",
        label="generic",
        task_description="review task",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task-compress",
        "review task",
        "generic",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
    )

    assert status.phase == "error"
    result = manager.drain_session_results("cli:direct", limit=1)[0]
    assert result.metadata["subagent_status"] == "error"
    assert "compression" in result.metadata["subagent_result"]


@pytest.mark.asyncio
async def test_compression_stop_outbound_is_not_marked_streamed(tmp_path) -> None:
    """compression_failed/limit stops must be delivered by the outbound message."""
    conversation = SessionCoordinator(
        MessageBus(), DummyProvider(), tmp_path
    ).conversation_loop
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="chat", content="hi")

    async def on_stream(_chunk: str) -> None:
        return None

    for stop_reason in ("compression_failed", "compression_limit"):
        outbound = conversation._assemble_outbound(
            msg,
            "compression failed: run stopped",
            None,
            stop_reason,
            False,
            [],
            on_stream,
            channel="cli",
            chat_id="chat",
        )
        assert outbound is not None
        # Not marked as streamed: the channel must actually send the error.
        assert "_streamed" not in outbound.metadata

    normal = conversation._assemble_outbound(
        msg,
        "the answer",
        None,
        "completed",
        False,
        [],
        on_stream,
        channel="cli",
        chat_id="chat",
    )
    assert normal is not None
    assert normal.metadata.get("_streamed") is True


@pytest.mark.asyncio
async def test_subagent_forwards_context_window_tokens_to_runner(tmp_path) -> None:
    """Regression: subagent AgentRunSpec must carry the manager's window.

    Before this wiring the subagent path left ``context_window_tokens``
    unset, so ``runner._snip_history`` skipped trimming and long reviewer
    runs grew unbounded.
    """
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        context_window_tokens=32_768,
    )
    runner = SpecCapturingRunner()
    manager.runner = runner  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task-window",
        label="generic",
        task_description="windowed task",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task-window",
        "windowed task",
        "generic",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
    )

    assert len(runner.specs) == 1
    assert runner.specs[0].context_window_tokens == 32_768


@pytest.mark.asyncio
async def test_subagent_set_provider_updates_context_window_tokens(tmp_path) -> None:
    """Regression: a runtime model switch must not leave a stale window.

    ``set_provider`` mirrors ``Consolidator.set_provider`` and forwards the
    snapshot's window so subagents spawned after the switch trim against
    the new model's context window.
    """
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        context_window_tokens=32_768,
    )
    runner = SpecCapturingRunner()
    manager.runner = runner  # type: ignore[assignment]
    manager.set_provider(DummyProvider(), "switched-model", 131_072)

    status = SubagentStatus(
        task_id="task-switch",
        label="generic",
        task_description="post-switch task",
        started_at=0.0,
    )
    await manager._run_subagent(
        "task-switch",
        "post-switch task",
        "generic",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
    )

    assert len(runner.specs) == 1
    assert runner.specs[0].context_window_tokens == 131_072


@pytest.mark.asyncio
async def test_subagent_hook_uses_streaming_request_path() -> None:
    provider = StreamingChoiceProvider()
    runner = AgentRunner(provider)
    hook = SubagentHook("task-1")
    spec = AgentRunSpec(
        frozen_messages=[{"role": "user", "content": "review this"}],
        working_messages=[],
        tools=ToolRegistry(),
        model="dummy",
        max_iterations=1,
        max_tool_result_chars=1000,
        hook=hook,
    )
    context = AgentHookContext(iteration=0, messages=[*spec.frozen_messages, *spec.working_messages])

    response = await runner._request_model(spec, [*spec.frozen_messages, *spec.working_messages], hook, context)

    assert provider.calls == ["stream"]
    assert response.content == "streamed final"
    assert context.streamed_content is True


@pytest.mark.asyncio
async def test_review_subagent_run_uses_terminal_submit_contract(tmp_path) -> None:
    """Single reviewer run: forcing review_submit is the runner's terminal-tool
    contract, and a canonical submission completes the dimension."""
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        execution_profiles=reviewer_execution_profiles(),
    )
    runner = ReviewSubmitRunner()
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
        origin_metadata={"profile_id": "security"},
    )

    assert len(runner.specs) == 1
    assert runner.specs[0].tool_choice is None
    assert "read_file" in runner.specs[0].soft_tool_error_tools
    assert "review_submit" not in runner.specs[0].soft_tool_error_tools
    assert runner.specs[0].terminal_tools == frozenset({"review_submit"})
    assert runner.specs[0].preserve_tool_result_tools == frozenset({"review_submit"})
    assert runner.specs[0].terminal_retry_limit >= 1
    assert status.phase == "done"
    assert status.stop_reason == "completed"
    assert manager._dimension_state("cli:direct", "security") == "completed"


@pytest.mark.asyncio
async def test_review_subagent_submit_result_is_announced_as_canonical_json(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        execution_profiles=reviewer_execution_profiles(),
    )
    runner = ReviewSubmitRunner()
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
        origin_metadata={"profile_id": "security"},
    )

    assert len(runner.specs) == 1
    assert runner.specs[0].tool_choice is None
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


class AlwaysNoSubmitRunner:
    """Runner whose single run never produces a review_submit result."""

    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return AgentRunResult(
            final_content="review prose without tool call",
            messages=[*spec.frozen_messages, *spec.working_messages],
            stop_reason="max_iterations",
        )


@pytest.mark.asyncio
async def test_review_subagent_without_submit_announces_error_not_success(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        execution_profiles=reviewer_execution_profiles(),
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
        origin_metadata={"profile_id": "security"},
    )

    assert len(runner.specs) == 1
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
        execution_profiles=reviewer_execution_profiles(),
    )

    captured: list[ToolRegistry] = []

    class WorkspaceCaptureRunner:
        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            captured.append(spec.tools)
            return AgentRunResult(
                final_content=None,
                messages=[*spec.frozen_messages, *spec.working_messages],
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
            "profile_id": "security",
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
        execution_profiles=reviewer_execution_profiles(),
    )

    class NoEvidenceSubmitRunner:
        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            return AgentRunResult(
                final_content=None,
                messages=[*spec.frozen_messages, *spec.working_messages],
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
        origin_metadata={"profile_id": "security", ReviewMetaKey.TARGET_TYPE: "local"},
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
        execution_profiles=reviewer_execution_profiles(),
    )

    class EvidenceSubmitRunner:
        async def run(self, spec: AgentRunSpec) -> AgentRunResult:
            return AgentRunResult(
                final_content=None,
                messages=[*spec.frozen_messages, *spec.working_messages],
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
                messages=[*spec.frozen_messages, *spec.working_messages],
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
async def test_review_turn_delegates_review_metadata_same_turn(tmp_path) -> None:
    """The turn's review metadata reaches ``ReviewLoop`` within the same turn.

    Preparation (which resolves the target and injects the reviewer prompt) now
    lives in ``ReviewLoop``; the coordinator's job is to hand over the turn
    metadata unchanged so preparation sees the target/focus carried by the
    message.
    """
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    runner = CapturingRunner()
    coordinator.conversation_loop._runner = runner
    requests = _stub_review_execution(coordinator)
    session_key = "websocket:review"
    metadata = {
        "review_target": "https://github.com/test/repo",
        "review_target_type": "github",
        "review_focus": ["dependency"],
        **_admit_review_run(coordinator, session_key),
    }

    await coordinator.process_direct(
        "请审查登录逻辑",
        session_key=session_key,
        channel="websocket",
        chat_id="review",
        metadata=metadata,
    )

    assert runner.initial_messages is None
    assert len(requests) == 1
    assert requests[0].metadata["review_target"] == "https://github.com/test/repo"
    assert requests[0].metadata["review_target_type"] == "github"
    assert requests[0].metadata["review_focus"] == ["dependency"]
    assert requests[0].msg.content.startswith("请审查登录逻辑")


@pytest.mark.asyncio
async def test_programmatic_review_report_is_sent_as_review_stream(tmp_path) -> None:
    report_markdown = "## Code Review Report: app.py\n\nNo actionable issues found."

    bus = MessageBus()
    coordinator = SessionCoordinator(bus, DummyProvider(), tmp_path)
    requests = _stub_review_execution(coordinator, report_markdown=report_markdown)
    session_key = "websocket:review-report"
    admitted = _admit_review_run(coordinator, session_key)

    await coordinator.process_direct(
        "审查",
        session_key=session_key,
        channel="websocket",
        chat_id="review-report",
        metadata={
            "_wants_stream": True,
            ReviewMetaKey.TARGET: "app.py",
            ReviewMetaKey.TARGET_TYPE: "local",
            **admitted,
        },
    )

    assert len(requests) == 1
    events = []
    while bus.outbound_size:
        events.append(await bus.consume_outbound())

    assert len(events) == 2
    assert events[0].metadata["_stream_kind"] == "review_report"
    assert events[0].metadata["_stream_delta"] is True
    assert events[1].metadata["_stream_kind"] == "review_report"
    assert events[1].metadata["_stream_end"] is True


@pytest.mark.asyncio
async def test_coordinator_keeps_manual_spawn_tool_registered(tmp_path) -> None:
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)

    assert coordinator.tools.has("spawn")


@pytest.mark.asyncio
async def test_process_system_message_accepts_conversation_return_shape(tmp_path) -> None:
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    runner = CapturingRunner()
    coordinator.conversation_loop._runner = runner
    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="websocket:parent",
        content="subagent result",
    )

    response = await coordinator.conversation_loop.process_system_message(
        msg, session_key="websocket:parent", turn_id="turn", target_root=tmp_path
    )

    assert response is not None
    assert response.content == "ok"


@pytest.mark.asyncio
async def test_unsettled_review_report_appends_the_settlement_failure(
    tmp_path,
) -> None:
    """A produced report whose run could not settle names the failure.

    The coordinator must not deliver the bare report as if nothing went wrong:
    when ``ReviewLoop`` produced a report but reported a settle failure, the
    bounded reason is appended so the user learns the session is still gated.
    """
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    _stub_review_execution(
        coordinator,
        report_markdown="## Code Review Report\n\nNo actionable issues found.",
        produces_report=True,
        stop_reason="error",
        error="the terminal review state could not be persisted (OSError)",
    )
    session_key = "websocket:review"
    admitted = _admit_review_run(coordinator, session_key)

    response = await coordinator.process_direct(
        "审查",
        session_key=session_key,
        channel="websocket",
        chat_id="review",
        metadata={
            ReviewMetaKey.TARGET: "app.py",
            ReviewMetaKey.TARGET_TYPE: "local",
            **admitted,
        },
    )

    assert response is not None
    assert "Review settlement failed" in response.content
    assert "OSError" in response.content
