"""Runtime execution profiles for the review subagent manager.

Every profile is declared by its owning feature (``review/profiles.py``) and
registered explicitly; the manager has no built-in default and refuses to run a
task whose metadata does not name a registered profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from nanoreview.agent.output_contract import OutputContract
from nanoreview.agent.runner import AgentRunResult

if TYPE_CHECKING:
    from nanoreview.agent.llm_runtime import LLMRuntime

BUG_REVIEWER_SCOPE = "reviewer.bug"
SECURITY_REVIEWER_SCOPE = "reviewer.security"
PERFORMANCE_REVIEWER_SCOPE = "reviewer.performance"
MAINTAINABILITY_REVIEWER_SCOPE = "reviewer.maintainability"
GENERAL_REVIEWER_SCOPE = "reviewer.general"


@dataclass(frozen=True, slots=True)
class SubagentExecutionLimits:
    """Per-task runtime limits supplied by a controlling orchestrator.

    There is deliberately no role-level wall-clock budget: a run is bounded by
    its request allowance (``max_iterations``) and by the per-request provider
    timeout, so a slow request can never silently truncate a reviewer's work
    halfway through a batch.
    """

    max_iterations: int | None = None
    #: Frozen model runtime for this task. ``None`` keeps the manager's live
    #: provider/model and its current generation settings.
    runtime: "LLMRuntime | None" = None


@dataclass(frozen=True, slots=True)
class SubagentCompletion:
    """Normalized outcome returned by a profile-specific result handler.

    ``payload`` carries the structured final result of a profile that declares
    an output contract. It is the only channel a controlling orchestrator reads
    a subagent's result from — the human-readable ``content`` is a bounded
    progress line, never the result itself.
    """

    content: str
    status: str = "ok"
    stop_reason: str | None = None
    payload: Any | None = None


class SubagentResultHandler(Protocol):
    async def __call__(
        self,
        *,
        result: AgentRunResult,
        metadata: dict[str, Any] | None = None,
    ) -> SubagentCompletion: ...


@dataclass(frozen=True, slots=True)
class SubagentExecutionProfile:
    """Declarative runtime policy consumed by :class:`SubagentManager`."""

    id: str
    scope: str
    required_tools: frozenset[str] = frozenset()
    soft_tool_error_tools: frozenset[str] = frozenset()
    #: Tools whose untruncated result must be preserved in the run's
    #: ``tool_events``. Declared per profile; a profile that keeps none lets
    #: ordinary tool results stay bounded by default.
    preserve_tool_result_tools: frozenset[str] = frozenset()
    #: Structured-output contract the subagent must satisfy. Declared when the
    #: profile's result is machine-consumed; the manager fails the task instead
    #: of handing back an answer the contract rejected.
    output_contract_factory: Callable[[], OutputContract] | None = None
    prompt_builder: Callable[[dict[str, Any], Path], str] | None = None
    workspace_resolver: Callable[[dict[str, Any], Path], Path] | None = None
    result_handler: SubagentResultHandler | None = None
    max_iterations_message: str | None = None
