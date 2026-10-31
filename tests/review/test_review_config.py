from __future__ import annotations

from nanoreview.config.schema import Config


def test_review_evidence_token_budget_fields_accept_camel_case() -> None:
    config = Config(
        review={
            "evidenceTokenBudget": 120_000,
            "prefetchBudgetChars": 12_000,
        }
    )

    assert config.review.evidence_token_budget == 120_000
    assert config.review.prefetch_budget_chars == 12_000


def test_removed_whole_repository_budget_fields_are_ignored() -> None:
    """旧配置里的完整文件管线字段按现有未知字段规则忽略，不做迁移。"""
    config = Config(
        review={
            "subagentEvidenceBudgetChars": 18_000,
            "prefetchDenseBackfillLimit": 64,
        }
    )

    assert not hasattr(config.review, "subagent_evidence_budget_chars")
    assert not hasattr(config.review, "prefetch_dense_backfill_limit")


def test_role_model_overrides_accept_camel_case() -> None:
    """Reviewer/Judge parameters are per-field overrides in camelCase."""
    config = Config.model_validate(
        {
            "review": {
                "reviewer": {
                    "model": "reviewer-model",
                    "contextWindowTokens": 131_072,
                    "maxTokens": 4_096,
                    "reasoningEffort": "medium",
                },
                "judge": {"temperature": 0.1},
            }
        }
    )

    reviewer = config.review.reviewer
    assert reviewer.model == "reviewer-model"
    assert reviewer.context_window_tokens == 131_072
    assert reviewer.max_tokens == 4_096
    assert reviewer.reasoning_effort == "medium"
    # Unset fields inherit the conversation agent, so they stay ``None``.
    assert reviewer.provider is None
    assert reviewer.temperature is None
    assert config.review.judge.temperature == 0.1


def test_the_removed_role_reasoning_setting_is_ignored() -> None:
    """``review.subagentReasoningEffort`` moved onto the reviewer role block."""
    config = Config.model_validate({"review": {"subagentReasoningEffort": "medium"}})

    assert not hasattr(config.review, "subagent_reasoning_effort")


def test_review_config_uses_per_review_concurrency_field() -> None:
    config = Config.model_validate({"review": {"maxConcurrentSubagents": 3}})

    assert config.review.max_concurrent_subagents == 3
