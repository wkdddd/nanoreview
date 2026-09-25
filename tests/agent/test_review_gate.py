"""Regression tests for the review session message gate.

The gate protects an invariant that must survive every supervisor migration:
once a review session reaches a terminal state, ordinary follow-up messages are
rejected *and* leave no trace in the session. The current implementation gets
that property from the gate returning ``shortcut`` in ``_state_command()``
before command dispatch and persistence run, which is easy to break silently —
nothing else asserts it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.loop import AgentLoop
from nanoreview.agent.review_state import (
    JudgeBatchState,
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
)
from nanoreview.bus.events import InboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review.types import ReviewMetaKey
from nanoreview.session.manager import SessionManager

REVIEW_SESSION_KEY = "cli:gate-session"
GATE_MARKERS = ("review session is complete", "review is already running", "ended with status")


class _DummyProvider(LLMProvider):
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
        _ = (
            messages,
            tools,
            model,
            max_tokens,
            temperature,
            reasoning_effort,
            tool_choice,
            response_format,
        )
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


def _review_session_loop(tmp_path: Path, *, status: str) -> AgentLoop:
    """Build a loop whose persisted session carries a review status."""
    loop = AgentLoop(MessageBus(), _DummyProvider(), tmp_path)
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    session.add_message("user", "review this repo")
    session.add_message("assistant", "## Code Review Report")
    session.metadata[ReviewMetaKey.RUN_ID] = "run-000111222333"
    session.metadata[ReviewMetaKey.STATUS] = status
    session.metadata[ReviewMetaKey.PHASE] = "done"
    loop.sessions.save(session)
    return loop


def _ordinary_message() -> InboundMessage:
    return InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="gate-session",
        content="one more follow-up question",
        session_key_override=REVIEW_SESSION_KEY,
    )


async def _drain_outbound(loop: AgentLoop) -> list[Any]:
    events = []
    while loop.bus.outbound_size:
        events.append(await loop.bus.consume_outbound())
    return events


@pytest.mark.asyncio
async def test_terminal_review_session_rejects_follow_up_without_persisting(
    tmp_path,
) -> None:
    """Gate response is returned, and neither memory nor disk grows."""
    loop = _review_session_loop(tmp_path, status="completed")
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    messages_before = len(session.messages)

    await loop._dispatch(_ordinary_message())

    events = await _drain_outbound(loop)
    contents = [event.content.lower() for event in events]
    assert any(
        marker in content for content in contents for marker in GATE_MARKERS
    ), contents

    assert len(session.messages) == messages_before

    reloaded = SessionManager(tmp_path).get_or_create(REVIEW_SESSION_KEY)
    assert len(reloaded.messages) == messages_before
    assert reloaded.metadata[ReviewMetaKey.STATUS] == "completed"


@pytest.mark.asyncio
async def test_running_metadata_without_live_run_is_gated(tmp_path) -> None:
    """A restarted process has no in-memory run; metadata alone must gate."""
    loop = _review_session_loop(tmp_path, status="running")
    assert loop._review_runs.get(REVIEW_SESSION_KEY) is None
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    messages_before = len(session.messages)

    await loop._dispatch(_ordinary_message())

    events = await _drain_outbound(loop)
    contents = [event.content.lower() for event in events]
    assert any(
        marker in content for content in contents for marker in GATE_MARKERS
    ), contents
    assert len(session.messages) == messages_before


def test_metadata_gate_leaves_non_review_and_internal_events_alone(tmp_path) -> None:
    """The gate must stay conditional, or commands and subagent results break."""
    loop = _review_session_loop(tmp_path, status="completed")
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    msg = _ordinary_message()

    assert loop._review_metadata_gate(session, msg) is not None

    internal = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="gate-session",
        content="reviewer result",
        session_key_override=REVIEW_SESSION_KEY,
        metadata={"injected_event": "subagent_result"},
    )
    assert loop._review_metadata_gate(session, internal) is None

    plain_session = loop.sessions.get_or_create("cli:no-review")
    assert loop._review_metadata_gate(plain_session, msg) is None

    session.metadata[ReviewMetaKey.STATUS] = "not-a-status"
    assert loop._review_metadata_gate(session, msg) is None


def _live_run(loop: AgentLoop) -> ReviewRunState:
    state = ReviewRunState(
        run_id="run-cancel",
        session_key=REVIEW_SESSION_KEY,
        input_fingerprint="fp",
        status=ReviewRunStatus.RUNNING,
        phase=ReviewPhase.REVIEW,
    )
    # A finished reviewer keeps its terminal state; work still in flight must not.
    done_reviewer = state.reviewer_state("security")
    done_reviewer.status = "completed"
    state.reviewer_state("bug").status = "running"
    batch = state.judge_batches.setdefault("judge", JudgeBatchState(batch_id="judge"))
    batch.status = "running"
    loop._review_runs[REVIEW_SESSION_KEY] = state
    return state


def test_stopped_run_marks_inflight_reviewer_and_judge_stopped(tmp_path) -> None:
    """/stop finalizes the run; unfinished work is ``stopped``, never completed."""
    loop = AgentLoop(MessageBus(), _DummyProvider(), tmp_path)
    state = _live_run(loop)

    loop._finalize_review_run(REVIEW_SESSION_KEY, ReviewRunStatus.STOPPED)

    assert state.status is ReviewRunStatus.STOPPED
    assert state.phase is ReviewPhase.DONE
    assert state.reviewers["security"].status == "completed"
    assert state.reviewers["security"].error == ""
    assert state.reviewers["bug"].status == "stopped"
    assert state.reviewers["bug"].error
    assert state.judge_batches["judge"].status == "stopped"
    # The terminal metadata is persisted for the session gate.
    persisted = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    assert persisted.metadata[ReviewMetaKey.STATUS] == "stopped"


def test_error_run_marks_inflight_reviewer_and_judge_error(tmp_path) -> None:
    """A failed finalize (e.g. artifact write) records in-flight work as error."""
    loop = AgentLoop(MessageBus(), _DummyProvider(), tmp_path)
    state = _live_run(loop)

    loop._finalize_review_run(
        REVIEW_SESSION_KEY,
        ReviewRunStatus.ERROR,
        warning="Review turn failed with an unexpected error.",
    )

    assert state.status is ReviewRunStatus.ERROR
    assert state.reviewers["security"].status == "completed"
    assert state.reviewers["bug"].status == "error"
    assert state.judge_batches["judge"].status == "error"
    assert state.judge_batches["judge"].error
