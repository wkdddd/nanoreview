"""SessionCoordinator contract tests: routing, handoff, and index persistence.

The coordinator is the only place that decides which agent phase owns a
session. These tests pin the decisions the rest of the system relies on:

* a *live* running run keeps the session in review, and its persisted metadata
  is never mistaken for an orphaned run (which would corrupt a healthy run);
* with no live executor, a persisted ``running`` status is repaired once so a
  restarted process never leaves its session gated forever;
* a terminal review opens the conversation with one handoff whose state
  (``complete`` / ``partial`` / ``failed``) never overstates what exists;
* the report artifact stays authoritative — the coordinator only writes a
  compact, replayable index entry and never rewrites the run or its report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanoreview.agent.coordinator import (
    INTERRUPTED_RUN_REASON,
    REVIEW_CONTEXT_EVENT,
    REVIEW_HANDOFF_EVENT,
    SessionCoordinator,
    SessionRoute,
)
from nanoreview.agent.review_board import (
    CHILD_ERROR,
    ReviewBoardView,
    ReviewBoardWriter,
)
from nanoreview.agent.review_loop import ReviewLoop
from nanoreview.agent.review_state import ReviewPhase, ReviewRunStatus
from nanoreview.agent.runner import AgentRunResult, AgentRunSpec
from nanoreview.bus.events import InboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review.result import ReviewHandoffState
from nanoreview.review.types import ReviewAssignment, ReviewMetaKey
from nanoreview.session.manager import SessionManager

SESSION_KEY = "cli:review"
REPORT_MARKDOWN = "## Code Review Report\n\nNo actionable issues found."


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
        _ = (messages, tools, model, max_tokens, temperature, reasoning_effort)
        _ = (tool_choice, response_format)
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


@dataclass
class _Env:
    sessions: SessionManager
    review_loop: ReviewLoop
    coordinator: SessionCoordinator
    workspace: Path


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    sessions = SessionManager(tmp_path)
    coordinator = SessionCoordinator(
        MessageBus(),
        _DummyProvider(),
        tmp_path,
        context_window_tokens=200_000,
        reserved_output_tokens=4096,
        session_manager=sessions,
    )
    return _Env(
        sessions=sessions,
        review_loop=coordinator.review_loop,
        coordinator=coordinator,
        workspace=tmp_path,
    )


SNAPSHOT_REF = "review-snapshots/run-a.json"


def _create(env: _Env, *, run_id: str = "run-a") -> ReviewBoardWriter:
    """Register one run's blackboard through the coordinator's store."""
    env.coordinator._review_boards.create(  # noqa: SLF001 - store is the writer
        SimpleNamespace(
            run_id=run_id,
            session_key=SESSION_KEY,
            input_fingerprint="fp",
            snapshot_ref=SNAPSHOT_REF,
            plan=None,
        )
    )
    writer = env.coordinator._review_boards.writer(SESSION_KEY)  # noqa: SLF001
    assert writer is not None
    return writer


def _board(env: _Env) -> ReviewBoardView:
    view = env.coordinator._review_boards.view(SESSION_KEY)  # noqa: SLF001
    assert view is not None
    return view


async def _live_run(
    env: _Env, *, phase: ReviewPhase = ReviewPhase.REVIEW
) -> ReviewBoardWriter:
    """A running run at *phase* whose metadata is already persisted."""
    writer = _create(env)
    if phase is not ReviewPhase.PREPARE:
        await writer.advance(phase)
    await writer.persist_metadata()
    return writer


async def _terminal_completed(env: _Env) -> ReviewBoardWriter:
    """A completed run with its report artifact persisted."""
    writer = _create(env)
    await writer.advance(ReviewPhase.CLEANUP)
    await writer.record_report(
        report_markdown=REPORT_MARKDOWN, findings=(), verdicts=[]
    )
    await writer.settle(ReviewRunStatus.COMPLETED)
    return writer


def _message() -> InboundMessage:
    return InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="review",
        content="follow-up",
        session_key_override=SESSION_KEY,
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def test_a_live_running_run_keeps_the_session_in_review(env: _Env) -> None:
    await _live_run(env)
    session = env.sessions.get_or_create(SESSION_KEY)

    assert env.coordinator.route(session) is SessionRoute.REVIEW


async def test_a_terminal_run_opens_the_conversation(env: _Env) -> None:
    writer = _create(env)
    await writer.settle(ReviewRunStatus.COMPLETED)
    session = env.sessions.get_or_create(SESSION_KEY)

    assert env.coordinator.route(session) is SessionRoute.CONVERSATION


def test_an_orphaned_running_run_is_repaired_once_and_reopens_the_session(
    env: _Env,
) -> None:
    """A restarted process has no executor, so the run must not stay gated."""
    session = env.sessions.get_or_create(SESSION_KEY)
    session.metadata[ReviewMetaKey.RUN_ID] = "run-a"
    session.metadata[ReviewMetaKey.STATUS] = "running"
    session.metadata[ReviewMetaKey.PHASE] = "review"
    env.sessions.save(session)
    assert env.coordinator._review_boards.view(SESSION_KEY) is None  # noqa: SLF001

    # The repairing read returns the precise cause...
    result = env.coordinator.result(session)
    assert result is not None
    assert result.handoff is ReviewHandoffState.FAILED
    assert result.error == INTERRUPTED_RUN_REASON
    assert session.metadata[ReviewMetaKey.STATUS] == "error"
    assert session.metadata[ReviewMetaKey.PHASE] == "done"
    # ...and the reason is persisted with the repair, not only in memory.
    assert session.metadata[ReviewMetaKey.SUMMARY] == INTERRUPTED_RUN_REASON
    assert session.metadata[ReviewMetaKey.ERROR] == INTERRUPTED_RUN_REASON

    assert env.coordinator.route(session) is SessionRoute.CONVERSATION

    # ...and the repair is persisted, so a fresh process reads a terminal run
    # that still explains why it failed.
    reloaded = SessionManager(env.workspace).get_or_create(SESSION_KEY)
    assert reloaded.metadata[ReviewMetaKey.STATUS] == "error"
    settled = env.coordinator.result(reloaded)
    assert settled is not None
    assert settled.is_terminal is True
    assert settled.handoff is ReviewHandoffState.FAILED
    assert settled.error == INTERRUPTED_RUN_REASON


def test_pending_handoff_repairs_an_orphan_without_a_manual_result_call(
    env: _Env,
) -> None:
    """The real handoff entry repairs an orphan, not just ``result()``.

    After a restart the persisted run is still ``running`` with no live
    executor. ``pending_handoff`` must normalize that orphan itself (it calls
    ``result()`` before the settlement check), so the first conversation turn
    injects the failure context instead of silently dropping the handoff while
    the metadata stays ``running`` forever.
    """
    session = env.sessions.get_or_create(SESSION_KEY)
    session.metadata[ReviewMetaKey.RUN_ID] = "run-a"
    session.metadata[ReviewMetaKey.STATUS] = "running"
    session.metadata[ReviewMetaKey.PHASE] = "cleanup"
    env.sessions.save(session)
    assert env.coordinator._review_boards.view(SESSION_KEY) is None  # noqa: SLF001

    handoff = env.coordinator.pending_handoff(session)

    assert handoff is not None
    assert handoff.result.handoff is ReviewHandoffState.FAILED
    assert handoff.result.error == INTERRUPTED_RUN_REASON
    # The orphan is normalized to a terminal error in memory and on disk.
    assert session.metadata[ReviewMetaKey.STATUS] == "error"
    assert session.metadata[ReviewMetaKey.PHASE] == "done"


def test_a_failed_orphan_repair_does_not_publish_done_in_cache(env: _Env) -> None:
    """A failed repair save restores the metadata instead of running ahead.

    ``_normalize_interrupted_run`` must not leave the cache claiming
    ``error/done`` while disk still says ``running``: on a failed save the
    previous metadata is restored so the run stays ``running`` and a later
    read retries the repair.
    """
    session = env.sessions.get_or_create(SESSION_KEY)
    session.metadata[ReviewMetaKey.RUN_ID] = "run-a"
    session.metadata[ReviewMetaKey.STATUS] = "running"
    session.metadata[ReviewMetaKey.PHASE] = "review"
    env.sessions.save(session)
    assert env.coordinator._review_boards.view(SESSION_KEY) is None  # noqa: SLF001

    original_save = env.sessions.save

    def _flaky_save(sess: Any) -> None:
        raise OSError("disk full (injected)")

    env.sessions.save = _flaky_save  # type: ignore[method-assign]
    try:
        result = env.coordinator.result(session)
    finally:
        env.sessions.save = original_save  # type: ignore[method-assign]

    # The repair failed: the cache must still reflect the persisted ``running``
    # state, not a premature ``error/done``.
    assert result is not None
    assert result.status is ReviewRunStatus.RUNNING
    assert session.metadata[ReviewMetaKey.STATUS] == "running"
    assert session.metadata[ReviewMetaKey.PHASE] == "review"
    # Once persistence works again, a later read repairs the orphan.
    assert env.coordinator.result(session).status is ReviewRunStatus.ERROR


async def test_a_live_run_is_never_mistaken_for_an_orphan(env: _Env) -> None:
    """The live run is authoritative; stale ``running`` metadata is not repair.

    Normalizing here would corrupt a perfectly healthy run: the persisted
    status is mid-flight, not abandoned.
    """
    await _live_run(env)
    session = env.sessions.get_or_create(SESSION_KEY)

    assert env.coordinator.result(session) is None
    assert env.coordinator.pending_handoff(session) is None
    assert env.coordinator.write_context_index(session) is False
    assert session.metadata[ReviewMetaKey.STATUS] == "running"
    assert session.messages == []


def test_a_plain_session_has_no_review_route_or_result(env: _Env) -> None:
    session = env.sessions.get_or_create("cli:plain")

    assert env.coordinator.route(session) is SessionRoute.CONVERSATION
    assert env.coordinator.result(session) is None
    assert env.coordinator.pending_handoff(session) is None


# ---------------------------------------------------------------------------
# Handoff: three states
# ---------------------------------------------------------------------------


async def test_a_complete_handoff_injects_the_report_verbatim(env: _Env) -> None:
    await _terminal_completed(env)
    session = env.sessions.get_or_create(SESSION_KEY)

    handoff = env.coordinator.pending_handoff(session)

    assert handoff is not None
    assert handoff.result.handoff is ReviewHandoffState.COMPLETE
    assert handoff.fits is True
    assert REPORT_MARKDOWN in handoff.block
    assert handoff.result.run_id in handoff.directive


async def test_a_run_with_gaps_hands_over_a_partial_report(env: _Env) -> None:
    writer = _create(env)
    await writer.advance(ReviewPhase.CLEANUP)
    await writer.start_assignments((ReviewAssignment(dimension="performance", focus="x"),))
    await writer.reviewer_settled(
        "performance", status=CHILD_ERROR, error="provider timeout"
    )
    await writer.record_report(
        report_markdown=REPORT_MARKDOWN, findings=(), verdicts=[]
    )
    await writer.settle(ReviewRunStatus.COMPLETED)
    session = env.sessions.get_or_create(SESSION_KEY)

    handoff = env.coordinator.pending_handoff(session)

    assert handoff is not None
    assert handoff.result.handoff is ReviewHandoffState.PARTIAL
    assert "Coverage gaps" in handoff.block
    assert "provider timeout" in handoff.block
    assert REPORT_MARKDOWN in handoff.block


async def test_a_missing_artifact_hands_over_an_explicit_failure(env: _Env) -> None:
    await _terminal_completed(env)
    # The artifact was written, then lost (a pruned workspace, a failed disk).
    env.coordinator._review_artifacts.path_for("run-a").unlink()  # noqa: SLF001
    session = env.sessions.get_or_create(SESSION_KEY)

    handoff = env.coordinator.pending_handoff(session)

    assert handoff is not None
    assert handoff.result.handoff is ReviewHandoffState.FAILED
    assert "no review report artifact is available" in handoff.block
    assert REPORT_MARKDOWN not in handoff.block


async def test_the_handoff_is_consumed_exactly_once(env: _Env) -> None:
    await _terminal_completed(env)
    session = env.sessions.get_or_create(SESSION_KEY)
    handoff = env.coordinator.pending_handoff(session)
    assert handoff is not None

    env.coordinator.consume_handoff(session, handoff)

    injected = [
        message
        for message in session.messages
        if message.get("injected_event") == REVIEW_HANDOFF_EVENT
    ]
    assert len(injected) == 1
    assert injected[0]["review_run_id"] == "run-a"
    assert session.metadata[ReviewMetaKey.HANDOFF_RUN_ID] == "run-a"
    # The report artifact is referenced, never copied into session metadata.
    assert REPORT_MARKDOWN not in str(session.metadata)

    assert env.coordinator.pending_handoff(session) is None


# ---------------------------------------------------------------------------
# Oversized report
# ---------------------------------------------------------------------------


async def test_an_oversized_report_rejects_the_first_turn(env: _Env) -> None:
    writer = _create(env)
    await writer.advance(ReviewPhase.CLEANUP)
    await writer.record_report(
        report_markdown="line of report\n" * 20_000, findings=(), verdicts=[]
    )
    await writer.settle(ReviewRunStatus.COMPLETED)
    session = env.sessions.get_or_create(SESSION_KEY)
    coordinator = SessionCoordinator(
        MessageBus(),
        _DummyProvider(),
        env.workspace,
        context_window_tokens=8_192,
        reserved_output_tokens=4_096,
        session_manager=env.sessions,
    )

    handoff = coordinator.pending_handoff(session)

    assert handoff is not None
    assert handoff.fits is False

    response = coordinator.report_too_large_response(_message(), handoff)
    gate = response.metadata["review_gate"]
    assert gate["code"] == "review_report_too_large"
    assert gate["accepted"] is False
    assert gate["run_id"] == "run-a"
    # The complete report is never replaced by a summary.
    assert REPORT_MARKDOWN not in response.content


async def test_an_unknown_context_window_never_blocks_a_handoff(env: _Env) -> None:
    await _terminal_completed(env)
    session = env.sessions.get_or_create(SESSION_KEY)
    coordinator = SessionCoordinator(
        MessageBus(),
        _DummyProvider(),
        env.workspace,
        context_window_tokens=0,
        session_manager=env.sessions,
    )

    handoff = coordinator.pending_handoff(session)

    assert handoff is not None
    assert handoff.fits is True


# ---------------------------------------------------------------------------
# Index + terminal persistence
# ---------------------------------------------------------------------------


async def test_context_index_is_written_once_and_stays_compact(env: _Env) -> None:
    await _terminal_completed(env)
    session = env.sessions.get_or_create(SESSION_KEY)

    assert env.coordinator.write_context_index(session) is True
    assert env.coordinator.write_context_index(session) is False

    index = [
        message
        for message in session.messages
        if message.get("injected_event") == REVIEW_CONTEXT_EVENT
    ]
    assert len(index) == 1
    assert index[0]["review_run_id"] == "run-a"
    assert index[0]["review_report_ref"] == (
        env.coordinator._review_artifacts.reference_for("run-a")  # noqa: SLF001
    )
    assert REPORT_MARKDOWN not in index[0]["content"]

    reloaded = SessionManager(env.workspace).get_or_create(SESSION_KEY)
    assert env.coordinator.write_context_index(reloaded) is False


@pytest.mark.asyncio
async def test_finalize_settles_the_run_and_indexes_the_result(env: _Env) -> None:
    writer = _create(env)
    await writer.advance(ReviewPhase.REVIEW)
    await writer.start_assignments((ReviewAssignment(dimension="security", focus="x"),))
    await writer.reviewer_started("security", evidence_assigned=1)

    result = await env.coordinator.finalize(SESSION_KEY, ReviewRunStatus.STOPPED)

    assert result is not None
    assert result.status is ReviewRunStatus.STOPPED
    assert result.handoff is ReviewHandoffState.FAILED
    assert _board(env).reviewer("security").status == "stopped"
    assert env.coordinator._review_boards.running(SESSION_KEY) is None  # noqa: SLF001

    session = env.sessions.get_or_create(SESSION_KEY)
    assert session.metadata[ReviewMetaKey.STATUS] == "stopped"
    assert env.coordinator.route(session) is SessionRoute.CONVERSATION
    assert any(
        message.get("injected_event") == REVIEW_CONTEXT_EVENT
        for message in session.messages
    )


@pytest.mark.asyncio
async def test_finalize_settles_a_run_that_never_started(env: _Env) -> None:
    """A run cancelled in PREPARE is settled with a reason, never dropped.

    Admission already persisted ``running`` for this turn, so dropping the live
    run would leave a claim no process can explain: the run is closed as the
    requested terminal status with a bounded reason and the index is written.
    """
    _create(env)

    result = await env.coordinator.finalize(SESSION_KEY, ReviewRunStatus.ERROR)

    assert result is not None
    assert result.status is ReviewRunStatus.ERROR
    assert result.error
    session = env.sessions.get_or_create(SESSION_KEY)
    assert env.coordinator._review_boards.running(SESSION_KEY) is None  # noqa: SLF001
    assert env.coordinator._review_boards.view(SESSION_KEY) is not None  # noqa: SLF001
    assert session.metadata[ReviewMetaKey.STATUS] == "error"
    assert session.metadata[ReviewMetaKey.PHASE] == "done"
    assert session.metadata[ReviewMetaKey.SUMMARY] == result.error
    assert session.metadata[ReviewMetaKey.ERROR] == result.error
    assert env.coordinator.route(session) is SessionRoute.CONVERSATION
    assert any(
        message.get("injected_event") == REVIEW_CONTEXT_EVENT
        for message in session.messages
    )

    # A fresh process reads the settled run and its reason from disk.
    reloaded = SessionManager(env.workspace).get_or_create(SESSION_KEY)
    assert reloaded.metadata[ReviewMetaKey.ERROR] == result.error


# ---------------------------------------------------------------------------
# Usage aggregation of returned results
# ---------------------------------------------------------------------------


class _UsageRunner:
    """Runner stub that returns a fixed usage payload for every run."""

    def __init__(self, usage: dict[str, int]) -> None:
        self.usage = usage
        self.runs = 0

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.runs += 1
        return AgentRunResult(
            final_content="ok",
            messages=list(spec.messages),
            usage=dict(self.usage),
        )


CONVERSATION_USAGE = {
    "prompt_tokens": 10,
    "completion_tokens": 4,
    "total_tokens": 14,
}


async def _direct(env: _Env, key: str) -> Any:
    return await env.coordinator.process_direct(
        "hi", session_key=key, channel="cli", chat_id=key
    )


@pytest.mark.asyncio
async def test_a_conversation_turns_usage_updates_last_and_total(env: _Env) -> None:
    env.coordinator.conversation_loop._runner = _UsageRunner(CONVERSATION_USAGE)

    await _direct(env, "cli:usage")

    assert env.coordinator._last_usage == CONVERSATION_USAGE
    assert env.coordinator._total_usage == CONVERSATION_USAGE


@pytest.mark.asyncio
async def test_usage_is_summed_once_per_returned_turn(env: _Env) -> None:
    """Two turns sum into the total; ``_last_usage`` is the latest turn's."""
    env.coordinator.conversation_loop._runner = _UsageRunner(
        {"prompt_tokens": 5, "total_tokens": 5}
    )

    await _direct(env, "cli:sum")
    await _direct(env, "cli:sum")

    assert env.coordinator._last_usage == {"prompt_tokens": 5, "total_tokens": 5}
    assert env.coordinator._total_usage["prompt_tokens"] == 10
    assert env.coordinator._total_usage["total_tokens"] == 10


@pytest.mark.asyncio
async def test_usage_is_recorded_even_when_no_reply_body_is_sent(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``None`` outbound (the reply went out through a tool) keeps its usage."""
    loop = env.coordinator.conversation_loop
    loop._runner = _UsageRunner(CONVERSATION_USAGE)
    monkeypatch.setattr(loop, "_assemble_outbound", lambda *a, **k: None)

    result = await _direct(env, "cli:nobody")

    assert result is None
    assert env.coordinator._last_usage == CONVERSATION_USAGE
    assert env.coordinator._total_usage == CONVERSATION_USAGE


@pytest.mark.asyncio
async def test_a_failed_turn_save_keeps_the_returned_usage(env: _Env) -> None:
    """Usage is recorded as the run returns, so a later save failure cannot
    wipe it — the turn's answer was already produced and paid for."""
    env.coordinator.conversation_loop._runner = _UsageRunner(CONVERSATION_USAGE)
    original_save = env.sessions.save
    calls = {"n": 0}

    def _flaky_save(session: Any) -> None:
        calls["n"] += 1
        if calls["n"] > 1:  # the early user-message write still succeeds
            raise OSError("disk full (injected)")
        original_save(session)

    env.sessions.save = _flaky_save  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError):
            await _direct(env, "cli:savefail")
    finally:
        env.sessions.save = original_save  # type: ignore[method-assign]

    assert env.coordinator._last_usage == CONVERSATION_USAGE
    assert env.coordinator._total_usage == CONVERSATION_USAGE


@pytest.mark.asyncio
async def test_a_review_runs_usage_is_recorded_once(env: _Env) -> None:
    """The run's single terminal result carries reviewers + judge + planner."""
    writer = await _live_run(env)
    await writer.add_usage(
        {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}
    )

    result = await env.coordinator.finalize(SESSION_KEY, ReviewRunStatus.STOPPED)

    assert result is not None
    assert env.coordinator._last_usage == {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}
    assert env.coordinator._total_usage["total_tokens"] == 40

    # The run is terminal: settling again yields nothing and adds nothing.
    assert await env.coordinator.finalize(SESSION_KEY, ReviewRunStatus.STOPPED) is None
    assert env.coordinator._total_usage["total_tokens"] == 40
