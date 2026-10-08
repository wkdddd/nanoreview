"""Planner triage tools: evidence decisions in, one explicit finish.

The planner never submits assignments. It reads the frozen diff evidence and
records risk decisions with ``submit_review_decision``; the program aggregates
those decisions into reviewer assignments (see
:mod:`nanoreview.review.planning.triage`). ``finish_review_triage`` is the sole
terminal tool, so a run that never finishes cannot be mistaken for a planner
that decided no reviewer was needed.

``list_review_diff``/``read_review_diff`` expose only the evidence the program
already built from the frozen diff. They cannot reach an arbitrary path, create
evidence, or read repository code outside the admitted change.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nanoreview.agent.tools.base import Tool, tool_parameters
from nanoreview.agent.tools.schema import (
    ArraySchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
)
from nanoreview.review.planning.triage import TriageReceiver
from nanoreview.review.types import REVIEW_RISK_LEVELS

#: Hard cap on evidence units returned by one ``list_review_diff`` call.
MAX_DIFF_PAGE_ITEMS = 12
#: Ceiling on the content of one ``read_review_diff`` call, in chars. The
#: effective budget is ``min(this, the run's max_tool_result_chars)`` so a page
#: the reader reports is never truncated afterwards by AgentRunner. There is no
#: separate per-unit cap: clipping one unit below the page budget would show a
#: head-only patch that looks complete.
MAX_DIFF_READ_CHARS = 24_000
#: Chars reserved for the trailing "these IDs were not shown" note so it can
#: never push the assembled page over budget and trigger a blind clip.
_DROPPED_NOTE_RESERVE = 400


class _DecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    risk_level: str = Field(min_length=1, max_length=16)
    dimensions: list[str] = Field(default_factory=list, max_length=4)
    focus: str = Field(default="", max_length=1_000)
    rationale: str = Field(default="", max_length=600)


@dataclass(frozen=True, slots=True)
class ReviewDiffUnit:
    """One planner-readable diff evidence unit (authorized content only)."""

    id: str
    path: str
    start_line: int | None
    end_line: int | None
    kind: str
    token_count: int
    preview: str
    preview_coverage: str
    excerpt: str

    def header(self, index: int | None = None) -> str:
        prefix = f"{index}. " if index is not None else ""
        location = self.path
        if self.start_line is not None:
            location += f":{self.start_line}"
            if self.end_line is not None and self.end_line != self.start_line:
                location += f"-{self.end_line}"
        return (
            f"{prefix}{self.id} {location} [{self.kind}] tokens={self.token_count or '?'}"
        )


@dataclass(frozen=True, slots=True)
class ReviewDiffReader:
    """Read-only window over the frozen diff evidence the program built.

    The reader never touches the filesystem: every response is rendered from the
    immutable evidence set handed to it, so a planner tool call cannot escape the
    admitted diff or invent evidence.

    ``max_result_chars`` is the runner's per-tool-result budget. Every response
    is kept inside it, because ``AgentRunner`` truncates an over-budget tool
    result with a plain "... (truncated)" suffix — a reader that returned more
    would have the tail of a diff page silently dropped *after* the reader had
    already reported that evidence as read.
    """

    units: tuple[ReviewDiffUnit, ...]
    max_result_chars: int = MAX_DIFF_READ_CHARS

    def by_id(self) -> dict[str, ReviewDiffUnit]:
        return {unit.id: unit for unit in self.units}

    def index_text(self, *, offset: int = 0, limit: int = MAX_DIFF_PAGE_ITEMS) -> str:
        total = len(self.units)
        if total == 0:
            return "No authorized diff evidence is available for this review."
        start = max(0, offset)
        page = self.units[start : start + limit]
        if not page:
            return (
                f"Error: offset {offset} is past the end of the evidence index "
                f"({total} evidence unit(s))."
            )
        lines = [
            f"Authorized diff evidence index ({start + 1}-{start + len(page)} of {total}):"
        ]
        lines.extend(unit.header() for unit in page)
        if start + len(page) < total:
            lines.append(
                f"Call list_review_diff with offset={start + len(page)} for the next page."
            )
        return self._bounded("\n".join(lines))

    def read_text(self, evidence_ids: list[str], *, limit: int | None = None) -> str:
        """Render the requested evidence units, honestly bounded.

        Every unit requested is served, bounded by ``max_result_chars`` rather
        than by a hidden per-call ID count: a caller that names eight IDs and
        receives four would otherwise triage four units it never read. When one
        unit alone exceeds the budget its body is clipped **with an in-body
        marker**, because a unit shown head-only looks identical to a complete
        patch and would be judged on partial content.
        """
        if not evidence_ids:
            return "Error: evidence_ids must name at least one authorized evidence ID."
        known = self.by_id()
        unknown = [item for item in evidence_ids if item not in known]
        if unknown:
            return (
                "Error: unknown evidence IDs "
                f"{unknown}. Call list_review_diff to see the authorized evidence IDs."
            )
        selected = [known[item] for item in evidence_ids]
        dropped: list[str] = []
        if limit is not None and limit > 0 and len(selected) > limit:
            dropped = [unit.id for unit in selected[limit:]]
            selected = selected[:limit]
        # Reserve room for the "not shown" note up front. It is appended after
        # the loop, so without this reservation the final page would overflow
        # and the page-level clip would cut the *first* unit's own honest
        # marker — exactly the silent-partial-read this reader must never emit.
        # The reserve only applies when it actually fits the budget; below that
        # the note is omitted rather than squeezing every unit to nothing.
        reserve = min(_DROPPED_NOTE_RESERVE, max(0, self.max_result_chars // 2))
        cap = max(1, self.max_result_chars - reserve)
        blocks: list[str] = []
        used = 0
        for index, unit in enumerate(selected):
            header = f"## {unit.header()}"
            joiner = 2 if blocks else 0
            remaining = cap - used - joiner - len(header) - 1
            body, _clipped = self._unit_body(unit, remaining)
            block = f"{header}\n{body}"
            if blocks and used + joiner + len(block) > cap:
                dropped.extend(item.id for item in selected[index:])
                break
            # A block that cannot fit even alone (budget smaller than its own
            # header plus marker) is dropped rather than emitted and later
            # clipped blind: serving a unit without its honest marker is the one
            # outcome this reader must never produce.
            if used + joiner + len(block) > self.max_result_chars:
                dropped.extend(item.id for item in selected[index:])
                break
            blocks.append(block)
            used += joiner + len(block)
        text = "\n\n".join(blocks)
        if dropped and reserve:
            note = "(not shown, request them separately: " + ", ".join(dropped) + ")"
            note = (
                note
                if len(note) <= reserve
                else note[: reserve - 1] + "…)"
            )
            text = f"{text}\n\n{note}" if text else note
        return self._bounded(text)

    def _unit_body(self, unit: ReviewDiffUnit, budget: int) -> tuple[str, bool]:
        """Return one unit's body, marking it when only part was served."""
        budget = max(1, budget)
        if len(unit.excerpt) <= budget:
            return unit.excerpt, False
        # Full marker when it fits the remaining budget, otherwise the shortest
        # form that still says the patch is incomplete — a head-only patch that
        # reads as complete is the failure this whole bound exists to prevent.
        for marker in (
            f"\n... (this evidence unit is {len(unit.excerpt)} chars; only the "
            f"first {budget} are shown, the rest of this patch was NOT shown)",
            "\n... (unit truncated, NOT complete)",
            "\n... (truncated)",
        ):
            if len(marker) < budget:
                keep = max(0, budget - len(marker))
                return unit.excerpt[:keep].rstrip() + marker, True
        # Budget too small for any marker: serve nothing rather than an
        # unmarked head, which the planner would read as the whole patch.
        return "", True

    def _bounded(self, text: str) -> str:
        """Keep a response inside the runner's tool-result budget, explicitly."""
        if len(text) <= self.max_result_chars:
            return text
        marker = "\n... (evidence page clipped to the tool-result budget; request fewer IDs)"
        budget = max(0, self.max_result_chars - len(marker))
        return text[:budget].rstrip() + marker


@dataclass(slots=True)
class DecisionReceiverAdapter:
    """Validates the planner's structured decision payload before the receiver.

    Pydantic enforces the shape (list lengths, string bounds, no extra keys) and
    :class:`TriageReceiver` enforces the review semantics (authorized IDs,
    allowed dimensions, one decision per evidence unit). Separating them keeps
    the error messages actionable inside the same AgentRun.
    """

    receiver: TriageReceiver

    def submit(self, decision: dict) -> tuple[bool, str]:
        try:
            payload = _DecisionPayload.model_validate(decision)
        except ValidationError as exc:
            return False, f"invalid triage decision: {exc.errors(include_url=False)}"
        return self.receiver.submit(
            evidence_ids=list(payload.evidence_ids),
            risk_level=payload.risk_level,
            dimensions=list(payload.dimensions),
            focus=payload.focus,
            rationale=payload.rationale,
        )


_DECISION_SCHEMA = ObjectSchema(
    {
        "evidence_ids": ArraySchema(
            StringSchema("Authorized evidence ID, such as ev-001.", min_length=1),
            description=(
                "One or more authorized evidence IDs this decision covers. Each "
                "evidence unit may be triaged by exactly one decision."
            ),
            min_items=1,
            max_items=20,
        ),
        "risk_level": StringSchema(
            "Risk judgement for this evidence.",
            enum=list(REVIEW_RISK_LEVELS),
        ),
        "dimensions": ArraySchema(
            StringSchema("Reviewer dimension key to dispatch.", min_length=1),
            description=(
                "Reviewer dimensions that should inspect this evidence. Required "
                "unless risk_level is 'low'; low risk with no dimensions records "
                "the evidence as dismissed and starts no reviewer."
            ),
            max_items=4,
        ),
        "focus": StringSchema(
            "Concrete risk or interaction the assigned reviewers should investigate.",
            min_length=1,
        ),
        "rationale": StringSchema(
            "Short reason for this risk judgement, for the run audit trail.",
            min_length=1,
        ),
    },
    required=["evidence_ids", "risk_level"],
    additional_properties=False,
)

_FINISH_SCHEMA = ObjectSchema({}, required=[], additional_properties=False)


@tool_parameters(_DECISION_SCHEMA.to_json_schema())
class SubmitReviewDecisionTool(Tool):
    """Record one planner triage decision over authorized diff evidence."""

    _plugin_discoverable = False

    def __init__(self, adapter: DecisionReceiverAdapter) -> None:
        self._adapter = adapter

    @property
    def name(self) -> str:
        return "submit_review_decision"

    @property
    def description(self) -> str:
        return (
            "Record one triage decision: which authorized diff evidence to review, "
            "its risk level, and the reviewer dimensions that should inspect it. "
            "Call once per risk judgement, then call finish_review_triage."
        )

    async def execute(self, **kwargs: object) -> str:
        accepted, detail = self._adapter.submit(dict(kwargs))
        return detail if accepted else f"Error: {detail}"


@tool_parameters(_FINISH_SCHEMA.to_json_schema())
class FinishReviewTriageTool(Tool):
    """Terminal planner tool: end triage with the decisions recorded so far."""

    _plugin_discoverable = False

    def __init__(self, receiver: TriageReceiver) -> None:
        self._receiver = receiver

    @property
    def name(self) -> str:
        return "finish_review_triage"

    @property
    def description(self) -> str:
        return (
            "Finish planner triage. Unread or unreported evidence stays unexamined "
            "and no reviewer is added for it; call this only when the evidence you "
            "want reviewed has been reported through submit_review_decision."
        )

    async def execute(self, **_: object) -> str:
        accepted, detail = self._receiver.finish()
        return detail if accepted else f"Error: {detail}"


@tool_parameters(
    ObjectSchema(
        {
            "offset": IntegerSchema(
                description="Zero-based index of the first evidence unit to list.",
                minimum=0,
            ),
            "limit": IntegerSchema(
                description="Maximum evidence units to list.",
                minimum=1,
                maximum=MAX_DIFF_PAGE_ITEMS,
            ),
        },
        required=[],
        additional_properties=False,
    ).to_json_schema()
)
class ListReviewDiffTool(Tool):
    """List the authorized diff evidence index for paged planner input."""

    _plugin_discoverable = False

    def __init__(self, reader: ReviewDiffReader) -> None:
        self._reader = reader

    @property
    def name(self) -> str:
        return "list_review_diff"

    @property
    def description(self) -> str:
        return (
            "List the authorized diff evidence units (ID, path, line range, kind, "
            "size). Use read_review_diff to read the patch content of specific units."
        )

    async def execute(
        self, offset: int = 0, limit: int = MAX_DIFF_PAGE_ITEMS, **_: object
    ) -> str:
        return self._reader.index_text(
            offset=max(0, int(offset or 0)),
            limit=max(1, min(int(limit or MAX_DIFF_PAGE_ITEMS), MAX_DIFF_PAGE_ITEMS)),
        )


@tool_parameters(
    ObjectSchema(
        {
            "evidence_ids": ArraySchema(
                StringSchema("Authorized evidence ID, such as ev-001.", min_length=1),
                description="Evidence units whose diff content should be read.",
                min_items=1,
                max_items=8,
            ),
        },
        required=["evidence_ids"],
        additional_properties=False,
    ).to_json_schema()
)
class ReadReviewDiffTool(Tool):
    """Read the diff content of specific authorized evidence units."""

    _plugin_discoverable = False

    def __init__(self, reader: ReviewDiffReader) -> None:
        self._reader = reader

    @property
    def name(self) -> str:
        return "read_review_diff"

    @property
    def description(self) -> str:
        return (
            "Read the frozen diff content of specific authorized evidence IDs. "
            "Only evidence produced for this review can be read; repository paths "
            "outside the frozen diff are not reachable with this tool."
        )

    async def execute(self, evidence_ids: list[str], **_: object) -> str:
        # Every requested ID is served (bounded by the reader's char budget with
        # an explicit marker); the tool must never drop IDs silently behind a
        # per-call count the schema does not advertise.
        return self._reader.read_text(list(evidence_ids or []))


__all__ = [
    "DecisionReceiverAdapter",
    "FinishReviewTriageTool",
    "ListReviewDiffTool",
    "MAX_DIFF_PAGE_ITEMS",
    "MAX_DIFF_READ_CHARS",
    "ReadReviewDiffTool",
    "ReviewDiffReader",
    "ReviewDiffUnit",
    "SubmitReviewDecisionTool",
]
