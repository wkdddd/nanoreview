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
        action=ReviewAction.REPO,
        depth="full",
        roles=list(ALL_REVIEW_ROLES.values()),
        routing_mode="auto",
        user_requirements="review auth",
    )


def _evidence() -> ReviewEvidenceBundle:
    return ReviewEvidenceBundle(
        references=(
            EvidenceReference(
                id="ev-001",
                path="src/auth.py",
                start_line=10,
                end_line=42,
                kind="function",
                token_count=8,
                preview="def login(token):\n    return verify(token)",
                risk_hints=("security:auth", "security:token"),
                preview_coverage="chunk lines 10-42; preview covers lines 10-11",
            ),
            EvidenceReference(
                id="ev-002",
                path="src/plain.py",
                start_line=1,
                end_line=4,
                kind="file",
                token_count=3,
                preview="value = 1",
            ),
        ),
        summary="## src/auth.py:10-42",
    )


def test_main_review_prompt_routes_with_risk_hints() -> None:
    prompt = render_review_prompt(_plan())

    # `matched` is described as user-query hit words, `risk_hints` as
    # program-generated candidate clues — not as confirmed findings.
    assert "`matched:` lists the user review-query terms" in prompt
    assert "`risk_hints:` lists program-generated candidate risk clues" in prompt
    assert "route files using `risk_hints:` candidate clues" in prompt


def test_narrow_coordinator_prompt_renders_risk_hints_and_coverage() -> None:
    prompt = render_review_coordinator_prompt(_plan(), _evidence())

    # Each reference renders its hints and coverage next to the preview.
    assert "risk_hints: security:auth, security:token" in prompt
    assert "preview_coverage: chunk lines 10-42; preview covers lines 10-11" in prompt
    # Units without hints fall back to none/unknown instead of vanishing.
    assert "risk_hints: none" in prompt
    assert "preview_coverage: unknown" in prompt
    # The prompt states hints are candidate clues, never confirmed findings,
    # and units without hints stay valid review scope.
    assert "NOT confirmed vulnerabilities or findings" in prompt
    assert "units without hints" in prompt
