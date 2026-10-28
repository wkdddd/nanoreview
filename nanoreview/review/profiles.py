"""Authoritative registry for specialized code-review profiles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.agent.subagent_profiles import (
    BUG_REVIEWER_SCOPE,
    GENERAL_REVIEWER_SCOPE,
    MAINTAINABILITY_REVIEWER_SCOPE,
    PERFORMANCE_REVIEWER_SCOPE,
    SECURITY_REVIEWER_SCOPE,
    SubagentCompletion,
    SubagentExecutionProfile,
)
from nanoreview.review.contracts import reviewer_contract
from nanoreview.utils.prompt_templates import render_template

_SOFT_TOOLS = frozenset({"read_file", "list_dir", "grep"})
_REVIEWER_REQUIRED_TOOLS = frozenset({"read_file", "list_dir", "grep"})
#: Tools whose successful call proves the reviewer read target evidence.
_REVIEWER_EVIDENCE_TOOLS = frozenset({"read_file", "grep"})

#: Profile id -> execution scope. Every registered profile must appear here;
#: lookup is explicit so a newly registered profile without a scope fails loudly
#: instead of silently falling back to some generic boundary.
_SCOPE_BY_PROFILE_ID: dict[str, str] = {
    "bug": BUG_REVIEWER_SCOPE,
    "security": SECURITY_REVIEWER_SCOPE,
    "performance": PERFORMANCE_REVIEWER_SCOPE,
    "maintainability": MAINTAINABILITY_REVIEWER_SCOPE,
    "general": GENERAL_REVIEWER_SCOPE,
}


@dataclass(frozen=True, slots=True)
class ReviewerProfile:
    id: str
    label: str
    planner_description: str
    prompt_template: str
    context_policy: str
    details_schema: dict[str, object]
    report_fields: tuple[tuple[str, str], ...]

    def public_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.planner_description,
        }

    def execution_profile(self) -> SubagentExecutionProfile:
        return SubagentExecutionProfile(
            id=self.id,
            scope=_SCOPE_BY_PROFILE_ID[self.id],
            required_tools=_REVIEWER_REQUIRED_TOOLS,
            soft_tool_error_tools=_SOFT_TOOLS,
            # The reviewer returns its findings as the run's final structured
            # output; the runner rejects anything else and lets the model fix it
            # inside the same run.
            output_contract_factory=lambda: reviewer_contract(self.id),
            prompt_builder=lambda metadata, workspace: _build_reviewer_prompt(
                self, metadata, workspace
            ),
            workspace_resolver=_reviewer_workspace,
            result_handler=_handle_reviewer_result,
            max_iterations_message=(
                "Review task ended without a final findings payload."
            ),
        )


def _string_details(*names: str, array_fields: frozenset[str] = frozenset()) -> dict[str, object]:
    properties: dict[str, object] = {}
    for name in names:
        if name in array_fields:
            properties[name] = {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            }
        else:
            properties[name] = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "properties": properties,
        "required": list(names),
        "additionalProperties": False,
    }


REVIEWER_PROFILES: dict[str, ReviewerProfile] = {
    "bug": ReviewerProfile(
        id="bug",
        label="Bug Reviewer",
        planner_description=(
            "Find reproducible logic errors, boundary failures, exception-path defects, "
            "state inconsistencies, regressions, and race conditions."
        ),
        prompt_template="agent/reviewers/bug.md",
        context_policy="Inspect callers, callees, exceptional branches, state reads/writes, and tests.",
        details_schema=_string_details("trigger", "expected_behavior", "actual_behavior"),
        report_fields=(("trigger", "Trigger"), ("expected_behavior", "Expected behavior"), ("actual_behavior", "Actual behavior")),
    ),
    "security": ReviewerProfile(
        id="security",
        label="Security Reviewer",
        planner_description=(
            "Find exploitable authentication, authorization, injection, path, network, "
            "secret-handling, dependency, and trust-boundary defects."
        ),
        prompt_template="agent/reviewers/security.md",
        context_policy="Trace input sources, authorization, configuration, resource access, and dependency boundaries.",
        details_schema=_string_details("trust_boundary", "attack_preconditions", "attack_path"),
        report_fields=(("trust_boundary", "Trust boundary"), ("attack_preconditions", "Attack preconditions"), ("attack_path", "Attack path")),
    ),
    "performance": ReviewerProfile(
        id="performance",
        label="Performance Reviewer",
        planner_description=(
            "Find material algorithmic, I/O, query, locking, blocking, memory, and "
            "scalability problems on plausible hot paths."
        ),
        prompt_template="agent/reviewers/performance.md",
        context_policy="Inspect loops, I/O, queries, locks, allocation, concurrency, and expected data scale.",
        details_schema=_string_details("hot_path", "scale_condition", "resource_impact"),
        report_fields=(("hot_path", "Hot path"), ("scale_condition", "Scale condition"), ("resource_impact", "Resource impact")),
    ),
    "maintainability": ReviewerProfile(
        id="maintainability",
        label="Maintainability Reviewer",
        planner_description=(
            "Find concrete boundary violations, coupling, duplication, complexity, and "
            "testability problems that amplify future changes or propagate defects."
        ),
        prompt_template="agent/reviewers/maintainability.md",
        context_policy="Inspect public interfaces, module dependencies, architecture rules, duplicate implementations, and tests.",
        details_schema=_string_details(
            "violated_boundary",
            "change_amplification",
            "affected_modules",
            array_fields=frozenset({"affected_modules"}),
        ),
        report_fields=(("violated_boundary", "Violated boundary"), ("change_amplification", "Change amplification"), ("affected_modules", "Affected modules")),
    ),
    "general": ReviewerProfile(
        id="general",
        label="General Reviewer",
        planner_description=(
            "Perform a single balanced review covering functional correctness, boundaries, "
            "error handling, security, performance, and maintainability for the change."
        ),
        prompt_template="agent/reviewers/general.md",
        context_policy=(
            "Inspect the change across functional, boundary, error-handling, security, "
            "performance, and maintainability concerns; follow callers and exceptional "
            "paths only as far as the evidence requires."
        ),
        details_schema=_string_details(
            "concern", "symptom", "affected_behavior", "recommended_fix"
        ),
        report_fields=(
            ("concern", "Concern"),
            ("symptom", "Symptom"),
            ("affected_behavior", "Affected behavior"),
            ("recommended_fix", "Recommended fix"),
        ),
    ),
}


def get_reviewer_profile(profile_id: str) -> ReviewerProfile | None:
    return REVIEWER_PROFILES.get(profile_id)


def public_reviewer_profiles() -> list[dict[str, str]]:
    return [profile.public_dict() for profile in REVIEWER_PROFILES.values()]


def reviewer_execution_profiles() -> dict[str, SubagentExecutionProfile]:
    return {key: profile.execution_profile() for key, profile in REVIEWER_PROFILES.items()}


def _reviewer_workspace(metadata: dict[str, Any], default: Path) -> Path:
    raw = metadata.get("repository_root") or metadata.get("review_local_root")
    if not raw:
        return default
    try:
        resolved = Path(str(raw)).expanduser().resolve()
    except (OSError, ValueError):
        return default
    return resolved if resolved.is_dir() else default


def _build_reviewer_prompt(
    profile: ReviewerProfile,
    metadata: dict[str, Any],
    workspace: Path,
) -> str:
    from nanoreview.agent.context import ContextBuilder

    fragment = render_template(profile.prompt_template, strip=True)
    # The shared rules live in the NanoReview workspace, which is not the
    # reviewer's target repository; the loop passes its path in the metadata.
    common_rules = ""
    rules_root = metadata.get("common_rules_workspace")
    if rules_root:
        common_rules = ContextBuilder.load_common_rules(Path(str(rules_root)))
        if not common_rules:
            logger.warning(
                "review.reviewer.common_rules.missing root={}", rules_root
            )
    return render_template(
        "agent/review_subagent_system.md",
        time_ctx=ContextBuilder._build_runtime_context(None, None),
        workspace=str(workspace),
        skills_summary="",
        common_rules=common_rules,
        reviewer_label=profile.label,
        reviewer_fragment=fragment,
        context_policy=profile.context_policy,
    )


async def _handle_reviewer_result(
    *,
    result: Any,
    metadata: dict[str, Any] | None = None,
) -> SubagentCompletion:
    """Interpret the accepted structured output of the reviewer's AgentRun.

    The runner already parsed and accepted the final answer against the
    reviewer's output contract, so this handler only decides what the accepted
    payload *means*: an empty finding list still has to be backed by evidence,
    and anything else is a bounded, explicit failure.
    """
    payload = result.structured_output
    stop_reason = result.stop_reason
    if not isinstance(payload, dict):
        return SubagentCompletion(
            "Error: Review incomplete - the reviewer produced no structured result.",
            status="error",
            stop_reason=stop_reason,
        )
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return SubagentCompletion(
            "Error: Review incomplete - the reviewer result carried no findings array.",
            status="error",
            stop_reason=stop_reason,
        )
    if not findings:
        # Evidence is provided two ways: the planner injects assigned excerpts
        # into the reviewer's frozen task, and the reviewer may read the target
        # directly with ``read_file``/``grep``. An empty finding list is only
        # incomplete when neither channel supplied evidence.
        has_assigned_evidence = bool((metadata or {}).get("assigned_evidence"))
        has_read_evidence = any(
            event.get("name") in _REVIEWER_EVIDENCE_TOOLS
            and event.get("status") == "ok"
            for event in result.tool_events or []
        )
        if not (has_assigned_evidence or has_read_evidence):
            return SubagentCompletion(
                "Error: Review incomplete - no target evidence was successfully read before reporting no findings.",
                status="error",
                stop_reason=stop_reason,
                payload=payload,
            )
    return SubagentCompletion(
        f"reviewer {payload.get('dimension') or 'dimension'} reported "
        f"{len(findings)} finding(s)",
        stop_reason=stop_reason,
        payload=payload,
    )
