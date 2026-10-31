"""Tests for the AI judge: one complete adjudication, budget rejection, stats.

These tests pin the judge contract directly (without the finalizer):
- every accepted/uncertain candidate enters the judge scope (no fixed cap);
- the whole candidate set is adjudicated in exactly one request — there is no
  batching, and a set that does not fit is rejected before the model is called;
- the request budget covers the input *and* the output the verdicts need;
- the run executes through the shared ``AgentRunner``; it has no tools at all
  and its final answer must be the structured verdict payload;
- any candidate without a valid verdict is explicitly needs_confirmation;
- ``JudgeExecutionResult`` carries verdicts, stats, usage and a bounded error;
- a failed or timed-out run still reports the tokens it already consumed.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.llm_runtime import ROLE_JUDGE, LLMRuntime
from nanoreview.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanoreview.bus.queue import MessageBus
from nanoreview.config.schema import ReviewConfig, ReviewJudgeSettings
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review.budget import ReviewBudget
from nanoreview.review.output.judge import JudgeExecutionResult, ReviewJudge, ReviewJudgeConfig
from nanoreview.review.types import (
    FindingVerdict,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
    ReviewJudgeDecision,
)

#: Window large enough that normal fixtures always fit in one request.
_LARGE_WINDOW = 200_000

#: Sentinel reply meaning "the model answered with prose instead of the payload".
_PROSE = object()


@dataclass(frozen=True, slots=True)
class _RawArgs:
    """Reply carrying a structurally wrong verdict payload."""

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
        payload = (
            reply.arguments if isinstance(reply, _RawArgs) else {"verdicts": reply}
        )
        return LLMResponse(content=json.dumps(payload), usage=usage)


class RecordingRunner:
    """Delegate to a real ``AgentRunner`` while capturing every spec."""

    def __init__(self, provider: FakeJudgeProvider) -> None:
        self.specs: list[AgentRunSpec] = []
        self.inner = AgentRunner(provider)

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return await self.inner.run(spec)


class StubPlanProvider(LLMProvider):
    """Minimal provider so an ``SessionCoordinator`` can be constructed in tests."""

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


def _budget(
    window: int = _LARGE_WINDOW,
    *,
    output: int = 8_192,
    margin: int = 4_000,
) -> ReviewBudget:
    """Budget of the judge under test; one object, no scalar round trip."""
    return ReviewBudget(
        context_window_tokens=window,
        judge_output_tokens=output,
        safety_margin_tokens=margin,
    )


def _runtime(
    provider: Any,
    *,
    budget: ReviewBudget | None = None,
    model: str = "test-model",
) -> LLMRuntime:
    """The Judge runtime frozen at admission.

    Window and output cap live here, not in the judge config: this is the
    snapshot whose numbers the request is both checked against and sent with.
    """
    resolved = budget or _budget()
    return LLMRuntime.capture(
        provider,
        model=model,
        context_window_tokens=resolved.context_window_tokens,
        role=ROLE_JUDGE,
    ).with_generation(max_tokens=resolved.judge_output_tokens)


def _judge(
    provider: FakeJudgeProvider,
    *,
    model: str = "test-model",
    runner: RecordingRunner | None = None,
    budget: ReviewBudget | None = None,
    **config: Any,
) -> ReviewJudge:
    """Build a judge whose runner is a real ``AgentRunner`` over ``provider``."""
    resolved = budget or _budget()
    return ReviewJudge(
        runner=runner or RecordingRunner(provider),
        runtime=_runtime(provider, budget=resolved, model=model),
        config=ReviewJudgeConfig(budget=resolved, **config),
    )


#: Window that a judge's fixed overhead (output cap 512 + no safety margin)
#: easily fits, so only the candidate payload can push the request over. A
#: window below that overhead would reject every payload, which would leave the
#: "the complete set must fit" rule untested.
_SMALL_WINDOW_BUDGET = _budget(8_000, output=512, margin=0)


def _small_window_judge(
    provider: FakeJudgeProvider, runner: RecordingRunner | None = None
) -> ReviewJudge:
    return ReviewJudge(
        runner=runner or RecordingRunner(provider),
        runtime=_runtime(provider, budget=_SMALL_WINDOW_BUDGET),
        config=ReviewJudgeConfig(budget=_SMALL_WINDOW_BUDGET),
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


async def test_judge_one_request_carries_the_whole_candidate_set() -> None:
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
    assert result.budget_rejected is False
    assert result.stats is not None
    assert result.stats.sent_candidates == 2
    assert result.stats.returned_verdicts == 2


async def test_judge_rejects_a_set_that_does_not_fit_instead_of_splitting() -> None:
    """The complete set is served in one request or not adjudicated at all."""
    provider = FakeJudgeProvider()
    runner = RecordingRunner(provider)
    judge = _small_window_judge(provider, runner=runner)
    # One candidate carrying a large evidence body is enough to overflow the
    # window; the fixed reserve alone would still fit.
    big_evidence = "lorem ipsum dolor sit amet " * 2_000
    dimensions = [_dimension("security", ["S1"], evidence=big_evidence)]

    result = await judge.judge_dimensions(dimensions)

    # Zero model calls and no AgentRun at all: the set never reached the judge.
    assert provider.calls == []
    assert runner.specs == []
    assert result.budget_rejected is True
    assert result.verdicts == {}
    assert result.error is not None
    assert "does not fit one judge request" in result.error
    stats = result.stats
    assert stats is not None
    assert stats.total_candidates == 1
    assert stats.sent_candidates == 0
    assert stats.returned_verdicts == 0
    assert stats.needs_confirmation == 1


async def test_judge_runs_when_the_candidate_set_fits_the_same_window() -> None:
    """Control case: the rejection above is caused by the payload, not the cap."""
    provider = FakeJudgeProvider()
    runner = RecordingRunner(provider)
    judge = _small_window_judge(provider, runner=runner)
    dimensions = [_dimension("security", ["S1", "S2"], evidence="value = 1")]

    result = await judge.judge_dimensions(dimensions)

    assert len(provider.calls) == 1
    assert len(runner.specs) == 1
    assert result.budget_rejected is False
    assert result.error is None


async def test_judge_rejects_a_cjk_candidate_set_without_a_tokenizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tokenizer-free fallback must not under-count a Chinese prompt.

    Four characters per token would score this 10k-character Chinese evidence
    at ~2.5k tokens and let the request through; the fallback has to stay an
    upper bound so the rejection happens here instead of at the provider.
    """
    monkeypatch.setattr(
        "nanoreview.utils.helpers.estimate_prompt_tokens",
        lambda *_args, **_kwargs: 0,
    )
    provider = FakeJudgeProvider()
    runner = RecordingRunner(provider)
    judge = _small_window_judge(provider, runner=runner)
    dimensions = [_dimension("security", ["S1"], evidence="连接池已耗尽请检查配置" * 1_000)]

    result = await judge.judge_dimensions(dimensions)

    assert provider.calls == []
    assert runner.specs == []
    assert result.budget_rejected is True
    assert result.error is not None
    assert "does not fit one judge request" in result.error


async def test_judge_rejects_a_window_below_its_own_reserve() -> None:
    """A window smaller than the output cap plus margin can never be served.

    This is the misconfiguration branch: the request is refused before any model
    call instead of being sent and failing at the provider.
    """
    provider = FakeJudgeProvider()
    runner = RecordingRunner(provider)
    budget = _budget(2_000, output=128)
    judge = ReviewJudge(
        runner=runner,
        runtime=_runtime(provider, budget=budget),
        config=ReviewJudgeConfig(budget=budget),
    )
    dimensions = [_dimension("security", ["S1"], evidence="value = 1")]

    result = await judge.judge_dimensions(dimensions)

    assert provider.calls == []  # nothing was ever sent
    assert runner.specs == []  # and no AgentRun was started
    assert result.budget_rejected is True
    assert result.verdicts == {}
    assert result.error is not None and "does not fit" in result.error
    stats = result.stats
    assert stats is not None
    assert stats.total_candidates == 1
    assert stats.sent_candidates == 0
    assert stats.needs_confirmation == 1


async def test_judge_rejects_when_the_output_reserve_cannot_hold_every_verdict() -> None:
    """Input fitting is not enough: the verdict payload must fit the output cap."""
    provider = FakeJudgeProvider()
    runner = RecordingRunner(provider)
    judge = _judge(provider, runner=runner, budget=_budget(output=256))
    dimensions = [_dimension("security", [f"S{index}" for index in range(60)])]

    result = await judge.judge_dimensions(dimensions)

    assert provider.calls == []
    assert runner.specs == []
    assert result.budget_rejected is True
    assert result.error is not None and "output tokens" in result.error
    assert "256" in result.error
    stats = result.stats
    assert stats is not None
    assert stats.total_candidates == 60
    assert stats.sent_candidates == 0
    assert stats.needs_confirmation == 60


async def test_judge_provider_failure_marks_candidates_needs_confirmation() -> None:
    """A provider failure never silently accepts the candidate set."""
    provider = FakeJudgeProvider(error=RuntimeError("provider down"))
    judge = _judge(provider)
    dimensions = [_dimension("security", ["S1", "S2"])]

    result = await judge.judge_dimensions(dimensions)

    assert len(result.verdicts) == 2
    assert all(
        verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        for verdict in result.verdicts.values()
    )
    assert all("AI judge failed" in verdict.reason for verdict in result.verdicts.values())
    stats = result.stats
    assert stats is not None
    # The request was attempted (sent) but returned nothing.
    assert stats.sent_candidates == 2
    assert stats.returned_verdicts == 0
    assert stats.needs_confirmation == 2
    # A provider failure is a judge-level error, not a clean completion.
    assert result.error is not None
    assert "provider down" in result.error


class _NeverReturningProvider:
    """Provider whose request never completes, so a run can still be cancelled."""

    async def chat_with_retry(self, **kwargs: Any):
        await asyncio.sleep(30)


class _SlowProvider:
    """Provider whose request is cut off by its own timeout.

    There is no judge-level wall clock any more: a slow request is bounded by
    the provider timeout, which surfaces as an error response and therefore as
    ``stop_reason="error"``. This is the shape the judge must still report.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content="Error calling LLM: timed out after 300s",
            finish_reason="error",
            error_kind="timeout",
        )


async def test_judge_provider_timeout_sets_error() -> None:
    """A request the provider times out is an error, not a completion."""
    provider = _SlowProvider()
    judge = ReviewJudge(
        runner=AgentRunner(provider),
        runtime=_runtime(provider, budget=_budget(output=128)),
    )

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert provider.calls == 1
    assert len(result.verdicts) == 1
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "AI judge failed" in verdict.reason
    # The bounded reason must be non-empty: the orchestrator treats an empty
    # error as a clean completion and would mark a timed-out run completed.
    assert result.error and "timed out" in result.error


class _TimeoutAfterUsageProvider:
    """Answers one prose turn, then times out on the retry."""

    def __init__(self, usage: dict[str, int]) -> None:
        self.calls = 0
        self._usage = usage

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="No verdicts from me.", usage=dict(self._usage))
        return LLMResponse(
            content="Error calling LLM: timed out after 300s",
            finish_reason="error",
            error_kind="timeout",
        )


async def test_judge_timeout_keeps_usage_from_completed_iterations() -> None:
    """Tokens burned before the provider timed out still reach the run total."""
    provider = _TimeoutAfterUsageProvider(
        {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    )
    judge = ReviewJudge(
        runner=AgentRunner(provider),
        runtime=_runtime(provider, budget=_budget(output=128)),
    )

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    # The first iteration's spend must not be dropped just because the run
    # ended in a provider timeout.
    assert provider.calls >= 2
    assert result.error is not None and "timed out" in result.error
    assert result.usage["total_tokens"] == 150
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "AI judge failed" in verdict.reason


async def test_judge_failed_run_keeps_consumed_usage() -> None:
    """A run that fails after real model calls still reports their tokens."""
    provider = FakeJudgeProvider(
        [_PROSE],  # never calls submit_verdicts -> terminal_tool_failed
        usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    )
    judge = _judge(provider)

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


class _ContextLimitRunner:
    """Judge runner stub whose run stops on the context limit."""

    def __init__(self, usage: dict[str, int]) -> None:
        self._usage = usage

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        return AgentRunResult(
            final_content=None,
            messages=list(spec.messages),
            stop_reason="context_limit",
            error="context window exceeded: 24000 > 16000",
            usage=dict(self._usage),
        )


async def test_judge_context_limit_is_a_failed_run_with_usage() -> None:
    """A context-stopped run fails but still reports the tokens it spent."""
    usage = {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}
    judge = ReviewJudge(
        runner=_ContextLimitRunner(usage),  # type: ignore[arg-type]
        runtime=_runtime(FakeJudgeProvider()),
    )

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert result.error is not None and "context window exceeded" in result.error
    # The spend is never hidden behind the failure.
    assert result.usage == usage
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION


async def test_judge_cancellation_propagates() -> None:
    """``/stop`` must cancel a judge batch instead of being swallowed."""
    provider = _NeverReturningProvider()
    judge = ReviewJudge(
        runner=AgentRunner(provider),
        runtime=_runtime(provider, budget=_budget(output=128)),
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


async def test_judge_usage_sums_every_request_of_the_single_run() -> None:
    """Output-correction retries are billed too, so the run total includes them."""
    provider = FakeJudgeProvider(
        [_PROSE, [_accept_verdict("security:src/app.py:1:s1")]],
        usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    )
    judge = _judge(provider)

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    calls = len(provider.calls)
    assert calls == 2, "the prose answer is rejected once, then corrected"
    assert result.usage["total_tokens"] == 150 * calls
    assert result.usage["prompt_tokens"] == 120 * calls
    assert result.usage["completion_tokens"] == 30 * calls


async def test_judge_correction_retry_is_not_a_second_adjudication() -> None:
    """A corrected answer still belongs to the same single judge run."""
    provider = FakeJudgeProvider(
        [_RawArgs({"verdicts": "not-an-array"}), [_accept_verdict("security:src/app.py:1:s1")]]
    )
    runner = RecordingRunner(provider)
    judge = _judge(provider, runner=runner)

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert len(provider.calls) == 2  # one rejection + its correction
    assert len(runner.specs) == 1  # both belong to one judge run
    assert result.error is None
    assert result.stats is not None
    assert result.stats.total_candidates == 1
    assert result.stats.sent_candidates == 1
    assert result.stats.returned_verdicts == 1


async def test_judge_usage_resets_between_runs() -> None:
    """A second run must not inherit the previous run's usage."""
    provider = FakeJudgeProvider(usage={"total_tokens": 10})
    judge = _judge(provider)
    first = await judge.judge_dimensions([_dimension("security", ["S1"])])
    assert first.usage["total_tokens"] == 10

    second = await judge.judge_dimensions([_dimension("security", [])])
    assert second.usage == {}


async def test_judge_run_uses_the_shared_runner_with_structured_output() -> None:
    """The run uses the injected runner and a fully configured AgentRunSpec."""
    provider = FakeJudgeProvider([[]])
    runner = RecordingRunner(provider)
    judge = _judge(provider, runner=runner, budget=_budget(output=128))
    dimensions = [_dimension("security", ["S1"])]

    await judge.judge_dimensions(dimensions)

    assert len(runner.specs) == 1
    spec = runner.specs[0]
    # Model, window and output cap all ride in the frozen runtime.
    assert spec.runtime.model == "test-model"
    assert spec.runtime.max_tokens == 128
    assert spec.runtime.context_window_tokens == _LARGE_WINDOW
    # The candidate set is authorized evidence and never summarizable.
    assert spec.accepted_message_count == 0
    assert spec.error_message is None
    # The judge has no tools at all: its final answer *is* the verdict payload.
    assert spec.tool_choice is None
    assert spec.tools.tool_names == []
    assert spec.output_contract is not None
    assert spec.output_contract.name == "judge_verdicts"
    assert spec.output_retry_limit == 3
    assert spec.max_iterations == spec.output_retry_limit + 3
    # The run shares no session, workspace, checkpoint or injection state.
    assert spec.workspace is None
    assert spec.session_key is None
    assert spec.checkpoint_callback is None
    assert spec.injection_callback is None
    messages = list(spec.messages)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "S1" in messages[1]["content"]


async def test_judge_prose_response_marks_candidates_needs_confirmation() -> None:
    """A model that only answers prose fails the run, never accepts silently."""
    provider = FakeJudgeProvider([_PROSE])
    judge = _judge(provider)

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert len(provider.calls) > 1  # the contract retry re-asks for the JSON
    assert result.error is not None
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "AI judge failed" in verdict.reason


async def test_judge_malformed_verdicts_are_retried_then_succeed() -> None:
    """A structurally wrong payload is fed back and a corrected answer wins."""
    candidate_id = "security:src/app.py:1:s1"
    provider = FakeJudgeProvider(
        [_RawArgs({"verdicts": "not-an-array"}), [_accept_verdict(candidate_id)]]
    )
    judge = _judge(provider)

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert len(provider.calls) == 2
    assert result.error is None
    assert result.verdicts[candidate_id].decision == ReviewJudgeDecision.ACCEPT


async def test_judge_permanently_malformed_verdicts_fail_the_run() -> None:
    """A payload rejected to the retry limit fails with a bounded error."""
    provider = FakeJudgeProvider([_RawArgs({"verdicts": "not-an-array"})])
    judge = _judge(provider)

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert result.error is not None
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
    assert "AI judge failed" in verdict.reason


async def test_judge_verdicts_matching_no_candidate_fail_the_run() -> None:
    """Invented verdict ids judge nothing, so the run must not look clean."""
    provider = FakeJudgeProvider([[_accept_verdict("security:src/app.py:99:ghost")]])
    judge = _judge(provider)

    result = await judge.judge_dimensions([_dimension("security", ["S1"])])

    assert result.error is not None
    assert "no verdict id matched" in result.error
    verdict = next(iter(result.verdicts.values()))
    assert verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION


async def test_judge_role_override_forks_only_the_judge(tmp_path: Path) -> None:
    """``review.judge.model`` moves the judge off the conversation agent.

    The runner (and therefore the tool/retry machinery) stays shared; only the
    frozen runtime differs, which is what makes a per-role model override safe.
    """
    loop = SessionCoordinator(
        MessageBus(),
        StubPlanProvider(),
        tmp_path,
        review_config=ReviewConfig(judge=ReviewJudgeSettings(model="judge-model")),
    )

    judge = loop._build_review_judge(None)  # noqa: SLF001

    assert judge is not None
    assert judge._runtime.model == "judge-model"  # noqa: SLF001
    assert loop.model == "plan-model"
    assert judge._runner is loop.runner  # noqa: SLF001
    assert judge._runner.provider is loop.provider  # noqa: SLF001


async def test_judge_window_and_output_cap_come_from_its_runtime(
    tmp_path: Path,
) -> None:
    """The judge is budgeted from its frozen runtime plus the configured reserves.

    Reverting ``_build_review_judge`` to a hard-coded window, to module defaults
    or to the conversation agent's numbers would leave every other case green,
    so this pins the wiring: the numbers that decide whether a candidate set fits.
    """
    loop = SessionCoordinator(
        MessageBus(),
        StubPlanProvider(),
        tmp_path,
        review_config=ReviewConfig(
            budget_safety_margin_tokens=777,
            judge=ReviewJudgeSettings(context_window_tokens=99_000, max_tokens=1_500),
        ),
    )

    judge = loop._build_review_judge(None)  # noqa: SLF001

    assert judge is not None
    assert judge._budget.context_window_tokens == 99_000  # noqa: SLF001
    assert judge._budget.judge_output_tokens == 1_500  # noqa: SLF001
    assert judge._budget.safety_margin_tokens == 777  # noqa: SLF001


async def test_judge_disabled_setting_yields_no_judge(tmp_path: Path) -> None:
    """A disabled judge keeps its original meaning: no judge object at all."""
    loop = SessionCoordinator(
        MessageBus(),
        StubPlanProvider(),
        tmp_path,
        review_config=ReviewConfig(judge=ReviewJudgeSettings(enabled=False)),
    )

    assert loop._build_review_judge(None) is None  # noqa: SLF001


def test_judge_execution_result_is_immutable() -> None:
    """The result is a frozen value object, not a mutable cross-call channel."""
    result = JudgeExecutionResult(verdicts={}, stats=None, usage={})
    with pytest.raises(Exception):
        result.error = "mutated"  # type: ignore[misc]


def _workspace_judge(tmp_path: Path) -> ReviewJudge:
    return ReviewJudge(
        runner=RecordingRunner(FakeJudgeProvider([[]])),
        runtime=_runtime(FakeJudgeProvider()),
        config=ReviewJudgeConfig(budget=_budget()),
        common_rules_workspace=tmp_path,
    )


def test_judge_system_prompt_appends_common_rules(tmp_path: Path) -> None:
    (tmp_path / "COMMON_RULES.md").write_text("judge shared rule", encoding="utf-8")

    prompt = _workspace_judge(tmp_path)._system_prompt()  # noqa: SLF001

    assert "strict code-review judge" in prompt
    assert "# Shared Rules" in prompt
    assert "judge shared rule" in prompt


def test_judge_system_prompt_without_workspace_skips_shared_rules() -> None:
    judge = _judge(FakeJudgeProvider([[]]))

    assert "# Shared Rules" not in judge._system_prompt()  # noqa: SLF001


def test_judge_system_prompt_missing_rules_skips_shared_rules(tmp_path: Path) -> None:
    # A workspace without COMMON_RULES.md must not raise nor append a stub.
    assert "# Shared Rules" not in _workspace_judge(tmp_path)._system_prompt()  # noqa: SLF001
