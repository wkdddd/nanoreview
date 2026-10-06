"""Runtime execution profiles for the review subagent manager.

Every profile is declared by its owning feature (``review/profiles.py``) and
registered explicitly; the manager has no built-in default and refuses to run a
task whose metadata does not name a registered profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from nanoreview.agent.runner import AgentRunResult

BUG_REVIEWER_SCOPE = "reviewer.bug"
SECURITY_REVIEWER_SCOPE = "reviewer.security"
PERFORMANCE_REVIEWER_SCOPE = "reviewer.performance"
MAINTAINABILITY_REVIEWER_SCOPE = "reviewer.maintainability"


@dataclass(frozen=True, slots=True)
class SubagentExecutionLimits:
    """Per-task runtime limits supplied by a controlling orchestrator."""

    max_iterations: int | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SubagentCompletion:
    """Normalized outcome returned by a profile-specific result handler."""

    content: str
    status: str = "ok"
    stop_reason: str | None = None


class SubagentResultHandler(Protocol):
    async def __call__(
        self,
        *,
        result: AgentRunResult,
    ) -> SubagentCompletion: ...


@dataclass(frozen=True, slots=True)
class SubagentExecutionProfile:
    """Declarative runtime policy consumed by :class:`SubagentManager`."""

    id: str
    scope: str
    required_tools: frozenset[str] = frozenset()
    terminal_tools: frozenset[str] = frozenset()
    soft_tool_error_tools: frozenset[str] = frozenset()
    #: Tools whose untruncated result must be preserved in the run's
    #: ``tool_events``. Declared per profile; a profile that keeps none lets
    #: ordinary tool results stay bounded by default.
    preserve_tool_result_tools: frozenset[str] = frozenset()
    prompt_builder: Callable[[dict[str, Any], Path], str] | None = None
    workspace_resolver: Callable[[dict[str, Any], Path], Path] | None = None
    result_handler: SubagentResultHandler | None = None
    max_iterations_message: str | None = None
