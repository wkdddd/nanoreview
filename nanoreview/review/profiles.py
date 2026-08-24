"""Authoritative registry for specialized code-review profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanoreview.agent.subagent_profiles import (
    SubagentCompletion,
    SubagentExecutionProfile,
)
from nanoreview.utils.prompt_templates import render_template

_BASE_TOOLS = frozenset({"read_file", "list_dir", "grep", "review_submit"})
_EVIDENCE_TOOLS = frozenset({"local_review", "github_review"})
_SOFT_TOOLS = frozenset({"read_file", "list_dir", "grep", "local_review", "github_review"})


@dataclass(frozen=True, slots=True)
class ReviewerProfile:
    id: str
    label: str
    planner_description: str
    prompt_template: str
    context_policy: str
    details_schema: dict[str, object]
    report_fields: tuple[tuple[str, str], ...]
    base_tools: frozenset[str] = _BASE_TOOLS
    extra_tools: frozenset[str] = frozenset()

    def public_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.planner_description,
        }

    def execution_profile(self) -> SubagentExecutionProfile:
        return SubagentExecutionProfile(
            id=self.id,
            tool_names=self.base_tools | self.extra_tools | _EVIDENCE_TOOLS,
            terminal_tools=frozenset({"review_submit"}),
            soft_tool_error_tools=_SOFT_TOOLS,
            prompt_builder=lambda metadata, workspace: _build_reviewer_prompt(
                self, metadata, workspace
            ),
            workspace_resolver=_reviewer_workspace,
            result_handler=_handle_reviewer_result,
            max_iterations_message=(
                "Review task completed but no structured findings were submitted."
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
    return render_template(
        "agent/review_subagent_system.md",
        time_ctx=ContextBuilder._build_runtime_context(None, None),
        workspace=str(workspace),
        skills_summary="",
        reviewer_label=profile.label,
        reviewer_fragment=fragment,
        context_policy=profile.context_policy,
    )


def _canonical_review_submit(result: Any) -> str | None:
    for event in reversed(result.tool_events or []):
        if event.get("name") == "review_submit" and event.get("status") == "ok":
            content = event.get("raw_result")
            if isinstance(content, str):
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    continue
                if data.get("submitted") is True and isinstance(data.get("findings"), list):
                    return json.dumps(data, ensure_ascii=False)
    for message in reversed(result.messages or []):
        if message.get("role") != "tool" or message.get("name") != "review_submit":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            continue
        if data.get("submitted") is True and isinstance(data.get("findings"), list):
            return json.dumps(data, ensure_ascii=False)
    return None


async def _handle_reviewer_result(*, result: Any, retry: Any, target_type: str) -> SubagentCompletion:
    content = _canonical_review_submit(result)
    stop_reason = result.stop_reason
    if content is None:
        content, stop_reason = await retry()
    if content is None:
        return SubagentCompletion(
            "No structured findings submitted: the reviewer did not produce a review_submit result.",
            status="error",
            stop_reason=stop_reason,
        )
    data = json.loads(content)
    if not data.get("findings"):
        evidence_tools = {"github_review"} if target_type == "github" else {"read_file", "grep", "local_review"}
        has_evidence = any(
            event.get("name") in evidence_tools and event.get("status") == "ok"
            for event in result.tool_events or []
        )
        if not has_evidence:
            return SubagentCompletion(
                "Error: Review incomplete - no target evidence was successfully read before submitting empty findings.",
                status="error",
                stop_reason=stop_reason,
            )
    return SubagentCompletion(content, stop_reason=stop_reason)
