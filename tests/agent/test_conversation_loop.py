"""Contract tests for the real ``ConversationLoop``.

``ConversationLoop`` owns one complete conversation turn: session/history,
frozen/working context, a core-only ``ToolRegistry``, one ``AgentRunner`` run,
history persistence and reply assembly. These tests pin the boundary the
coordinator and the review side rely on:

* the conversation core owns ``local_review`` but never sees the review-only
  coordinator tools (``review_judge`` / ``review_submit``);
* one turn runs exactly one runner and persists the user/assistant history;
* the review handoff reaches the first turn through the coordinator's own
  writer — the report artifact is never copied into session metadata, and the
  injection happens once even across later turns and a session reload;
* history consolidation runs before that write, and a failing write stops the
  turn before the runner starts;
* ``set_runtime_model`` moves the model id and context window for later turns.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.conversation_loop import (
    MAX_PENDING_CONVERSATION_MESSAGES,
    ConversationLoop,
)
from nanoreview.agent.coordinator import (
    REVIEW_HANDOFF_EVENT,
    ReviewHandoff,
    SessionCoordinator,
)
from nanoreview.agent.hooks.lifecycle import AgentHookContext
from nanoreview.agent.review_state import ReviewRunStatus
from nanoreview.agent.runner import (
    _MAX_INJECTIONS_PER_TURN,
    AgentRunResult,
    AgentRunSpec,
)
from nanoreview.agent.tools.context import current_workspace_scope
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.agent.tools.workspace_scope import (
    ACCESS_RESTRICTED,
    WORKSPACE_SCOPE_KEY,
)
from nanoreview.bus.events import InboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review.result import ReviewHandoffState, ReviewResult
from nanoreview.review.types import ReviewMetaKey

REPORT_REF = "review-artifacts/run-a.json"
REPORT_MARKDOWN = "## Code Review Report: repo\n\n### Findings\n\nNo issues.\n"


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
        _ = (tools, model, max_tokens, temperature, reasoning_effort, tool_choice)
        _ = response_format
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


class SpecCapturingRunner:
    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return AgentRunResult(
            final_content="ok",
            messages=[*spec.frozen_messages, *spec.working_messages],
        )


class InjectingRunner(SpecCapturingRunner):
    """Runner that pulls mid-turn injections the way the real runner does."""

    def __init__(self) -> None:
        super().__init__()
        self.injected: list[dict[str, Any]] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        if spec.injection_callback is not None:
            self.injected = await spec.injection_callback()
        return AgentRunResult(
            final_content="ok",
            messages=[
                *spec.frozen_messages,
                *spec.working_messages,
                *self.injected,
                {"role": "assistant", "content": "ok"},
            ],
            had_injections=bool(self.injected),
        )


class ContextRefreshRunner(SpecCapturingRunner):
    """Runner that refreshes the tool context the way the real runner does.

    ``AgentRunner`` calls ``hook.before_execute_tools`` before each batch so
    tools registered mid-turn (reconnected MCP capabilities) receive the
    request context. Recording the scope observed right after that refresh is
    what pins the turn scope against being dropped.
    """

    def __init__(self, batches: int = 2) -> None:
        super().__init__()
        self._batches = batches
        self.observed_scopes: list[Any] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        for _ in range(self._batches):
            await spec.hook.before_execute_tools(
                AgentHookContext(iteration=0, messages=[])
            )
            self.observed_scopes.append(current_workspace_scope())
        return AgentRunResult(
            final_content="ok",
            messages=[*spec.frozen_messages, *spec.working_messages],
        )


def _loop(tmp_path: Path) -> ConversationLoop:
    return SessionCoordinator(MessageBus(), DummyProvider(), tmp_path).conversation_loop


def _msg(content: str = "hello", *, chat_id: str = "direct") -> InboundMessage:
    return InboundMessage(
        channel="cli", sender_id="user", chat_id=chat_id, content=content
    )


def _handoff() -> ReviewHandoff:
    result = ReviewResult(
        run_id="run-a",
        session_key="cli:review",
        status=ReviewRunStatus.COMPLETED,
        handoff=ReviewHandoffState.COMPLETE,
        report_ref=REPORT_REF,
        coverage=("security",),
    )
    return ReviewHandoff(
        result=result, report_markdown=REPORT_MARKDOWN, fits=True
    )


def test_the_pending_cap_is_the_documented_bound() -> None:
    assert MAX_PENDING_CONVERSATION_MESSAGES == 20


@pytest.mark.asyncio
async def test_review_tools_are_excluded_from_the_conversation_turn(tmp_path) -> None:
    loop = _loop(tmp_path)
    runner = SpecCapturingRunner()
    loop._runner = runner

    await loop.process_message(
        _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
    )

    assert len(runner.specs) == 1
    tools = runner.specs[0].tools
    assert isinstance(tools, ToolRegistry)
    assert not tools.has("local_review")
    assert not tools.has("github_review")
    # The ordinary conversation tools stay available.
    assert tools.has("read_file")
    # Sub-agent dispatch is review-only now: the conversation turn must not
    # expose a spawn tool.
    assert not tools.has("spawn")


@pytest.mark.asyncio
async def test_a_conversation_turn_persists_history_and_returns_a_reply(
    tmp_path,
) -> None:
    loop = _loop(tmp_path)

    response = await loop.process_message(
        _msg("hello there"),
        session_key="cli:direct",
        turn_id="t1",
        target_root=tmp_path,
    )

    assert response is not None
    assert response.content == "ok"
    session = loop._sessions.get_or_create("cli:direct")
    roles = [message.get("role") for message in session.messages]
    assert roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_the_handoff_is_consumed_once_and_by_reference(tmp_path) -> None:
    loop = _loop(tmp_path)
    handoff = _handoff()

    await loop.process_message(
        _msg("follow-up"),
        session_key="cli:review",
        turn_id="t1",
        target_root=tmp_path,
        handoff=handoff,
    )

    session = loop._sessions.get_or_create("cli:review")
    injected = [
        message
        for message in session.messages
        if message.get("injected_event") == REVIEW_HANDOFF_EVENT
    ]
    assert len(injected) == 1
    assert injected[0]["review_run_id"] == "run-a"
    assert session.metadata[ReviewMetaKey.HANDOFF_RUN_ID] == "run-a"
    # The report artifact is referenced, never copied into session metadata.
    assert "report_markdown" not in str(session.metadata)


def test_set_runtime_model_moves_model_and_window(tmp_path) -> None:
    loop = _loop(tmp_path)

    loop.set_runtime_model(DummyProvider(), "switched-model", 131_072)

    assert loop._model == "switched-model"
    assert loop._context_window_tokens == 131_072


def _handoff_messages(loop: ConversationLoop, session_key: str) -> list[dict[str, Any]]:
    session = loop._sessions.get_or_create(session_key)
    return [
        message
        for message in session.messages
        if message.get("injected_event") == REVIEW_HANDOFF_EVENT
    ]


@pytest.mark.asyncio
async def test_the_first_turn_carries_the_report_and_its_directive(tmp_path) -> None:
    """The injected report reaches the runner, and the directive the system."""
    loop = _loop(tmp_path)
    runner = SpecCapturingRunner()
    loop._runner = runner

    await loop.process_message(
        _msg("what did you find?"),
        session_key="cli:review",
        turn_id="t1",
        target_root=tmp_path,
        handoff=_handoff(),
    )

    spec = runner.specs[0]
    # The complete report — not a summary — is what the model sees, replayed
    # from history in the working zone.
    assert len(spec.working_messages) == 1
    handoff_message = spec.working_messages[0]
    assert handoff_message["role"] == "assistant"
    assert handoff_message["content"].startswith("[ReviewAgent handoff]")
    assert REPORT_MARKDOWN in handoff_message["content"]
    # The provenance directive rides in the frozen system zone, which is never
    # summarized away mid-run.
    system = spec.frozen_messages[0]
    assert system["role"] == "system"
    assert _handoff().directive in system["content"]
    # The current user message stays in the frozen zone, after the system.
    assert spec.frozen_messages[1]["role"] == "user"
    assert "what did you find?" in spec.frozen_messages[1]["content"]


@pytest.mark.asyncio
async def test_a_later_turn_does_not_repeat_the_injection(tmp_path) -> None:
    loop = _loop(tmp_path)

    await loop.process_message(
        _msg("first"),
        session_key="cli:review",
        turn_id="t1",
        target_root=tmp_path,
        handoff=_handoff(),
    )
    await loop.process_message(
        _msg("second"),
        session_key="cli:review",
        turn_id="t2",
        target_root=tmp_path,
    )

    assert len(_handoff_messages(loop, "cli:review")) == 1


@pytest.mark.asyncio
async def test_a_reloaded_session_does_not_repeat_the_injection(tmp_path) -> None:
    """The consumed marker is on disk, so a restart cannot re-inject."""
    loop = _loop(tmp_path)

    await loop.process_message(
        _msg("first"),
        session_key="cli:review",
        turn_id="t1",
        target_root=tmp_path,
        handoff=_handoff(),
    )

    reloaded = SessionCoordinator(MessageBus(), DummyProvider(), tmp_path)
    reloaded_loop = reloaded.conversation_loop
    session = reloaded.sessions.get_or_create("cli:review")
    assert reloaded.pending_handoff(session) is None
    assert len(_handoff_messages(reloaded_loop, "cli:review")) == 1


@pytest.mark.asyncio
async def test_the_persisted_summary_reaches_the_model_after_a_restart(
    tmp_path,
) -> None:
    """Consolidation hides old turns; the summary must carry them back in.

    Written to disk by one process and replayed by the next, it has to land in
    the system prompt. Otherwise the replay window keeps hiding those turns —
    losing earlier decisions and open work — while the token probe still pays
    for a summary the model never receives.
    """
    summary = "PERSISTED-SUMMARY-MARKER: earlier decisions and open work"

    first = _loop(tmp_path)
    session = first._sessions.get_or_create("cli:direct")
    session.add_message("user", "old question")
    session.add_message("assistant", "old answer")
    session.metadata["_last_summary"] = {
        "text": summary,
        "last_active": session.updated_at.isoformat(),
    }
    first._sessions.save(session)

    # Process restart: a fresh coordinator only sees the JSONL on disk.
    loop = _loop(tmp_path)
    runner = SpecCapturingRunner()
    loop._runner = runner

    await loop.process_message(
        _msg("new question"),
        session_key="cli:direct",
        turn_id="t1",
        target_root=tmp_path,
    )

    system = runner.specs[0].frozen_messages[0]
    assert system["role"] == "system"
    assert summary in system["content"]


@pytest.mark.asyncio
async def test_a_session_without_a_summary_injects_none(tmp_path) -> None:
    loop = _loop(tmp_path)
    runner = SpecCapturingRunner()
    loop._runner = runner

    await loop.process_message(
        _msg("hello"),
        session_key="cli:direct",
        turn_id="t1",
        target_root=tmp_path,
    )

    system = runner.specs[0].frozen_messages[0]
    assert "[Archived Context Summary]" not in system["content"]


@pytest.mark.asyncio
async def test_history_is_consolidated_before_the_handoff_is_written(
    tmp_path,
) -> None:
    order: list[str] = []
    loop = _loop(tmp_path)
    original_consolidate = loop._consolidator.maybe_consolidate_by_tokens

    async def _traced_consolidate(*args, **kwargs):
        order.append("consolidate")
        return await original_consolidate(*args, **kwargs)

    loop._consolidator.maybe_consolidate_by_tokens = _traced_consolidate  # type: ignore[method-assign]
    loop._handoff_consumer = lambda session, handoff: order.append("handoff")  # type: ignore[assignment]

    await loop.process_message(
        _msg("hello"),
        session_key="cli:review",
        turn_id="t1",
        target_root=tmp_path,
        handoff=_handoff(),
    )

    assert order == ["consolidate", "handoff"]


@pytest.mark.asyncio
async def test_a_failed_handoff_write_stops_the_turn_before_the_runner(
    tmp_path,
) -> None:
    loop = _loop(tmp_path)
    runner = SpecCapturingRunner()
    loop._runner = runner

    def _failing_consumer(session, handoff) -> None:
        raise OSError("disk full (injected)")

    loop._handoff_consumer = _failing_consumer

    with pytest.raises(OSError, match="disk full"):
        await loop.process_message(
            _msg("hello"),
            session_key="cli:review",
            turn_id="t1",
            target_root=tmp_path,
            handoff=_handoff(),
        )

    assert runner.specs == []


@pytest.mark.asyncio
async def test_queued_messages_are_injected_and_saved(tmp_path) -> None:
    """A message queued mid-turn enters the run and the saved history."""
    loop = _loop(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="also check the tests",
            metadata={"message_id": "m-2"},
        )
    )
    runner = InjectingRunner()
    loop._runner = runner

    await loop.process_message(
        _msg("first"),
        session_key="cli:direct",
        turn_id="t1",
        target_root=tmp_path,
        pending_queue=queue,
    )

    # The drain handed the queued message to the run, metadata included.
    assert runner.injected == [
        {"role": "user", "content": "also check the tests", "_metadata": {"message_id": "m-2"}}
    ]
    session = loop._sessions.get_or_create("cli:direct")
    assert [m.get("role") for m in session.messages] == [
        "user",
        "user",
        "assistant",
    ]
    assert "also check the tests" in session.messages[1]["content"]


@pytest.mark.asyncio
async def test_the_drain_respects_the_injection_cap(tmp_path) -> None:
    loop = _loop(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    for index in range(_MAX_INJECTIONS_PER_TURN + 3):
        queue.put_nowait(_msg(f"queued {index}"))
    runner = InjectingRunner()
    loop._runner = runner

    await loop.process_message(
        _msg("first"),
        session_key="cli:direct",
        turn_id="t1",
        target_root=tmp_path,
        pending_queue=queue,
    )

    # The cap still holds and the surplus stays queued for a later turn.
    assert len(runner.injected) == _MAX_INJECTIONS_PER_TURN
    assert queue.qsize() == 3


@pytest.mark.asyncio
async def test_a_restricted_turn_scope_survives_the_pre_tool_context_refresh(
    tmp_path,
) -> None:
    """A ``restricted`` turn must not widen once tools start running.

    Regression: the progress hook rebuilds the request context before every
    tool batch and only knows channel/chat/metadata. Rebuilding without the
    turn scope reset it to ``None``, so later filesystem/shell calls fell back
    to the construction-time ``restrict_to_workspace`` (``full`` by default) and
    could reach outside the project root.
    """
    scope_root = tmp_path / "repo"
    scope_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET", encoding="utf-8")

    loop = _loop(tmp_path)
    runner = ContextRefreshRunner()
    loop._runner = runner

    await loop.process_message(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="hello",
            metadata={
                WORKSPACE_SCOPE_KEY: {
                    "project_path": str(scope_root),
                    "access_mode": "restricted",
                }
            },
        ),
        session_key="cli:direct",
        turn_id="t1",
        target_root=tmp_path,
    )

    assert len(runner.observed_scopes) == 2
    for observed in runner.observed_scopes:
        assert observed is not None
        assert observed.access_mode == ACCESS_RESTRICTED
        assert observed.project_path == scope_root.resolve()

    # And the boundary is enforced, not merely recorded.
    read_tool = runner.specs[0].tools.get("read_file")
    result = await read_tool.execute(path=str(outside))
    assert "SECRET" not in str(result)
