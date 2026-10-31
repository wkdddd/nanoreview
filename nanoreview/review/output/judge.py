"""AI judge for review finding candidates.

The judge receives *every* accepted/uncertain candidate in one request and
returns one verdict per candidate. Before any model call it checks the complete
request against the configured window: the prompt, the output the whole
candidate set needs for its verdicts, and the safety margin. A candidate set
that does not fit is never judged — the run becomes an ``error`` and the review
stays a draft, because adjudicating a subset would present an incomplete review
as a finished one.

The single request runs through the shared
:class:`~nanoreview.agent.runner.AgentRunner` with the same provider/model as
the coordinator/plan run, so the judge inherits provider retry, structured
output correction (a correction is not a second adjudication), cancellation and
usage accounting instead of calling the provider directly.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoreview.agent.hooks.lifecycle import AgentHook, AgentHookContext
from nanoreview.agent.hooks.turn_hooks import AgentTurnHookSpec, build_agent_turn_hook
from nanoreview.agent.review_state import bound_child_error
from nanoreview.agent.runner import AgentRunner, AgentRunSpec
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.review.budget import RequestBudget, ReviewBudget
from nanoreview.review.contracts import judge_contract
from nanoreview.review.types import (
    FindingVerdict,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
    ReviewJudgeDecision,
    ReviewJudgeVerdict,
)
from nanoreview.utils.helpers import (
    estimate_prompt_tokens_with_fallback,
    merge_token_usage,
)

if TYPE_CHECKING:
    from nanoreview.agent.llm_runtime import LLMRuntime

#: Output tokens reserved per candidate on top of its verdict skeleton, so the
#: reserve covers the free-text ``reason`` the model writes for each verdict.
_VERDICT_REASON_RESERVE_TOKENS = 48

#: Fixed instruction text prepended to the candidate JSON in the user prompt.
#: The run is tool-less, so this prompt — not a tool schema — is where the model
#: learns the verdict shape; the payload contract is appended right after it by
#: ``_build_prompt``.
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

#: Rejections of the final answer allowed before the judge run fails.
_JUDGE_OUTPUT_RETRY_LIMIT = 3
#: Request allowance: the contract rejections above plus the empty/length
#: recovery turns the runner may still need.
_JUDGE_MAX_ITERATIONS = _JUDGE_OUTPUT_RETRY_LIMIT + 3

#: Tool-result budget for the judge run. The judge is tool-less, so this only
#: bounds the runner's per-result normalization.
_JUDGE_MAX_TOOL_RESULT_CHARS = 4_096


@dataclass(frozen=True, slots=True)
class ReviewJudgeStats:
    """Explicit statistics for one judge run.

    ``sent_candidates`` counts candidates submitted in the single request
    (including a request that later failed); ``returned_verdicts`` counts
    verdicts the model returned for a real candidate. Candidates without a
    verdict are marked needs_confirmation and counted in ``needs_confirmation``.
    """

    total_candidates: int = 0
    sent_candidates: int = 0
    returned_verdicts: int = 0
    needs_confirmation: int = 0


@dataclass(frozen=True, slots=True)
class ReviewJudgeConfig:
    """Judge settings.

    There is no window, output cap or wall clock here: those travel inside the
    ``LLMRuntime`` frozen when the review started, so the numbers the batch is
    checked against are the ones the model actually runs with. ``budget`` only
    carries the reserves, and is derived from that same runtime when omitted.
    """

    enabled: bool = True
    #: Reserves for the request account; window and output caps come from the
    #: runtime (``ReviewBudget.from_runtimes``).
    budget: ReviewBudget | None = None


@dataclass(frozen=True, slots=True)
class JudgeExecutionResult:
    """Immutable outcome of one ``judge_dimensions`` call.

    The caller (finalizer, supervisor) reads everything it needs from the
    returned value, so concurrent or repeated judge runs can never inherit
    another run's state.

    ``verdicts`` covers every collected candidate whenever the judge actually
    ran (explicit ``needs_confirmation`` for anything unjudged). ``error`` is a
    bounded reason for a provider/output-contract/timeout failure. A run
    rejected before any model call sets ``budget_rejected`` and returns no
    verdicts: nothing was adjudicated, so the caller must not present the review
    as judged.
    """

    verdicts: dict[str, ReviewJudgeVerdict]
    stats: ReviewJudgeStats | None
    usage: dict[str, int]
    error: str | None = None
    #: True when the complete candidate set did not fit one judge request, so
    #: the model was never called.
    budget_rejected: bool = False


class _JudgeUsageObserver(AgentHook):
    """Snapshot per-iteration usage so a cancelled run keeps its spend.

    ``AgentRunner`` reports usage only through ``AgentRunResult``, which a run
    cancelled by the provider's request timeout never produces. This hook
    records each iteration's usage as it completes, so tokens already consumed
    before the cancellation stay visible to the caller instead of being dropped.
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


class _JudgeRunError(RuntimeError):
    """A failed judge run that may still have consumed tokens.

    ``usage`` carries whatever the run managed to spend — the run's reported
    usage, or the observer snapshot when the run was cancelled by the timeout —
    so the caller folds it into the judge total instead of dropping already
    billed tokens.
    """

    __slots__ = ("usage",)

    def __init__(self, message: str, *, usage: dict[str, int] | None = None) -> None:
        super().__init__(message)
        self.usage = dict(usage or {})


class ReviewJudge:
    """Use an LLM to judge whether subagent candidates are review-worthy.

    The judge shares the coordinator's ``AgentRunner`` but runs on the runtime
    frozen for its own role at review admission — never on the conversation
    agent's live model, which a ``/model`` switch could move mid-review. It
    never creates its own runner or provider: the run differs from a reviewer
    run only in the prompt and tool set — it has no tools at all and its final
    answer is the structured verdict payload.
    """

    def __init__(
        self,
        *,
        runner: AgentRunner,
        runtime: "LLMRuntime",
        config: ReviewJudgeConfig | None = None,
        common_rules_workspace: Path | None = None,
    ) -> None:
        self._runner = runner
        self._runtime = runtime
        self._config = config or ReviewJudgeConfig()
        #: One budget object for the check *and* the run, so the window that
        #: decided "it fits" is the window the run then executes in.
        self._budget = self._config.budget or ReviewBudget.from_runtimes(
            runtime, judge_runtime=runtime
        )
        #: NanoReview workspace whose ``COMMON_RULES.md`` is read once per run.
        self._common_rules_workspace = common_rules_workspace
        #: Emits the tokenizer-fallback warning at most once per judge.
        self._tokenizer_fallback_warned = False

    async def judge_dimensions(
        self,
        dimensions: list[ReviewDimensionResult],
    ) -> JudgeExecutionResult:
        """Judge all accepted/uncertain candidates in one request.

        Returns a :class:`JudgeExecutionResult` covering every candidate when
        the judge ran; candidates without a verdict (run failure, timeout,
        missing id) receive an explicit ``needs_confirmation`` verdict so they
        are never silently treated as accepted. Cancellation propagates;
        ordinary provider/output-contract/timeout errors become a bounded
        ``error`` plus ``needs_confirmation`` candidates; a candidate set that
        does not fit one request is rejected before the model is called.
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
            logger.info(
                "review.judge.skip reason=disabled total={}",
                total_candidates,
            )
            return JudgeExecutionResult(
                verdicts=self._needs_confirmation_verdicts(
                    candidates,
                    "AI judge is disabled; manual verification required",
                ),
                stats=ReviewJudgeStats(
                    total_candidates=total_candidates,
                    needs_confirmation=total_candidates,
                ),
                usage={},
                error=None,
            )

        verdict_tokens = self._verdict_tokens(candidates)
        budget = self._request_budget(candidates)
        rejection = self._budget_failure(budget, verdict_tokens, total_candidates)
        if rejection is not None:
            # Nothing is adjudicated: the caller turns this into a run error and
            # a draft review, so no candidate may carry a judge verdict.
            logger.warning(
                "review.judge.rejected reason=budget candidates={} required={} window={} {}",
                total_candidates,
                budget.required_tokens,
                budget.context_window_tokens,
                rejection,
            )
            return JudgeExecutionResult(
                verdicts={},
                stats=ReviewJudgeStats(
                    total_candidates=total_candidates,
                    needs_confirmation=total_candidates,
                ),
                usage={},
                error=rejection,
                budget_rejected=True,
            )

        trace_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        logger.info(
            "review.judge.start trace_id={} candidates={} model={} window={} required_tokens={}",
            trace_id,
            total_candidates,
            self._runtime.model,
            budget.context_window_tokens,
            budget.required_tokens,
        )

        verdicts: dict[str, ReviewJudgeVerdict] = {}
        usage: dict[str, int] = {}
        error: str | None = None
        try:
            verdicts, usage = await self._judge_once(candidates, trace_id)
        except Exception as exc:
            # An escaping failure is surfaced as a bounded judge error so the
            # run's judge child is never left as ``pending``/``running``. A
            # failed run is still billed, so recover the usage it reported (or
            # the observer snapshotted before its timeout).
            usage = dict(getattr(exc, "usage", None) or {})
            error = bound_child_error(exc) or f"AI judge failed ({type(exc).__name__})"
            logger.warning(
                "review.judge.failed trace_id={} reason={} candidates={} usage={}",
                trace_id,
                error,
                total_candidates,
                usage,
            )

        resolved, matched = self._resolve_verdicts(candidates, verdicts, failed=bool(error))
        stats = ReviewJudgeStats(
            total_candidates=total_candidates,
            sent_candidates=total_candidates,
            returned_verdicts=matched,
            needs_confirmation=sum(
                1
                for verdict in resolved.values()
                if verdict.decision == ReviewJudgeDecision.NEEDS_CONFIRMATION
            ),
        )
        logger.info(
            "review.judge.done trace_id={} status={} total={} sent={} verdicts={} needs_confirmation={} elapsed_ms={:.1f}",
            trace_id,
            "error" if error else "ok",
            stats.total_candidates,
            stats.sent_candidates,
            stats.returned_verdicts,
            stats.needs_confirmation,
            (time.perf_counter() - started) * 1000,
        )
        return JudgeExecutionResult(verdicts=resolved, stats=stats, usage=usage, error=error)

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

    def _resolve_verdicts(
        self,
        candidates: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]],
        verdicts: dict[str, ReviewJudgeVerdict],
        *,
        failed: bool,
    ) -> tuple[dict[str, ReviewJudgeVerdict], int]:
        """Map the model's verdicts onto the candidate set.

        Only verdicts naming a real candidate count; whatever is left over is
        marked needs_confirmation so a candidate is never presented as having
        passed the judge without a verdict for it.
        """
        known_ids = {candidate_id for candidate_id, _candidate, _hard in candidates}
        matched = {
            candidate_id: verdict
            for candidate_id, verdict in verdicts.items()
            if candidate_id in known_ids
        }
        resolved: dict[str, ReviewJudgeVerdict] = {}
        for candidate_id, candidate, _hard in candidates:
            verdict = matched.get(candidate_id)
            if verdict is not None:
                resolved[candidate_id] = verdict
                continue
            resolved[candidate_id] = ReviewJudgeVerdict(
                decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
                reason=(
                    "AI judge failed; manual verification required"
                    if failed
                    else "AI judge returned no verdict for this candidate"
                ),
                confidence="low",
                severity=candidate.severity,
            )
        return resolved, len(matched)

    # -- budget -------------------------------------------------------------

    def _request_budget(
        self,
        candidates: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]],
    ) -> RequestBudget:
        """Account the complete request: prompt plus the verdicts it must return."""
        prompt_tokens, _source = estimate_prompt_tokens_with_fallback(
            [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": self._build_prompt(candidates)},
            ]
        )
        return self._budget.judge_request(prompt_tokens=prompt_tokens)

    def _verdict_tokens(
        self,
        candidates: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]],
    ) -> int:
        """Output tokens a verdict for every candidate needs.

        The verdict skeleton (ids, decisions, severities) is measured on the
        real candidate ids; each verdict additionally reserves a bounded
        allowance for its free-text reason.
        """
        skeleton = json.dumps(
            {
                candidate_id: {
                    "decision": "needs_confirmation",
                    "reason": "",
                    "confidence": "medium",
                    "severity": "medium",
                }
                for candidate_id, _candidate, _hard in candidates
            },
            ensure_ascii=False,
        )
        return self._estimate_tokens(
            [{"role": "user", "content": skeleton}]
        ) + _VERDICT_REASON_RESERVE_TOKENS * len(candidates)

    def _budget_failure(
        self, budget: RequestBudget, verdict_tokens: int, candidate_count: int
    ) -> str | None:
        """Bounded reason when the complete request cannot be served, else None."""
        if not self._budget.judge_output_covers(verdict_tokens):
            return (
                f"AI judge was not run: adjudicating {candidate_count} candidates needs "
                f"about {verdict_tokens} output tokens, above the configured judge output "
                f"cap of {self._budget.judge_output_tokens} tokens"
            )
        if not budget.fits:
            return (
                "AI judge was not run: the complete candidate set does not fit one judge "
                f"request ({budget.describe()})"
            )
        return None

    # -- collected candidates ----------------------------------------------

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

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Tokens of one prompt via the shared counting policy."""
        tokens, source = estimate_prompt_tokens_with_fallback(messages)
        if source == "upper_bound" and not self._tokenizer_fallback_warned:
            logger.warning(
                "review.judge.tokenizer_unavailable fallback=upper_bound_per_character"
            )
            self._tokenizer_fallback_warned = True
        return tokens

    # -- single run ---------------------------------------------------------

    async def _judge_once(
        self,
        candidates: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]],
        trace_id: str,
    ) -> tuple[dict[str, ReviewJudgeVerdict], dict[str, int]]:
        """Run the complete candidate set through the shared ``AgentRunner``.

        Returns ``(verdicts, usage)``. The run is only successful when it
        completed *and* its final answer satisfied the verdict contract; every
        other outcome raises a :class:`_JudgeRunError` carrying the usage the
        run already consumed, so the caller marks the candidates
        ``needs_confirmation``, records a bounded error and still accounts for
        the spend.
        """
        # Observes per-iteration usage so a run rejected or cancelled mid-flight
        # still reports the tokens it burned; harmless on the success path,
        # where ``AgentRunResult.usage`` is authoritative.
        usage_observer = _JudgeUsageObserver()
        spec = AgentRunSpec(
            messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": self._build_prompt(candidates)},
            ],
            # The judge is a pure decision step: it needs no tools, only the
            # candidate set and its output contract.
            tools=ToolRegistry(),
            # Window, output cap and generation parameters all ride in the
            # frozen runtime; the assignment window and output reserve are
            # therefore the same numbers the request is actually sent with.
            runtime=self._runtime,
            max_iterations=_JUDGE_MAX_ITERATIONS,
            max_tool_result_chars=_JUDGE_MAX_TOOL_RESULT_CHARS,
            # The candidate set is authorized evidence: a checkpoint summary
            # may not stand in for it, so nothing here is summarizable.
            accepted_message_count=0,
            output_contract=judge_contract(),
            output_retry_limit=_JUDGE_OUTPUT_RETRY_LIMIT,
            error_message=None,
            concurrent_tools=False,
            # The judge run is self-contained: no persisted session, checkpoint,
            # injection or workspace is shared with the coordinator run.
            workspace=None,
            session_key=None,
            # Judging is self-contained: no file-edit observer and no
            # conversation progress, just the usage observer. The spec is
            # not ephemeral here, otherwise the builder would drop the
            # observer along with every other extra hook.
            hook=build_agent_turn_hook(
                AgentTurnHookSpec(turn_hooks=[usage_observer])
            ),
        )

        try:
            result = await self._runner.run(spec)
        except TimeoutError as exc:
            # The provider's own per-request timeout cancels the run mid-flight,
            # so there is no ``AgentRunResult``: the observer snapshot is the
            # only record of what this run already spent. Cancellation of the
            # judge itself (``/stop``) stays a ``CancelledError`` and still
            # propagates.
            detail = str(exc) or "model request timed out"
            logger.warning(
                "review.judge.run.rejected trace_id={} stop_reason=timeout reason={} observed_usage={}",
                trace_id,
                detail,
                usage_observer.usage,
            )
            raise _JudgeRunError(
                f"AI judge failed (timeout): {detail}",
                usage=usage_observer.usage,
            ) from exc
        if result.stop_reason != "completed":
            # ``error`` is the cause the run actually stopped for, and for a
            # contract rejection it is the rejection reason itself; reading it
            # first keeps an earlier rejection from masking the real failure.
            reason = (
                result.error
                or result.output_error
                or result.final_content
                or f"judge run stopped with {result.stop_reason}"
            )
            logger.warning(
                "review.judge.run.rejected trace_id={} stop_reason={} reason={}",
                trace_id,
                result.stop_reason,
                str(reason)[:200],
            )
            raise _JudgeRunError(
                f"AI judge failed ({result.stop_reason}): {reason}",
                usage=dict(result.usage),
            )
        payload = result.structured_output
        verdicts = payload.get("verdicts") if isinstance(payload, dict) else None
        if not isinstance(verdicts, dict):
            raise _JudgeRunError(
                "AI judge failed: no verdicts were returned",
                usage=dict(result.usage),
            )
        # An empty verdict map is a legitimate outcome: the judge declined to
        # decide, and every candidate degrades to ``needs_confirmation``. A
        # non-empty map whose ids match nothing is not — it means the model
        # invented ids, so no candidate would ever be judged.
        known_ids = {candidate_id for candidate_id, _candidate, _hard in candidates}
        if verdicts and not known_ids.intersection(verdicts):
            raise _JudgeRunError(
                "AI judge failed: no verdict id matched any candidate "
                "(verdict ids must be copied verbatim)",
                usage=dict(result.usage),
            )
        return verdicts, dict(result.usage)

    def _system_prompt(self) -> str:
        base = (
            "You are a strict code-review judge. Decide whether each candidate is "
            "actionable and supported. Reply with one JSON object holding the "
            "verdicts for the candidates."
        )
        if self._common_rules_workspace is None:
            return base
        # Read once per run so a rules edit reaches a later judge run without
        # the judge holding a stale snapshot across a long session.
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
        # The run has no tools, so the expected answer shape has to travel in
        # the prompt itself: without it the model guesses (a bare array, a dict
        # of id -> decision) and burns its first answer on a rejection.
        shape = judge_contract().shape
        return (
            f"{_PROMPT_INSTRUCTION}\n\n"
            f"Answer with one JSON object and nothing else. Expected shape: {shape}\n"
            "Return exactly one verdict per candidate, reusing each candidate's "
            '"id" value verbatim.\n\n'
            + json.dumps(payload, ensure_ascii=False)
        )


__all__ = [
    "JudgeExecutionResult",
    "ReviewJudge",
    "ReviewJudgeConfig",
    "ReviewJudgeStats",
]
