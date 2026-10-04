"""AI judge for review finding candidates.

The judge collects ALL accepted/uncertain candidates (no fixed max_candidates
truncation), estimates the token cost of each judge request (system prompt +
candidate JSON + reserved output tokens), and splits the candidate list into
batches that fit within the model's available context window. Batches are
executed sequentially and their verdicts are merged; each candidate is judged
at most once. Candidates that cannot fit into a single batch even on their own
are explicitly marked as ``needs_confirmation`` rather than silently accepted.

Each batch runs through the shared :class:`~nanoreview.agent.runner.AgentRunner`
with the same provider/model as the coordinator/plan run, so the judge inherits
provider retry, terminal retry, cancellation and usage accounting instead of
calling the provider directly.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.agent.hooks.lifecycle import AgentHook, AgentHookContext
from nanoreview.agent.review_state import bound_child_error
from nanoreview.agent.runner import AgentRunner, AgentRunSpec
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.agent.tools.review_judge import (
    VERDICT_TOOL_NAME,
    VERDICT_TOOL_SCHEMA,
    JudgeVerdictReceiver,
    SubmitJudgeVerdictsTool,
)
from nanoreview.review.types import (
    FindingVerdict,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
    ReviewJudgeDecision,
    ReviewJudgeVerdict,
)
from nanoreview.utils.helpers import estimate_prompt_tokens, merge_token_usage

#: Conservative fallback context window (tokens) when the runtime cannot
#: supply a usable value. Logged when used so operators can correct the
#: configuration. Chosen to be small enough to fit most judge models while
#: still allowing a meaningful single batch.
_FALLBACK_CONTEXT_WINDOW_TOKENS = 32_000

#: Rough chars-per-token estimate used ONLY when tiktoken is unavailable
#: (estimate_prompt_tokens returns 0). 4 chars/token is a conservative upper
#: bound for mixed ASCII/CJK content that avoids underestimating cost.
_CHARS_PER_TOKEN = 4

#: Reserved tokens for the model's response (matches ReviewJudgeConfig.max_tokens
#: by default). Keeps room for the tool-call verdict payload.
_DEFAULT_RESERVED_OUTPUT_TOKENS = 2048

#: Fixed instruction text prepended to the candidate JSON in each user prompt.
_PROMPT_INSTRUCTION = (
    "Judge these code review candidates. Use decision accept, reject, or "
    "needs_confirmation. Reject unsupported, vague, duplicate, or non-actionable "
    "items. Keep true high-risk issues. A hard_reason such as evidence not "
    "found in file can be caused by evidence formatting; if the candidate has "
    "a concrete file, line, and code-like evidence, do not reject solely for "
    "that hard_reason. Use accept when the claim is supported by the supplied "
    "evidence, or needs_confirmation when it is plausible but still requires "
    "manual verification."
)

#: Terminal submission attempts allowed for one judge batch inside one AgentRun.
_JUDGE_TERMINAL_RETRY_LIMIT = 5
#: Tool choice forces ``submit_verdicts`` each turn, so every iteration is one
#: terminal attempt; a few spare iterations absorb empty/length recovery turns.
_JUDGE_MAX_ITERATIONS = _JUDGE_TERMINAL_RETRY_LIMIT + 2

#: Tool-result budget for the judge run. The judge registers a single terminal
#: tool whose result is a short acknowledgement, so a small bound is enough and
#: keeps a pathological payload from inflating the batch conversation.
_JUDGE_MAX_TOOL_RESULT_CHARS = 4_096


@dataclass(frozen=True, slots=True)
class ReviewJudgeStats:
    """Explicit statistics for one judge run.

    ``sent_candidates`` counts candidates actually submitted in batches
    (including batches whose request later failed); ``returned_verdicts``
    counts verdicts the model returned. Candidates that could never be sent
    (unbatchable) or that received no verdict are marked needs_confirmation
    and counted in ``needs_confirmation``.
    """

    total_candidates: int = 0
    sent_candidates: int = 0
    returned_verdicts: int = 0
    needs_confirmation: int = 0
    batches: int = 0


@dataclass(frozen=True, slots=True)
class ReviewJudgeConfig:
    enabled: bool = True
    timeout_seconds: int = 60
    max_tokens: int = 2048
    #: Model context window (tokens) used to split candidates into batches.
    #: When 0/None the judge falls back to a conservative default and logs it.
    context_window_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class JudgeExecutionResult:
    """Immutable outcome of one ``judge_dimensions`` call.

    Replaces the previous mutable ``last_usage`` / ``last_stats`` /
    ``last_error`` cross-call interface: the caller (finalizer, supervisor)
    reads everything it needs from the returned value, so concurrent or
    repeated judge runs can never inherit another run's state.

    ``verdicts`` always covers every collected candidate (explicit
    ``needs_confirmation`` for anything unjudged); ``error`` is a bounded reason
    for a batch/provider/terminal failure, and is non-empty only when at least
    one batch failed. ``usage`` aggregates every batch that reached the model,
    including failed and timed-out ones: a failure never hides tokens that were
    already consumed.
    """

    verdicts: dict[str, ReviewJudgeVerdict]
    stats: ReviewJudgeStats | None
    usage: dict[str, int]
    error: str | None = None


class _JudgeUsageObserver(AgentHook):
    """Snapshot per-iteration usage so a timed-out batch keeps its spend.

    ``AgentRunner`` reports usage only through ``AgentRunResult``, which a run
    cancelled by its own ``asyncio.wait_for`` timeout never produces. This hook
    records each iteration's usage as it completes, so tokens already consumed
    before the timeout stay visible to the caller instead of being dropped.
    """

    __slots__ = ("usage",)

    def __init__(self) -> None:
        super().__init__()
        self.usage: dict[str, int] = {}

    async def after_iteration(self, context: AgentHookContext) -> None:
        # ``context.usage`` carries exactly one iteration's usage (including a
        # finalization retry merged into it), and the runner calls this once
        # per iteration, so accumulating here mirrors ``AgentRunResult.usage``.
        merge_token_usage(self.usage, context.usage)

    def record_compression_usage(self, usage: dict[str, int]) -> None:
        """Fold a completed run-compression request's usage into the snapshot.

        Run-level compression calls the provider outside the iteration loop, so
        ``after_iteration`` never sees that spend. The runner invokes this
        callback the moment each compression request's usage is billed, which
        keeps it visible even when the batch is cancelled by the outer timeout.
        """
        merge_token_usage(self.usage, usage)


class _JudgeBatchError(RuntimeError):
    """A failed judge batch that may still have consumed tokens.

    ``usage`` carries whatever the batch managed to spend — the run's reported
    usage, or the observer snapshot when the run was cancelled by the batch
    timeout — so the caller folds it into the judge total instead of dropping
    already-billed tokens.
    """

    __slots__ = ("usage",)

    def __init__(self, message: str, *, usage: dict[str, int] | None = None) -> None:
        super().__init__(message)
        self.usage = dict(usage or {})


class ReviewJudge:
    """Use an LLM to judge whether subagent candidates are review-worthy.

    The judge shares the coordinator/plan ``AgentRunner`` and model. It never
    creates its own provider, runner or model preset: batch execution differs
    from the plan run only in the prompt, tool set and terminal tool.
    """

    def __init__(
        self,
        *,
        runner: AgentRunner,
        model: str,
        config: ReviewJudgeConfig | None = None,
        common_rules_workspace: Path | None = None,
    ) -> None:
        self._runner = runner
        self._model = model
        self._config = config or ReviewJudgeConfig()
        #: NanoReview workspace whose ``COMMON_RULES.md`` is read once per batch.
        self._common_rules_workspace = common_rules_workspace
        #: Emits the tokenizer-fallback warning at most once per judge.
        self._tokenizer_fallback_warned = False

    async def judge_dimensions(
        self,
        dimensions: list[ReviewDimensionResult],
    ) -> JudgeExecutionResult:
        """Judge all accepted/uncertain candidates, batching by context window.

        Returns a :class:`JudgeExecutionResult` covering every candidate.
        Candidates that could not be judged (too large to fit a single batch,
        batch failure, timeout) receive an explicit ``needs_confirmation``
        verdict so they are never silently treated as accepted. Cancellation
        propagates; ordinary provider/terminal/timeout errors become a bounded
        ``error`` plus ``needs_confirmation`` candidates.
        """
        candidates = self._collect_candidates(dimensions)
        total_candidates = len(candidates)
        if total_candidates == 0:
            logger.info("review.judge.skip reason=no_candidates")
            return JudgeExecutionResult(verdicts={}, stats=None, usage={}, error=None)

        if not self._config.enabled:
            # Disabled judge is an ops switch, not a pass-through: candidates
            # get an explicit needs_confirmation verdict so the report never
            # presents them as judge-accepted.
            stats = ReviewJudgeStats(
                total_candidates=total_candidates,
                sent_candidates=0,
                returned_verdicts=0,
                needs_confirmation=total_candidates,
                batches=0,
            )
            logger.info(
                "review.judge.skip reason=disabled total={} needs_confirmation={}",
                total_candidates,
                total_candidates,
            )
            return JudgeExecutionResult(
                verdicts=self._needs_confirmation_verdicts(
                    candidates,
                    "AI judge is disabled; manual verification required",
                ),
                stats=stats,
                usage={},
                error=None,
            )

        context_window = self._resolve_context_window()
        reserved_output = max(1, int(self._config.max_tokens or _DEFAULT_RESERVED_OUTPUT_TOKENS))
        # Per-request fixed cost: system prompt, the verdict tool schema, the
        # fixed instruction text of the user prompt, and the reserved output.
        # Estimated with the shared tiktoken helper so judge budgeting matches
        # the rest of the runtime.
        fixed_tokens = (
            self._estimate_tokens(
                [
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": _PROMPT_INSTRUCTION},
                ],
                [VERDICT_TOOL_SCHEMA],
            )
            + reserved_output
        )
        # No minimum-batch floor: never let a batch exceed the real window.
        available_per_batch = context_window - fixed_tokens
        if available_per_batch <= 0:
            # Fixed overhead alone exceeds the context window: no request can
            # ever fit. Mark every candidate as needs_confirmation without
            # calling the provider, so nothing is silently accepted.
            logger.warning(
                "review.judge.window_exhausted_by_overhead fixed_tokens={} window={} candidates={}",
                fixed_tokens,
                context_window,
                total_candidates,
            )
            stats = ReviewJudgeStats(
                total_candidates=total_candidates,
                sent_candidates=0,
                returned_verdicts=0,
                needs_confirmation=total_candidates,
                batches=0,
            )
            logger.info(
                "review.judge.done trace_id=window status=exhausted total={} sent=0 verdicts=0 needs_confirmation={} batches=0",
                total_candidates,
                total_candidates,
            )
            return JudgeExecutionResult(
                verdicts=self._needs_confirmation_verdicts(
                    candidates,
                    "Judge context window is smaller than the fixed request overhead; "
                    "manual verification required",
                ),
                stats=stats,
                usage={},
                error=None,
            )

        batches, unbatchable = self._split_batches(candidates, available_per_batch)
        trace_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        logger.info(
            "review.judge.start trace_id={} total_candidates={} batches={} unbatchable={} model={} context_window={}",
            trace_id,
            total_candidates,
            len(batches),
            len(unbatchable),
            self._model,
            context_window,
        )

        try:
            verdicts, stats, usage, error = await self._run_batches(
                batches,
                unbatchable,
                total_candidates,
                trace_id,
            )
        except Exception as exc:
            # An escaping failure (unlikely beyond provider/timeout, which the
            # batch loop already pins) is surfaced as a bounded judge error so
            # the run's judge batch is never left as ``pending``/``running``.
            error = bound_child_error(exc)
            logger.warning(
                "review.judge.failed reason={} total={}",
                error,
                total_candidates,
            )
            return JudgeExecutionResult(
                verdicts=self._needs_confirmation_verdicts(
                    candidates,
                    "AI judge failed; manual verification required",
                ),
                stats=ReviewJudgeStats(
                    total_candidates=total_candidates,
                    sent_candidates=0,
                    returned_verdicts=0,
                    needs_confirmation=total_candidates,
                    batches=0,
                ),
                usage={},
                error=error,
            )

        logger.info(
            "review.judge.done trace_id={} status={} total={} sent={} verdicts={} needs_confirmation={} batches={} elapsed_ms={:.1f}",
            trace_id,
            "partial_error" if error else "ok",
            stats.total_candidates,
            stats.sent_candidates,
            stats.returned_verdicts,
            stats.needs_confirmation,
            stats.batches,
            (time.perf_counter() - started) * 1000,
        )
        return JudgeExecutionResult(
            verdicts=verdicts, stats=stats, usage=usage, error=error
        )

    @staticmethod
    def candidate_id(candidate: ReviewFindingCandidate) -> str:
        return f"{candidate.dimension}:{candidate.file}:{candidate.line or 0}:{candidate.title}".lower()

    @staticmethod
    def _needs_confirmation_verdicts(
        candidates: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]],
        reason: str,
    ) -> dict[str, ReviewJudgeVerdict]:
        return {
            candidate_id: ReviewJudgeVerdict(
                decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
                reason=reason,
                confidence="low",
                severity=candidate.severity,
            )
            for candidate_id, candidate, _hard in candidates
        }

    async def _run_batches(
        self,
        batches: list[list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]]],
        unbatchable: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]],
        total_candidates: int,
        trace_id: str,
    ) -> tuple[dict[str, ReviewJudgeVerdict], ReviewJudgeStats, dict[str, int], str | None]:
        """Execute every batch and fold in per-candidate needs_confirmation.

        A provider error, terminal failure or timeout on one batch marks the
        affected candidates ``needs_confirmation`` and records a bounded reason
        in the returned error (so the caller records an ``error`` batch) but
        does not abort the remaining batches. Whatever the failed batch already
        consumed is folded into ``usage`` — a failure must not make the run's
        token total understate real spend.
        """
        verdicts: dict[str, ReviewJudgeVerdict] = {}
        usage: dict[str, int] = {}
        sent_count = 0
        returned_count = 0
        error: str | None = None
        for index, batch in enumerate(batches):
            batch_trace = f"{trace_id}:b{index + 1}"
            batch_started = time.perf_counter()
            batch_failed = False
            try:
                batch_verdicts, batch_usage = await self._judge_batch(batch, batch_trace)
            except Exception as exc:
                batch_verdicts = {}
                # A failed batch is still billed: recover the usage the batch
                # reported (or the observer snapshotted before its timeout)
                # instead of dropping it from the run total.
                batch_usage = dict(getattr(exc, "usage", None) or {})
                batch_failed = True
                logger.warning(
                    "review.judge.batch.done trace_id={} status=error reason={} batch_size={} usage={} elapsed_ms={:.1f}",
                    batch_trace,
                    exc,
                    len(batch),
                    batch_usage,
                    (time.perf_counter() - batch_started) * 1000,
                )
                # A failed/timeout batch is a judge-level failure, not a clean
                # completion: record a bounded reason so the run's judge batch
                # is surfaced as ``error`` while the finalizer still marks the
                # affected candidates as needs_confirmation. The first failure
                # wins so the reported reason stays the root cause. The
                # fallback guarantees a non-empty signal even for exceptions
                # whose ``str()`` is empty (e.g. a bare timeout), because the
                # caller treats an empty error as "completed".
                if error is None:
                    error = bound_child_error(exc) or (
                        f"AI judge batch failed ({type(exc).__name__})"
                    )
            merge_token_usage(usage, batch_usage)
            sent_count += len(batch)
            # Count only verdicts that actually match a candidate in this
            # batch; unknown or duplicated model IDs must not inflate stats.
            batch_ids = {candidate_id for candidate_id, _candidate, _hard in batch}
            matched = {
                candidate_id: verdict
                for candidate_id, verdict in batch_verdicts.items()
                if candidate_id in batch_ids
            }
            returned_count += len(matched)
            covered = 0
            for candidate_id, candidate, _hard in batch:
                verdict = matched.get(candidate_id)
                if verdict is not None:
                    verdicts[candidate_id] = verdict
                    covered += 1
                else:
                    # Batch failure or missing verdict: mark explicitly as
                    # needs_confirmation so the candidate is surfaced for
                    # manual verification rather than silently accepted.
                    verdicts[candidate_id] = ReviewJudgeVerdict(
                        decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
                        reason=(
                            "AI judge batch failed; manual verification required"
                            if batch_failed
                            else "AI judge returned no verdict for this candidate"
                        ),
                        confidence="low",
                        severity=candidate.severity,
                    )
            if not batch_failed:
                logger.info(
                    "review.judge.batch.done trace_id={} status=ok batch_size={} verdicts={} covered={} usage={} elapsed_ms={:.1f}",
                    batch_trace,
                    len(batch),
                    len(matched),
                    covered,
                    batch_usage,
                    (time.perf_counter() - batch_started) * 1000,
                )

        # Candidates too large for any single batch: explicit needs_confirmation.
        for candidate_id, candidate, _hard in unbatchable:
            verdicts[candidate_id] = ReviewJudgeVerdict(
                decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
                reason="Candidate too large to fit a single judge batch; manual verification required",
                confidence="low",
                severity=candidate.severity,
            )

        needs_confirmation_count = sum(
            1
            for verdict in verdicts.values()
            if verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
        )
        stats = ReviewJudgeStats(
            total_candidates=total_candidates,
            sent_candidates=sent_count,
            returned_verdicts=returned_count,
            needs_confirmation=needs_confirmation_count,
            batches=len(batches),
        )
        return verdicts, stats, usage, error

    def _resolve_context_window(self) -> int:
        value = self._config.context_window_tokens
        if isinstance(value, int) and value > 0:
            return value
        logger.warning(
            "review.judge.context_window_unavailable fallback={}",
            _FALLBACK_CONTEXT_WINDOW_TOKENS,
        )
        return _FALLBACK_CONTEXT_WINDOW_TOKENS

    @classmethod
    def _collect_candidates(
        cls,
        dimensions: list[ReviewDimensionResult],
    ) -> list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]]:
        items: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]] = []
        for dimension in dimensions:
            for candidate in dimension.accepted:
                items.append((
                    cls.candidate_id(candidate),
                    candidate,
                    ReviewFindingVerdict(FindingVerdict.ACCEPTED, reason="hard validation accepted"),
                ))
            items.extend(
                (
                    cls.candidate_id(candidate),
                    candidate,
                    verdict,
                )
                for candidate, verdict in dimension.uncertain
            )
        return items

    def _estimate_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        """Estimate prompt tokens via the shared tiktoken helper.

        Falls back to a conservative chars-per-token heuristic (with a single
        warning) only when tiktoken is unavailable, so batch budgeting keeps
        working instead of crashing.
        """
        estimated = estimate_prompt_tokens(messages, tools)
        if estimated > 0:
            return estimated
        if not self._tokenizer_fallback_warned:
            logger.warning(
                "review.judge.tokenizer_unavailable fallback_chars_per_token={}",
                _CHARS_PER_TOKEN,
            )
            self._tokenizer_fallback_warned = True
        parts = [
            message.get("content")
            for message in messages
            if isinstance(message.get("content"), str)
        ]
        if tools:
            parts.append(json.dumps(tools, ensure_ascii=False))
        text = "\n".join(part for part in parts if part)
        return max(1, len(text) // _CHARS_PER_TOKEN) + 4 * len(messages)

    def _split_batches(
        self,
        candidates: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]],
        available_per_batch: int,
    ) -> tuple[list[list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]]], list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]]]:
        """Greedily pack candidates into batches that fit the token budget.

        Returns (batches, unbatchable). A candidate is unbatchable when its own
        token cost already exceeds the per-batch budget — it can never be sent
        and is returned for explicit ``needs_confirmation`` marking.
        """
        batches: list[list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]]] = []
        unbatchable: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]] = []
        current: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]] = []
        current_tokens = 0
        for candidate_id, candidate, hard in candidates:
            payload_entry = self._candidate_payload_entry(candidate_id, candidate, hard)
            entry_tokens = self._estimate_tokens(
                [{"role": "user", "content": json.dumps(payload_entry, ensure_ascii=False)}]
            )
            # Slack for the surrounding JSON array/structure framing.
            entry_tokens = entry_tokens + 8
            if entry_tokens > available_per_batch:
                unbatchable.append((candidate_id, candidate, hard))
                continue
            if current and current_tokens + entry_tokens > available_per_batch:
                batches.append(current)
                current = []
                current_tokens = 0
            current.append((candidate_id, candidate, hard))
            current_tokens += entry_tokens
        if current:
            batches.append(current)
        return batches, unbatchable

    async def _judge_batch(
        self,
        batch: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]],
        trace_id: str,
    ) -> tuple[dict[str, ReviewJudgeVerdict], dict[str, int]]:
        """Run one batch through the shared ``AgentRunner``.

        Returns ``(verdicts, usage)``. The batch is only successful when the run
        completed *and* the receiver captured a legal ``submit_verdicts``
        submission; every other outcome raises a :class:`_JudgeBatchError`
        carrying the usage the batch already consumed, so ``_run_batches`` marks
        the candidates ``needs_confirmation``, records a bounded error and still
        accounts for the spend.
        """
        receiver = JudgeVerdictReceiver()
        tools = ToolRegistry()
        tools.register(SubmitJudgeVerdictsTool(receiver))
        # Observes per-iteration usage so a batch cancelled by the timeout below
        # still reports the tokens it burned; harmless on the success path,
        # where ``AgentRunResult.usage`` is authoritative.
        usage_observer = _JudgeUsageObserver()
        spec = AgentRunSpec(
            frozen_messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": self._build_prompt(batch)},
            ],
            working_messages=[],
            tools=tools,
            model=self._model,
            max_iterations=_JUDGE_MAX_ITERATIONS,
            max_tool_result_chars=_JUDGE_MAX_TOOL_RESULT_CHARS,
            temperature=0,
            max_tokens=self._config.max_tokens,
            tool_choice={
                "type": "function",
                "function": {"name": VERDICT_TOOL_NAME},
            },
            terminal_tools=frozenset({VERDICT_TOOL_NAME}),
            terminal_retry_limit=_JUDGE_TERMINAL_RETRY_LIMIT,
            context_window_tokens=self._config.context_window_tokens,
            error_message=None,
            concurrent_tools=False,
            # The judge batch is a self-contained execution: no persisted
            # session, checkpoint, injection or workspace is shared with the
            # coordinator run.
            workspace=None,
            session_key=None,
            hook=usage_observer,
            # Bank compression usage the instant it is billed, so a batch that
            # is cancelled by the timeout below still reports that spend.
            compression_usage_callback=usage_observer.record_compression_usage,
        )

        try:
            result = await asyncio.wait_for(
                self._runner.run(spec),
                timeout=self._config.timeout_seconds,
            )
        except TimeoutError as exc:
            # ``wait_for`` cancels the run mid-flight, so there is no
            # ``AgentRunResult``: the observer snapshot is the only record of
            # what this batch already spent. Cancellation of the judge itself
            # (``/stop``) stays a ``CancelledError`` and still propagates.
            detail = str(exc) or f"exceeded {self._config.timeout_seconds}s"
            logger.warning(
                "review.judge.batch.rejected trace_id={} stop_reason=timeout reason={} observed_usage={}",
                trace_id,
                detail,
                usage_observer.usage,
            )
            raise _JudgeBatchError(
                f"AI judge batch failed (timeout): {detail}",
                usage=usage_observer.usage,
            ) from exc
        if result.stop_reason != "completed":
            reason = (
                result.terminal_error
                or result.error
                or result.final_content
                or f"judge batch stopped with {result.stop_reason}"
            )
            logger.warning(
                "review.judge.batch.rejected trace_id={} stop_reason={} reason={}",
                trace_id,
                result.stop_reason,
                str(reason)[:200],
            )
            raise _JudgeBatchError(
                f"AI judge batch failed ({result.stop_reason}): {reason}",
                usage=dict(result.usage),
            )
        if receiver.submission is None:
            raise _JudgeBatchError(
                "AI judge batch failed: submit_verdicts was not called",
                usage=dict(result.usage),
            )
        return receiver.submission, dict(result.usage)

    def _system_prompt(self) -> str:
        base = (
            "You are a strict code-review judge. Decide whether each candidate is "
            "actionable and supported. Call submit_verdicts with your decisions."
        )
        if self._common_rules_workspace is None:
            return base
        # Read once per batch so a rules edit reaches later batches without the
        # judge holding a stale snapshot across a long run.
        from nanoreview.agent.context import ContextBuilder

        rules = ContextBuilder.load_common_rules(self._common_rules_workspace)
        if not rules:
            logger.warning(
                "review.judge.common_rules.missing root={}",
                self._common_rules_workspace,
            )
            return base
        return f"{base}\n\n# Shared Rules\n\n{rules}"

    @staticmethod
    def _candidate_payload_entry(
        candidate_id: str,
        candidate: ReviewFindingCandidate,
        verdict: ReviewFindingVerdict,
    ) -> dict[str, Any]:
        return {
            "id": candidate_id,
            "severity": candidate.severity,
            "dimension": candidate.dimension,
            "file": candidate.file,
            "line": candidate.line,
            "title": candidate.title,
            "evidence": candidate.evidence,
            "impact": candidate.impact,
            "recommendation": candidate.recommendation,
            "hard_verdict": verdict.verdict.value,
            "hard_reason": verdict.reason,
        }

    @classmethod
    def _build_prompt(
        cls,
        candidates: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]],
    ) -> str:
        payload = [
            cls._candidate_payload_entry(candidate_id, candidate, verdict)
            for candidate_id, candidate, verdict in candidates
        ]
        return _PROMPT_INSTRUCTION + "\n\n" + json.dumps(payload, ensure_ascii=False)


#: Conservative fallback context window (tokens) when the runtime cannot
#: supply a usable value. Logged when used so operators can correct the
#: configuration. Chosen to be small enough to fit most judge models while
#: still allowing a meaningful single batch.
_FALLBACK_CONTEXT_WINDOW_TOKENS = 32_000

#: Rough chars-per-token estimate used ONLY when tiktoken is unavailable
#: (estimate_prompt_tokens returns 0). 4 chars/token is a conservative upper
#: bound for mixed ASCII/CJK content that avoids underestimating cost.
_CHARS_PER_TOKEN = 4

#: Reserved tokens for the model's response (matches ReviewJudgeConfig.max_tokens
#: by default). Keeps room for the tool-call verdict payload.
_DEFAULT_RESERVED_OUTPUT_TOKENS = 2048

#: Fixed instruction text prepended to the candidate JSON in each user prompt.
_PROMPT_INSTRUCTION = (
    "Judge these code review candidates. Use decision accept, reject, or "
    "needs_confirmation. Reject unsupported, vague, duplicate, or non-actionable "
    "items. Keep true high-risk issues. A hard_reason such as evidence not "
    "found in file can be caused by evidence formatting; if the candidate has "
    "a concrete file, line, and code-like evidence, do not reject solely for "
    "that hard_reason. Use accept when the claim is supported by the supplied "
    "evidence, or needs_confirmation when it is plausible but still requires "
    "manual verification."
)
