from __future__ import annotations

from pathlib import Path

import pytest

from nanoreview.agent.orchestration import ReviewOrchestrator
from nanoreview.review.output.report import render_review_report
from nanoreview.review.types import (
    EvidenceReference,
    ReviewAction,
    ReviewAssignment,
    ReviewBudgetSkip,
    ReviewEvidenceBundle,
    ReviewPlan,
)


def _orchestrator(tmp_path: Path) -> ReviewOrchestrator:
    return ReviewOrchestrator(
        runner=object(),
        subagentmanager=object(),
        model="test",
        workspace=tmp_path,
        max_tool_result_chars=1000,
        judge=None,
    )


def _plan(routing_mode: str) -> ReviewPlan:
    return ReviewPlan(
        target="repo",
        target_name="repo",
        target_type="local",
        action=ReviewAction.REPO,
        depth="full",
        roles=[],
        routing_mode=routing_mode,
    )


def _evidence(text: str) -> ReviewEvidenceBundle:
    return ReviewEvidenceBundle(
        references=(EvidenceReference(id="ev-1", path="app.py", excerpt=text),)
    )


@pytest.mark.asyncio
async def test_budget_scales_rounds_and_clamps_quota(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    small, _ = await orchestrator._admit_assignments(
        plan=_plan("explicit"),
        evidence=_evidence("x"),
        assignments=(ReviewAssignment("security", "check", ("ev-1",)),),
        validation_workspace=str(tmp_path),
        local_target=None,
        token_budget=100_000,
    )
    large, _ = await orchestrator._admit_assignments(
        plan=_plan("explicit"),
        evidence=_evidence("word " * 100_000),
        assignments=(ReviewAssignment("security", "check", ("ev-1",)),),
        validation_workspace=str(tmp_path),
        local_target=None,
        token_budget=100_000,
    )

    assert small[0][1].max_iterations == 11
    assert small[0][1].quota_tokens == 12_000
    assert large[0][1].max_iterations == 30
    assert large[0][1].quota_tokens == 30_000


@pytest.mark.asyncio
async def test_explicit_budget_admission_preserves_user_order(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    admitted, skipped = await orchestrator._admit_assignments(
        plan=_plan("explicit"),
        evidence=_evidence("word " * 30_000),
        assignments=(
            ReviewAssignment("performance", "check", ("ev-1",)),
            ReviewAssignment("security", "check", ("ev-1",)),
            ReviewAssignment("bug", "check", ("ev-1",)),
        ),
        validation_workspace=str(tmp_path),
        local_target=None,
        token_budget=60_000,
    )

    assert [item[0].dimension for item in admitted] == ["performance", "security"]
    assert [item.dimension for item in skipped] == ["bug"]


@pytest.mark.asyncio
async def test_auto_budget_admission_uses_review_priority(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    admitted, skipped = await orchestrator._admit_assignments(
        plan=_plan("auto"),
        evidence=_evidence("word " * 30_000),
        assignments=(
            ReviewAssignment("maintainability", "check", ("ev-1",)),
            ReviewAssignment("bug", "check", ("ev-1",)),
            ReviewAssignment("security", "check", ("ev-1",)),
        ),
        validation_workspace=str(tmp_path),
        local_target=None,
        token_budget=60_000,
    )

    assert [item[0].dimension for item in admitted] == ["bug", "security"]
    assert [item.dimension for item in skipped] == ["maintainability"]


def test_report_marks_budget_skipped_reviewer_as_incomplete() -> None:
    report = render_review_report(
        "app.py",
        [],
        routing_mode="explicit",
        selected_dimensions=["security"],
        budget_skipped=[ReviewBudgetSkip("security", 20_000, 30_000)],
    )

    assert "### Skipped Reviewers" in report
    assert "budget-skipped" in report
    assert "Review incomplete" in report
