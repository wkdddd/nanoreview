from __future__ import annotations

import asyncio

import pytest

from nanoreview.agent.orchestration import (
    ReviewExecutionContext,
    ReviewOrchestrator,
    ReviewPlanningError,
)
from nanoreview.agent.runner import AgentRunResult
from nanoreview.bus.events import InboundMessage
from nanoreview.review.types import (
    ALL_REVIEW_ROLES,
    EvidenceReference,
    ReviewAction,
    ReviewEvidenceBundle,
    ReviewPlan,
)


def _plan(*roles: str) -> ReviewPlan:
    return ReviewPlan(
        target="repo",
        target_name="repo",
        target_type="local",
        action=ReviewAction.REPO,
        depth="full",
        roles=[ALL_REVIEW_ROLES[role] for role in roles],
        routing_mode="explicit",
    )


def _evidence() -> ReviewEvidenceBundle:
    return ReviewEvidenceBundle(
        references=(
            EvidenceReference(
                id="ev-001",
                path="app.py",
                start_line=1,
                end_line=1,
                excerpt="## app.py:1-1\nvalue = 1",
            ),
        ),
        summary="## app.py:1-1",
    )


class _NoPlanRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, spec):
        self.calls += 1
        assert spec.tool_choice == {
            "type": "function",
            "function": {"name": "submit_review_plan"},
        }
        return AgentRunResult(final_content="not a tool call", messages=[])


class _PlanRunner:
    async def run(self, spec):
        tool = spec.tools.get("submit_review_plan")
        assert tool is not None
        result = await tool.execute(
            assignments=[
                {
                    "dimension": "security",
                    "focus": "authentication state",
                    "evidence_ids": ["ev-001"],
                },
                {
                    "dimension": "bug",
                    "focus": "exception paths",
                    "evidence_ids": ["ev-001"],
                },
            ]
        )
        assert result == "review plan accepted"
        return AgentRunResult(final_content="", messages=[])


class _Subagents:
    max_concurrent_subagents = 2

    def __init__(self) -> None:
        self.results: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.calls: list[dict] = []

    def get_running_count(self) -> int:
        return 0

    async def spawn(self, **kwargs):
        self.calls.append(kwargs)
        await self.results.put(
            InboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id="cli:review",
                content="completed",
                metadata={
                    "subagent_label": kwargs["label"],
                    "subagent_result": '{"submitted":true,"findings":[],"errors":[]}',
                },
            )
        )
        return "Review subagent started"

    async def wait_for_session_result(self, _session_key: str, *, timeout: float):
        return await asyncio.wait_for(self.results.get(), timeout=timeout)


@pytest.mark.asyncio
async def test_coordinator_plan_retries_three_times_before_failure(tmp_path) -> None:
    runner = _NoPlanRunner()
    orchestrator = ReviewOrchestrator(
        runner=runner,
        subagents=_Subagents(),
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )

    with pytest.raises(ReviewPlanningError, match="after 3 retries"):
        await orchestrator.execute(
            coordinator_messages=[{"role": "system", "content": "plan"}],
            plan=_plan("security"),
            evidence=_evidence(),
            context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
            validation_workspace=str(tmp_path),
        )

    assert runner.calls == 4


@pytest.mark.asyncio
async def test_program_dispatches_planned_dimensions_without_bus_injection(tmp_path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subagents = _Subagents()
    orchestrator = ReviewOrchestrator(
        runner=_PlanRunner(),
        subagents=subagents,
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )

    report = await orchestrator.execute(
        coordinator_messages=[{"role": "system", "content": "plan"}],
        plan=_plan("security", "bug"),
        evidence=_evidence(),
        context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
        validation_workspace=str(tmp_path),
    )

    assert [call["label"] for call in subagents.calls] == ["security", "bug"]
    assert all(call["deliver_to_bus"] is False for call in subagents.calls)
    assert "No actionable issues found" in report


@pytest.mark.asyncio
async def test_local_diff_without_evidence_explains_how_to_continue(tmp_path) -> None:
    plan = ReviewPlan(
        target=str(tmp_path / "app.py"),
        target_name="app.py",
        target_type="local",
        action=ReviewAction.DIFF,
        depth="full",
        roles=[ALL_REVIEW_ROLES["security"]],
        routing_mode="auto",
    )
    orchestrator = ReviewOrchestrator(
        runner=_NoPlanRunner(),
        subagentmanager=_Subagents(),
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )

    with pytest.raises(ReviewPlanningError, match="Switch Scope to Repo"):
        await orchestrator.execute(
            coordinator_messages=[],
            plan=plan,
            evidence=ReviewEvidenceBundle(),
            context=ReviewExecutionContext("cli", "review", "cli:review", None, {}, 1),
            validation_workspace=str(tmp_path),
        )
