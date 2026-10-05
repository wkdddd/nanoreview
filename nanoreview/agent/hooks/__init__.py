"""Hook primitives and built-in hook implementations."""

from nanoreview.agent.hooks.file_edit import (
    FileEditActivityHook,
    create_file_edit_activity_hook,
)
from nanoreview.agent.hooks.lifecycle import (
    AgentHook,
    AgentHookContext,
    AgentRunHookContext,
    AgentTurnHookContext,
    AgentTurnHookFactory,
    CompositeHook,
    FinalizeContentResult,
    finalize_content_result,
)
from nanoreview.agent.hooks.progress import AgentProgressHook
from nanoreview.agent.hooks.sdk import SDKCaptureHook
from nanoreview.agent.hooks.subagent import SubagentHook, SubagentStatus
from nanoreview.agent.hooks.turn_hooks import AgentTurnHookSpec, build_agent_turn_hook

__all__ = [
    "AgentHook",
    "AgentHookContext",
    "AgentProgressHook",
    "AgentRunHookContext",
    "AgentTurnHookContext",
    "AgentTurnHookFactory",
    "AgentTurnHookSpec",
    "CompositeHook",
    "FileEditActivityHook",
    "FinalizeContentResult",
    "SDKCaptureHook",
    "SubagentHook",
    "SubagentStatus",
    "build_agent_turn_hook",
    "create_file_edit_activity_hook",
    "finalize_content_result",
]
