"""Local repository review tool."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from loguru import logger

from nanoreview.agent.tools.base import tool_parameters
from nanoreview.agent.tools.context import current_request_context
from nanoreview.agent.tools.review_base import (
    ALL_REVIEW_TOOL_ACTIONS,
    READER_ACTIONS,
    ReviewToolBase,
)
from nanoreview.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanoreview.review.source.local import LocalRepoReader
from nanoreview.review.types import LocalReviewScope, ReviewAction, ReviewMetaKey


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "Local repository action: meta, tree, file, repo, or diff",
            enum=ALL_REVIEW_TOOL_ACTIONS,
            nullable=True,
        ),
        target=StringSchema(
            "Optional local file or directory path for meta/tree/file actions.",
            nullable=True,
        ),
        repo_path=StringSchema(
            "Local repository-relative file or directory path for action='file'.",
            nullable=True,
        ),
        tree_pattern=StringSchema(
            "Glob filter for local tree results (e.g. '*.py')",
            nullable=True,
        ),
        tree_limit=IntegerSchema(
            500,
            description="Maximum local tree entries to return",
            minimum=1,
            maximum=10000,
        ),
        offset=IntegerSchema(
            1,
            description="Line number to start reading from for action='file' (1-indexed)",
            minimum=1,
        ),
        limit=IntegerSchema(
            200,
            description="Maximum lines to return for action='file'",
            minimum=1,
            maximum=500,
            nullable=True,
        ),
        review_query=StringSchema(
            "Question or keywords describing repository review references to retrieve",
            nullable=True,
        ),
        max_results=IntegerSchema(
            5,
            description="Maximum repository review references to return",
            minimum=1,
            maximum=20,
        ),
        include_tests=BooleanSchema(
            description="Include likely related test file paths when available",
            default=True,
        ),
        required=[],
    )
)
class LocalReviewTool(ReviewToolBase):
    """Tool wrapper for local repository reading and review evidence."""

    @property
    def name(self) -> str:
        return "local_review"

    @property
    def description(self) -> str:
        return (
            "Local repository reader and review evidence tool. Use meta/tree/file for "
            "read-only local repository inspection, repo for programmatic full or scoped evidence preparation, "
            "and diff for programmatically filtered current local git patches."
        )

    async def execute(
        self,
        review_query: str | None = None,
        action: str | None = None,
        target: str | None = None,
        repo_path: str | None = None,
        tree_pattern: str | None = None,
        tree_limit: int = 500,
        offset: int = 1,
        limit: int | None = None,
        max_results: int = 5,
        include_tests: bool | None = None,
    ) -> str:
        trace_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        action_value = (action or ReviewAction.REPO.value).strip().lower()
        result_text = ""
        status = "ok"
        error = ""
        logger.info(
            "local_review.start trace_id={} action={} target={} query_chars={} max_results={}",
            trace_id,
            action_value,
            target or repo_path,
            len(review_query or ""),
            max_results,
        )
        try:
            if self._is_github_review_turn():
                result_text = (
                    "Error: local_review is disabled for GitHub review targets. "
                    "Use github_review(meta/tree/file/repo) for remote evidence; if GitHub content "
                    "cannot be read, report the evidence limitation instead of inspecting local files."
                )
                logger.warning(
                    "local_review.blocked_github_target trace_id={} action={} target={}",
                    trace_id,
                    action_value,
                    target or repo_path,
                )
                return result_text
            if action_value not in ALL_REVIEW_TOOL_ACTIONS:
                result_text = self._unknown_action(action_value)
                logger.warning(
                    "local_review.invalid_action trace_id={} action={}",
                    trace_id,
                    action_value,
                )
                return result_text
            if self._blocks_repo_action_during_diff(action_value):
                result_text = (
                    "Error: local_review action='repo' is unavailable during diff review. "
                    "Use local_review(action='meta'/'tree'/'file') or read_file."
                )
                logger.warning("local_review.repo_blocked_during_diff trace_id={}", trace_id)
                return result_text
            if action_value in READER_ACTIONS:
                path = repo_path or target
                local_scope = self._get_local_scope()
                reader = self._scoped_reader(local_scope)
                result_text = await reader.execute(
                    action=action_value,
                    path=path,
                    pattern=tree_pattern,
                    max_entries=tree_limit,
                    offset=offset,
                    limit=limit,
                )
                return result_text
            local_scope = self._get_local_scope()
            result_text = await self.evidence_service.dispatch(
                target_type="local",
                action=action_value,
                tree_pattern=tree_pattern,
                review_query=review_query,
                max_results=max_results,
                include_tests=include_tests,
                local_scope=local_scope,
                trace_id=trace_id,
                context_window_tokens=self._diff_context_window_tokens(),
            )
            return result_text
        except Exception as exc:
            status = "error"
            error = str(exc)
            logger.exception(
                "local_review.failed trace_id={} action={} target={}",
                trace_id,
                action_value,
                target or repo_path,
            )
            raise
        finally:
            self._log_finish(
                trace_id=trace_id,
                action=action_value,
                target_type="local",
                result_text=result_text,
                status=status,
                error=error,
                started=started,
            )

    @staticmethod
    def _is_github_review_turn() -> bool:
        '''disable local_review tool when github_review'''
        ctx = current_request_context()
        metadata = ctx.metadata if ctx is not None else {}
        return str(metadata.get(ReviewMetaKey.TARGET_TYPE) or "").strip().lower() == "github"

    def _get_local_scope(self) -> LocalReviewScope | None:
        ctx = current_request_context()
        metadata = ctx.metadata if ctx is not None else {}
        local_root = metadata.get(ReviewMetaKey.LOCAL_ROOT)
        if not local_root:
            return None
        return LocalReviewScope(
            review_root=str(local_root),
            kind=metadata.get(ReviewMetaKey.LOCAL_SCOPE_KIND, "directory"),
            target_path=metadata.get(ReviewMetaKey.LOCAL_TARGET, ""),
        )

    def _scoped_reader(self, local_scope: LocalReviewScope | None) -> LocalRepoReader:
        if local_scope is None:
            return self.local
        scoped_root = Path(local_scope.review_root).expanduser().resolve()
        if scoped_root == self.workspace:
            return self.local
        return LocalRepoReader(scoped_root, options=self.local.options)
