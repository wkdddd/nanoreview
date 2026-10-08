"""Tests for review planning prompt rendering (main + narrow coordinator)."""

from __future__ import annotations

from nanoreview.review.planning.prompt import (
    render_review_coordinator_prompt,
    render_review_prompt,
)
from nanoreview.review.types import (
    ALL_REVIEW_ROLES,
    EvidenceReference,
    ReviewAction,
    ReviewEvidenceBundle,
    ReviewPlan,
)


def _plan() -> ReviewPlan:
    return ReviewPlan(
        target=".",
        target_name="workspace",
        target_type="local",
        action=ReviewAction.DIFF,
        roles=list(ALL_REVIEW_ROLES.values()),
        mode="auto",
        user_requirements="review auth",
    )


def _evidence(*, input_mode: str = "direct") -> ReviewEvidenceBundle:
    return ReviewEvidenceBundle(
        references=(
            EvidenceReference(
                id="ev-001",
                path="src/auth.py",
                start_line=10,
                end_line=42,
                kind="function",
                token_count=8,
                excerpt="def login(token):\n    return verify(token)",
                preview="def login(token):\n    return verify(token)",
                matched=("token",),
                preview_coverage="chunk lines 10-42; preview covers lines 10-11",
            ),
            EvidenceReference(
                id="ev-002",
                path="src/plain.py",
                start_line=1,
                end_line=4,
                kind="file",
                token_count=3,
                excerpt="value = 1",
                preview="value = 1",
            ),
        ),
        summary="## src/auth.py:10-42",
        input_mode=input_mode,
    )


def test_main_review_prompt_routes_with_query_matches() -> None:
    prompt = render_review_prompt(_plan())

    # `matched` is described as user-query hit words; program-generated
    # candidate clues are no longer injected into the prompt.
    assert "`matched:` lists the user review-query terms" in prompt
    assert "risk_hints" not in prompt


def test_triage_prompt_inlines_evidence_in_direct_mode() -> None:
    prompt = render_review_coordinator_prompt(_plan(), _evidence())

    assert "matched: token" in prompt
    assert "preview_coverage: chunk lines 10-42; preview covers lines 10-11" in prompt
    # Units without matches fall back to none/unknown instead of vanishing.
    assert "matched: none" in prompt
    assert "preview_coverage: unknown" in prompt
    # Direct mode ships the diff content itself and needs no reader tools.
    assert "def login(token):" in prompt
    assert "diff-reading tools are not needed" in prompt


def test_triage_prompt_requires_bounded_reads_in_paged_mode() -> None:
    prompt = render_review_coordinator_prompt(_plan(), _evidence(input_mode="paged"))

    assert "`list_review_diff`" in prompt
    assert "`read_review_diff`" in prompt
    # The tool boundary is stated: frozen diff only, no repository paths.
    assert "cannot read repository paths" in prompt
    assert "call read_review_diff with this id" in prompt
    assert "def login(token):" not in prompt


def test_triage_prompt_never_mentions_risk_hints_or_assignment_submission() -> None:
    prompt = render_review_coordinator_prompt(_plan(), _evidence())

    assert "risk_hints" not in prompt
    assert "program-generated candidate routing clues" not in prompt
    assert "units without hints" not in prompt
    # The planner reports decisions; it does not submit assignments.
    assert "submit_review_plan" not in prompt
    assert "submit_review_decision" in prompt
    assert "finish_review_triage" in prompt


def test_triage_prompt_states_auto_minimum_sufficient_set() -> None:
    prompt = render_review_coordinator_prompt(_plan(), _evidence())

    assert "smallest set of specialized dimensions" in prompt
    assert "never adds a dimension you did not name" in prompt
    # Unreported evidence is the planner's explicit choice, not a program gap.
    assert "unexamined" in prompt


def test_triage_prompt_special_mode_requires_every_selected_dimension() -> None:
    from nanoreview.review.types import ALL_REVIEW_ROLES, ReviewAction, ReviewPlan

    plan = ReviewPlan(
        target=".",
        target_name="workspace",
        target_type="local",
        action=ReviewAction.DIFF,
        roles=[ALL_REVIEW_ROLES["bug"], ALL_REVIEW_ROLES["security"]],
        mode="special",
    )
    prompt = render_review_coordinator_prompt(plan, _evidence())

    assert "Reviewer mode: special" in prompt
    assert "must receive at least one decision that names it" in prompt


def test_triage_prompt_general_mode_restricts_to_general() -> None:
    from nanoreview.review.types import ALL_REVIEW_ROLES, ReviewAction, ReviewPlan

    plan = ReviewPlan(
        target=".",
        target_name="workspace",
        target_type="local",
        action=ReviewAction.DIFF,
        roles=[ALL_REVIEW_ROLES["general"]],
        mode="general",
    )
    prompt = render_review_coordinator_prompt(plan, _evidence())

    assert "Reviewer mode: general" in prompt
    assert "Use only the `general` dimension" in prompt
    assert "Do not introduce specialized dimensions" in prompt


def test_triage_prompt_rejects_non_low_risk_without_dimensions() -> None:
    prompt = render_review_coordinator_prompt(_plan(), _evidence())

    assert 'risk_level: "low"' in prompt
    assert "must name at least one dimension" in prompt
    # Coverage semantics are stated so a dismissed unit is deliberate.
    assert "dismissed" in prompt


def test_triage_prompt_states_the_decision_budget() -> None:
    """The planner must know it may group evidence instead of one decision each."""
    from nanoreview.review.types import MAX_TRIAGE_DECISIONS

    prompt = render_review_coordinator_prompt(_plan(), _evidence())

    assert f"at most {MAX_TRIAGE_DECISIONS} decisions" in prompt
    assert "cover several units in one decision" in prompt
