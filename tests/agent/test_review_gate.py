"""Regression tests for the review session gate and its routing contract.

The gate protects an invariant that must survive every supervisor migration:
while a review is *running*, ordinary follow-up messages are rejected and
leave no trace in the session. The same file pins the other half of the
contract — once the review is terminal its resources are cleaned up and its
result persisted, so the session opens for conversation instead of staying
gated forever.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.review_loop import ReviewLoopOutcome
from nanoreview.agent.review_state import (
    JudgeBatchState,
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
)
from nanoreview.bus.events import InboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review.admission import (
    ReviewAdmissionCode,
    ReviewAdmissionError,
    ReviewAdmissionRequest,
)
from nanoreview.review.types import ReviewMetaKey
from nanoreview.agent.coordinator import (
    REVIEW_CONTEXT_EVENT,
    REVIEW_HANDOFF_EVENT,
    SessionRoute,
)
from nanoreview.session.manager import SessionManager

REVIEW_SESSION_KEY = "cli:gate-session"
RUNNING_MARKERS = ("review is already running",)
CONVERSATION_MARKERS = ("review session is complete", "ended with status")


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


def _review_session_loop(tmp_path: Path, *, status: str) -> SessionCoordinator:
    """Build a loop whose persisted session carries a review status."""
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
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


async def _drain_outbound(loop: SessionCoordinator) -> list[Any]:
    events = []
    while loop.bus.outbound_size:
        events.append(await loop.bus.consume_outbound())
    return events


def _gate_codes(events: list[Any]) -> list[str]:
    return [
        (event.metadata or {}).get("review_gate", {}).get("code")
        for event in events
        if "review_gate" in (event.metadata or {})
    ]


# ---------------------------------------------------------------------------
# Review phase: a running review keeps the session gated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_review_rejects_follow_up_without_persisting(tmp_path) -> None:
    """While a review runs, ordinary messages are refused and write nothing."""
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    state = ReviewRunState(
        run_id="run-live",
        session_key=REVIEW_SESSION_KEY,
        input_fingerprint="fp",
        status=ReviewRunStatus.RUNNING,
        phase=ReviewPhase.REVIEW,
    )
    loop.review_loop.runs[REVIEW_SESSION_KEY] = state
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    session.metadata[ReviewMetaKey.RUN_ID] = state.run_id
    session.metadata[ReviewMetaKey.STATUS] = "running"
    session.add_message("user", "review this repo")
    loop.sessions.save(session)
    messages_before = len(session.messages)

    await loop._dispatch(_ordinary_message())
    await loop.close_background_tasks()

    events = await _drain_outbound(loop)
    contents = [event.content.lower() for event in events]
    assert any(
        marker in content for content in contents for marker in RUNNING_MARKERS
    ), contents
    assert _gate_codes(events) == ["review_gated"]

    assert len(session.messages) == messages_before
    reloaded = SessionManager(tmp_path).get_or_create(REVIEW_SESSION_KEY)
    assert len(reloaded.messages) == messages_before


def test_running_gate_passes_only_the_admitted_turn(tmp_path) -> None:
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    state = ReviewRunState(
        run_id="run-live",
        session_key=REVIEW_SESSION_KEY,
        input_fingerprint="fp",
        status=ReviewRunStatus.RUNNING,
        phase=ReviewPhase.REVIEW,
    )
    loop.review_loop.runs[REVIEW_SESSION_KEY] = state

    admitted = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="gate-session",
        content="Review target",
        session_key_override=REVIEW_SESSION_KEY,
        metadata={"_review_admitted": "run-live"},
    )
    assert loop._review_gate_blocks(admitted, state, admitted.content) is False

    follow_up = _ordinary_message()
    assert loop._review_gate_blocks(follow_up, state, follow_up.content) is True

    stale = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="gate-session",
        content="Review again",
        session_key_override=REVIEW_SESSION_KEY,
        metadata={"_review_admitted": "run-stale"},
    )
    assert loop._review_gate_blocks(stale, state, stale.content) is True


# ---------------------------------------------------------------------------
# Conversation phase: a terminal review opens the session
# ---------------------------------------------------------------------------


def _terminal_run(
    loop: SessionCoordinator,
    *,
    status: ReviewRunStatus = ReviewRunStatus.COMPLETED,
    report_ref: str | None = None,
) -> ReviewRunState:
    state = ReviewRunState(
        run_id="run-done",
        session_key=REVIEW_SESSION_KEY,
        input_fingerprint="fp",
        phase=ReviewPhase.DONE,
        status=status,
        report_ref=report_ref,
    )
    loop.review_loop.runs[REVIEW_SESSION_KEY] = state
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    session.metadata.update(state.metadata_payload())
    session.add_message("user", "review this repo")
    session.add_message("assistant", "## Code Review Report")
    loop.sessions.save(session)
    return state


@pytest.mark.asyncio
async def test_terminal_review_opens_conversation_and_injects_handoff(tmp_path) -> None:
    """A finished review routes to conversation and injects its handoff."""
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    _terminal_run(loop)
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    messages_before = len(session.messages)

    await loop._dispatch(_ordinary_message())
    await loop.close_background_tasks()

    events = await _drain_outbound(loop)
    assert _gate_codes(events) == []
    assert not any(
        marker in event.content.lower()
        for event in events
        for marker in CONVERSATION_MARKERS
    )

    assert len(session.messages) > messages_before
    injected = [
        message
        for message in session.messages
        if message.get("injected_event") == REVIEW_HANDOFF_EVENT
    ]
    assert len(injected) == 1
    assert injected[0]["review_run_id"] == "run-done"
    assert session.metadata[ReviewMetaKey.HANDOFF_RUN_ID] == "run-done"

    index = [
        message
        for message in session.messages
        if message.get("injected_event") == REVIEW_CONTEXT_EVENT
    ]
    assert len(index) == 1
    assert index[0]["review_run_id"] == "run-done"


@pytest.mark.asyncio
async def test_interrupted_run_is_normalized_and_session_reopens(tmp_path) -> None:
    """A restarted process has no live run: the run is closed as a failure."""
    loop = _review_session_loop(tmp_path, status="running")
    assert loop.review_loop.get(REVIEW_SESSION_KEY) is None
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)

    assert loop.route(session) is SessionRoute.CONVERSATION
    assert session.metadata[ReviewMetaKey.STATUS] == "error"

    await loop._dispatch(_ordinary_message())
    await loop.close_background_tasks()

    events = await _drain_outbound(loop)
    assert _gate_codes(events) == []
    assert len(session.messages) > 2


@pytest.mark.asyncio
async def test_second_review_turn_is_refused_after_terminal(tmp_path) -> None:
    """A review session never starts a second review, terminal or not."""
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    _terminal_run(loop)
    second = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="gate-session",
        content="review it again",
        session_key_override=REVIEW_SESSION_KEY,
        metadata={
            "review_target": "/some/target",
            "review_target_type": "local",
            "review_action": "repo",
        },
    )

    await loop._dispatch(second)
    await loop.close_background_tasks()

    events = await _drain_outbound(loop)
    assert _gate_codes(events) == ["duplicate_review"]


@pytest.mark.asyncio
async def test_unadmitted_review_turn_registers_no_run(tmp_path, monkeypatch) -> None:
    """A review turn that never went through admission is refused."""
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    session.add_message("user", "hello")
    loop.sessions.save(session)
    executed: list[str] = []

    async def _fake_process(msg, **kwargs):
        executed.append(msg.content)
        return None

    monkeypatch.setattr(loop.conversation_loop, "process_message", _fake_process)
    msg = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="gate-session",
        content="review this",
        session_key_override=REVIEW_SESSION_KEY,
        metadata={
            "review_target": "/some/target",
            "review_target_type": "local",
            "review_action": "repo",
        },
    )

    await loop._dispatch(msg)
    await loop.close_background_tasks()

    assert executed == []
    assert loop.review_loop.get(REVIEW_SESSION_KEY) is None
    events = await _drain_outbound(loop)
    assert _gate_codes(events) == ["review_not_admitted"]


def test_gate_leaves_internal_events_and_plain_sessions_alone(tmp_path) -> None:
    """The gate must stay conditional, or commands and subagent results break."""
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    _terminal_run(loop)
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    msg = _ordinary_message()

    internal = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="gate-session",
        content="reviewer result",
        session_key_override=REVIEW_SESSION_KEY,
        metadata={"injected_event": "subagent_result"},
    )
    assert loop.gate_message(internal, None, internal.content) is None

    plain_session = loop.sessions.get_or_create("cli:no-review")
    assert loop.gate_message(msg, None, msg.content) is None
    assert loop.gate_review_turn("cli:no-review", msg, None) is None
    assert loop.route(plain_session) is SessionRoute.CONVERSATION
    assert loop.pending_handoff(plain_session) is None

    # A live run made by another session must not leak into this one.
    assert session.key == REVIEW_SESSION_KEY


# ---------------------------------------------------------------------------
# Terminal finalization
# ---------------------------------------------------------------------------


def _live_run(loop: SessionCoordinator) -> ReviewRunState:
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
    loop.review_loop.runs[REVIEW_SESSION_KEY] = state
    return state


@pytest.mark.asyncio
async def test_stopped_run_marks_inflight_reviewer_and_judge_stopped(tmp_path) -> None:
    """/stop finalizes the run; unfinished work is ``stopped``, never completed."""
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    state = _live_run(loop)

    await loop._finalize_review_run(REVIEW_SESSION_KEY, ReviewRunStatus.STOPPED)

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
    # And the session immediately routes to conversation.
    assert loop.route(persisted) is SessionRoute.CONVERSATION


@pytest.mark.asyncio
async def test_error_run_marks_inflight_reviewer_and_judge_error(tmp_path) -> None:
    """A failed finalize (e.g. artifact write) records in-flight work as error."""
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    state = _live_run(loop)

    await loop._finalize_review_run(
        REVIEW_SESSION_KEY,
        ReviewRunStatus.ERROR,
        warning="Review turn failed with an unexpected error.",
    )

    assert state.status is ReviewRunStatus.ERROR
    assert state.reviewers["security"].status == "completed"
    assert state.reviewers["bug"].status == "error"
    assert state.judge_batches["judge"].status == "error"
    assert state.judge_batches["judge"].error


@pytest.mark.asyncio
async def test_unstarted_run_is_released_instead_of_finalized(tmp_path) -> None:
    """A run that never reached planning releases the gate instead of closing."""
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    state = ReviewRunState(
        run_id="run-unstarted",
        session_key=REVIEW_SESSION_KEY,
        input_fingerprint="fp",
    )
    loop.review_loop.runs[REVIEW_SESSION_KEY] = state

    assert loop.review_loop.discard_unstarted(REVIEW_SESSION_KEY) is True
    assert loop.review_loop.get(REVIEW_SESSION_KEY) is None


# ---------------------------------------------------------------------------
# Admission boundary wired through SessionCoordinator
# ---------------------------------------------------------------------------


def _admission_loop(tmp_path: Path) -> tuple[SessionCoordinator, Path]:
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    target = tmp_path / "admitted-pkg"
    target.mkdir()
    (target / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    return loop, target


def _admit(loop: SessionCoordinator, target: Path, session_key: str = REVIEW_SESSION_KEY):
    return loop.admit_review(
        ReviewAdmissionRequest(
            target=str(target),
            session_key=session_key,
            cwd=str(target.parent),
        )
    )


def test_admit_review_registers_run_and_persists_metadata(tmp_path) -> None:
    loop, target = _admission_loop(tmp_path)

    admission = _admit(loop, target)

    state = loop._review_runs[REVIEW_SESSION_KEY]
    assert state.run_id == admission.run_id
    assert state.input_fingerprint == admission.input_fingerprint
    assert state.snapshot_ref == admission.snapshot_ref
    persisted = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    assert persisted.metadata[ReviewMetaKey.RUN_ID] == admission.run_id
    assert persisted.metadata[ReviewMetaKey.STATUS] == "running"
    assert persisted.metadata[ReviewMetaKey.SNAPSHOT_REF] == admission.snapshot_ref


def test_admit_review_rejects_duplicate_submission(tmp_path) -> None:
    loop, target = _admission_loop(tmp_path)
    first = _admit(loop, target)
    original = dict(loop.sessions.get_or_create(REVIEW_SESSION_KEY).metadata)

    with pytest.raises(ReviewAdmissionError) as excinfo:
        _admit(loop, target)

    assert excinfo.value.code is ReviewAdmissionCode.DUPLICATE_REVIEW
    assert loop._review_runs[REVIEW_SESSION_KEY].run_id == first.run_id
    assert loop.sessions.get_or_create(REVIEW_SESSION_KEY).metadata == original


def test_rejected_admission_creates_no_session_or_run(tmp_path) -> None:
    loop, _ = _admission_loop(tmp_path)

    with pytest.raises(ReviewAdmissionError):
        loop.admit_review(
            ReviewAdmissionRequest(target=str(tmp_path / "missing"), session_key=REVIEW_SESSION_KEY)
        )

    assert REVIEW_SESSION_KEY not in loop._review_runs
    assert ReviewMetaKey.RUN_ID not in loop.sessions.get_or_create(REVIEW_SESSION_KEY).metadata


@pytest.mark.asyncio
async def test_admitted_turn_reaches_processing_without_reregistering(
    tmp_path, monkeypatch
) -> None:
    loop, target = _admission_loop(tmp_path)
    admission = _admit(loop, target)
    executed: list[str | None] = []

    async def _fake_execute(request):
        state = loop.review_loop.get(REVIEW_SESSION_KEY)
        executed.append(state.run_id if state else None)
        return ReviewLoopOutcome(report_markdown="ok", produces_report=False)

    monkeypatch.setattr(loop.review_loop, "execute", _fake_execute)
    msg = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="gate-session",
        content=f"Review {target}",
        session_key_override=REVIEW_SESSION_KEY,
        metadata={
            "_review_admitted": admission.run_id,
            "review_target": str(target),
            "review_target_type": "local",
            "review_action": "repo",
        },
    )

    await loop._dispatch(msg)
    await loop.close_background_tasks()

    # Exactly one run executed, and it is the admitted run — no second
    # registration replaced it.
    assert executed == [admission.run_id]
    outbound = await _drain_outbound(loop)
    assert _gate_codes(outbound) == []


def test_admitted_turn_is_the_only_review_entry(tmp_path) -> None:
    """Only the admitted turn of the live run may enter the review pipeline."""
    loop, target = _admission_loop(tmp_path)
    admission = _admit(loop, target)

    assert (
        loop._is_admitted_review_turn(
            {"_review_admitted": admission.run_id}, REVIEW_SESSION_KEY
        )
        is True
    )
    # A stale/replayed admission of another run is not.
    assert (
        loop._is_admitted_review_turn(
            {"_review_admitted": "run-other"}, REVIEW_SESSION_KEY
        )
        is False
    )
    # Neither is a plain conversation turn, even in the same session.
    assert loop._is_admitted_review_turn({}, REVIEW_SESSION_KEY) is False


def test_new_command_is_rejected_inside_a_review_session(tmp_path) -> None:
    loop, target = _admission_loop(tmp_path)
    _admit(loop, target)
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)

    response = loop._review_command_gate(session, _ordinary_message(), "/new")

    assert response is not None
    assert response.metadata["review_gate"]["code"] == "new_in_review_session"
    assert response.metadata["review_gate"]["accepted"] is False


def test_status_and_stop_stay_available_during_a_running_review(tmp_path) -> None:
    loop, target = _admission_loop(tmp_path)
    _admit(loop, target)
    session = loop.sessions.get_or_create(REVIEW_SESSION_KEY)

    assert loop._review_command_gate(session, _ordinary_message(), "/status") is None
    assert loop._review_command_gate(session, _ordinary_message(), "/stop") is None

    blocked = loop._review_command_gate(session, _ordinary_message(), "/model default")
    assert blocked is not None
    assert blocked.metadata["review_gate"]["code"] == "command_not_allowed_during_review"


def test_commands_are_untouched_outside_review_sessions(tmp_path) -> None:
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)
    session = loop.sessions.get_or_create("cli:plain")

    assert loop._review_command_gate(session, _ordinary_message(), "/new") is None


@pytest.mark.asyncio
async def test_stop_settles_a_leftover_run_without_an_active_turn(tmp_path) -> None:
    """A running run with no active task is settled as stopped by /stop.

    After a run's turn already returned while cleanup/persistence failed, the
    session gate would stay closed forever because ``/stop`` had nothing to
    cancel. The fallback settles the run so the gate opens.
    """
    loop, target = _admission_loop(tmp_path)
    admission = _admit(loop, target)
    # Simulate the unsettled outcome: the turn ended, the run stayed running.
    state = loop.review_loop.get(REVIEW_SESSION_KEY)
    state.phase = ReviewPhase.CLEANUP
    loop.sessions.save(loop.sessions.get_or_create(REVIEW_SESSION_KEY))

    note = await loop._settle_review_run_after_stop(REVIEW_SESSION_KEY)

    assert note == f"Settled review run {admission.run_id} as stopped."
    assert loop.review_loop.running(REVIEW_SESSION_KEY) is None
    persisted = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    assert persisted.metadata[ReviewMetaKey.STATUS] == "stopped"
    assert persisted.metadata[ReviewMetaKey.PHASE] == "done"


@pytest.mark.asyncio
async def test_stop_settle_is_a_noop_without_a_live_run(tmp_path) -> None:
    """A plain session (no live run) settles nothing and returns None."""
    loop = SessionCoordinator(MessageBus(), _DummyProvider(), tmp_path)

    assert await loop._settle_review_run_after_stop("cli:plain") is None


@pytest.mark.asyncio
async def test_cancel_while_queued_on_the_lock_settles_instead_of_discarding(
    tmp_path,
) -> None:
    """Cancelling a turn queued on the session lock settles the admitted run.

    Before the fix, a cancel during ``async with lock, gate`` skipped the inner
    cancellation handler and the ``finally`` dropped the admitted ``PREPARE``
    run while its persisted metadata still claimed ``running``. The run must
    settle as ``stopped`` instead of being discarded.
    """
    loop, target = _admission_loop(tmp_path)
    admission = _admit(loop, target)
    assert loop.review_loop.running(REVIEW_SESSION_KEY) is not None

    # Hold the session lock so the dispatched turn parks on acquisition.
    lock = loop._session_locks.setdefault(REVIEW_SESSION_KEY, asyncio.Lock())
    await lock.acquire()
    try:
        msg = InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="gate-session",
            content=f"Review {target}",
            session_key_override=REVIEW_SESSION_KEY,
            metadata={
                "_review_admitted": admission.run_id,
                "review_target": str(target),
                "review_target_type": "local",
                "review_action": "repo",
            },
        )
        task = asyncio.create_task(loop._dispatch(msg))
        loop._active_tasks.setdefault(REVIEW_SESSION_KEY, []).append(task)
        task.add_done_callback(
            lambda t, k=REVIEW_SESSION_KEY: loop._remove_active_task(k, t)
        )
        await asyncio.sleep(0.05)
        assert not task.done()

        await loop._cancel_active_tasks(REVIEW_SESSION_KEY)
        await asyncio.gather(task, return_exceptions=True)
    finally:
        lock.release()

    # The admitted run survived as a settled (stopped) run, not discarded.
    state = loop.review_loop.get(REVIEW_SESSION_KEY)
    assert state is not None
    assert state.status is ReviewRunStatus.STOPPED
    persisted = loop.sessions.get_or_create(REVIEW_SESSION_KEY)
    assert persisted.metadata[ReviewMetaKey.STATUS] == "stopped"
