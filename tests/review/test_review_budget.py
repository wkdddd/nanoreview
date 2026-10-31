"""Tests for the review request budgets.

Review no longer trims its input, so these numbers are the only guard between
an admitted change and a model call: the reviewer pre-flight and the judge
pre-flight both have to fail *before* the model runs.
"""
from __future__ import annotations

from nanoreview.agent.llm_runtime import ROLE_REVIEWER, LLMRuntime
from nanoreview.config.schema import ReviewConfig
from nanoreview.providers.base import GenerationSettings
from nanoreview.review.budget import (
    REVIEWER_TOOL_SCHEMA_TOKENS,
    RequestBudget,
    ReviewBudget,
)


class _RuntimeProvider:
    """Provider stand-in; only its generation settings are read."""

    def __init__(self, **generation: int) -> None:
        self.generation = GenerationSettings(**generation)


def _runtime(*, window: int = 200_000, max_tokens: int = 8_192) -> LLMRuntime:
    """The reviewer runtime a review run freezes at admission."""
    return LLMRuntime.capture(
        _RuntimeProvider(max_tokens=max_tokens),
        model="reviewer-model",
        context_window_tokens=window,
        role=ROLE_REVIEWER,
    )


def test_module_defaults_match_an_unoverridden_run() -> None:
    """With nothing configured the runtime numbers equal the module defaults."""
    config = ReviewConfig()
    budget = ReviewBudget.from_runtimes(_runtime(), review_config=config)

    assert budget.context_window_tokens == ReviewBudget().context_window_tokens == 200_000
    assert budget.reviewer_output_tokens == ReviewBudget().reviewer_output_tokens == 8_192
    assert (
        budget.reviewer_context_read_reserve_tokens
        == config.reviewer_context_read_reserve_tokens
        == 32_000
    )
    # The judge inherits the reviewer runtime's cap when it declares none.
    assert config.judge.max_tokens is None
    assert budget.judge_output_tokens == 8_192
    assert budget.safety_margin_tokens == config.budget_safety_margin_tokens == 4_000


def test_budget_reads_the_runtime_numbers_and_the_configured_reserves() -> None:
    """Window and output cap come from the runtime; only reserves from config."""
    config = ReviewConfig(
        reviewer_context_read_reserve_tokens=12_000,
        budget_safety_margin_tokens=1_000,
        judge={"max_tokens": 4_800},
    )

    budget = ReviewBudget.from_runtimes(
        _runtime(window=128_000, max_tokens=4_096),
        judge_runtime=_runtime(window=128_000, max_tokens=4_800),
        review_config=config,
    )

    assert budget.context_window_tokens == 128_000
    assert budget.reviewer_output_tokens == 4_096
    assert budget.reviewer_context_read_reserve_tokens == 12_000
    assert budget.safety_margin_tokens == 1_000
    assert budget.judge_output_tokens == 4_800


def test_budget_falls_back_to_defaults_without_a_window_or_config() -> None:
    """A runtime with no window or cap still yields a usable, defaulted budget."""
    assert ReviewBudget.from_runtimes(_runtime(window=0, max_tokens=0)) == ReviewBudget()


def test_reviewer_request_accounts_prompt_diff_reserves_and_margin() -> None:
    budget = ReviewBudget()

    request = budget.reviewer_request(prompt_tokens=120_000)

    # output reserve + context read reserve + safety margin, on top of prompt.
    assert request.output_reserve_tokens == 8_192
    assert request.reserve_tokens == 32_000 + 4_000
    assert request.required_tokens == 120_000 + 8_192 + 36_000


def test_reviewer_request_fits_exactly_at_the_window_and_fails_one_token_over() -> None:
    budget = ReviewBudget(context_window_tokens=100_000, reviewer_output_tokens=8_000)
    limit = 100_000 - 8_000 - budget.reviewer_context_read_reserve_tokens - budget.safety_margin_tokens

    fits = budget.reviewer_request(prompt_tokens=limit)
    over = budget.reviewer_request(prompt_tokens=limit + 1)

    assert fits.fits is True
    assert fits.over_by_tokens == 0
    assert over.fits is False
    assert over.over_by_tokens == 1


def test_judge_request_reserves_the_configured_output_cap() -> None:
    """The cap always holds every verdict, so the reserve is just the cap."""
    budget = ReviewBudget(judge_output_tokens=2_000, safety_margin_tokens=1_000)

    request = budget.judge_request(prompt_tokens=10_000)

    assert request.output_reserve_tokens == 2_000
    assert request.reserve_tokens == 1_000
    assert request.required_tokens == 10_000 + 2_000 + 1_000


def test_judge_output_cap_bounds_the_whole_verdict_payload() -> None:
    budget = ReviewBudget(judge_output_tokens=2_048)

    assert budget.judge_output_covers(2_048) is True
    assert budget.judge_output_covers(2_049) is False


def test_request_budget_describes_the_account() -> None:
    request = RequestBudget(
        context_window_tokens=100_000,
        prompt_tokens=80_000,
        output_reserve_tokens=9_000,
        reserve_tokens=12_000,
    )

    assert request.required_tokens == 101_000
    assert request.over_by_tokens == 1_000
    described = request.describe()
    assert "80000" in described and "100000" in described


def test_contract_records_every_configured_number() -> None:
    budget = ReviewBudget(
        context_window_tokens=64_000,
        reviewer_output_tokens=2_048,
        reviewer_context_read_reserve_tokens=4_000,
        judge_output_tokens=1_024,
        safety_margin_tokens=512,
    )

    assert budget.contract() == {
        "context_window_tokens": 64_000,
        "reviewer_output_tokens": 2_048,
        "reviewer_context_read_reserve_tokens": 4_000,
        "judge_output_tokens": 1_024,
        "safety_margin_tokens": 512,
    }


def test_reviewer_tool_schema_reserve_stays_conservative() -> None:
    # The reviewer scope ships three small tool schemas; the reserve only has to
    # cover them, so keep it well under a percent of a typical window.
    assert 0 < REVIEWER_TOOL_SCHEMA_TOKENS < 8_000
