"""Structured judge verdict submission for the AI review judge batch.

The judge runs one ``AgentRunner`` batch per context-window split and must
deliver its decisions through the ``submit_verdicts`` terminal tool instead of
a prose answer. This module owns the tool schema, the receiver that validates
and normalizes the model's payload, and the tool itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from nanoreview.agent.tools.base import Tool, tool_parameters
from nanoreview.agent.tools.schema import ArraySchema, ObjectSchema, StringSchema
from nanoreview.review.types import ReviewJudgeDecision, ReviewJudgeVerdict

#: Terminal tool name shared by the tool, the forced tool choice and the
#: judge batch's ``terminal_tools`` set. Candidate ids arrive lower-cased
#: because ``ReviewJudge.candidate_id`` lower-cases every component; the
#: receiver normalizes model output to match.
VERDICT_TOOL_NAME = "submit_verdicts"

_DECISION_VALUES = tuple(decision.value for decision in ReviewJudgeDecision)


@dataclass(slots=True)
class JudgeVerdictReceiver:
    """Validates the judge's sole deliverable for one batch.

    ``verdicts`` is a JSON array; an empty array is a legal submission (the
    model judged nothing but must still terminate through the tool). Unknown
    and duplicate candidate ids are intentionally *not* rejected here: verdicts
    are folded into a mapping (last write wins) and the batch aggregation
    filters ids that do not belong to the batch.
    """

    submission: dict[str, ReviewJudgeVerdict] | None = None

    def submit(self, verdicts: list[dict]) -> tuple[bool, str]:
        if not isinstance(verdicts, list):
            return False, f"verdicts must be an array, got {type(verdicts).__name__}"
        normalized: dict[str, ReviewJudgeVerdict] = {}
        for index, item in enumerate(verdicts):
            if not isinstance(item, dict):
                return False, f"verdicts[{index}] must be an object"
            candidate_id = str(item.get("id", "")).strip().lower()
            if not candidate_id:
                return False, f"verdicts[{index}].id must be a non-empty string"
            decision_raw = str(item.get("decision", "")).strip().lower()
            try:
                decision = ReviewJudgeDecision(decision_raw)
            except ValueError:
                return False, (
                    f"verdicts[{index}].decision must be one of {list(_DECISION_VALUES)}"
                )
            severity = item.get("severity")
            normalized[candidate_id] = ReviewJudgeVerdict(
                decision=decision,
                reason=str(item.get("reason", "")),
                confidence=str(item.get("confidence", "medium")),
                severity=str(severity).lower() if severity else None,
            )
        self.submission = normalized
        return True, f"judge verdicts accepted: {len(normalized)}"


_VERDICT_SCHEMA = ObjectSchema(
    {
        "id": StringSchema("Candidate id exactly as supplied in the batch.", min_length=1),
        "decision": StringSchema(
            "Final decision for this candidate.",
            enum=tuple(_DECISION_VALUES),
        ),
        "reason": StringSchema("Concrete justification for the decision."),
        "confidence": StringSchema("Model confidence in this decision."),
    },
    required=["id", "decision", "reason", "confidence"],
)


@tool_parameters(
    ObjectSchema(
        {
            "verdicts": ArraySchema(
                _VERDICT_SCHEMA,
                description=(
                    "One verdict per candidate in the batch. Use an empty array "
                    "when there is nothing to decide."
                ),
            ),
        },
        required=["verdicts"],
    ).to_json_schema()
)
class SubmitJudgeVerdictsTool(Tool):
    """Capture validated judge verdicts without ending the review."""

    _plugin_discoverable = False

    def __init__(self, receiver: JudgeVerdictReceiver) -> None:
        self._receiver = receiver

    @property
    def name(self) -> str:
        return VERDICT_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "Submit the final judge verdicts for every candidate in this batch. "
            "This is the judge's required terminal deliverable."
        )

    async def execute(self, verdicts: list[dict], **_: object) -> str:
        accepted, detail = self._receiver.submit(verdicts)
        if not accepted:
            return f"Error: {detail}"
        return json.dumps({"submitted": True, "count": len(verdicts)}, ensure_ascii=False)


#: Frozen OpenAI schema of the judge terminal tool. The judge reuses it to
#: budget the fixed per-request prompt cost without building a tool instance.
VERDICT_TOOL_SCHEMA: dict = SubmitJudgeVerdictsTool(JudgeVerdictReceiver()).to_schema()
