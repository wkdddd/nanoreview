from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.hooks import AgentHookContext
from nanoreview.agent.hooks.subagent import SubagentHook, SubagentStatus
from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.review_loop import ReviewLoopOutcome, ReviewTurnRequest
from nanoreview.agent.review_state import ReviewPhase, ReviewRunState, ReviewRunStatus
from nanoreview.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanoreview.agent.subagent import SubagentManager
from nanoreview.agent.subagent_profiles import (
    SubagentExecutionLimits,
    SubagentExecutionProfile,
)
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


#: A minimal non-review profile for runtime-plumbing tests.
#:
#: The manager no longer has a built-in generic default, so tests that only
#: exercise the runtime plumbing (limits, timeout, context window, hooks)
#: declare their own explicit profile instead of relying on an implicit
#: fallback. ``core`` is always a declared scope, so the profile validates.
_TEST_PROFILE_ID = "test-plumbing"


def _explicit_profile_manager(tmp_path: Path, **kwargs: Any) -> SubagentManager:
    profile = SubagentExecutionProfile(id=_TEST_PROFILE_ID, scope="core")
    return SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        execution_profiles={_TEST_PROFILE_ID: profile},
        **kwargs,
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


class OrdinaryUserDrainRunner:
    """Injector probe: the mid-turn drain only ever yields ordinary user text."""

    def __init__(self, pending: asyncio.Queue) -> None:
        self.pending = pending
        self.injected: list[dict[str, Any]] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        assert spec.injection_callback is not None
        await self.pending.put(_inbound("user interjection", sender_id="user"))
        self.injected = await spec.injection_callback(limit=3)
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
async def test_conversation_drain_only_yields_ordinary_user_messages(
    tmp_path,
) -> None:
    """The conversation drain never waits on or consumes subagent results.

    Sub-agent results now belong to ``ReviewLoop``'s own result queue; the
    conversation turn only injects ordinary user messages queued behind it.
    """
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    session_key = "cli:pending"
    pending: asyncio.Queue = asyncio.Queue()
    runner = OrdinaryUserDrainRunner(pending)
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

    assert runner.injected == [
        {"role": "user", "content": "user interjection"}
    ]


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


def test_subagent_profile_is_required_and_has_no_generic_fallback(tmp_path) -> None:
    """A subagent task must name a registered profile; there is no default.

    The old generic profile (core tools, read-only) was the implicit fallback —
    removing it means an unlabelled task fails loudly instead of running with a
    silently wrong scope.
    """
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        execution_profiles=reviewer_execution_profiles(),
    )

    for missing in ({}, {"profile_id": ""}, {"profile_id": "generic"}):
        with pytest.raises(ValueError):
            manager.resolve_profile(missing)


def test_subagent_profiles_authorize_tools_by_scope(tmp_path) -> None:
    manager = SubagentManager(
        DummyProvider(),
        tmp_path,
        MessageBus(),
        max_tool_result_chars=1000,
        execution_profiles=reviewer_execution_profiles(),
    )

    reviewer_profile = manager.resolve_profile({"profile_id": "security"})
    reviewer = manager.build_tools(reviewer_profile, tmp_path, target_type="local")
    github_reviewer = manager.build_tools(reviewer_profile, tmp_path, target_type="github")

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
    manager = _explicit_profile_manager(tmp_path)
    runner = SpecCapturingRunner()
    manager.runner = runner  # type: ignore[assignment]
    limits = SubagentExecutionLimits(
        max_iterations=13,
        max_tokens=777,
        timeout_seconds=30,
    )
    status = SubagentStatus(
        task_id="task-limits",
        label=_TEST_PROFILE_ID,
        task_description="bounded task",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task-limits",
        "bounded task",
        _TEST_PROFILE_ID,
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        origin_metadata={"profile_id": _TEST_PROFILE_ID},
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
    manager = _explicit_profile_manager(tmp_path)
    manager.runner = SlowRunner()  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task-timeout",
        label=_TEST_PROFILE_ID,
        task_description="slow task",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task-timeout",
        "slow task",
        _TEST_PROFILE_ID,
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        origin_metadata={"profile_id": _TEST_PROFILE_ID},
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
    manager = _explicit_profile_manager(tmp_path)
    manager.runner = CompressionStopRunner(  # type: ignore[assignment]
        stop_reason, "sync compression failed after 2 attempts: no content"
    )
    status = SubagentStatus(
        task_id="task-compress",
        label=_TEST_PROFILE_ID,
        task_description="review task",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task-compress",
        "review task",
        _TEST_PROFILE_ID,
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        origin_metadata={"profile_id": _TEST_PROFILE_ID},
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
    manager = _explicit_profile_manager(tmp_path, context_window_tokens=32_768)
    runner = SpecCapturingRunner()
    manager.runner = runner  # type: ignore[assignment]
    status = SubagentStatus(
        task_id="task-window",
        label=_TEST_PROFILE_ID,
        task_description="windowed task",
        started_at=0.0,
    )

    await manager._run_subagent(
        "task-window",
        "windowed task",
        _TEST_PROFILE_ID,
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        origin_metadata={"profile_id": _TEST_PROFILE_ID},
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
    manager = _explicit_profile_manager(tmp_path, context_window_tokens=32_768)
    runner = SpecCapturingRunner()
    manager.runner = runner  # type: ignore[assignment]
    manager.set_provider(DummyProvider(), "switched-model", 131_072)

    status = SubagentStatus(
        task_id="task-switch",
        label=_TEST_PROFILE_ID,
        task_description="post-switch task",
        started_at=0.0,
    )
    await manager._run_subagent(
        "task-switch",
        "post-switch task",
        _TEST_PROFILE_ID,
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
        status,
        origin_metadata={"profile_id": _TEST_PROFILE_ID},
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
    manager = _explicit_profile_manager(tmp_path)
    runner = BlockingSubmitRunner()
    manager.runner = runner  # type: ignore[assignment]

    first = await manager.spawn(
        "review security",
        "security",
        origin_channel="cli",
        origin_chat_id="direct",
        session_key="cli:direct",
        origin_metadata={"profile_id": _TEST_PROFILE_ID},
    )
    running_duplicate = await manager.spawn(
        "review security again",
        "security",
        origin_channel="cli",
        origin_chat_id="direct",
        session_key="cli:direct",
        origin_metadata={"profile_id": _TEST_PROFILE_ID},
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
        origin_metadata={"profile_id": _TEST_PROFILE_ID},
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
    manager = _explicit_profile_manager(tmp_path)
    tools = manager.build_tools(
        manager.resolve_profile({"profile_id": _TEST_PROFILE_ID}), tmp_path
    )

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

    manager = _explicit_profile_manager(tmp_path)
    tools = manager.build_tools(
        manager.resolve_profile({"profile_id": _TEST_PROFILE_ID}),
        tmp_path,
        target_type="github",
    )

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
        origin_metadata={"profile_id": "security", ReviewMetaKey.TARGET_TYPE: "local"},
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
        execution_profiles=reviewer_execution_profiles(),
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
        origin_metadata={"profile_id": "security", ReviewMetaKey.TARGET_TYPE: "github"},
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
async def test_coordinator_has_no_spawn_tool(tmp_path) -> None:
    """Sub-agent dispatch is review-only; the default registry exposes no spawn."""
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)

    assert not coordinator.tools.has("spawn")


@pytest.mark.asyncio
async def test_legacy_internal_event_is_dropped_without_a_model_turn(tmp_path) -> None:
    """A legacy system/subagent event must not start a turn or consume handoff.

    Such events no longer drive the conversation agent; the coordinator logs a
    warning and drops them instead of running a model call.
    """
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    runner = CapturingRunner()
    coordinator.conversation_loop._runner = runner
    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="websocket:parent",
        content="subagent result",
        session_key_override="websocket:parent",
    )

    response = await coordinator._execute_turn(
        msg,
        "websocket:parent",
        pending_queue=None,
        on_progress=None,
    )

    assert response is None
    assert runner.initial_messages is None


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


# ---------------------------------------------------------------------------
# Direct entry: session routing, serialisation and cancellation registration
# ---------------------------------------------------------------------------


def _current_user_text(spec: AgentRunSpec) -> str:
    """The turn's own user message (last user block in the frozen zone)."""
    for message in reversed(spec.frozen_messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


class SerializationProbeRunner:
    """Runner that records overlap so serialisation of direct turns is visible."""

    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []
        self.active = 0
        self.max_active = 0

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return AgentRunResult(
            final_content="ok",
            messages=[*spec.frozen_messages, *spec.working_messages],
        )


@pytest.mark.asyncio
async def test_same_session_direct_requests_serialize_and_each_get_replies(
    tmp_path,
) -> None:
    """Two direct requests for one session run one after another.

    The second request waits for the session lock instead of injecting itself
    into the running turn, so each request gets its own run, its own user
    message and its own reply.
    """
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    probe = SerializationProbeRunner()
    coordinator.conversation_loop._runner = probe
    key = "cli:serial"

    first = asyncio.create_task(
        coordinator.process_direct(
            "one", session_key=key, channel="cli", chat_id="serial"
        )
    )
    await asyncio.sleep(0.005)  # let the first request acquire the lock
    second = asyncio.create_task(
        coordinator.process_direct(
            "two", session_key=key, channel="cli", chat_id="serial"
        )
    )
    r1, r2 = await asyncio.gather(first, second)

    assert probe.max_active == 1  # serialised: never two concurrent runs
    assert r1 is not None and r1.content == "ok"
    assert r2 is not None and r2.content == "ok"
    assert len(probe.specs) == 2
    assert _current_user_text(probe.specs[0]).startswith("one")
    assert _current_user_text(probe.specs[1]).startswith("two")
    # The second request did not inject itself into the first turn's context.
    first_context = json.dumps(
        [*probe.specs[0].frozen_messages, *probe.specs[0].working_messages]
    )
    assert "two" not in first_context


@pytest.mark.asyncio
async def test_direct_request_waits_on_the_coordinator_session_lock(tmp_path) -> None:
    """Direct requests share the coordinator's per-session lock.

    The HTTP layer no longer keeps its own lock registry, so a direct request
    and a bus turn for the same session serialise against the same lock.
    """
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    coordinator.conversation_loop._runner = SpecCapturingRunner()
    key = "cli:shared"
    lock = coordinator._session_locks.setdefault(key, asyncio.Lock())

    await lock.acquire()  # simulate a bus turn holding the session
    direct = asyncio.create_task(
        coordinator.process_direct(
            "hi", session_key=key, channel="cli", chat_id="shared"
        )
    )
    await asyncio.sleep(0.01)
    assert not direct.done()  # blocked on the shared lock, not a private one
    lock.release()

    response = await asyncio.wait_for(direct, timeout=1.0)
    assert response is not None and response.content == "ok"


@pytest.mark.asyncio
async def test_priority_control_command_does_not_wait_for_the_lock(tmp_path) -> None:
    """``/stop`` via the direct entry runs without the execution lock.

    It must be dispatchable while the session lock is held — cancelling a
    running turn is the whole point of a stop — and it must not register itself
    in the cancellation set, or a later stop would cancel the previous stop.
    """
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    key = "cli:control"
    lock = coordinator._session_locks.setdefault(key, asyncio.Lock())

    await lock.acquire()
    try:
        response = await asyncio.wait_for(
            coordinator.process_direct(
                "/stop", session_key=key, channel="cli", chat_id="control"
            ),
            timeout=1.0,
        )
    finally:
        lock.release()

    assert response is not None
    assert "stop" in response.content.lower()
    assert key not in coordinator._active_tasks


@pytest.mark.asyncio
async def test_direct_uses_passed_session_key_for_review_lookup(tmp_path) -> None:
    """An admitted review runs even when its session key is not channel:chat_id.

    The CLI admits a run under an arbitrary key (e.g. ``review:<id>``) and then
    calls the direct entry with ``chat_id="review"``. The turn must look the run
    up by the passed session key, otherwise the review silently degrades into a
    plain conversation turn for the wrong session.
    """
    coordinator = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    requests = _stub_review_execution(coordinator)
    session_key = "review:abc123def456"
    admitted = _admit_review_run(coordinator, session_key)

    await coordinator.process_direct(
        "请审查登录逻辑",
        session_key=session_key,
        channel="cli",
        chat_id="review",
        metadata={
            **admitted,
            ReviewMetaKey.TARGET: "app.py",
            ReviewMetaKey.TARGET_TYPE: "local",
        },
    )

    assert len(requests) == 1
    assert requests[0].session_key == session_key
