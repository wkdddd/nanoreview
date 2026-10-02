"""ReviewLoop: owner of one review run's lifecycle, state, and persistence.

``ReviewLoop`` executes one complete code review — planning, evidence,
reviewers, Judge, finalizer — and owns the authoritative ``ReviewRunState``
for the session's single review run. It is the only place that:

* registers and drops run state,
* moves a run between phases and to a terminal status,
* persists the report artifact and the terminal session metadata,
* produces the structured :class:`ReviewResult` the rest of the system reads.

Execution itself is delegated to :class:`ReviewOrchestrator`, which remains a
pure review-domain component: it runs the pipeline and reports an outcome, but
never writes run state or session metadata. ``AgentRunner`` writes nothing
about review state either — the Runner only executes one agent run.

The module calls no model, opens no transport, and keeps no persisted state
machine beyond the review run itself.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoreview.agent.orchestration import (
    ReviewExecutionContext,
    ReviewExecutionOutcome,
    ReviewOrchestrator,
)
from nanoreview.agent.review_state import (
    REVIEW_TERMINAL_STATUSES,
    ReviewArtifactStore,
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
    build_report_artifact,
    compute_review_input_fingerprint,
    serialize_finalizer_result,
)
from nanoreview.review.admission import (
    ReviewAdmission,
    register_review_run,
)
from nanoreview.review.result import ReviewResult, result_from_run_state
from nanoreview.review.types import ReviewMetaKey

if TYPE_CHECKING:
    from nanoreview.agent.runner import AgentRunner
    from nanoreview.agent.subagent import SubagentManager
    from nanoreview.review.output.judge import ReviewJudge
    from nanoreview.review.types import ReviewEvidenceBundle, ReviewPlan
    from nanoreview.session.manager import Session, SessionManager

#: Session metadata cleared when a review session is reset via ``/new``.
_RESET_METADATA_KEYS = (
    ReviewMetaKey.RUN_ID,
    ReviewMetaKey.STATUS,
    ReviewMetaKey.PHASE,
    ReviewMetaKey.REPORT_REF,
    ReviewMetaKey.INPUT_FINGERPRINT,
    ReviewMetaKey.SNAPSHOT_REF,
    ReviewMetaKey.HANDOFF_RUN_ID,
)


class ReviewLoop:
    """Own one review run's state, persistence, and structured result."""

    def __init__(
        self,
        *,
        workspace: Path,
        sessions: "SessionManager",
        runner: "AgentRunner",
        subagents: "SubagentManager",
        model: str,
        max_tool_result_chars: int,
        context_window_tokens: int | None = None,
        judge_factory: Callable[[], "ReviewJudge | None"] | None = None,
        artifact_store: ReviewArtifactStore | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._sessions = sessions
        self._runner = runner
        self._subagents = subagents
        self._model = model
        self._max_tool_result_chars = max_tool_result_chars
        self._context_window_tokens = context_window_tokens
        self._judge_factory = judge_factory
        self._artifacts = artifact_store or ReviewArtifactStore(self._workspace)
        #: session_key -> ReviewRunState. This is the authoritative in-process
        #: registry; ``AgentLoop`` aliases it for backward compatibility.
        self.runs: dict[str, ReviewRunState] = {}

    # -- registry -----------------------------------------------------------

    @property
    def artifacts(self) -> ReviewArtifactStore:
        return self._artifacts

    def get(self, session_key: str) -> ReviewRunState | None:
        return self.runs.get(session_key)

    def running(self, session_key: str) -> ReviewRunState | None:
        state = self.runs.get(session_key)
        if state is None or state.status is not ReviewRunStatus.RUNNING:
            return None
        return state

    def register(self, admission: ReviewAdmission) -> ReviewRunState:
        """Register the authoritative run state for an accepted admission."""
        state = register_review_run(admission)
        self.runs[admission.session_key] = state
        logger.info(
            "review.run.registered session={} run_id={} snapshot_ref={}",
            admission.session_key,
            admission.run_id,
            admission.snapshot_ref,
        )
        return state

    def discard(self, session_key: str) -> ReviewRunState | None:
        """Drop the run state without touching session metadata."""
        return self.runs.pop(session_key, None)

    def discard_unstarted(self, session_key: str) -> bool:
        """Release the gate for a run that never entered the review pipeline.

        A turn that failed or was rerouted before planning leaves a run state
        that would otherwise gate the session forever. Nothing was produced,
        so the run is simply dropped. Returns True when a run was dropped.
        """
        state = self.runs.get(session_key)
        if (
            state is None
            or state.status is not ReviewRunStatus.RUNNING
            or state.phase is not ReviewPhase.PREPARE
        ):
            return False
        self.runs.pop(session_key, None)
        logger.info(
            "review.run.discarded session={} run_id={} reason=never_started",
            session_key,
            state.run_id,
        )
        return True

    def mark_responding(self, session_key: str) -> None:
        """Record that the review run is now assembling its response."""
        state = self.runs.get(session_key)
        if state is not None:
            state.enter_phase(ReviewPhase.RESPOND)

    def mark_responded(self, session: "Session | None") -> None:
        """Mark a terminal run as fully answered and persist its metadata.

        Called once the turn that produced the report has finished, so the
        persisted phase matches the run's real terminal state before any
        conversation turn reads it.
        """
        if session is None:
            return
        state = self.runs.get(session.key)
        if (
            state is None
            or state.status not in REVIEW_TERMINAL_STATUSES
            or state.phase is ReviewPhase.DONE
        ):
            return
        state.phase = ReviewPhase.DONE
        self._persist_metadata(state, session)

    def reset(self, session_key: str) -> None:
        """Drop the run and its persisted review metadata (``/new``)."""
        self.runs.pop(session_key, None)
        session = self._sessions.get_or_create(session_key)
        changed = False
        for key in _RESET_METADATA_KEYS:
            if key in session.metadata:
                session.metadata.pop(key, None)
                changed = True
        if changed:
            self._sessions.save(session)

    # -- execution ----------------------------------------------------------

    async def execute(
        self,
        *,
        run_state: ReviewRunState | None,
        session: "Session | None",
        coordinator_messages: list[dict[str, Any]],
        plan: "ReviewPlan",
        evidence: "ReviewEvidenceBundle",
        execution_context: ReviewExecutionContext,
        validation_workspace: str,
        changed_files: list[str] | None = None,
        local_target: str | None = None,
        remote_diff: Any | None = None,
    ) -> ReviewExecutionOutcome:
        """Run the review pipeline and persist a terminal outcome.

        On success the report artifact is written and the run is marked
        ``completed`` before this returns, so the session can never be handed
        a "finished" run whose report is not on disk. A failed artifact write
        degrades the run to ``error`` with a recorded warning instead of
        leaving a half-persisted result.
        """
        self._begin_plan(run_state, plan, evidence, session)
        orchestrator = ReviewOrchestrator(
            runner=self._runner,
            subagentmanager=self._subagents,
            model=self._model,
            workspace=self._workspace,
            max_tool_result_chars=self._max_tool_result_chars,
            judge=self._judge_factory() if self._judge_factory else None,
            context_window_tokens=self._context_window_tokens,
        )
        outcome = await orchestrator.execute_run(
            coordinator_messages=coordinator_messages,
            plan=plan,
            evidence=evidence,
            context=execution_context,
            validation_workspace=validation_workspace,
            changed_files=changed_files,
            local_target=local_target,
            remote_diff=remote_diff,
            run_state=run_state,
        )
        self._save_report(run_state, outcome, session)
        return outcome

    def _begin_plan(
        self,
        run_state: ReviewRunState | None,
        plan: "ReviewPlan",
        evidence: "ReviewEvidenceBundle",
        session: "Session | None",
    ) -> None:
        """Record the plan identity, fingerprint, and running status."""
        if run_state is None:
            return
        run_state.enter_phase(ReviewPhase.PLAN)
        run_state.plan = plan
        run_state.input_fingerprint = compute_review_input_fingerprint(plan, evidence)
        self._persist_metadata(run_state, session)
        logger.info(
            "review.run.started session={} run_id={} fingerprint={}",
            run_state.session_key,
            run_state.run_id,
            run_state.input_fingerprint[:12],
        )

    def _save_report(
        self,
        run_state: ReviewRunState | None,
        outcome: ReviewExecutionOutcome,
        session: "Session | None",
    ) -> None:
        """Serialize, persist, and mark the terminal status of a finished run."""
        if run_state is None:
            return
        run_state.enter_phase(ReviewPhase.SAVE)
        findings, verdicts = serialize_finalizer_result(outcome.finalizer_result)
        run_state.findings = findings
        run_state.set_summary(outcome.report_markdown)
        # Decide the terminal status before serialization: the artifact must
        # not freeze a run that is still marked running. A failed write leaves
        # no artifact behind, so the run itself degrades to error instead.
        report_ref = self._artifacts.write(
            build_report_artifact(
                run_state,
                report_markdown=outcome.report_markdown,
                verdicts=verdicts,
                status=ReviewRunStatus.COMPLETED,
            )
        )
        if report_ref is not None:
            run_state.report_ref = report_ref
            run_state.status = ReviewRunStatus.COMPLETED
        else:
            run_state.status = ReviewRunStatus.ERROR
            run_state.add_warning("Failed to persist the review report artifact.")
        self._persist_metadata(run_state, session)

    def mark_failed(
        self,
        run_state: ReviewRunState | None,
        reason: str,
        session: "Session | None" = None,
    ) -> None:
        """Record a failure that happened before a report could be produced."""
        if run_state is None:
            return
        run_state.status = ReviewRunStatus.ERROR
        run_state.set_summary(reason)
        run_state.add_warning(reason)
        self._persist_metadata(run_state, session)

    # -- terminal -----------------------------------------------------------

    async def finalize(
        self,
        session_key: str,
        status: ReviewRunStatus,
        *,
        warning: str | None = None,
    ) -> ReviewResult | None:
        """Move a running review run to a terminal status, then clean up.

        A run that never entered the review pipeline (phase still ``PREPARE``)
        releases the gate instead of being closed permanently: the session
        stays usable rather than being bricked by a turn that failed before
        planning.

        Child work still in flight must not read as ``completed``: unfinished
        reviewers/judge are recorded with the target terminal status and a
        bounded reason, while finished work keeps its own terminal state.
        """
        state = self.get(session_key)
        if state is None or state.status is not ReviewRunStatus.RUNNING:
            return None
        if state.phase is ReviewPhase.PREPARE:
            self.discard(session_key)
            return None
        state.status = status
        state.phase = ReviewPhase.DONE
        if warning:
            state.add_warning(warning)
            state.set_summary(warning)
        child_status = "stopped" if status is ReviewRunStatus.STOPPED else "error"
        child_reason = (
            "review stopped before the run finished"
            if status is ReviewRunStatus.STOPPED
            else "review failed before the run finished"
        )
        for reviewer in state.reviewers.values():
            if reviewer.status in ("pending", "running"):
                reviewer.status = child_status
                reviewer.error = child_reason
        for batch in state.judge_batches.values():
            if batch.status in ("pending", "running"):
                batch.status = child_status
                batch.error = child_reason
        await self._cancel_children(session_key)
        session = self._sessions.get_or_create(session_key)
        session.metadata.update(state.metadata_payload())
        self._sessions.save(session)
        logger.info(
            "review.run.finalized session={} run_id={} status={} report_ref={}",
            session_key,
            state.run_id,
            state.status.value,
            state.report_ref,
        )
        return self.result(session_key)

    async def _cancel_children(self, session_key: str) -> None:
        """Best-effort cleanup of any subagent still owned by the run."""
        cancel = getattr(self._subagents, "cancel_by_session", None)
        if not callable(cancel):
            return
        try:
            cancelled = await cancel(session_key)
        except Exception:  # pragma: no cover - defensive cleanup
            logger.debug(
                "review.run.cleanup.failed session={}", session_key, exc_info=True
            )
            return
        if cancelled:
            logger.info(
                "review.run.cleanup.cancelled session={} count={}", session_key, cancelled
            )

    # -- result -------------------------------------------------------------

    def result(self, session_key: str) -> ReviewResult | None:
        """Structured result of *session_key*'s review, or ``None``.

        Returns ``None`` for a run that is still running: only a terminal run
        has a result to hand to the conversation side.
        """
        state = self.runs.get(session_key)
        if state is None:
            return None
        if state.status not in REVIEW_TERMINAL_STATUSES:
            return None
        session = self._sessions.get_or_create(session_key)
        return result_from_run_state(state, session_metadata=session.metadata)

    # -- internals ----------------------------------------------------------

    def _persist_metadata(self, run_state: ReviewRunState, session: "Session | None") -> None:
        if session is None:
            return
        session.metadata.update(run_state.metadata_payload())
        self._sessions.save(session)


__all__ = ["ReviewLoop"]
