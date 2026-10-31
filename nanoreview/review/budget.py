"""Explicit per-request token budgets for the review pipeline.

Review never trims its input to make it fit. The admitted diff reaches every
selected reviewer in full and the whole candidate set reaches the judge in one
request; the only remaining decision is a pre-flight check — does the complete
request fit the configured model window?

Every request account below uses the same shape, so a reviewer and the judge
are budgeted the same way::

    prompt + output reserve + reserve (context reads / safety margin)
        <= configured context window

A request that does not fit rejects the run instead of quietly reviewing (or
adjudicating) a subset of the input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanoreview.agent.llm_runtime import LLMRuntime
    from nanoreview.config.schema import ReviewConfig

#: Tool schemas of the reviewer scope (``read_file``, ``list_dir``, ``grep``).
#: They are sent with every reviewer request, but the loop cannot enumerate
#: them without the subagent manager, so they are accounted as a conservative
#: constant instead of being measured per run.
REVIEWER_TOOL_SCHEMA_TOKENS = 3_000

#: Defaults mirror ``ReviewConfig``; the config values win whenever present.
DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000
DEFAULT_REVIEWER_OUTPUT_TOKENS = 8_192
DEFAULT_REVIEWER_CONTEXT_READ_RESERVE_TOKENS = 32_000
#: One response now carries a verdict for *every* candidate, so the cap has to
#: hold the whole verdict payload (~85 tokens per candidate). 2_048 covered only
#: ~24 findings, which a routine multi-reviewer review exceeds.
DEFAULT_JUDGE_OUTPUT_TOKENS = 8_192
DEFAULT_SAFETY_MARGIN_TOKENS = 4_000


@dataclass(frozen=True, slots=True)
class RequestBudget:
    """One request accounted against the configured context window."""

    context_window_tokens: int
    prompt_tokens: int
    output_reserve_tokens: int
    reserve_tokens: int

    @property
    def required_tokens(self) -> int:
        return self.prompt_tokens + self.output_reserve_tokens + self.reserve_tokens

    @property
    def fits(self) -> bool:
        return self.required_tokens <= self.context_window_tokens

    @property
    def over_by_tokens(self) -> int:
        return max(0, self.required_tokens - self.context_window_tokens)

    def describe(self) -> str:
        """Bounded, operator-readable account of the check."""
        return (
            f"prompt {self.prompt_tokens} + output {self.output_reserve_tokens} "
            f"+ reserve {self.reserve_tokens} = {self.required_tokens} tokens "
            f"against a {self.context_window_tokens}-token window"
        )


@dataclass(frozen=True, slots=True)
class ReviewBudget:
    """Configured review window, output caps and reserves.

    Replaces the previously hard-coded 200k window: the numbers come from
    ``ReviewConfig`` so an operator can align them with the model actually
    serving the review roles.
    """

    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    reviewer_output_tokens: int = DEFAULT_REVIEWER_OUTPUT_TOKENS
    reviewer_context_read_reserve_tokens: int = DEFAULT_REVIEWER_CONTEXT_READ_RESERVE_TOKENS
    judge_output_tokens: int = DEFAULT_JUDGE_OUTPUT_TOKENS
    safety_margin_tokens: int = DEFAULT_SAFETY_MARGIN_TOKENS

    @classmethod
    def from_runtimes(
        cls,
        reviewer_runtime: "LLMRuntime",
        *,
        judge_runtime: "LLMRuntime | None" = None,
        review_config: "ReviewConfig | None" = None,
    ) -> "ReviewBudget":
        """Derive the budget from the runtimes frozen for this review.

        The window and both output caps come from the models that actually serve
        the review roles, so a role override moves the budget with it instead of
        checking requests against a window nobody is running. The two reserves
        stay configured: they describe how much room a reviewer needs *inside* a
        run, which a model runtime knows nothing about.
        """
        if review_config is None:
            read_reserve = DEFAULT_REVIEWER_CONTEXT_READ_RESERVE_TOKENS
            safety_margin = DEFAULT_SAFETY_MARGIN_TOKENS
        else:
            read_reserve = review_config.reviewer_context_read_reserve_tokens
            safety_margin = review_config.budget_safety_margin_tokens
        judge = judge_runtime or reviewer_runtime
        return cls(
            context_window_tokens=(
                reviewer_runtime.context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS
            ),
            reviewer_output_tokens=(
                reviewer_runtime.max_tokens or DEFAULT_REVIEWER_OUTPUT_TOKENS
            ),
            reviewer_context_read_reserve_tokens=read_reserve,
            judge_output_tokens=judge.max_tokens or DEFAULT_JUDGE_OUTPUT_TOKENS,
            safety_margin_tokens=safety_margin,
        )

    def reviewer_request(self, *, prompt_tokens: int) -> RequestBudget:
        """Account one reviewer's initial request.

        The reserve keeps room for the tool results a reviewer still has to
        read after its first request lands in the same window.
        """
        return RequestBudget(
            context_window_tokens=self.context_window_tokens,
            prompt_tokens=prompt_tokens,
            output_reserve_tokens=self.reviewer_output_tokens,
            reserve_tokens=(
                self.reviewer_context_read_reserve_tokens + self.safety_margin_tokens
            ),
        )

    def judge_request(self, *, prompt_tokens: int) -> RequestBudget:
        """Account the single judge request for the whole candidate set.

        The output reserve is always the configured cap:
        :meth:`judge_output_covers` has already established that it holds every
        verdict, so there is nothing left to size per run.
        """
        return RequestBudget(
            context_window_tokens=self.context_window_tokens,
            prompt_tokens=prompt_tokens,
            output_reserve_tokens=self.judge_output_tokens,
            reserve_tokens=self.safety_margin_tokens,
        )

    def judge_output_covers(self, verdict_tokens: int) -> bool:
        """Whether one response can hold a verdict for every candidate."""
        return verdict_tokens <= self.judge_output_tokens

    def contract(self) -> dict[str, int]:
        """Audit projection recorded with the run's frozen input snapshot."""
        return {
            "context_window_tokens": self.context_window_tokens,
            "reviewer_output_tokens": self.reviewer_output_tokens,
            "reviewer_context_read_reserve_tokens": self.reviewer_context_read_reserve_tokens,
            "judge_output_tokens": self.judge_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
        }




__all__ = [
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "DEFAULT_JUDGE_OUTPUT_TOKENS",
    "DEFAULT_REVIEWER_CONTEXT_READ_RESERVE_TOKENS",
    "DEFAULT_REVIEWER_OUTPUT_TOKENS",
    "DEFAULT_SAFETY_MARGIN_TOKENS",
    "REVIEWER_TOOL_SCHEMA_TOKENS",
    "RequestBudget",
    "ReviewBudget",
]
