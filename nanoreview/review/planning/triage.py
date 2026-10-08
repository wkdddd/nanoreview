"""Planner triage contract: decisions in, program-aggregated assignments out.

The planner's only job is pre-review triage: read the frozen diff evidence and
say, per evidence unit, how risky it looks and which reviewer dimensions should
look at it. It never maintains an assignment list — this module aggregates the
submitted decisions into one :class:`~nanoreview.review.types.ReviewAssignment`
per chosen dimension, so the planner cannot dispatch a dimension the rules
disallow. A dimension the user pinned in ``special``/``general`` mode cannot be
silently dropped either: ``finish`` is rejected until a decision names it.

Coverage is derived here, not reported by the model: an evidence unit is
``assigned`` when at least one decision claimed it with a dimension,
``dismissed`` when a decision explicitly judged it low risk with no dimensions,
and ``unexamined`` when no decision ever mentioned it. Nothing is auto-dispatched
from an unexamined unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nanoreview.review.types import (
    MAX_TRIAGE_DECISIONS,
    REVIEW_RISK_LEVELS,
    TRIAGE_ASSIGNED,
    TRIAGE_DISMISSED,
    TRIAGE_UNEXAMINED,
    ReviewAssignment,
    ReviewEvidenceTriage,
    ReviewTriageDecision,
    ReviewTriageSummary,
    normalize_review_dimension,
)

#: Focus text kept per dimension when several decisions target it; bounded so a
#: long planner rationale cannot inflate the reviewer task.
_MAX_FOCUS_CHARS = 1_000
#: Rationale bound for the run audit trail.
_MAX_RATIONALE_CHARS = 600


@dataclass(slots=True)
class TriageReceiver:
    """Accumulates planner triage decisions for one review run.

    The planner may call ``submit_review_decision`` any number of times (bounded
    by ``max_decisions``) and must then call ``finish_review_triage``. Every
    rejected submission returns a concrete reason the model can correct inside
    the same AgentRun: unknown evidence IDs, duplicate coverage, an illegal
    dimension for the mode, or a non-low risk level with no dimensions.
    """

    allowed_dimensions: set[str]
    evidence_ids: set[str]
    mode: str = "auto"
    evidence_paths: dict[str, str] = field(default_factory=dict)
    #: Authorized evidence IDs in their frozen manifest order, so coverage
    #: records (unexamined/dismissed) are stable across runs.
    ordered_evidence_ids: tuple[str, ...] = ()
    #: Dimensions the user pinned in ``special``/``general`` mode. Each one must
    #: be named by at least one decision before ``finish`` succeeds, so a user
    #: selection can never be silently dropped; the program enforces that by
    #: rejecting the finish, never by widening a reviewer's evidence scope.
    required_dimensions: tuple[str, ...] = ()
    max_decisions: int = MAX_TRIAGE_DECISIONS
    decisions: list[ReviewTriageDecision] = field(default_factory=list)
    submitted_ids: set[str] = field(default_factory=set)
    finished: bool = False
    error: str = ""

    def __post_init__(self) -> None:
        if not self.ordered_evidence_ids:
            self.ordered_evidence_ids = tuple(sorted(self.evidence_ids))

    # -- submission ---------------------------------------------------------

    def submit(
        self,
        *,
        evidence_ids: list[str],
        risk_level: str,
        dimensions: list[str] | None = None,
        focus: str = "",
        rationale: str = "",
    ) -> tuple[bool, str]:
        """Validate and accumulate one triage decision."""
        if self.finished:
            return False, "invalid triage decision: triage is already finished"
        if len(self.decisions) >= self.max_decisions:
            return False, (
                f"invalid triage decision: at most {self.max_decisions} decisions "
                "are accepted per review"
            )

        ids = [str(item).strip() for item in (evidence_ids or []) if str(item).strip()]
        if not ids:
            return False, (
                "invalid triage decision: evidence_ids must name at least one "
                "authorized evidence ID"
            )
        if len(set(ids)) != len(ids):
            return False, "invalid triage decision: duplicate evidence IDs in one decision"
        unknown = sorted(set(ids) - self.evidence_ids)
        if unknown:
            return False, f"invalid triage decision: unknown evidence IDs {unknown}"
        already = sorted(set(ids) & self.submitted_ids)
        if already:
            return False, (
                "invalid triage decision: evidence already triaged in an earlier "
                f"decision {already}; submit one decision per evidence unit"
            )

        level = str(risk_level or "").strip().lower()
        if level not in REVIEW_RISK_LEVELS:
            return False, (
                "invalid triage decision: risk_level must be one of "
                f"{', '.join(REVIEW_RISK_LEVELS)}"
            )

        normalized: list[str] = []
        for raw_dimension in dimensions or []:
            dimension = normalize_review_dimension(raw_dimension)
            if dimension is None or dimension not in self.allowed_dimensions:
                return False, (
                    f"invalid triage decision: disallowed dimension {raw_dimension!r}; "
                    f"allowed dimensions: {', '.join(sorted(self.allowed_dimensions))}"
                )
            if dimension not in normalized:
                normalized.append(dimension)

        if not normalized and level != "low":
            return False, (
                "invalid triage decision: only risk_level='low' may omit dimensions; "
                "this evidence is "
                f"{level!r} so it must name at least one dimension from "
                f"{', '.join(sorted(self.allowed_dimensions))}"
            )

        text = str(focus or "").strip()
        if normalized and not text:
            return False, (
                "invalid triage decision: focus must state the concrete risk or "
                "interaction the assigned reviewers should investigate"
            )

        self.decisions.append(
            ReviewTriageDecision(
                evidence_ids=tuple(ids),
                risk_level=level,
                dimensions=tuple(normalized),
                focus=text[:_MAX_FOCUS_CHARS],
                rationale=str(rationale or "").strip()[:_MAX_RATIONALE_CHARS],
            )
        )
        self.submitted_ids.update(ids)
        return True, (
            f"triage decision accepted ({len(self.decisions)}/"
            f"{self.max_decisions} decisions, {len(self.submitted_ids)}/"
            f"{len(self.evidence_ids)} evidence triaged)"
        )

    def finish(self) -> tuple[bool, str]:
        """Mark triage complete; the terminal tool calls this exactly once.

        In ``special`` mode the user's selection is a hard requirement, so
        finishing with an unreported pinned dimension is rejected: the planner
        must triage evidence for every dimension the user asked for.
        """
        if self.finished:
            return False, "invalid triage: finish_review_triage was already called"
        reported = {
            dimension
            for decision in self.decisions
            for dimension in decision.dimensions
        }
        missing = [
            dimension
            for dimension in self.required_dimensions
            if dimension not in reported
        ]
        if missing:
            return False, (
                "invalid triage: the user selected these dimensions and each one "
                f"needs at least one decision naming it: {', '.join(missing)}"
            )
        self.finished = True
        assigned = len(self.assigned_ids())
        return True, (
            f"review triage finished: {len(self.decisions)} decision(s) over "
            f"{len(self.submitted_ids)}/{len(self.evidence_ids)} evidence unit(s); "
            f"{assigned} evidence unit(s) assigned to reviewers, "
            f"{len(self.evidence_ids) - len(self.submitted_ids)} left unexamined"
        )

    # -- aggregation --------------------------------------------------------

    def assignments(self) -> tuple[ReviewAssignment, ...]:
        """One assignment per selected dimension, in stable dimension order.

        Evidence IDs are collected per dimension in submission order so the
        reviewer sees a deterministic scope. A low-risk decision contributes no
        assignment and is recorded as dismissed instead.

        Every dimension that receives an assignment is one the planner named in
        a decision, or — for ``special``/``general`` — one the user pinned and
        that :meth:`finish` already proved was named. The program therefore
        never substitutes a dimension the planner did not choose: a user
        selection is enforced by rejecting the finish, not by silently widening
        the reviewer's scope.
        """
        by_dimension: dict[str, list[str]] = {}
        focus_by_dimension: dict[str, list[str]] = {}
        for decision in self.decisions:
            for dimension in decision.dimensions:
                ids = by_dimension.setdefault(dimension, [])
                for evidence_id in decision.evidence_ids:
                    if evidence_id not in ids:
                        ids.append(evidence_id)
                if decision.focus and decision.focus not in focus_by_dimension.setdefault(
                    dimension, []
                ):
                    focus_by_dimension[dimension].append(decision.focus)
        assignments: list[ReviewAssignment] = []
        for dimension in sorted(by_dimension):
            focus = " ".join(focus_by_dimension.get(dimension, []))[:_MAX_FOCUS_CHARS]
            assignments.append(
                ReviewAssignment(
                    dimension=dimension,
                    focus=focus or f"Triage assigned {dimension} review.",
                    evidence_ids=tuple(by_dimension[dimension]),
                    source="user" if dimension in self.required_dimensions else "planner",
                )
            )
        return tuple(assignments)

    def assigned_ids(self) -> tuple[str, ...]:
        """Evidence IDs that reach at least one dispatched reviewer.

        Derived from the aggregated assignments rather than from the raw
        decisions, so a user-pinned dimension that the planner never reported
        still counts the evidence it was dispatched over as covered.
        """
        seen: list[str] = []
        for assignment in self.assignments():
            for evidence_id in assignment.evidence_ids:
                if evidence_id not in seen:
                    seen.append(evidence_id)
        return tuple(seen)

    def dismissed_ids(self) -> tuple[str, ...]:
        """Evidence IDs the planner explicitly judged low risk with no dimension.

        Only evidence that no assignment covered can be dismissed: an explicit
        low-risk verdict never overrides a dimension that is actually running.
        """
        assigned = set(self.assigned_ids())
        seen: list[str] = []
        for decision in self.decisions:
            if decision.dimensions:
                continue
            for evidence_id in decision.evidence_ids:
                if evidence_id not in seen and evidence_id not in assigned:
                    seen.append(evidence_id)
        return tuple(seen)

    def summary(self) -> ReviewTriageSummary:
        """Program-derived triage record for run state, snapshot and audit."""
        assigned = set(self.assigned_ids())
        dismissed = set(self.dismissed_ids())
        triage: list[ReviewEvidenceTriage] = []
        for decision in self.decisions:
            for evidence_id in decision.evidence_ids:
                if evidence_id in assigned:
                    status = TRIAGE_ASSIGNED
                elif evidence_id in dismissed:
                    status = TRIAGE_DISMISSED
                else:  # pragma: no cover - defensive, one of the two always holds
                    status = TRIAGE_UNEXAMINED
                triage.append(
                    ReviewEvidenceTriage(
                        evidence_id=evidence_id,
                        path=self.evidence_paths.get(evidence_id, ""),
                        status=status,
                        risk_level=decision.risk_level,
                        dimensions=tuple(decision.dimensions),
                    )
                )
        ordered_ids = [str(item) for item in self.ordered_evidence_ids]
        return ReviewTriageSummary(
            mode=self.mode,
            decisions=tuple(self.decisions),
            triage=tuple(triage),
            unexamined_ids=tuple(
                evidence_id
                for evidence_id in ordered_ids
                if evidence_id not in assigned and evidence_id not in dismissed
            ),
            dismissed_ids=tuple(
                evidence_id for evidence_id in ordered_ids if evidence_id in dismissed
            ),
            no_assignments=not self.assignments(),
            error=self.error,
        )
