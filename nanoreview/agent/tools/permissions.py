"""Tool execution approval policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanoreview.agent.tools.base import Tool


class PermissionVerdict(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class PermissionPolicy:
    approval_enabled: bool = False


def _tool_requires_approval(
    tool_name: str,
    tool: "Tool | None",
    params: dict[str, Any],
) -> bool:
    if tool is None:
        return False
    requires_approval = getattr(tool, "requires_approval", None)
    if callable(requires_approval):
        return bool(requires_approval(params))
    return False


def check_permission(
    tool_name: str,
    tool: "Tool | None",
    params: dict[str, Any],
    policy: PermissionPolicy,
) -> PermissionVerdict:
    """Return whether a tool call can run or needs user approval."""
    if policy.approval_enabled and _tool_requires_approval(tool_name, tool, params):
        return PermissionVerdict.CONFIRM
    return PermissionVerdict.ALLOW


def resolve_policy(
    config_permissions: Any,
    session_metadata: dict[str, Any],
    *,
    approval_enabled_override: bool | None = None,
) -> PermissionPolicy:
    """Merge global config + session override into a binary approval policy."""
    approval_enabled = bool(getattr(config_permissions, "approval_enabled", False))

    sess_perms = session_metadata.get("permissions")
    if isinstance(sess_perms, dict) and "approval_enabled" in sess_perms:
        approval_enabled = bool(sess_perms["approval_enabled"])

    if approval_enabled_override is not None:
        approval_enabled = approval_enabled_override

    return PermissionPolicy(approval_enabled=approval_enabled)
