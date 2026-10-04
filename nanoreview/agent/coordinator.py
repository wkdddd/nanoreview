"""Session coordinator: admission, routing, command gating, and handoff.

``SessionCoordinator`` is the process-level decision point between the two
agent phases a NanoReview session can be in:

* **review** — one review run owns the session; ordinary messages and
  non-control commands are refused without leaving history behind.
* **conversation** — the review reached a terminal status, its resources were
  cleaned up and its result was persisted; ordinary messages are accepted and
  the first one carries the review handoff.

The coordinator is deliberately thin. It calls no model, executes no tool, and
keeps no persisted state machine of its own: every routing decision is derived
from the live ``ReviewRunState``, the session metadata, the report artifact,
and the handoff index. Review execution lives in ``ReviewLoop``; it is the only
writer of review run state, report artifacts and terminal metadata.

Handoff states
--------------
``complete``  — report artifact on disk, run completed without gaps.
``partial``   — report artifact on disk, but the run reported gaps.
``failed``    — no usable artifact. The conversation still opens, but the
                injected system context states the failure, what partial
                results exist and which coverage gaps remain.

No handoff is ever retried, auto-repaired or re-run: a failed handoff stays
failed for the life of the session, and the persisted artifact (when there is
one) remains readable.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoreview.agent.review_state import (
    ReviewArtifactError,
    ReviewArtifactStore,
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
)
from nanoreview.bus.events import InboundMessage, OutboundMessage
from nanoreview.review.admission import (
    ReviewAdmission,
    ReviewAdmissionCode,
    ReviewAdmissionError,
    ReviewAdmissionRequest,
    ReviewAdmissionService,
)
from nanoreview.review.result import (
    ReviewHandoffState,
    ReviewResult,
    render_handoff_block,
    render_handoff_directive,
    render_review_context_index,
    result_from_session_metadata,
)
from nanoreview.review.types import ReviewMetaKey
from nanoreview.utils.helpers import estimate_prompt_tokens

if TYPE_CHECKING:
    from nanoreview.agent.review_loop import ReviewLoop
    from nanoreview.session.manager import Session, SessionManager

#: Commands that stay usable while a review run owns the session.
REVIEW_ALLOWED_COMMANDS = frozenset({"/status", "/stop"})

#: Index / handoff markers stored on the persisted session messages.
REVIEW_CONTEXT_EVENT = "review_context"
REVIEW_HANDOFF_EVENT = "review_handoff"

#: How much of the context window is held back from the handoff check for the
#: system prompt, runtime block and the model's own output.
_HANDOFF_PROMPT_RESERVE_TOKENS = 1024

#: Reason recorded when a run lost its executor before producing a result.
INTERRUPTED_RUN_REASON = (
    "the review was interrupted before it produced a result and cannot be resumed"
)

#: Placeholder for "key absent" when snapshotting session metadata before a
#: repair write, so a failed save can restore the exact previous state.
_ABSENT = object()


def _is_review_turn(metadata: dict[str, Any] | None) -> bool:
    meta = metadata or {}
    return bool(meta.get(ReviewMetaKey.TARGET) or meta.get("review_target"))


def _is_internal_event(msg: InboundMessage) -> bool:
    """Subagent results and system events bypass review session gating."""
    meta = msg.metadata if isinstance(msg.metadata, dict) else {}
    return (
        msg.channel == "system"
        or msg.sender_id == "subagent"
        or meta.get("injected_event") in ("subagent_result", "subagent_barrier")
    )


class SessionRoute(StrEnum):
    """Which agent phase owns the session right now."""

    REVIEW = "review"
    CONVERSATION = "conversation"


@dataclass(frozen=True, slots=True)
class ReviewHandoff:
    """The review result about to be handed to the first conversation turn."""

    result: ReviewResult
    report_markdown: str | None
    fits: bool

    @property
    def block(self) -> str:
        """Full injected context: framing, gaps, and the complete report."""
        return render_handoff_block(self.result, self.report_markdown)

    @property
    def directive(self) -> str:
        """Short system-level directive naming provenance and handoff state."""
        return render_handoff_directive(self.result)


class SessionCoordinator:
    """Route one session between review and conversation, and gate messages."""

    def __init__(
        self,
        *,
        sessions: "SessionManager",
        workspace: Path,
        review_loop: "ReviewLoop",
        artifacts: ReviewArtifactStore | None = None,
        context_window_tokens: int = 0,
        reserved_output_tokens: int = 4096,
        admission_service: ReviewAdmissionService | None = None,
    ) -> None:
        self._sessions = sessions
        self._workspace = Path(workspace)
        self._review_loop = review_loop
        self._artifacts = artifacts or ReviewArtifactStore(self._workspace)
        self._context_window_tokens = int(context_window_tokens or 0)
        self._reserved_output_tokens = int(reserved_output_tokens or 0)
        self._admission_service = admission_service

    # -- admission ----------------------------------------------------------

    def admissions(self) -> ReviewAdmissionService:
        """Shared admission/domain service used by every review entry point."""
        if self._admission_service is None:
            self._admission_service = ReviewAdmissionService(
                sessions=self._sessions,
                workspace=self._workspace,
            )
        return self._admission_service

    def admit(self, request: ReviewAdmissionRequest) -> ReviewAdmission:
        """Validate, snapshot, and register one review run before delivery.

        Rejection raises :class:`ReviewAdmissionError` and leaves no session,
        run, snapshot or history behind. Acceptance registers the single
        review run for the session; a session already owning a review run is
        refused with ``duplicate_review``.
        """
        live = self._review_loop.get(request.session_key or "")
        if live is not None:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.DUPLICATE_REVIEW,
                (
                    f"Session '{live.session_key}' already has review run "
                    f"'{live.run_id}' in progress."
                ),
            )
        admission = self.admissions().admit(request)
        self._review_loop.register(admission)
        return admission

    # -- routing ------------------------------------------------------------

    def route(self, session: "Session") -> SessionRoute:
        """Decide which phase owns *session*.

        A live running run keeps the session in the review phase. Everything
        else routes to conversation, because a review that cannot finish must
        not leave its session gated forever. With no live run the persisted
        metadata is consulted through :meth:`result`, which repairs an orphaned
        ``running`` run once so the session becomes usable again.
        """
        live = self._review_loop.get(session.key)
        if live is not None:
            if live.status is ReviewRunStatus.RUNNING:
                return SessionRoute.REVIEW
            return SessionRoute.CONVERSATION
        self.result(session)
        return SessionRoute.CONVERSATION

    def result(self, session: "Session") -> ReviewResult | None:
        """Structured review result for *session*, or ``None`` if it has none.

        A *live* run is always authoritative: while it runs it has no result
        yet, and its persisted metadata must not be mistaken for an orphaned
        run. Only when no live run exists does the persisted metadata decide —
        and there a ``running`` status means the owning process is gone, so it
        is normalized once to a terminal ``error`` (never resumed, never
        re-run).
        """
        if self._review_loop.get(session.key) is not None:
            return self._review_loop.result(session.key)
        result = result_from_session_metadata(session.key, session.metadata)
        if result is None:
            return None
        if result.status is ReviewRunStatus.RUNNING:
            return self._normalize_interrupted_run(session, result)
        return result

    def _normalize_interrupted_run(
        self, session: "Session", result: ReviewResult
    ) -> ReviewResult:
        """Mark an orphaned ``running`` run as a failed handoff (one write).

        The process that owned the run is gone and this version never resumes
        a review, so the persisted status is corrected to ``error`` with a
        bounded reason. The repair is idempotent: it runs once and later reads
        see the terminal status.

        The repair mutates the session metadata in place but must not publish
        a terminal state it cannot persist: on a failed save the previous
        values are restored so the cache never runs ahead of disk (the run
        stays ``running`` and a later read retries the repair).
        """
        keys = (
            ReviewMetaKey.STATUS,
            ReviewMetaKey.PHASE,
            ReviewMetaKey.SUMMARY,
            ReviewMetaKey.ERROR,
        )
        previous = {
            key: session.metadata.get(key, _ABSENT) for key in keys
        }
        session.metadata[ReviewMetaKey.STATUS] = ReviewRunStatus.ERROR.value
        session.metadata[ReviewMetaKey.PHASE] = ReviewPhase.DONE.value
        # Persist the bounded reason alongside the repaired status so every
        # later reader (other transports, a restart) sees why the run failed,
        # not just that it failed.
        session.metadata[ReviewMetaKey.SUMMARY] = INTERRUPTED_RUN_REASON
        session.metadata[ReviewMetaKey.ERROR] = INTERRUPTED_RUN_REASON
        try:
            self._sessions.save(session)
        except Exception as exc:
            for key, value in previous.items():
                if value is _ABSENT:
                    session.metadata.pop(key, None)
                else:
                    session.metadata[key] = value
            logger.warning(
                "review.route.interrupted_persist_failed session={} run_id={} reason={}",
                session.key,
                result.run_id,
                exc,
            )
            return result
        logger.info(
            "review.route.interrupted session={} run_id={} reason=no_live_executor",
            session.key,
            result.run_id,
        )
        repaired = result_from_session_metadata(session.key, session.metadata)
        if repaired is None:
            return result
        return dataclasses.replace(repaired, error=INTERRUPTED_RUN_REASON, summary=INTERRUPTED_RUN_REASON)

    # -- gating -------------------------------------------------------------

    def gate_message(
        self,
        msg: InboundMessage,
        live_run: ReviewRunState | None,
        raw: str,
    ) -> OutboundMessage | None:
        """Refuse an ordinary message that reaches a running review.

        Only a *running* run gates: once the review is terminal its resources
        are cleaned up and its result is persisted, so the session has moved
        to conversation. Commands keep their own gate and internal events stay
        available; the admitted review turn itself (``_review_admitted``
        matching the live run) must pass or no review could ever execute.
        """
        if _is_internal_event(msg) or raw.startswith("/"):
            return None
        if live_run is None or live_run.status is not ReviewRunStatus.RUNNING:
            return None
        admitted_run_id = msg.metadata.get("_review_admitted")
        if (
            isinstance(admitted_run_id, str)
            and admitted_run_id
            and live_run.run_id == admitted_run_id
        ):
            return None
        logger.info(
            "review.gate.rejected session={} run_id={} source={}",
            live_run.session_key,
            live_run.run_id,
            msg.metadata.get("injected_event") or "user_message",
        )
        return self._gate_response(
            msg,
            run_id=live_run.run_id,
            status=live_run.status,
            code="review_gated",
            content=(
                "Review is already running. "
                "Use /status to check progress or /stop to cancel."
            ),
        )

    def gate_command(
        self, session: "Session", msg: InboundMessage, raw: str
    ) -> OutboundMessage | None:
        """Refuse a slash command inside a review session.

        While a run is live only ``/status`` and ``/stop`` stay available; any
        other command is refused with a structured error and leaves no history
        behind. A session that merely *owns* a review run (terminal, or
        restored after restart) additionally refuses ``/new`` so a review
        session is never silently repurposed as a plain chat — new reviews go
        through a dedicated new session.
        """
        if _is_internal_event(msg):
            return None
        command = raw.strip().split(maxsplit=1)[0].lower()
        live = self._review_loop.get(session.key)
        owns_review = live is not None or bool(session.metadata.get(ReviewMetaKey.RUN_ID))
        if not owns_review:
            return None
        if command == "/new":
            run_id = live.run_id if live is not None else str(
                session.metadata.get(ReviewMetaKey.RUN_ID) or "unknown"
            )
            status = live.status if live is not None else ReviewRunStatus.COMPLETED
            code = "new_in_review_session"
            content = (
                "/new is not available in a review session. "
                "Start a new review session to run another review."
            )
        elif live is not None and live.status is ReviewRunStatus.RUNNING:
            if command in REVIEW_ALLOWED_COMMANDS:
                return None
            run_id, status = live.run_id, live.status
            code = "command_not_allowed_during_review"
            content = (
                f"'{command}' is not available while a review is running. "
                "Use /status to check progress or /stop to cancel."
            )
        else:
            return None
        logger.info(
            "review.gate.command_rejected session={} command={} code={}",
            session.key,
            command,
            code,
        )
        return self._gate_response(
            msg, run_id=run_id, status=status, code=code, content=content
        )

    def gate_review_turn(
        self, session_key: str, msg: InboundMessage, live_run: ReviewRunState | None
    ) -> OutboundMessage | None:
        """Refuse a second review turn for a session that already owns one.

        One session executes at most one review run, and a review is only
        started through admission. Any review-shaped message that did not come
        from an admitted run — including a replay of an older one — is refused
        instead of silently registering a second run.
        """
        if _is_internal_event(msg) or msg.content.strip().startswith("/"):
            return None
        if not _is_review_turn(msg.metadata):
            return None
        admitted_run_id = msg.metadata.get("_review_admitted")
        if (
            isinstance(admitted_run_id, str)
            and admitted_run_id
            and live_run is not None
            and live_run.run_id == admitted_run_id
            and live_run.status is ReviewRunStatus.RUNNING
        ):
            return None
        session = self._sessions.get_or_create(session_key)
        persisted_run_id = session.metadata.get(ReviewMetaKey.RUN_ID)
        run_id = (
            live_run.run_id
            if live_run is not None
            else str(persisted_run_id or admitted_run_id or "unknown")
        )
        status = (
            live_run.status
            if live_run is not None
            else ReviewRunStatus.COMPLETED
        )
        if live_run is not None or persisted_run_id:
            code = "duplicate_review"
            content = (
                "This review session already owns a review run. "
                "Start a new review session to run another review."
            )
        else:
            code = "review_not_admitted"
            content = (
                "A code review must be submitted through the review entry point "
                "(target, action and focus) so it can be validated and registered."
            )
        logger.info(
            "review.gate.review_turn_rejected session={} code={}", session_key, code
        )
        return self._gate_response(
            msg, run_id=run_id, status=status, code=code, content=content
        )

    @staticmethod
    def _gate_response(
        msg: InboundMessage,
        *,
        run_id: str,
        status: ReviewRunStatus,
        code: str,
        content: str,
    ) -> OutboundMessage:
        """Structured rejection shared by every review gate."""
        metadata = {
            **dict(msg.metadata or {}),
            "render_as": "text",
            "review_gate": {
                "code": code,
                "status": status.value,
                "run_id": run_id,
                "accepted": False,
            },
        }
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            metadata=metadata,
        )

    def report_too_large_response(
        self, msg: InboundMessage, handoff: ReviewHandoff
    ) -> OutboundMessage:
        """Refuse the first conversation turn when the report cannot fit.

        The complete report is never replaced by an automatic summary, so an
        oversized report rejects the turn with an explicit reason instead.
        """
        return self._gate_response(
            msg,
            run_id=handoff.result.run_id,
            status=handoff.result.status,
            code="review_report_too_large",
            content=(
                "The review report for this session does not fit in the model "
                "context window, so it cannot be injected in full. Use a model "
                "with a larger context window to continue this conversation."
            ),
        )

    # -- terminal persistence ----------------------------------------------

    def _review_settled(self, session: "Session") -> bool:
        """Whether the session's review run reached its final ``DONE`` phase.

        A terminal status alone is not enough: ``ReviewLoop`` writes ``DONE``
        only after child cleanup and result persistence returned, so the
        handoff and the index wait for that settled phase rather than acting on
        a bare ``review_status``.
        """
        live = self._review_loop.get(session.key)
        if live is not None:
            return live.phase is ReviewPhase.DONE
        return (
            session.metadata.get(ReviewMetaKey.PHASE) == ReviewPhase.DONE.value
        )

    async def finalize(
        self,
        session_key: str,
        status: ReviewRunStatus,
        *,
        warning: str | None = None,
    ) -> ReviewResult | None:
        """Finalize the review run, then publish its index to the session.

        Delegates the whole terminal transition to ``ReviewLoop`` (status,
        child cleanup, metadata) and adds the one thing the session layer
        owns: the ``review_context`` index entry that lets every consumer find
        the report artifact.
        """
        result = await self._review_loop.finalize(session_key, status, warning=warning)
        if result is None:
            return None
        session = self._sessions.get_or_create(session_key)
        self.write_context_index(session)
        return result

    def write_context_index(self, session: "Session") -> bool:
        """Write the review_context index message for a terminal review.

        The index is small, replayable and idempotent: it names the run, the
        report artifact reference and the coverage/gap summary, and never
        carries the full report (that stays the artifact's job). It is only
        written for a run that published its final ``DONE`` phase, so the index
        can never point at a result the run has not settled. Returns True when
        a new entry was appended.
        """
        if not self._review_settled(session):
            return False
        result = self.result(session)
        if result is None or not result.is_terminal:
            return False
        if any(
            message.get("injected_event") == REVIEW_CONTEXT_EVENT
            and message.get("review_run_id") == result.run_id
            for message in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            render_review_context_index(result),
            injected_event=REVIEW_CONTEXT_EVENT,
            review_run_id=result.run_id,
            review_report_ref=result.report_ref,
            review_source="review_agent",
        )
        self._sessions.save(session)
        logger.info(
            "review.context.indexed session={} run_id={} handoff={}",
            session.key,
            result.run_id,
            result.handoff.value,
        )
        return True

    # -- handoff ------------------------------------------------------------

    def pending_handoff(self, session: "Session") -> ReviewHandoff | None:
        """The handoff the next conversation turn must inject, if any.

        Returns ``None`` once the current run's report has already been
        injected, when the session has no terminal review, when the run is
        still running, or when the run has not published its final ``DONE``
        phase yet — a terminal status without ``DONE`` means cleanup or result
        persistence has not settled, and handing over then would announce a
        result the run has not finished writing.

        A report the coordinator cannot actually load downgrades the handoff to
        ``failed``: a reference without readable content is not a usable report,
        and reporting it as complete would inject a success framing over a
        handoff that has nothing to hand over.

        ``result()`` is called before the settlement check on purpose: for a
        session restored after a restart (no live executor) the persisted run
        is still ``running``, and ``result()`` is what normalizes that orphan to
        a terminal ``error``. Checking settlement first would return ``None``
        forever and never repair the metadata.
        """
        result = self.result(session)
        if not self._review_settled(session):
            return None
        if result is None or not result.is_terminal:
            return None
        if session.metadata.get(ReviewMetaKey.HANDOFF_RUN_ID) == result.run_id:
            return None
        report = self._read_report(result)
        if report is None and result.handoff is not ReviewHandoffState.FAILED:
            result = dataclasses.replace(
                result,
                handoff=ReviewHandoffState.FAILED,
                error=result.error or "the review report artifact is unavailable",
            )
        return ReviewHandoff(
            result=result,
            report_markdown=report,
            fits=self._handoff_fits(result, report),
        )

    def consume_handoff(self, session: "Session", handoff: ReviewHandoff) -> None:
        """Persist the injected handoff so later turns keep the same context.

        The block enters the conversation history (an assistant message that
        names ReviewAgent as its source) instead of being re-added to every
        system prompt, so it stays available to later turns and to
        consolidation while the report artifact remains the authoritative
        copy.
        """
        session.add_message(
            "assistant",
            handoff.block,
            injected_event=REVIEW_HANDOFF_EVENT,
            review_run_id=handoff.result.run_id,
            review_report_ref=handoff.result.report_ref,
            review_source="review_agent",
            review_handoff=handoff.result.handoff.value,
        )
        session.metadata[ReviewMetaKey.HANDOFF_RUN_ID] = handoff.result.run_id
        self._sessions.save(session)
        logger.info(
            "review.handoff.injected session={} run_id={} handoff={} report_chars={}",
            session.key,
            handoff.result.run_id,
            handoff.result.handoff.value,
            len(handoff.report_markdown or ""),
        )

    def _read_report(self, result: ReviewResult) -> str | None:
        """Load the authoritative report markdown for *result*.

        A missing or unreadable artifact is not repaired here: the handoff
        simply renders as failed with whatever partial results exist.
        """
        if not result.report_ref:
            return None
        try:
            artifact = self._artifacts.read(
                run_id=result.run_id,
                session_key=result.session_key,
                input_fingerprint=result.input_fingerprint,
            )
        except ReviewArtifactError as exc:
            logger.warning(
                "review.handoff.artifact_unreadable run_id={} reason={}",
                result.run_id,
                exc.reason,
            )
            return None
        markdown = artifact.get("report_markdown")
        return markdown if isinstance(markdown, str) and markdown.strip() else None

    def _prompt_budget(self) -> int:
        if self._context_window_tokens <= 0:
            return 0
        budget = (
            self._context_window_tokens
            - max(1, self._reserved_output_tokens)
            - _HANDOFF_PROMPT_RESERVE_TOKENS
        )
        return budget if budget > 0 else 0

    def _handoff_fits(self, result: ReviewResult, report_markdown: str | None) -> bool:
        """Whether the complete handoff fits the model context window.

        An unknown window (``context_window_tokens <= 0``) cannot be judged,
        so the handoff is allowed rather than blocked.
        """
        budget = self._prompt_budget()
        if budget <= 0:
            return True
        tokens = estimate_prompt_tokens(
            [{"role": "system", "content": render_handoff_block(result, report_markdown)}]
        )
        if tokens <= 0:
            # Tokenizer unavailable: fall back to a conservative chars/4
            # estimate rather than letting an oversized report through.
            tokens = max(1, len(report_markdown or "") // 4)
        return tokens <= budget


__all__ = [
    "REVIEW_ALLOWED_COMMANDS",
    "REVIEW_CONTEXT_EVENT",
    "REVIEW_HANDOFF_EVENT",
    "ReviewHandoff",
    "SessionCoordinator",
    "SessionRoute",
]
