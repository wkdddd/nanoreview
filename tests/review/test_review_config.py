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


def test_review_config_accepts_subagent_reasoning_effort() -> None:
    config = Config.model_validate({"review": {"subagentReasoningEffort": "medium"}})
    assert config.review.subagent_reasoning_effort == "medium"


def test_review_config_uses_per_review_concurrency_field() -> None:
    config = Config.model_validate({"review": {"maxConcurrentSubagents": 3}})

    assert config.review.max_concurrent_subagents == 3
