"""Structured output contracts of the three review roles.

Review agents no longer submit through a terminal tool. Each role returns one
final JSON object as its answer, and the program validates it here:

* Planner -> every triage decision in one payload (business validation is the
  existing :class:`~nanoreview.review.planning.triage.TriageReceiver`, so
  coverage, duplicate-evidence and pinned-dimension rules are unchanged).
* Reviewer -> ``{"findings": [...]}`` (the finding field contract, reused from
  the old submission tool).
* Judge -> ``{"verdicts": [...]}`` for the candidates of one batch.

Validators are pure: they never touch the blackboard and never accumulate
state across attempts. A reused validator therefore cannot turn a rejected
attempt into an accepted result on the next try.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nanoreview.agent.output_contract import OutputContract

if TYPE_CHECKING:
    from nanoreview.review.types import (
        ReviewAssignment,
        ReviewTriageSummary,
    )

_SEVERITIES = frozenset(("critical", "high", "medium", "low"))
_REQUIRED_FINDING_FIELDS = (
    "severity",
    "file",
    "line",
    "title",
    "evidence",
    "impact",
    "recommendation",
    "details",
)
_FINDING_TEXT_FIELDS = ("file", "title", "evidence", "impact", "recommendation")

#: Fence languages stripped before parsing. A fenced block is accepted because
#: it is still a single JSON value; prose around it is not.
_FENCE_PREFIXES = ("```json", "```JSON", "```")

_PLANNER_SHAPE = (
    '{"decisions":[{"evidence_ids":["ev-001"],"risk_level":"low|medium|high",'
    '"dimensions":["bug"],"focus":"...","rationale":"..."}]}'
)
_REVIEWER_SHAPE = '{"findings":[{"severity":"high","file":"a.py","line":12,...}]}'
_JUDGE_SHAPE = '{"verdicts":[{"id":"<candidate id>","decision":"accept|reject|needs_confirmation","reason":"...","confidence":"high"}]}'


class StructuredOutputError(ValueError):
    """Raised when a model answer is not one usable JSON object."""


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse *text* as exactly one JSON object.

    A single markdown fence around the object is tolerated (models habitually
    wrap JSON); anything else — prose, a truncated object, a JSON array, a
    second value — is rejected so a cut-off answer can never read as success.
    """
    raw = (text or "").strip()
    if not raw:
        raise StructuredOutputError("the final answer was empty")
    for fence in _FENCE_PREFIXES:
        if raw.startswith(fence):
            raw = raw[len(fence) :].lstrip()
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
            break
    if not raw.startswith("{"):
        raise StructuredOutputError(
            "the final answer must be one JSON object, not prose"
        )
    if not raw.endswith("}"):
        raise StructuredOutputError(
            "the final answer ended before the JSON object was complete"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"invalid JSON ({exc.msg})") from exc
    if not isinstance(payload, dict):
        raise StructuredOutputError(
            f"the final answer must be a JSON object, got {type(payload).__name__}"
        )
    return payload


@dataclass(frozen=True, slots=True)
class PlannerDecision:
    """One raw triage decision as returned by the planner."""

    evidence_ids: tuple[str, ...]
    risk_level: str
    dimensions: tuple[str, ...]
    focus: str
    rationale: str


@dataclass(frozen=True, slots=True)
class PlannerTriageOutput:
    """Accepted planner result: the assignments and audit derived from it."""

    decisions: tuple[PlannerDecision, ...]
    assignments: tuple[ReviewAssignment, ...]
    summary: ReviewTriageSummary


class _DecisionPayload(BaseModel):
    """Shape contract of one planner triage decision.

    Pydantic enforces the structural bounds (list sizes, string lengths, no
    extra keys) and the triage receiver enforces the review semantics
    (authorized IDs, allowed dimensions, one decision per evidence unit), so a
    rejected payload names exactly which rule it broke.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    risk_level: str = Field(min_length=1, max_length=16)
    dimensions: list[str] = Field(default_factory=list, max_length=4)
    focus: str = Field(default="", max_length=1_000)
    rationale: str = Field(default="", max_length=600)


def planner_contract(
    receiver_factory: Callable[[], Any],
) -> OutputContract:
    """Build the planner contract around a fresh triage receiver per attempt."""

    def _parse(text: str) -> tuple[PlannerTriageOutput | None, str | None]:
        try:
            payload = parse_json_object(text)
        except StructuredOutputError as exc:
            return None, str(exc)
        raw_decisions = payload.get("decisions")
        if not isinstance(raw_decisions, list):
            return None, "decisions must be an array"
        receiver = receiver_factory()
        decisions: list[PlannerDecision] = []
        for index, item in enumerate(raw_decisions):
            path = f"decisions[{index}]"
            if not isinstance(item, dict):
                return None, f"{path} must be an object"
            try:
                decision_payload = _DecisionPayload.model_validate(item)
            except ValidationError as exc:
                return None, (
                    f"{path} is malformed: "
                    f"{exc.errors(include_url=False)}"
                )
            accepted, detail = receiver.submit(
                evidence_ids=list(decision_payload.evidence_ids),
                risk_level=decision_payload.risk_level,
                dimensions=list(decision_payload.dimensions),
                focus=decision_payload.focus,
                rationale=decision_payload.rationale,
            )
            if not accepted:
                return None, f"{path}: {detail}"
            decisions.append(
                PlannerDecision(
                    evidence_ids=tuple(decision_payload.evidence_ids),
                    risk_level=decision_payload.risk_level.strip().lower(),
                    dimensions=tuple(decision_payload.dimensions),
                    focus=decision_payload.focus,
                    rationale=decision_payload.rationale,
                )
            )
        finished, detail = receiver.finish()
        if not finished:
            return None, detail
        return (
            PlannerTriageOutput(
                decisions=tuple(decisions),
                assignments=receiver.assignments(),
                summary=receiver.summary(),
            ),
            None,
        )

    return OutputContract(
        name="planner_triage",
        parse=_parse,
        shape=_PLANNER_SHAPE,
        extra_instructions=(
            "Triage every evidence unit you were shown exactly once; a decision "
            "may name several evidence units and several dimensions."
        ),
    )


#: Problems reported in one correction message. A rejected payload only has a
#: couple of correction attempts, so every independent problem is reported in a
#: single bounded message instead of one per round.
_MAX_VALIDATION_PROBLEMS = 5
_MAX_VALIDATION_MESSAGE_CHARS = 1_500


def _validation_message(problems: list[str]) -> str:
    """One bounded corrective message carrying every problem found."""
    shown = problems[:_MAX_VALIDATION_PROBLEMS]
    text = "; ".join(shown)
    hidden = len(problems) - len(shown)
    if hidden > 0:
        text = f"{text}; (+{hidden} more problem(s))"
    return text[:_MAX_VALIDATION_MESSAGE_CHARS]


def validate_reviewer_findings(
    findings: Any,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Normalize and validate a reviewer's ``findings`` array.

    Returns ``(findings, None)`` when every entry is well formed, or
    ``(None, error)`` with every concrete field problem otherwise. An empty
    array is legal here: whether "no findings" is meaningful depends on whether
    the reviewer had authorized evidence, which the caller checks.
    """
    if not isinstance(findings, list):
        return None, f"findings must be an array, got {type(findings).__name__}"
    normalized: list[dict[str, Any]] = []
    problems: list[str] = []
    for index, raw in enumerate(findings):
        path = f"findings[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{path} must be an object")
            continue
        missing = [name for name in _REQUIRED_FINDING_FIELDS if name not in raw]
        if missing:
            problems.append(f"{path} missing required fields: {', '.join(missing)}")
            continue
        severity = str(raw.get("severity", "")).strip().lower()
        if severity not in _SEVERITIES:
            problems.append(f"{path}.severity must be one of {sorted(_SEVERITIES)}")
            continue
        line = raw.get("line")
        if line is not None:
            if isinstance(line, bool) or not isinstance(line, int):
                problems.append(f"{path}.line must be an integer or null")
                continue
            if line < 1:
                problems.append(f"{path}.line must be >= 1")
                continue
        details = raw.get("details")
        if not isinstance(details, dict):
            problems.append(f"{path}.details must be an object")
            continue
        finding = {
            "severity": severity,
            "file": str(raw.get("file", "")).strip(),
            "line": line,
            "title": str(raw.get("title", "")).strip(),
            "evidence": str(raw.get("evidence", "")).strip(),
            "impact": str(raw.get("impact", "")).strip(),
            "recommendation": str(raw.get("recommendation", "")).strip(),
            "details": details,
        }
        empty = [name for name in _FINDING_TEXT_FIELDS if not finding[name]]
        if empty:
            problems.append(f"{path} has empty required fields: {', '.join(empty)}")
            continue
        normalized.append(finding)
    if problems:
        return None, _validation_message(problems)
    return normalized, None


def reviewer_contract(dimension: str) -> OutputContract:
    """Build the reviewer contract for one dimension."""

    def _parse(text: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            payload = parse_json_object(text)
        except StructuredOutputError as exc:
            return None, str(exc)
        findings, error = validate_reviewer_findings(payload.get("findings"))
        if error:
            return None, error
        return {"dimension": dimension, "findings": findings}, None

    return OutputContract(
        name=f"reviewer.{dimension}",
        parse=_parse,
        shape=_REVIEWER_SHAPE,
        extra_instructions="Use an empty findings array when no issue is confirmed.",
    )


def judge_contract() -> OutputContract:
    """Build the judge contract for one candidate batch."""
    # Imported here rather than at module scope: ``review.types`` imports
    # ``review.profiles``, which imports this module, so a module-level import
    # would close a cycle.
    from nanoreview.review.types import ReviewJudgeDecision, ReviewJudgeVerdict

    decisions = tuple(decision.value for decision in ReviewJudgeDecision)

    def _parse(
        text: str,
    ) -> tuple[dict[str, dict[str, ReviewJudgeVerdict]] | None, str | None]:
        try:
            payload = parse_json_object(text)
        except StructuredOutputError as exc:
            return None, str(exc)
        verdicts = payload.get("verdicts")
        if not isinstance(verdicts, list):
            return None, "verdicts must be an array"
        normalized: dict[str, ReviewJudgeVerdict] = {}
        for index, item in enumerate(verdicts):
            path = f"verdicts[{index}]"
            if not isinstance(item, dict):
                return None, f"{path} must be an object"
            candidate_id = str(item.get("id", "")).strip().lower()
            if not candidate_id:
                return None, f"{path}.id must be a non-empty string"
            try:
                decision = ReviewJudgeDecision(
                    str(item.get("decision", "")).strip().lower()
                )
            except ValueError:
                return None, f"{path}.decision must be one of {list(decisions)}"
            severity = item.get("severity")
            normalized[candidate_id] = ReviewJudgeVerdict(
                decision=decision,
                reason=str(item.get("reason", "")),
                confidence=str(item.get("confidence", "medium")),
                severity=str(severity).lower() if severity else None,
            )
        return {"verdicts": normalized}, None

    return OutputContract(
        name="judge_verdicts",
        parse=_parse,
        shape=_JUDGE_SHAPE,
        extra_instructions=(
            "Return one verdict per candidate id given in the batch, copying "
            "each id verbatim; when the evidence cannot settle a candidate, "
            "answer needs_confirmation rather than omitting it, and never "
            "invent an id."
        ),
    )


__all__ = [
    "PlannerDecision",
    "PlannerTriageOutput",
    "StructuredOutputError",
    "judge_contract",
    "parse_json_object",
    "planner_contract",
    "reviewer_contract",
    "validate_reviewer_findings",
]
