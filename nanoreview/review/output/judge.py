"""AI judge for review finding candidates.

The judge collects ALL accepted/uncertain candidates (no fixed max_candidates
truncation), estimates the token cost of each judge request (system prompt +
candidate JSON + reserved output tokens), and splits the candidate list into
batches that fit within the model's available context window. Batches are
executed sequentially and their verdicts are merged; each candidate is judged
at most once. Candidates that cannot fit into a single batch even on their own
are explicitly marked as ``needs_confirmation`` rather than silently accepted.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from loguru import logger

from nanoreview.review.types import (
    FindingVerdict,
    ReviewDimensionResult,
    ReviewFindingCandidate,
    ReviewFindingVerdict,
    ReviewJudgeDecision,
    ReviewJudgeVerdict,
)
from nanoreview.utils.helpers import estimate_prompt_tokens

_VERDICT_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "submit_verdicts",
        "description": "Submit judge verdicts for all candidates.",
        "parameters": {
            "type": "object",
            "properties": {
                "verdicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "decision": {
                                "type": "string",
                                "enum": ["accept", "reject", "needs_confirmation"],
                            },
                            "reason": {"type": "string"},
                            "confidence": {"type": "string"},
                        },
                        "required": ["id", "decision", "reason", "confidence"],
                    },
                }
            },
            "required": ["verdicts"],
        },
    },
}

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


class ReviewJudge:
    """Use an LLM to judge whether subagent candidates are review-worthy."""

    def __init__(
        self,
        *,
        provider: Any,
        model: str,
        config: ReviewJudgeConfig | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._config = config or ReviewJudgeConfig()
        #: Statistics of the most recent judge_dimensions call (None when the
        #: call judged nothing, e.g. there were no candidates).
        self.last_stats: ReviewJudgeStats | None = None
        #: Emits the tokenizer-fallback warning at most once per judge.
        self._tokenizer_fallback_warned = False

    async def judge_dimensions(
        self,
        dimensions: list[ReviewDimensionResult],
    ) -> dict[str, ReviewJudgeVerdict]:
        """Judge all accepted/uncertain candidates, batching by context window.

        Returns a mapping of candidate-id -> verdict. Candidates that could
        not be judged (e.g. too large to fit a single batch, or batch failure)
        receive an explicit ``needs_confirmation`` verdict so they are never
        silently treated as accepted.
        """
        candidates = self._collect_candidates(dimensions)
        total_candidates = len(candidates)
        if total_candidates == 0:
            self.last_stats = None
            logger.info("review.judge.skip reason=no_candidates")
            return {}

        if not self._config.enabled:
            # Disabled judge is an ops switch, not a pass-through: candidates
            # get an explicit needs_confirmation verdict so the report never
            # presents them as judge-accepted.
            self.last_stats = ReviewJudgeStats(
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
            return {
                candidate_id: ReviewJudgeVerdict(
                    decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
                    reason="AI judge is disabled; manual verification required",
                    confidence="low",
                    severity=candidate.severity,
                )
                for candidate_id, candidate, _hard in candidates
            }

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
                [_VERDICT_TOOL],
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
            verdicts = {
                candidate_id: ReviewJudgeVerdict(
                    decision=ReviewJudgeDecision.NEEDS_CONFIRMATION,
                    reason=(
                        "Judge context window is smaller than the fixed request overhead; "
                        "manual verification required"
                    ),
                    confidence="low",
                    severity=candidate.severity,
                )
                for candidate_id, candidate, _hard in candidates
            }
            stats = ReviewJudgeStats(
                total_candidates=total_candidates,
                sent_candidates=0,
                returned_verdicts=0,
                needs_confirmation=total_candidates,
                batches=0,
            )
            self.last_stats = stats
            logger.info(
                "review.judge.done trace_id=window status=exhausted total={} sent=0 verdicts=0 needs_confirmation={} batches=0",
                total_candidates,
                total_candidates,
            )
            return verdicts

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

        verdicts: dict[str, ReviewJudgeVerdict] = {}
        sent_count = 0
        returned_count = 0
        for index, batch in enumerate(batches):
            batch_trace = f"{trace_id}:b{index + 1}"
            batch_started = time.perf_counter()
            batch_failed = False
            try:
                batch_verdicts = await self._judge_batch(batch, batch_trace)
            except Exception as exc:
                logger.warning(
                    "review.judge.batch.done trace_id={} status=error reason={} batch_size={} elapsed_ms={:.1f}",
                    batch_trace,
                    exc,
                    len(batch),
                    (time.perf_counter() - batch_started) * 1000,
                )
                batch_verdicts = {}
                batch_failed = True
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
                    "review.judge.batch.done trace_id={} status=ok batch_size={} verdicts={} covered={} elapsed_ms={:.1f}",
                    batch_trace,
                    len(batch),
                    len(matched),
                    covered,
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
        self.last_stats = stats
        logger.info(
            "review.judge.done trace_id={} status=ok total={} sent={} verdicts={} needs_confirmation={} batches={} elapsed_ms={:.1f}",
            trace_id,
            stats.total_candidates,
            stats.sent_candidates,
            stats.returned_verdicts,
            stats.needs_confirmation,
            stats.batches,
            (time.perf_counter() - started) * 1000,
        )
        return verdicts

    @staticmethod
    def candidate_id(candidate: ReviewFindingCandidate) -> str:
        return f"{candidate.dimension}:{candidate.file}:{candidate.line or 0}:{candidate.title}".lower()

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
    ) -> dict[str, ReviewJudgeVerdict]:
        prompt = self._build_prompt(batch)
        response = await asyncio.wait_for(
            self._provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": prompt},
                ],
                tools=[_VERDICT_TOOL],
                model=self._model,
                max_tokens=self._config.max_tokens,
                temperature=0,
                tool_choice={
                    "type": "function",
                    "function": {"name": "submit_verdicts"},
                },
            ),
            timeout=self._config.timeout_seconds,
        )
        return self._parse_verdicts(response.tool_calls)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a strict code-review judge. Decide whether each candidate is "
            "actionable and supported. Call submit_verdicts with your decisions."
        )

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

    @staticmethod
    def _parse_verdicts(tool_calls: list) -> dict[str, ReviewJudgeVerdict]:
        if not tool_calls:
            logger.warning("review.judge.parse_failed reason=no_tool_calls")
            return {}
        data = tool_calls[0].arguments.get("verdicts", [])
        if not isinstance(data, list):
            return {}
        verdicts: dict[str, ReviewJudgeVerdict] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("id", "")).strip().lower()
            if not candidate_id:
                continue
            decision_raw = str(item.get("decision", "needs_confirmation")).strip().lower()
            try:
                decision = ReviewJudgeDecision(decision_raw)
            except ValueError:
                decision = ReviewJudgeDecision.NEEDS_CONFIRMATION
            severity = item.get("severity")
            verdicts[candidate_id] = ReviewJudgeVerdict(
                decision=decision,
                reason=str(item.get("reason", "")),
                confidence=str(item.get("confidence", "medium")),
                severity=str(severity).lower() if severity else None,
            )
        return verdicts
