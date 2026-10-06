"""Workspace access scope for tool execution.

Each agent turn binds one immutable ``WorkspaceScope`` before tools run. The
scope decides whether the generic filesystem/shell/message tools may reach
outside the project root (``full``) or must stay inside it (``restricted``).

Resolution order is message metadata -> session metadata -> global config. Any
illegal payload falls back to the whole config-derived default; no field of an
invalid payload is adopted and no user confirmation is awaited.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

#: Metadata key holding the scope payload, on both message and session metadata.
WORKSPACE_SCOPE_KEY = "workspace_scope"

#: Canonical access modes.
ACCESS_RESTRICTED = "restricted"
ACCESS_FULL = "full"

#: Nanobot input aliases, normalized to the canonical values above.
_ACCESS_MODE_ALIASES = {
    "restricted": ACCESS_RESTRICTED,
    "restrict": ACCESS_RESTRICTED,
    "full": ACCESS_FULL,
    "full-access": ACCESS_FULL,
}


@dataclass(frozen=True)
class WorkspaceScope:
    """Immutable per-turn workspace access decision."""

    project_path: Path
    access_mode: str

    @property
    def is_restricted(self) -> bool:
        return self.access_mode == ACCESS_RESTRICTED


def _default_scope(project_path: Path, restrict_to_workspace: bool) -> WorkspaceScope:
    """Build the config-derived fallback scope.

    ``ToolsConfig.restrict_to_workspace`` defaults to ``False``, so the default
    access mode is ``full``; an explicit ``True`` yields ``restricted``.
    """
    return WorkspaceScope(
        project_path=project_path,
        access_mode=ACCESS_RESTRICTED if restrict_to_workspace else ACCESS_FULL,
    )


def _parse_mode(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    return _ACCESS_MODE_ALIASES.get(raw.strip().lower())


def _parse_payload(payload: Any) -> WorkspaceScope | None:
    """Parse one metadata payload into a scope, or ``None`` when it is invalid.

    The payload is accepted only as a whole: a missing field, an unknown mode,
    a relative path, or a path that is not an existing directory rejects the
    entire payload.
    """
    if not isinstance(payload, Mapping):
        return None

    raw_path = payload.get("project_path")
    if not isinstance(raw_path, (str, os.PathLike)) or not str(raw_path).strip():
        return None

    raw_mode = payload.get("access_mode")
    mode = _parse_mode(raw_mode)
    if mode is None:
        return None

    try:
        path = Path(raw_path).expanduser()
    except (TypeError, ValueError, OSError):
        return None
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_dir():
        return None

    return WorkspaceScope(project_path=resolved, access_mode=mode)


def resolve_workspace_scope(
    *,
    default_project_path: Path,
    restrict_to_workspace: bool,
    message_metadata: Mapping[str, Any] | None = None,
    session_metadata: Mapping[str, Any] | None = None,
) -> WorkspaceScope:
    """Resolve the turn's scope from metadata, falling back to config.

    Message metadata wins over session metadata; session metadata wins over the
    global config default. A level that omits the key is simply not a source and
    resolution continues. A level that *carries* an invalid payload aborts the
    whole chain and returns the config-derived default: no field of the illegal
    payload is adopted, and no lower level is consulted either.
    """
    default_scope = _default_scope(default_project_path, restrict_to_workspace)

    for source_name, metadata in (
        ("message", message_metadata),
        ("session", session_metadata),
    ):
        if not isinstance(metadata, Mapping):
            continue
        if WORKSPACE_SCOPE_KEY not in metadata:
            continue
        payload = metadata.get(WORKSPACE_SCOPE_KEY)
        parsed = _parse_payload(payload)
        if parsed is None:
            # The payload is rejected as a whole: an illegal scope declaration
            # must not silently inherit a wider scope from a lower level.
            logger.warning(
                "workspace_scope.invalid source={} fallback=config", source_name
            )
            return default_scope
        return parsed

    return default_scope


def review_workspace_scope(project_path: Path) -> WorkspaceScope:
    """Scope fixed for ReviewLoop, planner, reviewer and Judge.

    Review roles always run restricted against the target repository root. They
    never accept conversation metadata that would widen their access.
    """
    try:
        resolved = Path(project_path).expanduser().resolve()
    except OSError:
        resolved = Path(project_path)
    return WorkspaceScope(project_path=resolved, access_mode=ACCESS_RESTRICTED)
