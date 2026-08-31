"""Runtime profiles for the generic subagent manager."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from nanoreview.agent.runner import AgentRunResult


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
        retry: Callable[[], Awaitable[tuple[str | None, str]]],
        target_type: str,
    ) -> SubagentCompletion: ...


@dataclass(frozen=True, slots=True)
class SubagentExecutionProfile:
    """Declarative runtime policy consumed by :class:`SubagentManager`."""

    id: str
    scope: str
    terminal_tools: frozenset[str] = frozenset()
    soft_tool_error_tools: frozenset[str] = frozenset()
    prompt_builder: Callable[[dict[str, Any], Path], str] | None = None
    workspace_resolver: Callable[[dict[str, Any], Path], Path] | None = None
    result_handler: SubagentResultHandler | None = None
    max_iterations_message: str | None = None


GENERIC_SUBAGENT_PROFILE = SubagentExecutionProfile(
    id="generic",
    scope="subagent",
)
