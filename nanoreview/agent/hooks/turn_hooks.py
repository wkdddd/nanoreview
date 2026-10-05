"""Turn-scoped hook assembly for agent runs.

Every ``AgentRunner`` call site builds its hook chain here so turn-local state
(stream buffers, tool trackers, file-edit trackers) never leaks across turns.
Assembly order is fixed: progress hook, registered factories, registered hooks,
turn factories, turn hooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanoreview.agent.hooks.lifecycle import (
    AgentHook,
    AgentTurnHookContext,
    AgentTurnHookFactory,
    CompositeHook,
)
from nanoreview.agent.hooks.progress import AgentProgressHook
from nanoreview.events import NO_EVENTS, EventSink

__all__ = ["AgentTurnHookFactory", "AgentTurnHookSpec", "build_agent_turn_hook"]


@dataclass(slots=True)
class AgentTurnHookSpec:
    """Inputs needed to build the hook chain for one agent turn."""

    events: EventSink = NO_EVENTS
    streaming: bool = False
    channel: str = "cli"
    chat_id: str = "direct"
    message_id: str | None = None
    metadata: dict[str, Any] | None = None
    session_key: str | None = None
    workspace: Path | None = None
    tool_hint_max_length: int = 40
    set_tool_context: Callable[..., None] | None = None
    on_iteration: Callable[[int], None] | None = None
    suppress_content_progress: bool = False
    registered_hook_factories: list[AgentTurnHookFactory] = field(default_factory=list)
    turn_hook_factories: list[AgentTurnHookFactory] = field(default_factory=list)
    registered_hooks: list[AgentHook] = field(default_factory=list)
    turn_hooks: list[AgentHook] = field(default_factory=list)
    ephemeral: bool = False
    run_extra_hooks_for_ephemeral: bool = False
    attributes: dict[str, Any] | None = None


def build_agent_turn_hook(spec: AgentTurnHookSpec) -> AgentHook:
    """Build the hook chain used by ``AgentRunner`` for one turn."""
    progress_hook = AgentProgressHook(
        spec.events,
        streaming=spec.streaming,
        channel=spec.channel,
        chat_id=spec.chat_id,
        message_id=spec.message_id,
        metadata=spec.metadata,
        session_key=spec.session_key,
        tool_hint_max_length=spec.tool_hint_max_length,
        set_tool_context=spec.set_tool_context,
        on_iteration=spec.on_iteration,
        suppress_content_progress=spec.suppress_content_progress,
    )
    if spec.ephemeral and not spec.run_extra_hooks_for_ephemeral:
        return progress_hook

    turn_context = AgentTurnHookContext(
        events=spec.events,
        workspace=spec.workspace,
        channel=spec.channel,
        chat_id=spec.chat_id,
        message_id=spec.message_id,
        session_key=spec.session_key,
        metadata=dict(spec.metadata or {}),
        attributes=dict(spec.attributes or {}),
        ephemeral=spec.ephemeral,
    )
    hook_chain: list[AgentHook] = [progress_hook]

    for factory in spec.registered_hook_factories:
        created_hook = _create_hook(factory, turn_context)
        if created_hook is not None:
            hook_chain.append(created_hook)

    hook_chain.extend(spec.registered_hooks)

    for factory in spec.turn_hook_factories:
        created_hook = _create_hook(factory, turn_context)
        if created_hook is not None:
            hook_chain.append(created_hook)

    hook_chain.extend(spec.turn_hooks)
    return CompositeHook(hook_chain) if len(hook_chain) > 1 else progress_hook


def _create_hook(
    factory: AgentTurnHookFactory,
    turn_context: AgentTurnHookContext,
) -> AgentHook | None:
    """Build one hook, skipping (but recording) a failing factory."""
    try:
        return factory(turn_context)
    except Exception:
        logger.exception("Agent turn hook factory failed: {}", getattr(factory, "__name__", factory))
        return None
