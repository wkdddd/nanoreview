"""AI judge for review finding candidates."""
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


@dataclass(frozen=True, slots=True)
class ReviewJudgeConfig:
    enabled: bool = True
    max_candidates: int = 40
    timeout_seconds: int = 60
    max_tokens: int = 2048


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

    async def judge_dimensions(
        self,
        dimensions: list[ReviewDimensionResult],
    ) -> dict[str, ReviewJudgeVerdict]:
        if not self._config.enabled:
            return {}
        candidates = self._collect_candidates(dimensions)[: self._config.max_candidates]
        if not candidates:
            logger.info("review.judge.skip reason=no_candidates")
            return {}

        trace_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        logger.info(
            "review.judge.start trace_id={} candidates={} model={}",
            trace_id,
            len(candidates),
            self._model,
        )
        prompt = self._build_prompt(candidates)
        try:
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
        except Exception as exc:
            logger.warning(
                "review.judge.done trace_id={} status=error reason={} elapsed_ms={:.1f}",
                trace_id,
                exc,
                (time.perf_counter() - started) * 1000,
            )
            return {}

        verdicts = self._parse_verdicts(response.tool_calls)
        logger.info(
            "review.judge.done trace_id={} status=ok verdicts={} elapsed_ms={:.1f}",
            trace_id,
            len(verdicts),
            (time.perf_counter() - started) * 1000,
        )
        return verdicts

    @staticmethod
    def candidate_id(candidate: ReviewFindingCandidate) -> str:
        return f"{candidate.dimension}:{candidate.file}:{candidate.line or 0}:{candidate.title}".lower()

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

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a strict code-review judge. Decide whether each candidate is "
            "actionable and supported. Call submit_verdicts with your decisions."
        )

    @staticmethod
    def _build_prompt(
        candidates: list[tuple[str, ReviewFindingCandidate, ReviewFindingVerdict]],
    ) -> str:
        payload = []
        for candidate_id, candidate, verdict in candidates:
            payload.append({
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
            })
        return (
            "Judge these code review candidates. Use decision accept, reject, or "
            "needs_confirmation. Reject unsupported, vague, duplicate, or non-actionable "
            "items. Keep true high-risk issues. A hard_reason such as evidence not "
            "found in file can be caused by evidence formatting; if the candidate has "
            "a concrete file, line, and code-like evidence, do not reject solely for "
            "that hard_reason. Use accept when the claim is supported by the supplied "
            "evidence, or needs_confirmation when it is plausible but still requires "
            "manual verification.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )

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
