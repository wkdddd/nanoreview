"""Structured final-output contract for one agent run.

An agent that must return machine-readable output declares an
:class:`OutputContract`. The runner parses the model's *final* answer with it:
a valid payload completes the run, an invalid one is fed back as a concrete
error so the model can correct itself inside the same run, bounded by
``AgentRunSpec.output_retry_limit``.

The contract is a pure function of the final text. It never touches run state,
and the runner never writes a result the contract did not accept — which is why
a truncated answer, a prose answer or a failed run can never be mistaken for a
successful structured result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Parses final model text into ``(payload, error)``. Exactly one is non-None.
OutputParser = Callable[[str], "tuple[Any | None, str | None]"]


@dataclass(frozen=True, slots=True)
class OutputContract:
    """What one run must return as its final answer.

    ``shape`` is a one-line description of the expected payload used in the
    correction prompt (e.g. ``{"findings": [...]}``). It is documentation for
    the model, not a schema the runner enforces — the parser is authoritative.
    """

    name: str
    parse: OutputParser
    shape: str = ""
    extra_instructions: str = field(default="")

    def validate(self, text: str) -> tuple[Any | None, str | None]:
        """Return ``(payload, error)`` for one candidate final answer."""
        try:
            payload, error = self.parse(text)
        except Exception as exc:  # pragma: no cover - defensive: parser bug
            return None, f"output parser failed ({type(exc).__name__}: {exc})"
        if error:
            return None, str(error)
        if payload is None:
            return None, "the final answer did not contain a structured result"
        return payload, None

    def repair_prompt(self, error: str) -> str:
        """Concrete correction instruction injected after an invalid answer."""
        shape = f" Expected shape: {self.shape}." if self.shape else ""
        extra = f" {self.extra_instructions}" if self.extra_instructions else ""
        return (
            f"Your final answer was rejected: {error}.{shape}\n"
            "Reply with one JSON object only — no prose, no markdown fence, no "
            f"commentary before or after it.{extra}"
        )


__all__ = ["OutputContract", "OutputParser"]
