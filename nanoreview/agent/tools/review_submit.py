"""Structured finding submission tool for review subagents."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from nanoreview.agent.tools.base import Tool, tool_parameters
from nanoreview.agent.tools.schema import ArraySchema, IntegerSchema, ObjectSchema, StringSchema

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


_FINDING_SCHEMA = ObjectSchema(
    {
        "severity": StringSchema(
            "Finding severity.",
            enum=("critical", "high", "medium", "low"),
        ),
        "file": StringSchema("Path to the affected file.", min_length=1),
        "line": IntegerSchema(description="Relevant line number.", minimum=1, nullable=True),
        "title": StringSchema("Short finding title.", min_length=1),
        "evidence": StringSchema(
            "Verbatim single-line code snippet in backticks from near the reported line, "
            "copied character-for-character. Example: `candidate.line = matched_line`",
            min_length=1,
        ),
        "impact": StringSchema("What can go wrong.", min_length=1),
        "recommendation": StringSchema("How to fix or mitigate the issue.", min_length=1),
        "details": ObjectSchema(
            {},
            description="Reviewer-specific causal details required by the assigned profile.",
            additional_properties=True,
        ),
    },
    required=[
        "severity",
        "file",
        "line",
        "title",
        "evidence",
        "impact",
        "recommendation",
        "details",
    ],
    additional_properties=True,
)


@dataclass(slots=True)
class ReviewSubmitResult:
    """Canonical review finding submission."""

    submitted: bool
    findings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "submitted": self.submitted,
                "findings": self.findings,
                "errors": self.errors,
            },
            ensure_ascii=False,
        )


def review_submit(findings: list[dict[str, Any]]) -> ReviewSubmitResult:
    """Normalize and validate a review subagent's final findings."""
    if not isinstance(findings, list):
        return ReviewSubmitResult(
            submitted=False,
            errors=[f"findings must be an array, got {type(findings).__name__}"],
        )

    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw in enumerate(findings):
        path = f"findings[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{path} must be an object")
            continue

        missing = [field for field in _REQUIRED_FINDING_FIELDS if field not in raw]
        if missing:
            errors.append(f"{path} missing required fields: {', '.join(missing)}")
            continue

        severity = str(raw.get("severity", "")).strip().lower()
        if severity not in _SEVERITIES:
            errors.append(f"{path}.severity must be one of {sorted(_SEVERITIES)}")
            continue

        line = raw.get("line")
        if line is not None:
            if isinstance(line, bool) or not isinstance(line, int):
                errors.append(f"{path}.line must be an integer or null")
                continue
            if line < 1:
                errors.append(f"{path}.line must be >= 1")
                continue

        finding = {
            "severity": severity,
            "file": str(raw.get("file", "")).strip(),
            "line": line,
            "title": str(raw.get("title", "")).strip(),
            "evidence": str(raw.get("evidence", "")).strip(),
            "impact": str(raw.get("impact", "")).strip(),
            "recommendation": str(raw.get("recommendation", "")).strip(),
            "details": raw.get("details"),
        }
        if not isinstance(finding["details"], dict):
            errors.append(f"{path}.details must be an object")
            continue
        empty_text_fields = [
            field
            for field in ("file", "title", "evidence", "impact", "recommendation")
            if not finding[field]
        ]
        if empty_text_fields:
            errors.append(
                f"{path} has empty required fields: {', '.join(empty_text_fields)}"
            )
            continue
        normalized.append(finding)

    return ReviewSubmitResult(
        submitted=not errors,
        findings=normalized if not errors else [],
        errors=errors,
    )


@tool_parameters(
    ObjectSchema(
        {
            "findings": ArraySchema(
                _FINDING_SCHEMA,
                description="Structured review findings. Use an empty array when no issues are found.",
            ),
        },
        required=["findings"],
    ).to_json_schema()
)
class SubmitReviewFindingsTool(Tool):
    """Submit final structured findings for a review subagent."""

    _scopes = {
        "reviewer.bug", "reviewer.security",
        "reviewer.performance", "reviewer.maintainability",
    }

    @property
    def name(self) -> str:
        return "review_submit"

    @property
    def description(self) -> str:
        return (
            "Submit the final structured review findings. Review subagents must "
            "use this tool as their final deliverable instead of writing a prose report."
        )

    async def execute(self, findings: list[dict[str, Any]], **kwargs: Any) -> str:
        result = review_submit(findings)
        if not result.submitted:
            return "Error: " + "; ".join(result.errors)
        return result.to_json()
