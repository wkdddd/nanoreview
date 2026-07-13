"""Spawn tool for dedicated review subagents."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.review.types import ReviewMetaKey, normalize_review_dimension

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager

# Metadata keys that are safe to forward to subagent runtime.
# Excludes EVIDENCE_PROVIDER and other internal objects that are not
# JSON-serializable or should not leak across the subagent boundary.
_SAFE_METADATA_KEYS = frozenset(
    {
        ReviewMetaKey.MODE,
        ReviewMetaKey.TARGET,
        ReviewMetaKey.TARGET_TYPE,
        ReviewMetaKey.MODE_VARIANT,
        ReviewMetaKey.ACTION,
        ReviewMetaKey.FOCUS,
        ReviewMetaKey.TARGET_REF,
        ReviewMetaKey.LOCAL_ROOT,
        ReviewMetaKey.LOCAL_TARGET,
        ReviewMetaKey.LOCAL_SCOPE_KIND,
        ReviewMetaKey.MAX_SUBAGENTS,
        ReviewMetaKey.ALLOWED_DIMENSIONS,
        ReviewMetaKey.GITHUB_PREFETCH_READY,
    }
)


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The review task for the dedicated review subagent."),
        label=StringSchema("Review dimension key or label, such as security or Security Reviewer."),
        required=["task", "label"],
    )
)
class SpawnTool(Tool, ContextAware):
    """Spawn a dedicated review subagent with structured finding submission."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel: ContextVar[str] = ContextVar(
            "review_spawn_origin_channel",
            default="cli",
        )
        self._origin_chat_id: ContextVar[str] = ContextVar(
            "review_spawn_origin_chat_id",
            default="direct",
        )
        self._session_key: ContextVar[str] = ContextVar(
            "review_spawn_session_key",
            default="cli:direct",
        )
        self._origin_message_id: ContextVar[str | None] = ContextVar(
            "review_spawn_origin_message_id",
            default=None,
        )
        self._metadata: ContextVar[dict[str, Any]] = ContextVar(
            "review_spawn_metadata",
            default={},
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    def set_context(self, ctx: RequestContext) -> None:
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")
        self._origin_message_id.set(ctx.message_id)
        self._metadata.set(dict(ctx.metadata or {}))

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a dedicated code-review subagent. Use only during review mode. "
            "The subagent submits findings through review_submit."
        )

    async def execute(self, task: str, label: str, **kwargs: Any) -> str:
        metadata = self._metadata.get()
        allowed = self._normalize_allowed_review_dimensions(metadata)
        if allowed is None:
            return "Error: dimensions is missing"
        dimension = normalize_review_dimension(label)
        if dimension is None or dimension not in allowed:
            return (
                "Error: Cannot spawn review subagent: dimension "
                f"'{label}' is not allowed. Allowed dimensions: "
                f"{', '.join(sorted(allowed))}."
            )
        running = self._manager.get_running_count()
        limit = self._manager.max_concurrent_subagents
        if running >= limit:
            return (
                f"Error: Cannot spawn review subagent: concurrency limit reached "
                f"({running}/{limit} running). Wait for a running subagent "
                f"to complete before spawning a new one."
            )
        origin_metadata = {
            key: value for key, value in metadata.items() if key in _SAFE_METADATA_KEYS
        }
        return await self._manager.spawn(
            task=task,
            label=dimension,
            origin_channel=self._origin_channel.get(),
            origin_chat_id=self._origin_chat_id.get(),
            session_key=self._session_key.get(),
            origin_message_id=self._origin_message_id.get(),
            origin_metadata=origin_metadata,
        )

    @staticmethod
    def _normalize_allowed_review_dimensions(metadata: dict[str, Any]) -> set[str] | None:
        raw = metadata.get(ReviewMetaKey.ALLOWED_DIMENSIONS)
        if not isinstance(raw, list):
            return None
        allowed = {
            dimension for item in raw if (dimension := normalize_review_dimension(str(item)))
        }
        return allowed or None
