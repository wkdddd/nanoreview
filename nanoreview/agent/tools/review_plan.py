"""Structured coordinator-plan submission for program-controlled reviews."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nanoreview.agent.tools.base import Tool, tool_parameters
from nanoreview.agent.tools.schema import ArraySchema, ObjectSchema, StringSchema
from nanoreview.review.types import ReviewAssignment, normalize_review_dimension


class _AssignmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    dimension: str = Field(min_length=1, max_length=64)
    focus: str = Field(min_length=1, max_length=1_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class _PlanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    assignments: list[_AssignmentPayload] = Field(default_factory=list, max_length=10)


@dataclass(slots=True)
class ReviewPlanReceiver:
    """Validates the sole coordinator deliverable for one review attempt."""

    allowed_dimensions: set[str]
    evidence_ids: set[str]
    routing_mode: str = "auto"
    submission: tuple[ReviewAssignment, ...] | None = None

    def submit(self, assignments: list[dict]) -> tuple[bool, str]:
        try:
            payload = _PlanPayload.model_validate({"assignments": assignments})
        except ValidationError as exc:
            return False, f"invalid review plan: {exc.errors(include_url=False)}"

        normalized: list[ReviewAssignment] = []
        seen_dimensions: set[str] = set()
        for item in payload.assignments:
            dimension = normalize_review_dimension(item.dimension)
            if dimension is None or dimension not in self.allowed_dimensions:
                return False, f"invalid review plan: disallowed dimension {item.dimension!r}"
            if dimension in seen_dimensions:
                return False, f"invalid review plan: duplicate dimension {dimension!r}"
            seen_dimensions.add(dimension)
            evidence_ids = tuple(item.evidence_ids)
            if not evidence_ids:
                return False, (
                    f"invalid review plan: {dimension!r} requires a non-empty evidence_ids "
                    "list referencing authorized evidence IDs"
                )
            if len(set(evidence_ids)) != len(evidence_ids):
                return False, f"invalid review plan: duplicate evidence IDs for {dimension!r}"
            unknown = sorted(set(evidence_ids) - self.evidence_ids)
            if unknown:
                return False, f"invalid review plan: unknown evidence IDs {unknown}"
            normalized.append(
                ReviewAssignment(
                    dimension=dimension,
                    focus=item.focus.strip(),
                    evidence_ids=evidence_ids,
                )
            )
        if self.routing_mode == "auto" and not normalized:
            return False, "invalid review plan: auto must select between 1 and 4 reviewers"
        if len(normalized) > 4:
            return False, "invalid review plan: at most 4 reviewers may be selected"
        if self.routing_mode == "explicit" and seen_dimensions != self.allowed_dimensions:
            missing = sorted(self.allowed_dimensions - seen_dimensions)
            unexpected = sorted(seen_dimensions - self.allowed_dimensions)
            return False, (
                "invalid review plan: explicit assignments must exactly cover requested dimensions; "
                f"missing={missing}, unexpected={unexpected}"
            )
        self.submission = tuple(normalized)
        return True, "review plan accepted"


_ASSIGNMENT_SCHEMA = ObjectSchema(
    {
        "dimension": StringSchema("Required review dimension key.", min_length=1),
        "focus": StringSchema("Concrete risk or interaction to investigate.", min_length=1),
        "evidence_ids": ArraySchema(
            StringSchema("Authorized evidence ID, such as ev-001.", min_length=1),
            description=(
                "Non-empty list of authorized evidence IDs for this dimension. "
                "IDs must come from the Authorized Evidence manifest."
            ),
            min_items=1,
            max_items=20,
        ),
    },
    required=["dimension", "focus", "evidence_ids"],
    additional_properties=False,
)


@tool_parameters(
    ObjectSchema(
        {
            "assignments": ArraySchema(
                _ASSIGNMENT_SCHEMA,
                description="Reviewer assignments selected for this review.",
                max_items=4,
            ),
        },
        required=["assignments"],
        additional_properties=False,
    ).to_json_schema()
)
class SubmitReviewPlanTool(Tool):
    """Capture a validated review plan without starting subagents."""

    _plugin_discoverable = False

    def __init__(self, receiver: ReviewPlanReceiver) -> None:
        self._receiver = receiver

    @property
    def name(self) -> str:
        return "submit_review_plan"

    @property
    def description(self) -> str:
        return "Submit the structured coordinator plan for program-controlled code review dispatch."

    async def execute(self, assignments: list[dict], **_: object) -> str:
        accepted, detail = self._receiver.submit(assignments)
        return detail if accepted else f"Error: {detail}"
