"""The single model-facing tool for PR review context.

The tool is a thin adapter: the host binds a :class:`PrContextSession` that
already knows the snapshot, the pinned SHAs, the role policy and the budgets.
The model can choose *what* to inspect (action, path, query), never which
repository, commit, URL or permission level to use.
"""

from __future__ import annotations

from typing import Any

from nanoreview.agent.tools.base import Tool, tool_parameters
from nanoreview.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanoreview.review.source.pr_context import PrContextSession


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "Operation: diff (changed hunks), read (pinned file text), "
            "list (pinned file tree), search (pinned content search)"
        ),
        path=StringSchema("Repository-relative path for diff/read/list"),
        side=StringSchema("read side: new (head) or old (merge base); default new"),
        offset=IntegerSchema(
            0,
            description="First line for action=read (1-based); item offset for list/search",
            minimum=0,
        ),
        limit=IntegerSchema(
            200, description="Maximum lines for action=read (max 200)", minimum=1, maximum=200
        ),
        query=StringSchema("Text or regex for action=search"),
        scope=StringSchema("Repository-relative directory or path prefix for action=search"),
        glob=StringSchema("Path glob for action=search, e.g. 'src/**/*.py'"),
        type=StringSchema("File type for action=search, e.g. 'py' or 'ts'"),
        fixed_strings=BooleanSchema(
            default=True, description="Treat query literally (default true)"
        ),
        case_insensitive=BooleanSchema(default=False, description="Ignore case (default false)"),
        output_mode=StringSchema(
            "search output: content (default), files_with_matches, count"
        ),
        context_before=IntegerSchema(5, description="Context lines before a match", minimum=0, maximum=20),
        context_after=IntegerSchema(5, description="Context lines after a match", minimum=0, maximum=20),
        head_limit=IntegerSchema(
            250, description="Maximum matches or entries (250 default; 0 for all)", minimum=0, maximum=1000
        ),
        required=["action"],
    )
)
class PrContextTool(Tool):
    """Read-only access to the frozen pull-request snapshot."""

    _scopes = {
        "reviewer.bug", "reviewer.security", "reviewer.performance",
        "reviewer.maintainability", "reviewer.general", "judge",
    }

    def __init__(self, session: PrContextSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "pr_context"

    @property
    def description(self) -> str:
        return (
            "Inspect the pull request being reviewed at its pinned commits. "
            "action=diff returns changed hunks; read returns numbered lines from the "
            "head (new) or merge-base (old) side; list returns the pinned file tree; "
            "search uses the shared plain-text matcher. All reads are read-only and "
            "budgeted; cite returned evidence_id values in findings."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        action: str,
        path: str | None = None,
        side: str = "new",
        offset: int = 0,
        limit: int | None = None,
        query: str | None = None,
        scope: str = "",
        glob: str | None = None,
        type: str | None = None,
        fixed_strings: bool = True,
        case_insensitive: bool = False,
        output_mode: str = "content",
        context_before: int = 5,
        context_after: int = 5,
        head_limit: int | None = None,
        **kwargs: Any,
    ) -> str:
        normalized = (action or "").strip().lower()
        if normalized == "diff":
            return (await self._session.diff(path)).text
        if normalized == "read":
            if not path:
                return "Error: action=read requires a path"
            return (
                await self._session.read(
                    path, side=side, offset=max(1, offset), limit=limit
                )
            ).text
        if normalized == "list":
            return (
                await self._session.list(
                    path or "", recursive=True, head_limit=head_limit, offset=max(0, offset)
                )
            ).text
        if normalized == "search":
            if not query:
                return "Error: action=search requires a query"
            return (
                await self._session.search(
                    query,
                    scope=scope or "",
                    glob=glob,
                    type=type,
                    case_insensitive=case_insensitive,
                    fixed_strings=fixed_strings,
                    output_mode=output_mode,
                    context_before=context_before,
                    context_after=context_after,
                    head_limit=head_limit,
                    offset=max(0, offset),
                )
            ).text
        return (
            f"Error: unknown action {action!r}; use diff, read, list or search"
        )


__all__ = ["PrContextTool"]
