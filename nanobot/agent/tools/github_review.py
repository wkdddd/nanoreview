"""GitHub repository review tool."""

from __future__ import annotations

import time
import uuid

from loguru import logger

from nanobot.agent.tools.base import tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.review_base import (
    ALL_REVIEW_TOOL_ACTIONS,
    READER_ACTIONS,
    ReviewToolBase,
)
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.review.source.utils import parse_pr_target
from nanobot.review.types import ReviewAction, ReviewMetaKey


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "GitHub repository action: meta, tree, file, repo, or diff",
            enum=ALL_REVIEW_TOOL_ACTIONS,
            nullable=True,
        ),
        target=StringSchema(
            "Optional GitHub repo URL or GitHub PR URL. PR URLs imply action='diff'.",
            nullable=True,
        ),
        target_repo=StringSchema(
            "GitHub repo in 'owner/repo' format or a full GitHub URL.",
            nullable=True,
        ),
        pr_number=IntegerSchema(
            0,
            description="GitHub pull request number for action='diff'",
            minimum=0,
            maximum=1000000,
        ),
        repo_path=StringSchema(
            "File path within the GitHub repo for action='file'.",
            nullable=True,
        ),
        ref=StringSchema("GitHub branch, tag, or commit SHA", nullable=True),
        tree_pattern=StringSchema(
            "Glob filter for GitHub tree or repo snapshot results (e.g. '*.py')",
            nullable=True,
        ),
        tree_limit=IntegerSchema(
            500,
            description="Maximum GitHub tree entries to return",
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
class GitHubReviewTool(ReviewToolBase):
    """Tool wrapper for GitHub repository reading and review evidence."""

    @property
    def name(self) -> str:
        return "github_review"

    @property
    def description(self) -> str:
        return (
            "GitHub repository reader and review evidence tool. Use meta/tree/file for "
            "read-only GitHub API inspection, repo for RAG-backed full or scoped remote evidence retrieval, "
            "and diff for programmatically filtered GitHub pull request patches. Do not clone repositories; repo/diff "
            "actions use fixed snapshots under workspace/.nanobot/review_github."
        )

    async def execute(
        self,
        review_query: str | None = None,
        action: str | None = None,
        target: str | None = None,
        target_repo: str | None = None,
        pr_number: int = 0,
        repo_path: str | None = None,
        ref: str | None = None,
        tree_pattern: str | None = None,
        tree_limit: int = 500,
        offset: int = 1,
        limit: int = 200,
        max_results: int = 5,
        include_tests: bool | None = None,
    ) -> str:
        trace_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        action_value = (action or ReviewAction.REPO.value).strip().lower()
        result_text = ""
        status = "ok"
        error = ""
        pr_repo, parsed_pr_number = parse_pr_target(target)
        if pr_repo:
            target_repo = target_repo or pr_repo
            pr_number = pr_number or parsed_pr_number or 0
            if action_value == ReviewAction.REPO.value:
                action_value = ReviewAction.DIFF.value
        repo = (target_repo or target or "").strip()
        logger.info(
            "github_review.start trace_id={} action={} target={} target_repo={} pr={} query_chars={} max_results={}",
            trace_id,
            action_value,
            target,
            target_repo,
            pr_number,
            len(review_query or ""),
            max_results,
        )
        try:
            if self._is_local_review_turn():
                result_text = (
                    "Error: github_review is disabled for local review targets. "
                    "Use local_review(meta/tree/file/repo/diff), read_file, grep, "
                    "or list_dir for local evidence."
                )
                logger.warning(
                    "github_review.blocked_local_target trace_id={} action={} repo={}",
                    trace_id,
                    action_value,
                    repo,
                )
                return result_text
            if action_value not in ALL_REVIEW_TOOL_ACTIONS:
                result_text = self._unknown_action(action_value)
                logger.warning(
                    "github_review.invalid_action trace_id={} action={} repo={}",
                    trace_id,
                    action_value,
                    repo,
                )
                return result_text
            if self._blocks_rag_repo_action(action_value):
                result_text = (
                    "Error: github_review action='repo' is unavailable during diff review because "
                    "it uses RAG. Use github_review(action='meta'/'tree'/'file')."
                )
                logger.warning("github_review.rag_repo_blocked trace_id={} repo={}", trace_id, repo)
                return result_text
            if not self.github.config.enable:
                result_text = "Error: GitHub repository access is disabled by tools.githubRepo.enable."
                logger.warning("github_review.disabled trace_id={} repo={}", trace_id, repo)
                return result_text
            if not repo:
                result_text = "Error: target_repo is required for github_review."
                logger.warning("github_review.missing_repo trace_id={} action={}", trace_id, action_value)
                return result_text
            if action_value == "file" and not repo_path:
                result_text = "Error: repo_path is required for github_review action='file'."
                logger.warning("github_review.missing_path trace_id={} repo={}", trace_id, repo)
                return result_text
            if action_value == ReviewAction.DIFF.value and int(pr_number or 0) <= 0:
                result_text = "Error: pr_number is required for github_review action='diff'."
                logger.warning("github_review.missing_pr trace_id={} repo={}", trace_id, repo)
                return result_text
            if action_value == ReviewAction.REPO.value and self._has_github_prefetch_for(repo):
                result_text = (
                    "Error: full GitHub repository evidence was already prefetched for this review turn. "
                    "Use github_review(action='meta'/'tree'/'file') for precise follow-up evidence instead "
                    "of re-fetching the whole repository."
                )
                logger.warning(
                    "github_review.duplicate_repo_blocked trace_id={} repo={}",
                    trace_id,
                    repo,
                )
                return result_text
            if action_value in READER_ACTIONS:
                result_text = await self.github.execute(
                    action=action_value,
                    repo=repo,
                    path=repo_path,
                    ref=ref or self._github_pr_head_ref(),
                    pattern=tree_pattern,
                    max_entries=tree_limit,
                    pr_number=int(pr_number or 0),
                    offset=offset,
                    limit=limit,
                )
                return result_text
            result_text = await self.evidence_service.dispatch(
                target_type="github",
                action=action_value,
                repo=repo,
                ref=ref,
                pr_number=int(pr_number or 0),
                tree_pattern=tree_pattern,
                review_query=review_query,
                max_results=max_results,
                include_tests=include_tests,
                trace_id=trace_id,
                context_window_tokens=self._diff_context_window_tokens(),
            )
            return result_text
        except Exception as exc:
            status = "error"
            error = str(exc)
            logger.exception(
                "github_review.failed trace_id={} action={} repo={} pr={}",
                trace_id,
                action_value,
                repo,
                pr_number,
            )
            raise
        finally:
            self._log_finish(
                trace_id=trace_id,
                action=action_value,
                target_type="github",
                result_text=result_text,
                status=status,
                error=error,
                started=started,
            )

    @staticmethod
    def _has_github_prefetch_for(repo: str) -> bool:
        ctx = current_request_context()
        metadata = ctx.metadata if ctx is not None else {}
        if str(metadata.get(ReviewMetaKey.TARGET_TYPE) or "").strip().lower() != "github":
            return False
        if not metadata.get(ReviewMetaKey.GITHUB_PREFETCH_READY):
            return False
        target_repo = str(metadata.get(ReviewMetaKey.TARGET) or metadata.get("github_repo") or "")
        if "/" in repo and "/" in target_repo:
            return repo.strip().lower().rstrip("/") in target_repo.lower()
        return True

    @staticmethod
    def _is_local_review_turn() -> bool:
        """Disable github_review tool when the current review targets local files."""
        ctx = current_request_context()
        metadata = ctx.metadata if ctx is not None else {}
        return str(metadata.get(ReviewMetaKey.TARGET_TYPE) or "").strip().lower() == "local"
