"""Tests for the AI judge: AgentRunner-backed batches, failure handling, stats.

These tests pin the judge contract directly (without the finalizer):
- every accepted/uncertain candidate enters the judge scope (no fixed cap);
- candidates are batched greedily within the runtime context window;
- each batch executes through the shared ``AgentRunner`` and the
  ``submit_verdicts`` terminal tool, with the same model as the plan run;
- any candidate without a valid verdict is explicitly needs_confirmation;
- ``JudgeExecutionResult`` carries verdicts, stats, usage and a bounded error;
- a failed or timed-out batch still reports the tokens it already consumed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.loop import AgentLoop
from nanoreview.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanoreview.bus.queue import MessageBus
from nanoreview.config.schema import (
    ModelPresetConfig,
    ReviewConfig,
    ReviewJudgeSettings,
)
from nanoreview.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanoreview.review.output.judge import JudgeExecutionResult, ReviewJudge, ReviewJudgeConfig
from nanoreview.review.types import (
    FindingVerdict,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
    ReviewJudgeDecision,
)

#: Window large enough that normal fixtures always fit in one batch.
_LARGE_WINDOW = 200_000

#: Sentinel reply meaning "the model answered with prose instead of the tool".
_PROSE = object()


@dataclass(frozen=True, slots=True)
class _RawArgs:
    """Reply that calls ``submit_verdicts`` with unchecked raw arguments."""

    arguments: dict[str, Any]


class FakeJudgeProvider:
    """Provider fake that records requests and returns scripted judge replies.

    ``replies`` is consumed per provider call; the last entry repeats once the
    script is exhausted (so ``[_PROSE]`` means "always answers prose"). Entries
    are a verdict list, a :class:`_RawArgs`, or ``_PROSE``.
    """

    def __init__(
        self,
        replies: list[Any] | None = None,
        *,
        errors: list[Exception | None] | None = None,
        error: Exception | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._replies = list(replies or [])
        self._errors = list(errors or [])
        self._error = error
        self._usage = usage

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        index = len(self.calls)
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        if index < len(self._errors) and self._errors[index] is not None:
            raise self._errors[index]
        reply = (
            self._replies[index]
            if index < len(self._replies)
            else (self._replies[-1] if self._replies else [])
        )
        usage = dict(self._usage) if self._usage else {}
        if reply is _PROSE:
            return LLMResponse(content="All candidates look fine to me.", usage=usage)
        arguments = (
            reply.arguments if isinstance(reply, _RawArgs) else {"verdicts": reply}
        )
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id=f"call-{index + 1}",
                    name="submit_verdicts",
                    arguments=arguments,
                )
            ],
            usage=usage,
        )


class RecordingRunner:
    """Delegate to a real ``AgentRunner`` while capturing every spec."""

    def __init__(self, provider: FakeJudgeProvider) -> None:
        self.specs: list[AgentRunSpec] = []
        self.inner = AgentRunner(provider)

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return await self.inner.run(spec)


class StubPlanProvider(LLMProvider):
    """Minimal provider so an ``AgentLoop`` can be constructed in tests."""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "plan-model"


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


def _judge(
    provider: FakeJudgeProvider,
    *,
    model: str = "test-model",
    runner: RecordingRunner | None = None,
    **config: Any,
) -> ReviewJudge:
    """Build a judge whose runner is a real ``AgentRunner`` over ``provider``."""
    return ReviewJudge(
        runner=runner or RecordingRunner(provider),
        model=model,
        config=ReviewJudgeConfig(context_window_tokens=_LARGE_WINDOW, **config),
    )


_SMALL_WINDOW_CONFIG = ReviewJudgeConfig(
    context_window_tokens=1_200,
    max_tokens=128,
    timeout_seconds=5,
)


def _small_window_judge(provider: FakeJudgeProvider) -> ReviewJudge:
    return ReviewJudge(
        runner=RecordingRunner(provider),
        model="test-model",
        config=_SMALL_WINDOW_CONFIG,
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
    provider = FakeJudgeProvider([[]])
    judge = _judge(provider)
    dimensions = [
        _dimension("security", ["S1", "S2"], ["S3"]),
        _dimension("bug", ["B1"]),
    ]

    result = await judge.judge_dimensions(dimensions)

    assert len(result.verdicts) == 4
    # One batch carried every candidate in a single payload.
    assert len(provider.calls) == 1
    prompt = provider.calls[0]["messages"][1]["content"]
    for title in ("S1", "S2", "S3", "B1"):
        assert title in prompt
    # No verdicts returned -> every candidate must be needs_confirmation.
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in result.verdicts.values()
    )
    assert result.error is None


async def test_judge_single_batch_fits_large_window() -> None:
    """A window that fits everything issues exactly one request."""
    provider = FakeJudgeProvider(
        [[_accept_verdict("security:src/app.py:1:s1"), _accept_verdict("security:src/app.py:1:s2")]]
    )
    judge = _judge(provider)
    dimensions = [_dimension("security", ["S1", "S2"])]

    result = await judge.judge_dimensions(dimensions)

    assert len(provider.calls) == 1
    assert all(
        verdict.decision == ReviewJudgeDecision.ACCEPT for verdict in result.verdicts.values()
    )
    assert result.stats is not None
    assert result.stats.batches == 1
    assert result.stats.sent_candidates == 2
    assert result.stats.returned_verdicts == 2


async def test_judge_splits_multiple_batches_under_small_window() -> None:
    """A small window forces sequential greedy batches; all candidates sent."""
    provider = FakeJudgeProvider([[], [], []])
    judge = _small_window_judge(provider)
    big_evidence = "lorem ipsum dolor sit amet " * 80  # ~700 tokens
    dimensions = [_dimension("security", ["S1", "S2", "S3"], evidence=big_evidence)]

    result = await judge.judge_dimensions(dimensions)

    assert len(provider.calls) >= 2
    assert result.stats is not None
    assert result.stats.batches == len(provider.calls)
    assert result.stats.sent_candidates == 3
    assert result.stats.returned_verdicts == 0
    # Model returned no verdicts: all candidates explicitly needs_confirmation.
    assert len(result.verdicts) == 3
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in result.verdicts.values()
    )


async def test_judge_single_batch_failure_does_not_block_other_batches() -> None:
    """One failing batch must not abort the remaining batches."""
    first_id = "security:src/app.py:1:s1"
    provider = FakeJudgeProvider(
        replies=[
            [],  # batch 1 fails before returning anything
            [_accept_verdict("security:src/app.py:1:s2")],
            [],
        ],
        errors=[RuntimeError("batch one down"), None, None],
    )
    judge = _small_window_judge(provider)
    big_evidence = "lorem ipsum dolor sit amet " * 80
    dimensions = [_dimension("security", ["S1", "S2", "S3"], evidence=big_evidence)]

    result = await judge.judge_dimensions(dimensions)

    assert len(provider.calls) >= 2
    assert result.error is not None and "batch one down" in result.error
    assert result.verdicts[first_id].decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "batch failed" in result.verdicts[first_id].reason
    # The surviving batches still produced real verdicts.
    assert any(
        verdict.decision == ReviewJudgeDecision.ACCEPT
        for verdict in result.verdicts.values()
    )
    assert result.stats is not None
    assert result.stats.batches == len(provider.calls)


async def test_judge_candidate_larger_than_window_is_needs_confirmation() -> None:
    """A candidate that cannot fit any batch is never sent to the model."""
    provider = FakeJudgeProvider()
    runner = RecordingRunner(provider)
    judge = ReviewJudge(
        runner=runner,
        model="test-model",
        config=ReviewJudgeConfig(
            context_window_tokens=2_000,
            max_tokens=128,
            timeout_seconds=5,
        ),
    )
    huge = "x" * 20_000
    dimensions = [_dimension("security", ["Huge"], evidence=huge)]

    result = await judge.judge_dimensions(dimensions)

    assert provider.calls == []  # nothing was ever sent
    assert runner.specs == []  # and no AgentRun was started
    assert len(result.verdicts) == 1
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "too large" in verdict.reason
    stats = result.stats
    assert stats is not None
    assert stats.total_candidates == 1
    assert stats.sent_candidates == 0
    assert stats.returned_verdicts == 0
    assert stats.needs_confirmation == 1
    assert stats.batches == 0


async def test_judge_fixed_overhead_exceeding_window_marks_all() -> None:
    """Window smaller than fixed request overhead: nothing sent, all pending."""
    provider = FakeJudgeProvider()
    runner = RecordingRunner(provider)
    judge = ReviewJudge(
        runner=runner,
        model="test-model",
        # Reserved output alone (2048) exceeds the window.
        config=ReviewJudgeConfig(context_window_tokens=1_000),
    )
    dimensions = [_dimension("security", ["S1", "S2"])]

    result = await judge.judge_dimensions(dimensions)

    assert provider.calls == []
    assert runner.specs == []
    assert len(result.verdicts) == 2
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in result.verdicts.values()
    )
    assert all("context window" in verdict.reason for verdict in result.verdicts.values())
    stats = result.stats
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

    result = await judge.judge_dimensions(dimensions)

    assert len(result.verdicts) == 2
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in result.verdicts.values()
    )
    assert all("batch failed" in verdict.reason for verdict in result.verdicts.values())
    stats = result.stats
    assert stats is not None
    # The batch was attempted (sent) but returned nothing.
    assert stats.sent_candidates == 2
    assert stats.returned_verdicts == 0
    assert stats.needs_confirmation == 2
    assert stats.batches == 1
    # A provider failure is a judge-level error, not a clean completion.
    assert result.error is not None
    assert "provider down" in result.error


class _NeverReturningProvider:
    """Provider whose request never completes, so the judge times out."""

    async def chat_with_retry(self, **kwargs: Any):
        await asyncio.sleep(30)


async def test_judge_batch_timeout_sets_error() -> None:
    """A judge request that exceeds the timeout is an error, not a completion."""
    judge = ReviewJudge(
        runner=AgentRunner(_NeverReturningProvider()),
        model="test-model",
        config=ReviewJudgeConfig(
            context_window_tokens=_LARGE_WINDOW,
            max_tokens=128,
            timeout_seconds=0.05,
        ),
    )

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert len(result.verdicts) == 1
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "batch failed" in verdict.reason
    # The bounded reason must be non-empty: the orchestrator treats an empty
    # error as a clean completion and would mark a timed-out batch completed.
    assert result.error and "timeout" in result.error


class _TimeoutAfterUsageProvider:
    """Answers one prose turn, then blocks so the judge batch times out."""

    def __init__(self, usage: dict[str, int]) -> None:
        self.calls = 0
        self._usage = usage

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="No verdicts from me.", usage=dict(self._usage))
        await asyncio.sleep(30)
        raise AssertionError("unreachable: the batch must be cancelled")


async def test_judge_timeout_keeps_usage_from_completed_iterations() -> None:
    """Tokens burned before a batch timeout still reach the run total."""
    provider = _TimeoutAfterUsageProvider(
        {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    )
    judge = ReviewJudge(
        runner=AgentRunner(provider),
        model="test-model",
        config=ReviewJudgeConfig(
            context_window_tokens=_LARGE_WINDOW,
            max_tokens=128,
            timeout_seconds=0.05,
        ),
    )

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    # The batch never returns a result, so only the observer can report the
    # first iteration's usage; dropping it would understate real spend.
    assert provider.calls >= 2
    assert result.error is not None and "timeout" in result.error
    assert result.usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "batch failed" in verdict.reason


async def test_judge_failed_batch_keeps_consumed_usage() -> None:
    """A batch that fails after real model calls still reports their tokens."""
    provider = FakeJudgeProvider(
        [_PROSE],  # never calls submit_verdicts -> terminal_tool_failed
        usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    )
    judge = _judge(provider, timeout_seconds=5)

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert result.error is not None
    assert len(provider.calls) > 1  # retries really hit the model
    assert result.usage == {
        "prompt_tokens": 120 * len(provider.calls),
        "completion_tokens": 30 * len(provider.calls),
        "total_tokens": 150 * len(provider.calls),
    }
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION


class _CompressionStopRunner:
    """Judge runner stub whose batch is stopped by run-level compression."""

    def __init__(self, stop_reason: str, usage: dict[str, int]) -> None:
        self.stop_reason = stop_reason
        self._usage = usage

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        return AgentRunResult(
            final_content=None,
            messages=list([*spec.frozen_messages, *spec.working_messages]),
            stop_reason=self.stop_reason,
            error="sync compression failed after 2 attempts: no content",
            usage=dict(self._usage),
        )


async def test_judge_compression_stop_is_a_failed_batch_with_usage() -> None:
    """A compression-stopped batch fails but still reports the tokens it spent."""
    usage = {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}
    judge = ReviewJudge(
        runner=_CompressionStopRunner("compression_failed", usage),  # type: ignore[arg-type]
        model="test-model",
        config=ReviewJudgeConfig(context_window_tokens=_LARGE_WINDOW, timeout_seconds=5),
    )

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert result.error is not None and "compression" in result.error
    # The spend is never hidden behind the failure.
    assert result.usage == usage
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION


def test_judge_usage_observer_folds_compression_usage() -> None:
    """The observer accumulates compression usage so it survives a timeout."""
    from nanoreview.review.output.judge import _JudgeUsageObserver

    observer = _JudgeUsageObserver()
    observer.record_compression_usage({"prompt_tokens": 7, "completion_tokens": 3})
    observer.record_compression_usage({"prompt_tokens": 5, "completion_tokens": 2})

    assert observer.usage == {"prompt_tokens": 12, "completion_tokens": 5}


async def test_judge_cancellation_propagates() -> None:
    """``/stop`` must cancel a judge batch instead of being swallowed."""
    judge = ReviewJudge(
        runner=AgentRunner(_NeverReturningProvider()),
        model="test-model",
        config=ReviewJudgeConfig(
            context_window_tokens=_LARGE_WINDOW,
            max_tokens=128,
            timeout_seconds=30,
        ),
    )

    task = asyncio.create_task(judge.judge_dimensions([_dimension("security", ["S1"])]))
    await asyncio.sleep(0)  # let the batch reach the provider await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_judge_error_resets_between_runs() -> None:
    """A later run must not inherit the previous run's judge error."""
    provider = FakeJudgeProvider(error=RuntimeError("boom"))
    judge = _judge(provider)
    first = await judge.judge_dimensions([_dimension("security", ["S1"])])
    assert first.error and "boom" in first.error

    second = await judge.judge_dimensions([_dimension("security", [])])
    assert second.error is None
    assert second.stats is None
    assert second.usage == {}


async def test_judge_disabled_leaves_no_error() -> None:
    """Disabled/overhead/unbatchable are normal no-request paths, not errors."""
    provider = FakeJudgeProvider()
    judge = _judge(provider, enabled=False)
    result = await judge.judge_dimensions([_dimension("security", ["S1"])])
    assert result.error is None


async def test_judge_partial_and_unknown_verdicts() -> None:
    """Missing, unknown, and duplicate verdicts do not leak into stats."""
    first_id = "security:src/app.py:1:s1"
    provider = FakeJudgeProvider(
        [
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

    result = await judge.judge_dimensions(dimensions)

    verdicts = result.verdicts
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
    stats = result.stats
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

    result = await judge.judge_dimensions(dimensions)

    assert provider.calls == []
    assert len(result.verdicts) == 2
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in result.verdicts.values()
    )
    assert all("disabled" in verdict.reason for verdict in result.verdicts.values())
    stats = result.stats
    assert stats is not None
    assert stats.total_candidates == 2
    assert stats.sent_candidates == 0
    assert stats.returned_verdicts == 0
    assert stats.needs_confirmation == 2
    assert stats.batches == 0


async def test_judge_stats_reflect_exact_send_and_return_counts() -> None:
    """Stats match the actual request/verdict flow end to end."""
    ids = [f"security:src/app.py:1:s{i}" for i in range(1, 4)]
    provider = FakeJudgeProvider(
        [
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

    result = await judge.judge_dimensions(dimensions)

    stats = result.stats
    assert stats is not None
    assert stats.total_candidates == 3
    assert stats.sent_candidates == 3
    assert stats.returned_verdicts == 3
    assert stats.needs_confirmation == 1
    assert stats.batches == 1
    verdicts = result.verdicts
    assert len(verdicts) == 3
    assert verdicts[ids[0]].decision == ReviewJudgeDecision.ACCEPT
    assert verdicts[ids[1]].decision == ReviewJudgeDecision.REJECT
    assert verdicts[ids[2]].decision == ReviewJudgeDecision.NEEDS_CONFIRMATION


async def test_judge_no_candidates_leaves_stats_none() -> None:
    """Nothing to judge: no request, no stats."""
    provider = FakeJudgeProvider()
    judge = _judge(provider)

    result = await judge.judge_dimensions([_dimension("security", [])])

    assert result.verdicts == {}
    assert provider.calls == []
    assert result.stats is None
    assert result.usage == {}


async def test_judge_usage_aggregates_every_batch() -> None:
    """Judge tokens are summed across batches so the run can report them."""
    provider = FakeJudgeProvider(
        [[], [], []],
        usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    )
    big_evidence = "lorem ipsum dolor sit amet " * 80  # ~700 tokens
    dimensions = [_dimension("security", ["S1", "S2", "S3"], evidence=big_evidence)]
    judge = _small_window_judge(provider)

    result = await judge.judge_dimensions(dimensions)

    calls = len(provider.calls)
    assert calls >= 2, "expected the fixtures to split into batches"
    # One batch would return 150 here; multi-batch runs multiply it.
    assert result.usage["total_tokens"] == 150 * calls
    assert result.usage["prompt_tokens"] == 120 * calls
    assert result.usage["completion_tokens"] == 30 * calls


async def test_judge_usage_resets_between_runs() -> None:
    """A second run must not inherit the previous run's usage."""
    provider = FakeJudgeProvider(usage={"total_tokens": 10})
    judge = _judge(provider)
    first = await judge.judge_dimensions([_dimension("security", ["S1"])])
    assert first.usage["total_tokens"] == 10

    second = await judge.judge_dimensions([_dimension("security", [])])
    assert second.usage == {}


async def test_judge_batch_runs_through_shared_runner_with_terminal_spec() -> None:
    """The batch uses the injected runner and a fully configured AgentRunSpec."""
    provider = FakeJudgeProvider([[]])
    runner = RecordingRunner(provider)
    judge = _judge(provider, runner=runner, max_tokens=128)
    dimensions = [_dimension("security", ["S1"])]

    await judge.judge_dimensions(dimensions)

    assert len(runner.specs) == 1
    spec = runner.specs[0]
    assert spec.model == "test-model"
    assert spec.temperature == 0
    assert spec.max_tokens == 128
    assert spec.context_window_tokens == _LARGE_WINDOW
    assert spec.error_message is None
    assert spec.tool_choice == {
        "type": "function",
        "function": {"name": "submit_verdicts"},
    }
    assert spec.terminal_tools == frozenset({"submit_verdicts"})
    assert spec.terminal_retry_limit == 5
    assert spec.max_iterations > spec.terminal_retry_limit
    # Only the judge terminal tool is exposed, and the batch shares no session,
    # workspace, checkpoint, injection or permission state with the plan run.
    assert spec.tools.tool_names == ["submit_verdicts"]
    assert spec.workspace is None
    assert spec.session_key is None
    assert spec.checkpoint_callback is None
    assert spec.injection_callback is None
    assert spec.permission_policy is None
    assert [message["role"] for message in [*spec.frozen_messages, *spec.working_messages]] == ["system", "user"]
    assert "S1" in [*spec.frozen_messages, *spec.working_messages][1]["content"]


async def test_judge_prose_response_marks_candidates_needs_confirmation() -> None:
    """A model that only answers prose fails the batch, never accepts silently."""
    provider = FakeJudgeProvider([_PROSE])
    judge = _judge(provider, timeout_seconds=5)

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert len(provider.calls) > 1  # terminal retry re-asks for the tool
    assert result.error is not None
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "batch failed" in verdict.reason


async def test_judge_invalid_tool_arguments_are_retried_then_succeed() -> None:
    """Bad terminal arguments are fed back and a corrected submission wins."""
    candidate_id = "security:src/app.py:1:s1"
    provider = FakeJudgeProvider(
        [_RawArgs({"verdicts": "not-an-array"}), [_accept_verdict(candidate_id)]]
    )
    judge = _judge(provider, timeout_seconds=5)

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert len(provider.calls) == 2
    assert result.error is None
    assert result.verdicts[candidate_id].decision == ReviewJudgeDecision.ACCEPT


async def test_judge_permanently_invalid_arguments_fail_the_batch() -> None:
    """Invalid arguments repeated to the retry limit fail with a bounded error."""
    provider = FakeJudgeProvider([_RawArgs({"verdicts": "not-an-array"})])
    judge = _judge(provider, timeout_seconds=5)

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert result.error is not None
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "batch failed" in verdict.reason


async def test_judge_ignores_configured_model_preset(tmp_path: Path) -> None:
    """``review.judge.model_preset`` must not fork the judge onto another model."""
    loop = AgentLoop(
        MessageBus(),
        StubPlanProvider(),
        tmp_path,
        review_config=ReviewConfig(
            judge=ReviewJudgeSettings(model_preset="judge-premium")
        ),
        model_presets={
            "judge-premium": ModelPresetConfig(model="other-model", provider="auto")
        },
    )

    judge = loop._build_review_judge()  # noqa: SLF001

    assert judge is not None
    # Even though the preset exists and would resolve to a different model, the
    # judge runs on the plan's runner and model.
    assert loop.model_presets["judge-premium"].model == "other-model"
    assert judge._runner is loop.runner  # noqa: SLF001
    assert judge._runner.provider is loop.provider  # noqa: SLF001
    assert judge._model == loop.model  # noqa: SLF001
    assert judge._model == "plan-model"  # noqa: SLF001


async def test_judge_disabled_setting_yields_no_judge(tmp_path: Path) -> None:
    """A disabled judge keeps its original meaning: no judge object at all."""
    loop = AgentLoop(
        MessageBus(),
        StubPlanProvider(),
        tmp_path,
        review_config=ReviewConfig(judge=ReviewJudgeSettings(enabled=False)),
    )

    assert loop._build_review_judge() is None  # noqa: SLF001


def test_judge_execution_result_is_immutable() -> None:
    """The result is a frozen value object, not a mutable cross-call channel."""
    result = JudgeExecutionResult(verdicts={}, stats=None, usage={})
    with pytest.raises(Exception):
        result.error = "mutated"  # type: ignore[misc]
