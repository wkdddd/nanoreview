"""Per-run review blackboard: owned and written only by ``SessionCoordinator``.

One review run has exactly one authoritative mutable record — its blackboard.
The blackboard is created at admission and lives in the coordinator's
:class:`ReviewBoardStore` for the whole run. Nothing else holds a mutable
reference to it:

* ``ReviewLoop`` receives a :class:`ReviewBoardWriter` (a narrow request
  interface bound to one run id) and a read-only :class:`ReviewBoardView`.
* Reviewers never see the board; they only get their frozen assignment.
* The Judge reads this run's authorized data through the view and hands its
  structured result back, which the coordinator validates and writes.
* The conversation agent never touches the board; it only receives the final
  report.

Every write goes through the store's per-run lock, so concurrent reviewer
completions are serialized and can neither drop nor double-count a result. The
lock is never held across a model or tool call: the writer is only called
*after* an agent run returned, to record its outcome.

The board is not an execution checkpoint. It is not restored after a restart:
only the terminal session metadata is, and a persisted ``running`` run without
a live board stays interrupted (never re-run).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoreview.agent.review_state import (
    REVIEW_SUMMARY_MAX_CHARS,
    REVIEW_TERMINAL_STATUSES,
    ReviewPhase,
    ReviewRunStatus,
    bound_child_error,
    build_report_artifact,
)
from nanoreview.review.types import ReviewMetaKey
from nanoreview.utils.helpers import merge_token_usage

if TYPE_CHECKING:
    from nanoreview.agent.review_state import ReviewArtifactStore
    from nanoreview.review.admission import ReviewAdmission
    from nanoreview.review.output.finalizer import ReviewJudgeStats
    from nanoreview.review.types import (
        ReviewAssignment,
        ReviewDimensionResult,
        ReviewPlan,
    )
    from nanoreview.session.manager import SessionManager

#: Child (reviewer/judge) status vocabulary. ``stopped`` records work cancelled
#: before it finished and is never a successful completion.
CHILD_PENDING = "pending"
CHILD_RUNNING = "running"
CHILD_COMPLETED = "completed"
CHILD_ERROR = "error"
CHILD_STOPPED = "stopped"

#: Session metadata keys that describe *this* run and its outcome, cleared when
#: the session's review is reset (``/new``). They are the run's persisted
#: identity, so leaving them behind makes the next admission look like a
#: duplicate and lets a finished run's report be re-read by a chat that started
#: over. Navigation metadata (the target the session reviews) deliberately stays.
RESET_METADATA_KEYS = (
    ReviewMetaKey.RUN_ID,
    ReviewMetaKey.STATUS,
    ReviewMetaKey.PHASE,
    ReviewMetaKey.REPORT_REF,
    ReviewMetaKey.INPUT_FINGERPRINT,
    ReviewMetaKey.SNAPSHOT_REF,
    ReviewMetaKey.SUMMARY,
    ReviewMetaKey.ERROR,
    ReviewMetaKey.HANDOFF_RUN_ID,
)


class ReviewBoardPersistenceError(RuntimeError):
    """Raised when the terminal state could not be durably saved.

    The board stays ``running`` (the conversation gate stays closed) so a live
    ``DONE`` is only ever published after the terminal metadata reached disk.
    """


class ReviewBoardError(RuntimeError):
    """Raised when a write is requested for a run the board no longer accepts."""


@dataclass(frozen=True, slots=True)
class ReviewerSnapshot:
    """Bounded, immutable view of one reviewer's execution state."""

    dimension: str
    status: str = CHILD_PENDING
    error: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    evidence_assigned: int = 0
    duplicate_reads: int = 0
    duplicate_searches: int = 0


@dataclass(frozen=True, slots=True)
class JudgeBatchSnapshot:
    """Bounded, immutable view of one judge batch's execution state."""

    batch_id: str
    status: str = CHILD_PENDING
    error: str = ""
    stats: Mapping[str, int] = field(default_factory=dict)
    usage: Mapping[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ReviewerRunState:
    """Mutable per-reviewer state; only the board store writes it."""

    dimension: str
    status: str = CHILD_PENDING
    error: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    evidence_assigned: int = 0
    duplicate_reads: int = 0
    duplicate_searches: int = 0

    def add_usage(self, usage: Mapping[str, Any] | None) -> None:
        merge_token_usage(self.usage, usage)

    def snapshot(self) -> ReviewerSnapshot:
        return ReviewerSnapshot(
            dimension=self.dimension,
            status=self.status,
            error=self.error,
            usage=dict(self.usage),
            evidence_assigned=self.evidence_assigned,
            duplicate_reads=self.duplicate_reads,
            duplicate_searches=self.duplicate_searches,
        )


@dataclass(slots=True)
class JudgeBatchState:
    """Mutable per-batch judge state; only the board store writes it."""

    batch_id: str
    status: str = CHILD_PENDING
    error: str = ""
    stats: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)

    def add_usage(self, usage: Mapping[str, Any] | None) -> None:
        merge_token_usage(self.usage, usage)

    def snapshot(self) -> JudgeBatchSnapshot:
        return JudgeBatchSnapshot(
            batch_id=self.batch_id,
            status=self.status,
            error=self.error,
            stats=dict(self.stats),
            usage=dict(self.usage),
        )


@dataclass(slots=True)
class ReviewRunBoard:
    """The single mutable authority for one review run.

    Only :class:`ReviewBoardWriter` mutates this object. Every field is either
    plain data or a domain value object that the board *replaces* rather than
    mutates in place, so a :class:`ReviewBoardView` taken under the board lock
    stays internally consistent.
    """

    run_id: str
    session_key: str
    input_fingerprint: str = ""
    snapshot_ref: str | None = None
    phase: ReviewPhase = ReviewPhase.PREPARE
    status: ReviewRunStatus = ReviewRunStatus.RUNNING
    plan: "ReviewPlan | None" = None
    assignments: tuple["ReviewAssignment", ...] = ()
    reviewers: dict[str, ReviewerRunState] = field(default_factory=dict)
    #: Validated candidate sets keyed by dimension — the only result store.
    dimensions: tuple["ReviewDimensionResult", ...] = ()
    judge_stats: "ReviewJudgeStats | None" = None
    judge_batches: dict[str, JudgeBatchState] = field(default_factory=dict)
    findings: tuple[dict[str, Any], ...] = ()
    report_ref: str | None = None
    summary: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    skipped_files: tuple[str, ...] = ()
    #: Preprocessing acceptance statistics: how many evidence units were
    #: accepted, how many the budget skipped, and the evidence status.
    evidence_stats: Mapping[str, Any] = field(default_factory=dict)
    #: Program-generated task split: one entry per selected reviewer with the
    #: number of accepted evidence units it received.
    assignment_stats: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewBoardView:
    """Read-only projection of one run's board.

    Holds no mutable reference into the board: reviewer and judge states are
    immutable snapshots, collections are tuples. Phase/status readers use it
    for gating; the report path uses it for serialization.
    """

    run_id: str
    session_key: str
    input_fingerprint: str = ""
    snapshot_ref: str | None = None
    phase: ReviewPhase = ReviewPhase.PREPARE
    status: ReviewRunStatus = ReviewRunStatus.RUNNING
    plan: "ReviewPlan | None" = None
    assignments: tuple["ReviewAssignment", ...] = ()
    reviewers: tuple[ReviewerSnapshot, ...] = ()
    dimensions: tuple["ReviewDimensionResult", ...] = ()
    judge_stats: "ReviewJudgeStats | None" = None
    judge_batches: tuple[JudgeBatchSnapshot, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    report_ref: str | None = None
    summary: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    skipped_files: tuple[str, ...] = ()
    #: Preprocessing acceptance statistics: how many evidence units were
    #: accepted, how many the budget skipped, and the evidence status.
    evidence_stats: Mapping[str, Any] = field(default_factory=dict)
    #: Program-generated task split: one entry per selected reviewer with the
    #: number of accepted evidence units it received.
    assignment_stats: tuple[Mapping[str, Any], ...] = ()

    @property
    def is_running(self) -> bool:
        return self.status is ReviewRunStatus.RUNNING

    @property
    def is_terminal(self) -> bool:
        return self.status in REVIEW_TERMINAL_STATUSES

    @property
    def is_done(self) -> bool:
        return self.phase is ReviewPhase.DONE

    def reviewer(self, dimension: str) -> ReviewerSnapshot | None:
        for reviewer in self.reviewers:
            if reviewer.dimension == dimension:
                return reviewer
        return None

    def metadata_payload(
        self,
        *,
        status: ReviewRunStatus | None = None,
        phase: ReviewPhase | None = None,
    ) -> dict[str, Any]:
        """Stable session metadata fields describing this run.

        Only the artifact reference, run id, status, phase, fingerprint and the
        bounded summary/reason cross the wire — never the full report, any
        absolute server path, or a child transcript.
        """
        effective_status = status or self.status
        payload: dict[str, Any] = {
            ReviewMetaKey.RUN_ID: self.run_id,
            ReviewMetaKey.STATUS: effective_status.value,
            ReviewMetaKey.PHASE: (phase or self.phase).value,
            ReviewMetaKey.INPUT_FINGERPRINT: self.input_fingerprint,
        }
        if self.report_ref is not None:
            payload[ReviewMetaKey.REPORT_REF] = self.report_ref
        if self.summary:
            payload[ReviewMetaKey.SUMMARY] = self.summary[:REVIEW_SUMMARY_MAX_CHARS]
        if (
            effective_status in (ReviewRunStatus.ERROR, ReviewRunStatus.STOPPED)
            and self.warnings
        ):
            payload[ReviewMetaKey.ERROR] = bound_child_error(self.warnings[-1])
        return payload

    def coverage_payload(self) -> dict[str, Any]:
        """Compact review-boundary and per-reviewer summary for the report."""
        reviewers = {
            reviewer.dimension: {
                "status": reviewer.status,
                "evidence_assigned": reviewer.evidence_assigned,
                "duplicate_reads": reviewer.duplicate_reads,
                "duplicate_searches": reviewer.duplicate_searches,
                "usage": dict(reviewer.usage),
            }
            for reviewer in self.reviewers
        }
        return {
            "changed_files": list(self.changed_files),
            "skipped_files": list(self.skipped_files),
            "evidence": dict(self.evidence_stats),
            "assignments": [dict(entry) for entry in self.assignment_stats],
            "reviewers": reviewers,
            "incomplete_dimensions": sorted(
                reviewer.dimension
                for reviewer in self.reviewers
                if reviewer.status != CHILD_COMPLETED
            ),
            "judge_batches": {
                batch.batch_id: {"status": batch.status, "error": batch.error}
                for batch in self.judge_batches
            },
        }


class ReviewBoardWriter:
    """Narrow write interface handed to ``ReviewLoop`` for one run.

    Every call is a *request*: the store validates it, applies it under the
    run's lock and returns the freshly projected view. There is no way to reach
    the mutable board through this object, so ``ReviewLoop`` cannot change a
    field the coordinator did not agree to write.
    """

    __slots__ = ("_store", "_session_key", "_run_id")

    def __init__(
        self, store: "ReviewBoardStore", session_key: str, run_id: str
    ) -> None:
        self._store = store
        self._session_key = session_key
        self._run_id = run_id

    @property
    def run_id(self) -> str:
        return self._run_id

    async def advance(self, phase: ReviewPhase) -> None:
        """Move the run to *phase*; terminal runs ignore phase writes."""
        await self._store._mutate(self._session_key, self._run_id, _advance, phase)

    async def record_accepted_input(
        self,
        *,
        plan: "ReviewPlan | None",
        input_fingerprint: str,
        changed_files: list[str],
        skipped_files: list[str],
        evidence_stats: Mapping[str, Any],
        assignment_stats: list[Mapping[str, Any]],
    ) -> None:
        """Record the resolved plan identity and the review boundary."""
        await self._store._mutate(
            self._session_key,
            self._run_id,
            _record_input,
            plan,
            input_fingerprint,
            changed_files,
            skipped_files,
            evidence_stats,
            assignment_stats,
        )

    async def start_assignments(
        self, assignments: tuple["ReviewAssignment", ...]
    ) -> None:
        """Record the validated assignments and open their reviewer slots."""
        await self._store._mutate(
            self._session_key, self._run_id, _start_assignments, assignments
        )

    async def reviewer_started(self, dimension: str, *, evidence_assigned: int) -> None:
        """Mark one reviewer as running with its authorized evidence count."""
        await self._store._mutate(
            self._session_key,
            self._run_id,
            _reviewer_started,
            dimension,
            evidence_assigned,
        )

    async def reviewer_settled(
        self,
        dimension: str,
        *,
        status: str,
        error: str = "",
        usage: Mapping[str, Any] | None = None,
        duplicate_reads: int = 0,
        duplicate_searches: int = 0,
    ) -> None:
        """Record one reviewer's terminal outcome exactly once.

        A repeated completion for the same dimension is rejected by the store
        (``completed`` is terminal), so one assignment can never be counted
        twice.
        """
        await self._store._mutate(
            self._session_key,
            self._run_id,
            _reviewer_settled,
            dimension,
            status,
            error,
            dict(usage or {}),
            duplicate_reads,
            duplicate_searches,
        )

    async def record_dimension(self, result: "ReviewDimensionResult") -> None:
        """Record one validated dimension result (the board replaces it)."""
        await self._store._mutate(
            self._session_key, self._run_id, _record_dimension, result
        )

    async def judge_started(self, batch_id: str) -> None:
        """Mark one judge batch as running before its pass starts."""
        await self._store._mutate(
            self._session_key, self._run_id, _judge_started, batch_id
        )

    async def judge_settled(
        self,
        batch_id: str,
        *,
        status: str,
        error: str = "",
        stats: Mapping[str, int] | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """Record one judge batch's terminal outcome and its usage."""
        await self._store._mutate(
            self._session_key,
            self._run_id,
            _judge_settled,
            batch_id,
            status,
            error,
            dict(stats or {}),
            dict(usage or {}),
        )

    async def record_judged(
        self,
        dimensions: tuple["ReviewDimensionResult", ...],
        stats: "ReviewJudgeStats | None",
    ) -> None:
        """Replace the candidate sets with their judge-applied version.

        Judge verdicts and the report therefore read the same candidate
        version: there is one copy, replaced once.
        """
        await self._store._mutate(
            self._session_key, self._run_id, _record_judged, dimensions, stats
        )

    async def add_warning(self, message: str) -> None:
        """Append one bounded, deduplicated run warning."""
        await self._store._mutate(
            self._session_key, self._run_id, _add_warning, message
        )

    async def record_summary(self, text: str) -> None:
        """Record the bounded run summary carried in the session metadata.

        A run that ends without a report (stop, error, aborted turn) still has
        to explain itself on disk, so the reason is recorded as the summary as
        well as a warning. A later report write replaces it with the report
        digest.
        """
        await self._store._mutate(
            self._session_key, self._run_id, _record_summary, text
        )

    async def add_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Fold one agent run's token usage into the run total."""
        await self._store._mutate(
            self._session_key, self._run_id, _add_usage, dict(usage or {})
        )

    async def record_report(
        self,
        *,
        report_markdown: str,
        findings: tuple[dict[str, Any], ...],
        verdicts: list[dict[str, Any]],
        status: ReviewRunStatus = ReviewRunStatus.COMPLETED,
    ) -> str | None:
        """Persist the report artifact and record its reference.

        ``status`` is the terminal status the run will settle to. It is written
        into the artifact because the artifact is the durable record of the run:
        a draft report that settles as ``error`` must not be stored as a
        completed review. Returns the wire reference, or ``None`` when the
        artifact could not be written; the caller must then settle the run as
        ``error`` instead of claiming a complete review with no report on disk.
        """
        return await self._store._record_report(
            self._session_key,
            self._run_id,
            report_markdown=report_markdown,
            findings=findings,
            verdicts=verdicts,
            status=status,
        )

    async def persist_metadata(self) -> None:
        """Mirror the current run metadata onto the session."""
        await self._store._persist_metadata(self._session_key, self._run_id)

    async def settle(self, status: ReviewRunStatus) -> ReviewBoardView:
        """Persist the terminal state, then publish ``DONE`` on the board.

        Persistence comes first: a failed save keeps the run ``running`` and
        raises :class:`ReviewBoardPersistenceError`, so the conversation gate
        never opens over a state the process cannot prove.
        """
        return await self._store._settle(self._session_key, self._run_id, status)


# ---------------------------------------------------------------------------
# Mutators — module-level so the store applies them uniformly under its lock
# ---------------------------------------------------------------------------


def _advance(board: ReviewRunBoard, phase: ReviewPhase) -> None:
    if board.status is not ReviewRunStatus.RUNNING:
        return
    board.phase = phase


def _record_input(
    board: ReviewRunBoard,
    plan: "ReviewPlan | None",
    input_fingerprint: str,
    changed_files: list[str],
    skipped_files: list[str],
    evidence_stats: Mapping[str, Any],
    assignment_stats: list[Mapping[str, Any]],
) -> None:
    board.plan = plan
    board.input_fingerprint = input_fingerprint
    board.changed_files = tuple(changed_files)
    board.skipped_files = tuple(skipped_files)
    board.evidence_stats = dict(evidence_stats)
    board.assignment_stats = tuple(dict(entry) for entry in assignment_stats)


def _start_assignments(
    board: ReviewRunBoard, assignments: tuple["ReviewAssignment", ...]
) -> None:
    board.assignments = tuple(assignments)
    for assignment in assignments:
        board.reviewers.setdefault(
            assignment.dimension, ReviewerRunState(dimension=assignment.dimension)
        )


def _reviewer_started(
    board: ReviewRunBoard, dimension: str, evidence_assigned: int
) -> None:
    reviewer = board.reviewers.setdefault(
        dimension, ReviewerRunState(dimension=dimension)
    )
    reviewer.status = CHILD_RUNNING
    reviewer.evidence_assigned = evidence_assigned


def _reviewer_settled(
    board: ReviewRunBoard,
    dimension: str,
    status: str,
    error: str,
    usage: dict[str, Any],
    duplicate_reads: int,
    duplicate_searches: int,
) -> None:
    reviewer = board.reviewers.setdefault(
        dimension, ReviewerRunState(dimension=dimension)
    )
    if reviewer.status != CHILD_COMPLETED:
        reviewer.status = status
        reviewer.error = "" if status == CHILD_COMPLETED else bound_child_error(error)
    # Usage and duplicate counters describe cost and efficiency, not
    # correctness, so they are folded in on success and failure alike.
    reviewer.add_usage(usage)
    reviewer.duplicate_reads = duplicate_reads
    reviewer.duplicate_searches = duplicate_searches


def _record_dimension(board: ReviewRunBoard, result: "ReviewDimensionResult") -> None:
    dimensions = list(board.dimensions)
    for index, existing in enumerate(dimensions):
        if existing.dimension == result.dimension:
            dimensions[index] = result
            board.dimensions = tuple(dimensions)
            return
    dimensions.append(result)
    board.dimensions = tuple(dimensions)


def _judge_started(board: ReviewRunBoard, batch_id: str) -> None:
    batch = board.judge_batches.setdefault(
        batch_id, JudgeBatchState(batch_id=batch_id)
    )
    batch.status = CHILD_RUNNING
    batch.error = ""


def _judge_settled(
    board: ReviewRunBoard,
    batch_id: str,
    status: str,
    error: str,
    stats: dict[str, int],
    usage: dict[str, Any],
) -> None:
    batch = board.judge_batches.setdefault(
        batch_id, JudgeBatchState(batch_id=batch_id)
    )
    batch.status = status
    batch.error = bound_child_error(error)
    batch.stats = dict(stats)
    batch.add_usage(usage)


def _record_judged(
    board: ReviewRunBoard,
    dimensions: tuple["ReviewDimensionResult", ...],
    stats: "ReviewJudgeStats | None",
) -> None:
    board.dimensions = tuple(dimensions)
    board.judge_stats = stats


def _add_warning(board: ReviewRunBoard, message: str) -> None:
    text = str(message).strip()
    if text and text not in board.warnings:
        board.warnings = (*board.warnings, text)


def _record_summary(board: ReviewRunBoard, text: str) -> None:
    board.summary = _summary_text(text)


def _add_usage(board: ReviewRunBoard, usage: dict[str, Any]) -> None:
    merge_token_usage(board.usage, usage)


def _summary_text(text: str, *, limit: int = REVIEW_SUMMARY_MAX_CHARS) -> str:
    return " ".join(str(text or "").split())[:limit]


# ---------------------------------------------------------------------------
# Store — owned by the coordinator
# ---------------------------------------------------------------------------


class ReviewBoardStore:
    """The coordinator's registry of live review blackboards.

    One board per session (a session runs at most one review). The store is the
    only object that mutates a board, and it serializes every write for one run
    with that run's lock.
    """

    def __init__(
        self,
        *,
        sessions: "SessionManager",
        artifacts: "ReviewArtifactStore",
    ) -> None:
        self._sessions = sessions
        self._artifacts = artifacts
        self._boards: dict[str, ReviewRunBoard] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- lifecycle ----------------------------------------------------------

    def create(self, admission: "ReviewAdmission") -> ReviewBoardView:
        """Create the authoritative board for an accepted admission."""
        board = ReviewRunBoard(
            run_id=admission.run_id,
            session_key=admission.session_key,
            input_fingerprint=admission.input_fingerprint,
            snapshot_ref=admission.snapshot_ref,
            plan=admission.plan,
        )
        self._boards[admission.session_key] = board
        self._locks[admission.session_key] = asyncio.Lock()
        logger.info(
            "review.board.created session={} run_id={} snapshot_ref={}",
            admission.session_key,
            admission.run_id,
            admission.snapshot_ref,
        )
        return _project(board)

    def view(self, session_key: str) -> ReviewBoardView | None:
        """Read-only projection of *session_key*'s run, or ``None``."""
        board = self._boards.get(session_key)
        return _project(board) if board is not None else None

    def running(self, session_key: str) -> ReviewBoardView | None:
        """The session's live ``running`` run, or ``None``."""
        view = self.view(session_key)
        if view is None or not view.is_running:
            return None
        return view

    def writer(self, session_key: str) -> ReviewBoardWriter | None:
        """The write handle for *session_key*'s run, or ``None``."""
        board = self._boards.get(session_key)
        if board is None:
            return None
        return ReviewBoardWriter(self, session_key, board.run_id)

    def drop(self, session_key: str) -> ReviewBoardView | None:
        """Drop the session's board and return its last projection."""
        board = self._boards.pop(session_key, None)
        self._locks.pop(session_key, None)
        return _project(board) if board is not None else None

    def reset(self, session_key: str) -> None:
        """Drop the session's run *and* its persisted review metadata (``/new``).

        The board disappears with the process, so the run's identity and
        outcome have to be cleared from the session too: only that makes the
        chat reusable for a new review.
        """
        dropped = self.drop(session_key)
        self.clear_run_metadata(session_key)
        if dropped is not None:
            logger.info(
                "review.board.reset session={} run_id={}",
                session_key,
                dropped.run_id,
            )

    def clear_run_metadata(self, session_key: str) -> None:
        """Remove the run identity and outcome persisted on the session.

        A board that no longer exists cannot be settled, so a session still
        carrying ``RUN_ID``/``STATUS=running`` would refuse every later review
        as a duplicate of a run nothing can finish.
        """
        session = self._sessions.get_or_create(session_key)
        changed = False
        for key in RESET_METADATA_KEYS:
            if key in session.metadata:
                session.metadata.pop(key, None)
                changed = True
        if changed:
            self._sessions.save(session)

    def drop_unstarted(
        self, session_key: str, *, run_id: str
    ) -> ReviewBoardView | None:
        """Drop *run_id*'s board if it never left ``PREPARE``.

        Such a turn failed or was rerouted before preparation, so nothing was
        produced and the gate must not stay closed behind a run that will never
        execute. Returns the dropped view, or ``None`` when the run had already
        started, does not exist, or is not the run being released.

        The run id is required because the ending turn only owns its own run: an
        unscoped drop would delete a review that was admitted for the same
        session while the turn was running and is still waiting in the pending
        queue, and the retried turn would then be refused as a duplicate of a
        run that no longer exists.
        """
        view = self.view(session_key)
        if (
            view is None
            or view.run_id != run_id
            or not view.is_running
            or view.phase is not ReviewPhase.PREPARE
        ):
            return None
        logger.info(
            "review.board.dropped session={} run_id={} reason=never_started",
            session_key,
            view.run_id,
        )
        dropped = self.drop(session_key)
        # The run's identity is also on the session (written at admission).
        # Dropping only the board would leave a persisted ``running`` run that
        # rejects every later review as a duplicate and that no turn can settle.
        try:
            self.clear_run_metadata(session_key)
        except OSError as exc:
            # Called from a turn's ``finally``: a failed cleanup must not mask
            # the exception that ended the turn.
            logger.warning(
                "review.board.release.persist_failed session={} run_id={} reason={}",
                session_key,
                run_id,
                exc,
            )
        return dropped

    # -- writes -------------------------------------------------------------

    async def _mutate(
        self, session_key: str, run_id: str, mutator: Any, *args: Any
    ) -> None:
        board = self._require(session_key, run_id)
        async with self._lock_for(session_key):
            # Re-check under the lock: the run may have been dropped while the
            # writer was waiting for it.
            board = self._require(session_key, run_id)
            mutator(board, *args)

    async def _persist_metadata(self, session_key: str, run_id: str) -> None:
        async with self._lock_for(session_key):
            board = self._require(session_key, run_id)
            view = _project(board)
            session = self._sessions.get_or_create(session_key)
            session.metadata.update(view.metadata_payload())
            self._sessions.save(session)

    async def _record_report(
        self,
        session_key: str,
        run_id: str,
        *,
        report_markdown: str,
        findings: tuple[dict[str, Any], ...],
        verdicts: list[dict[str, Any]],
        status: ReviewRunStatus = ReviewRunStatus.COMPLETED,
    ) -> str | None:
        async with self._lock_for(session_key):
            board = self._require(session_key, run_id)
            board.findings = findings
            board.summary = _summary_text(report_markdown)
            report_ref = self._artifacts.write(
                build_report_artifact(
                    _project(board),
                    report_markdown=report_markdown,
                    verdicts=verdicts,
                    status=status,
                )
            )
            if report_ref is None:
                _add_warning(board, "Failed to persist the review report artifact.")
            else:
                board.report_ref = report_ref
                logger.info(
                    "review.artifact.persisted session={} run_id={} ref={}",
                    session_key,
                    run_id,
                    report_ref,
                )
            return report_ref

    async def _settle(
        self, session_key: str, run_id: str, status: ReviewRunStatus
    ) -> ReviewBoardView:
        """Persist the terminal state first, then publish ``DONE``."""
        async with self._lock_for(session_key):
            board = self._require(session_key, run_id)
            if board.status is not ReviewRunStatus.RUNNING:
                return _project(board)
            if status is not ReviewRunStatus.COMPLETED:
                _settle_unfinished_children(board, status)
            view = _project(board)
            payload = view.metadata_payload(status=status, phase=ReviewPhase.DONE)
            session = self._sessions.get_or_create(session_key)
            previous = {
                key: session.metadata.get(key, _ABSENT) for key in payload
            }
            session.metadata.update(payload)
            try:
                self._sessions.save(session)
            except Exception as exc:
                for key, value in previous.items():
                    if value is _ABSENT:
                        session.metadata.pop(key, None)
                    else:
                        session.metadata[key] = value
                reason = bound_child_error(
                    "the terminal review state could not be persisted"
                    f" ({type(exc).__name__}: {exc})"
                )
                _add_warning(board, reason)
                logger.warning(
                    "review.board.settle.persist_failed session={} run_id={}",
                    session_key,
                    run_id,
                    exc_info=True,
                )
                raise ReviewBoardPersistenceError(reason) from exc
            board.phase = ReviewPhase.DONE
            board.status = status
            logger.info(
                "review.board.settled session={} run_id={} status={} phase={} "
                "report_ref={}",
                session_key,
                run_id,
                status.value,
                board.phase.value,
                board.report_ref,
            )
            return _project(board)

    # -- internals ----------------------------------------------------------

    def _require(self, session_key: str, run_id: str) -> ReviewRunBoard:
        board = self._boards.get(session_key)
        if board is None:
            raise ReviewBoardError(
                f"no live review board for session {session_key!r}"
            )
        if board.run_id != run_id:
            raise ReviewBoardError(
                f"review board run mismatch for session {session_key!r}"
            )
        return board

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        lock = self._locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_key] = lock
        return lock


#: Placeholder for "key absent" when snapshotting session metadata before a
#: terminal write, so a failed save can restore the exact previous state.
_ABSENT = object()


def _settle_unfinished_children(
    board: ReviewRunBoard, status: ReviewRunStatus
) -> None:
    """Settle work still in flight so the audit trail has no live children."""
    child_status = CHILD_STOPPED if status is ReviewRunStatus.STOPPED else CHILD_ERROR
    child_reason = (
        "review stopped before the run finished"
        if status is ReviewRunStatus.STOPPED
        else "review failed before the run finished"
    )
    for reviewer in board.reviewers.values():
        if reviewer.status in (CHILD_PENDING, CHILD_RUNNING):
            reviewer.status = child_status
            reviewer.error = child_reason
    for batch in board.judge_batches.values():
        if batch.status in (CHILD_PENDING, CHILD_RUNNING):
            batch.status = child_status
            batch.error = child_reason


def _project(board: ReviewRunBoard) -> ReviewBoardView:
    """Freeze a board into a read-only view."""
    return ReviewBoardView(
        run_id=board.run_id,
        session_key=board.session_key,
        input_fingerprint=board.input_fingerprint,
        snapshot_ref=board.snapshot_ref,
        phase=board.phase,
        status=board.status,
        plan=board.plan,
        assignments=board.assignments,
        reviewers=tuple(
            reviewer.snapshot() for reviewer in board.reviewers.values()
        ),
        dimensions=board.dimensions,
        judge_stats=board.judge_stats,
        judge_batches=tuple(
            batch.snapshot() for batch in board.judge_batches.values()
        ),
        findings=board.findings,
        report_ref=board.report_ref,
        summary=board.summary,
        usage=dict(board.usage),
        warnings=board.warnings,
        changed_files=board.changed_files,
        skipped_files=board.skipped_files,
        evidence_stats=dict(board.evidence_stats),
        assignment_stats=board.assignment_stats,
    )


__all__ = [
    "CHILD_COMPLETED",
    "CHILD_ERROR",
    "CHILD_PENDING",
    "CHILD_RUNNING",
    "CHILD_STOPPED",
    "JudgeBatchSnapshot",
    "JudgeBatchState",
    "ReviewBoardError",
    "ReviewBoardPersistenceError",
    "ReviewBoardStore",
    "ReviewBoardView",
    "ReviewBoardWriter",
    "ReviewRunBoard",
    "ReviewerRunState",
    "ReviewerSnapshot",
]
