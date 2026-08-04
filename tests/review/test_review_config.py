from __future__ import annotations

from nanobot.config.schema import Config


def test_review_token_budget_fields_accept_camel_case() -> None:
    config = Config(
        review={
            "tokenBudget": 120_000,
            "prefetchBudgetChars": 12_000,
            "subagentEvidenceBudgetChars": 18_000,
        }
    )

    assert config.review.token_budget == 120_000
    assert config.review.prefetch_budget_chars == 12_000
    assert config.review.subagent_evidence_budget_chars == 18_000


def test_review_config_accepts_subagent_reasoning_effort() -> None:
    config = Config.model_validate({"review": {"subagentReasoningEffort": "medium"}})
    assert config.review.subagent_reasoning_effort == "medium"


def test_review_config_uses_per_review_concurrency_field() -> None:
    config = Config.model_validate({"review": {"maxConcurrentSubagents": 3}})

    assert config.review.max_concurrent_subagents == 3
