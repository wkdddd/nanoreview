"""Tests for the AI judge: full-candidate batching, failure handling, and stats.

These tests pin the judge contract directly (without the finalizer):
- every accepted/uncertain candidate enters the judge scope (no fixed cap);
- candidates are batched greedily within the runtime context window;
- any candidate without a valid verdict is explicitly needs_confirmation;
- ``last_stats`` reflects what was actually sent and returned.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from nanoreview.providers.base import LLMResponse, ToolCallRequest
from nanoreview.review.output.judge import ReviewJudge, ReviewJudgeConfig
from nanoreview.review.types import (
    FindingVerdict,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
    ReviewJudgeDecision,
    ReviewJudgeVerdict,
)

#: Window large enough that normal fixtures always fit in one batch.
_LARGE_WINDOW = 200_000


class FakeJudgeProvider:
    """Provider fake that records requests and returns canned verdicts."""

    def __init__(
        self,
        verdicts_by_call: list[list[dict[str, str]]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdicts_by_call = verdicts_by_call or []
        self._error = error

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        index = len(self.calls) - 1
        verdicts = (
            self._verdicts_by_call[index]
            if index < len(self._verdicts_by_call)
            else []
        )
        tool_call = ToolCallRequest(
            id="call-1",
            name="submit_verdicts",
            arguments={"verdicts": verdicts},
        )
        return LLMResponse(content=None, tool_calls=[tool_call])


def _candidate(
    dimension: str,
    title: str,
    *,
    evidence: str = "value = 1",
    severity: str = "high",
) -> ReviewFindingCandidate:
    return ReviewFindingCandidate(
        severity=severity,
        dimension=dimension,
        file="src/app.py",
        line=1,
        title=title,
        evidence=evidence,
        impact="bad",
        recommendation="fix",
    )


def _dimension(
    name: str,
    accepted_titles: list[str],
    uncertain_titles: list[str] | None = None,
    *,
    evidence: str = "value = 1",
) -> ReviewDimensionResult:
    return ReviewDimensionResult(
        dimension=name,
        status="validated",
        accepted=[
            _candidate(name, title, evidence=evidence)
            for title in accepted_titles
        ],
        uncertain=[
            (
                _candidate(name, title, evidence=evidence),
                ReviewFindingVerdict(
                    verdict=FindingVerdict.UNCERTAIN,
                    reason="evidence not found in file",
                ),
            )
            for title in (uncertain_titles or [])
        ],
    )


def _judge(provider: FakeJudgeProvider, **config: Any) -> ReviewJudge:
    return ReviewJudge(
        provider=provider,
        model="test-model",
        config=ReviewJudgeConfig(context_window_tokens=_LARGE_WINDOW, **config),
    )


def _accept_verdict(candidate_id: str) -> dict[str, str]:
    return {
        "id": candidate_id,
        "decision": "accept",
        "reason": "supported by evidence",
        "confidence": "high",
    }


async def test_judge_collects_all_candidates_from_all_dimensions() -> None:
    """Accepted and uncertain candidates across dimensions all enter scope."""
    provider = FakeJudgeProvider(verdicts_by_call=[[]])
    judge = _judge(provider)
    dimensions = [
        _dimension("security", ["S1", "S2"], ["S3"]),
        _dimension("bug", ["B1"]),
    ]

    verdicts = await judge.judge_dimensions(dimensions)

    assert len(verdicts) == 4
    # One request carried every candidate in a single payload.
    assert len(provider.calls) == 1
    prompt = provider.calls[0]["messages"][1]["content"]
    for title in ("S1", "S2", "S3", "B1"):
        assert title in prompt
    # No verdicts returned -> every candidate must be needs_confirmation.
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in verdicts.values()
    )


async def test_judge_single_batch_fits_large_window() -> None:
    """A window that fits everything issues exactly one request."""
    provider = FakeJudgeProvider(
        verdicts_by_call=[
            [
                _accept_verdict("security:src/app.py:1:s1"),
                _accept_verdict("security:src/app.py:1:s2"),
            ]
        ]
    )
    judge = _judge(provider)
    dimensions = [_dimension("security", ["S1", "S2"])]

    verdicts = await judge.judge_dimensions(dimensions)

    assert len(provider.calls) == 1
    assert all(
        verdict.decision == ReviewJudgeDecision.ACCEPT for verdict in verdicts.values()
    )
    assert judge.last_stats is not None
    assert judge.last_stats.batches == 1
    assert judge.last_stats.sent_candidates == 2
    assert judge.last_stats.returned_verdicts == 2


async def test_judge_splits_multiple_batches_under_small_window() -> None:
    """A small window forces sequential greedy batches; all candidates sent."""
    provider = FakeJudgeProvider(verdicts_by_call=[[], [], []])
    # Window only fits the fixed overhead plus roughly one large candidate.
    judge = ReviewJudge(
        provider=provider,
        model="test-model",
        config=ReviewJudgeConfig(
            context_window_tokens=1_200,
            max_tokens=128,
            timeout_seconds=5,
        ),
    )
    big_evidence = "lorem ipsum dolor sit amet " * 80  # ~700 tokens
    dimensions = [_dimension("security", ["S1", "S2", "S3"], evidence=big_evidence)]

    verdicts = await judge.judge_dimensions(dimensions)

    assert len(provider.calls) >= 2
    assert judge.last_stats is not None
    assert judge.last_stats.batches == len(provider.calls)
    assert judge.last_stats.sent_candidates == 3
    assert judge.last_stats.returned_verdicts == 0
    # Model returned no verdicts: all candidates explicitly needs_confirmation.
    assert len(verdicts) == 3
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in verdicts.values()
    )


async def test_judge_candidate_larger_than_window_is_needs_confirmation() -> None:
    """A candidate that cannot fit any batch is never sent to the model."""
    provider = FakeJudgeProvider()
    judge = ReviewJudge(
        provider=provider,
        model="test-model",
        config=ReviewJudgeConfig(
            context_window_tokens=2_000,
            max_tokens=128,
            timeout_seconds=5,
        ),
    )
    huge = "x" * 20_000
    dimensions = [_dimension("security", ["Huge"], evidence=huge)]

    verdicts = await judge.judge_dimensions(dimensions)

    assert provider.calls == []  # nothing was ever sent
    assert len(verdicts) == 1
    verdict = next(iter(verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "too large" in verdict.reason
    stats = judge.last_stats
    assert stats is not None
    assert stats.total_candidates == 1
    assert stats.sent_candidates == 0
    assert stats.returned_verdicts == 0
    assert stats.needs_confirmation == 1
    assert stats.batches == 0


async def test_judge_fixed_overhead_exceeding_window_marks_all() -> None:
    """Window smaller than fixed request overhead: nothing sent, all pending."""
    provider = FakeJudgeProvider()
    judge = ReviewJudge(
        provider=provider,
        model="test-model",
        # Reserved output alone (2048) exceeds the window.
        config=ReviewJudgeConfig(context_window_tokens=1_000),
    )
    dimensions = [_dimension("security", ["S1", "S2"])]

    verdicts = await judge.judge_dimensions(dimensions)

    assert provider.calls == []
    assert len(verdicts) == 2
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in verdicts.values()
    )
    assert all("context window" in verdict.reason for verdict in verdicts.values())
    stats = judge.last_stats
    assert stats is not None
    assert stats.total_candidates == 2
    assert stats.sent_candidates == 0
    assert stats.returned_verdicts == 0
    assert stats.needs_confirmation == 2
    assert stats.batches == 0


async def test_judge_batch_exception_marks_batch_needs_confirmation() -> None:
    """A provider failure never silently accepts the batch's candidates."""
    provider = FakeJudgeProvider(error=RuntimeError("provider down"))
    judge = _judge(provider)
    dimensions = [_dimension("security", ["S1", "S2"])]

    verdicts = await judge.judge_dimensions(dimensions)

    assert len(verdicts) == 2
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in verdicts.values()
    )
    assert all("batch failed" in verdict.reason for verdict in verdicts.values())
    stats = judge.last_stats
    assert stats is not None
    # The batch was attempted (sent) but returned nothing.
    assert stats.sent_candidates == 2
    assert stats.returned_verdicts == 0
    assert stats.needs_confirmation == 2
    assert stats.batches == 1


async def test_judge_partial_and_unknown_verdicts() -> None:
    """Missing, unknown, and duplicate verdicts do not leak into stats."""
    first_id = "security:src/app.py:1:s1"
    provider = FakeJudgeProvider(
        verdicts_by_call=[
            [
                _accept_verdict(first_id),
                # Unknown id: must not be counted or returned.
                _accept_verdict("unknown:src/app.py:1:ghost"),
                # Duplicate id: dict collapse means one verdict.
                {
                    "id": first_id,
                    "decision": "reject",
                    "reason": "dup",
                    "confidence": "low",
                },
            ]
        ]
    )
    judge = _judge(provider)
    dimensions = [_dimension("security", ["S1", "S2"])]

    verdicts = await judge.judge_dimensions(dimensions)

    assert set(verdicts) == {
        "security:src/app.py:1:s1",
        "security:src/app.py:1:s2",
    }
    # Duplicate overwrote the accept: last write wins per candidate id.
    assert verdicts[first_id].decision == ReviewJudgeDecision.REJECT
    # S2 got no verdict -> explicit needs_confirmation.
    assert (
        verdicts["security:src/app.py:1:s2"].decision
        == ReviewJudgeDecision.NEEDS_CONFIRMATION
    )
    stats = judge.last_stats
    assert stats is not None
    assert stats.total_candidates == 2
    assert stats.sent_candidates == 2
    # Only the verdict matching a real candidate id counts once.
    assert stats.returned_verdicts == 1
    assert stats.needs_confirmation == 1


async def test_judge_disabled_marks_all_needs_confirmation() -> None:
    """A disabled judge is an ops switch, not a silent pass-through."""
    provider = FakeJudgeProvider()
    judge = _judge(provider, enabled=False)
    dimensions = [_dimension("security", ["S1"], ["S2"])]

    verdicts = await judge.judge_dimensions(dimensions)

    assert provider.calls == []
    assert len(verdicts) == 2
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in verdicts.values()
    )
    assert all("disabled" in verdict.reason for verdict in verdicts.values())
    stats = judge.last_stats
    assert stats is not None
    assert stats.total_candidates == 2
    assert stats.sent_candidates == 0
    assert stats.returned_verdicts == 0
    assert stats.needs_confirmation == 2
    assert stats.batches == 0


async def test_judge_stats_reflect_exact_send_and_return_counts() -> None:
    """last_stats matches the actual request/verdict flow end to end."""
    ids = [f"security:src/app.py:1:s{i}" for i in range(1, 4)]
    provider = FakeJudgeProvider(
        verdicts_by_call=[
            [
                {
                    "id": ids[0],
                    "decision": "accept",
                    "reason": "ok",
                    "confidence": "high",
                },
                {
                    "id": ids[1],
                    "decision": "reject",
                    "reason": "vague",
                    "confidence": "high",
                },
                {
                    "id": ids[2],
                    "decision": "needs_confirmation",
                    "reason": "plausible but unverified",
                    "confidence": "medium",
                },
            ]
        ]
    )
    judge = _judge(provider)
    dimensions = [_dimension("security", ["S1", "S2", "S3"])]

    verdicts = await judge.judge_dimensions(dimensions)

    stats = judge.last_stats
    assert stats is not None
    assert stats.total_candidates == 3
    assert stats.sent_candidates == 3
    assert stats.returned_verdicts == 3
    assert stats.needs_confirmation == 1
    assert stats.batches == 1
    assert len(verdicts) == 3
    assert verdicts[ids[0]].decision == ReviewJudgeDecision.ACCEPT
    assert verdicts[ids[1]].decision == ReviewJudgeDecision.REJECT
    assert verdicts[ids[2]].decision == ReviewJudgeDecision.NEEDS_CONFIRMATION


async def test_judge_no_candidates_leaves_stats_none() -> None:
    """Nothing to judge: no request, no stats."""
    provider = FakeJudgeProvider()
    judge = _judge(provider)

    verdicts = await judge.judge_dimensions([_dimension("security", [])])

    assert verdicts == {}
    assert provider.calls == []
    assert judge.last_stats is None
