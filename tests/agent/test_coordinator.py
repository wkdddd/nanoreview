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
from typing import Any

import pytest

from nanoreview.agent.review_loop import ReviewLoop
from nanoreview.agent.review_state import (
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
    build_report_artifact,
)
from nanoreview.bus.events import InboundMessage
from nanoreview.review.result import ReviewHandoffState
from nanoreview.review.types import ReviewMetaKey
from nanoreview.agent.coordinator import (
    INTERRUPTED_RUN_REASON,
    REVIEW_CONTEXT_EVENT,
    REVIEW_HANDOFF_EVENT,
    SessionCoordinator,
    SessionRoute,
)
from nanoreview.bus.queue import MessageBus
from nanoreview.providers.base import LLMProvider, LLMResponse
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


def _register(env: _Env, state: ReviewRunState, *, persist: bool = True) -> ReviewRunState:
    env.review_loop.runs[state.session_key] = state
    session = env.sessions.get_or_create(state.session_key)
    session.metadata.update(state.metadata_payload())
    if persist:
        env.sessions.save(session)
    return state


def _live_run(
    env: _Env,
    *,
    phase: ReviewPhase = ReviewPhase.REVIEW,
    status: ReviewRunStatus = ReviewRunStatus.RUNNING,
) -> ReviewRunState:
    return _register(
        env,
        ReviewRunState(
            run_id="run-a",
            session_key=SESSION_KEY,
            input_fingerprint="fp",
            status=status,
            phase=phase,
        ),
    )


def _persist_report(env: _Env, state: ReviewRunState) -> str:
    reference = env.review_loop.artifacts.write(
        build_report_artifact(
            state,
            report_markdown=REPORT_MARKDOWN,
            status=state.status,
        )
    )
    assert reference is not None
    state.report_ref = reference
    session = env.sessions.get_or_create(state.session_key)
    session.metadata[ReviewMetaKey.REPORT_REF] = reference
    env.sessions.save(session)
    return reference


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


def test_a_live_running_run_keeps_the_session_in_review(env: _Env) -> None:
    _live_run(env)
    session = env.sessions.get_or_create(SESSION_KEY)

    assert env.coordinator.route(session) is SessionRoute.REVIEW


def test_a_terminal_run_opens_the_conversation(env: _Env) -> None:
    state = _live_run(env, status=ReviewRunStatus.COMPLETED, phase=ReviewPhase.DONE)
    _register(env, state)
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
    assert env.review_loop.get(SESSION_KEY) is None

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
    assert env.review_loop.get(SESSION_KEY) is None

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
    assert env.review_loop.get(SESSION_KEY) is None

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


def test_a_live_run_is_never_mistaken_for_an_orphan(env: _Env) -> None:
    """The live run is authoritative; stale ``running`` metadata is not repair.

    Normalizing here would corrupt a perfectly healthy run: the persisted
    status is mid-flight, not abandoned.
    """
    _live_run(env)
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


def _terminal_completed(env: _Env) -> ReviewRunState:
    state = _live_run(env, status=ReviewRunStatus.COMPLETED, phase=ReviewPhase.DONE)
    _persist_report(env, state)
    _register(env, state)
    return state


def test_a_complete_handoff_injects_the_report_verbatim(env: _Env) -> None:
    _terminal_completed(env)
    session = env.sessions.get_or_create(SESSION_KEY)

    handoff = env.coordinator.pending_handoff(session)

    assert handoff is not None
    assert handoff.result.handoff is ReviewHandoffState.COMPLETE
    assert handoff.fits is True
    assert REPORT_MARKDOWN in handoff.block
    assert handoff.result.run_id in handoff.directive


def test_a_run_with_gaps_hands_over_a_partial_report(env: _Env) -> None:
    state = _terminal_completed(env)
    state.reviewer_state("performance").status = "error"
    state.reviewer_state("performance").error = "provider timeout"
    session = env.sessions.get_or_create(SESSION_KEY)

    handoff = env.coordinator.pending_handoff(session)

    assert handoff is not None
    assert handoff.result.handoff is ReviewHandoffState.PARTIAL
    assert "Coverage gaps" in handoff.block
    assert "provider timeout" in handoff.block
    assert REPORT_MARKDOWN in handoff.block


def test_a_missing_artifact_hands_over_an_explicit_failure(env: _Env) -> None:
    state = _live_run(env, status=ReviewRunStatus.COMPLETED, phase=ReviewPhase.DONE)
    state.report_ref = "review-artifacts/run-a.json"  # never written
    _register(env, state)
    session = env.sessions.get_or_create(SESSION_KEY)

    handoff = env.coordinator.pending_handoff(session)

    assert handoff is not None
    assert handoff.result.handoff is ReviewHandoffState.FAILED
    assert "no review report artifact is available" in handoff.block
    assert REPORT_MARKDOWN not in handoff.block


def test_the_handoff_is_consumed_exactly_once(env: _Env) -> None:
    _terminal_completed(env)
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


def test_an_oversized_report_rejects_the_first_turn(env: _Env) -> None:
    state = _live_run(env, status=ReviewRunStatus.COMPLETED, phase=ReviewPhase.DONE)
    env.review_loop.artifacts.write(
        build_report_artifact(
            state,
            report_markdown="line of report\n" * 20_000,
            status=ReviewRunStatus.COMPLETED,
        )
    )
    state.report_ref = env.review_loop.artifacts.reference_for(state.run_id)
    _register(env, state)
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


def test_an_unknown_context_window_never_blocks_a_handoff(env: _Env) -> None:
    _terminal_completed(env)
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


def test_context_index_is_written_once_and_stays_compact(env: _Env) -> None:
    _terminal_completed(env)
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
    assert index[0]["review_report_ref"] == env.review_loop.artifacts.reference_for(
        "run-a"
    )
    assert REPORT_MARKDOWN not in index[0]["content"]

    reloaded = SessionManager(env.workspace).get_or_create(SESSION_KEY)
    assert env.coordinator.write_context_index(reloaded) is False


@pytest.mark.asyncio
async def test_finalize_settles_the_run_and_indexes_the_result(env: _Env) -> None:
    state = _live_run(env)
    state.reviewer_state("security").status = "running"

    result = await env.coordinator.finalize(SESSION_KEY, ReviewRunStatus.STOPPED)

    assert result is not None
    assert result.status is ReviewRunStatus.STOPPED
    assert result.handoff is ReviewHandoffState.FAILED
    assert state.reviewers["security"].status == "stopped"
    assert env.review_loop.running(SESSION_KEY) is None

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
    _live_run(env, phase=ReviewPhase.PREPARE)

    result = await env.coordinator.finalize(SESSION_KEY, ReviewRunStatus.ERROR)

    assert result is not None
    assert result.status is ReviewRunStatus.ERROR
    assert result.error
    session = env.sessions.get_or_create(SESSION_KEY)
    assert env.review_loop.running(SESSION_KEY) is None
    assert env.review_loop.get(SESSION_KEY) is not None
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
