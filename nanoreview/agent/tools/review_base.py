"""Shared base for repository review tools."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.agent.tools.base import Tool
from nanoreview.agent.tools.context import current_request_context
from nanoreview.rag import create_rag_runtime
from nanoreview.rag.config import RAGConfig
from nanoreview.rag.review_service import (
    SOURCE_TYPE,
    RepositoryRAGOptions,
    RepositoryRAGService,
)
from nanoreview.rag.runtime import RAGRuntime
from nanoreview.review.planning.evidence import ReviewEvidenceService
from nanoreview.review.source.github import GitHubRepoConfig, GitHubRepoReader
from nanoreview.review.source.local import LocalRepoReader
from nanoreview.review.types import ReviewAction, ReviewMetaKey

READER_ACTIONS = ("meta", "tree", "file")
REVIEW_ACTIONS = tuple(action.value for action in ReviewAction)
ALL_REVIEW_TOOL_ACTIONS = (*READER_ACTIONS, *REVIEW_ACTIONS)


def review_result_kind(result: str, *, status: str) -> str:
    if status == "error":
        return "error"
    if result.startswith("Error:"):
        return "error"
    if result.startswith("No text files found"):
        return "empty_files"
    if "No relevant" in result:
        return "no_hits"
    return "success"


class ReviewToolBase(Tool):
    _scopes = {"core", "reviewer"}

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        rag_config = getattr(ctx, "rag_config", None) or RAGConfig()
        runtime = create_rag_runtime(rag_config)
        tools_config = ctx.config if ctx.config else None
        review_config = getattr(ctx, "review_config", None)
        return cls(
            workspace=Path(ctx.workspace),
            runtime=runtime,
            github_config=getattr(tools_config, "github_repo", None),
            dense_backfill_limit=getattr(review_config, "prefetch_dense_backfill_limit", 256),
        )

    def __init__(
        self,
        workspace: Path,
        embedding_client: Any | None = None,
        rerank_client: Any | None = None,
        vector_store: Any | None = None,
        runtime: RAGRuntime | None = None,
        github_config: GitHubRepoConfig | None = None,
        dense_backfill_limit: int = 256,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        if runtime is None:
            runtime = RAGRuntime(
                embedding_client=embedding_client,
                rerank_client=rerank_client,
                vector_store=vector_store,
            )
        self.runtime = runtime
        options = RepositoryRAGOptions.from_retrieval_config(runtime.retrieval)
        options.dense_backfill_limit = max(0, int(dense_backfill_limit))
        self.repository_rag = RepositoryRAGService(
            workspace,
            runtime=runtime,
            options=options,
            source_type=SOURCE_TYPE,
        )
        self.local = LocalRepoReader(self.workspace, options=options)
        self.github = GitHubRepoReader(github_config, workspace=workspace)
        self.evidence_service = ReviewEvidenceService(
            self.repository_rag,
            self.github,
            workspace=self.workspace,
        )

    @property
    def evidence_provider(self) -> ReviewEvidenceService:
        return self.evidence_service

    @property
    def read_only(self) -> bool:
        return False

    def _unknown_action(self, action: str) -> str:
        allowed = ", ".join(ALL_REVIEW_TOOL_ACTIONS)
        return f"Error: unknown {self.name} action '{action}'. Use {allowed}."

    @staticmethod
    def _blocks_rag_repo_action(action: str) -> bool:
        ctx = current_request_context()
        metadata = ctx.metadata if ctx is not None else {}
        return (
            action == ReviewAction.REPO.value
            and str(metadata.get(ReviewMetaKey.ACTION) or "").strip().lower()
            == ReviewAction.DIFF.value
        )

    @staticmethod
    def _diff_context_window_tokens() -> int | None:
        ctx = current_request_context()
        metadata = ctx.metadata if ctx is not None else {}
        value = metadata.get(ReviewMetaKey.DIFF_CONTEXT_WINDOW_TOKENS)
        try:
            return int(value) if value is not None and int(value) > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _github_pr_head_ref() -> str | None:
        ctx = current_request_context()
        metadata = ctx.metadata if ctx is not None else {}
        if str(metadata.get(ReviewMetaKey.ACTION) or "").strip().lower() != ReviewAction.DIFF.value:
            return None
        value = str(metadata.get(ReviewMetaKey.GITHUB_PR_HEAD_REF) or "").strip()
        return value or None

    def _log_finish(
        self,
        *,
        trace_id: str,
        action: str,
        target_type: str,
        result_text: str,
        status: str,
        error: str,
        started: float,
    ) -> None:
        result_kind = review_result_kind(result_text, status=status)
        msg = (
            "{}.finish {} trace_id={} action={} target_type={} "
            "status={} result_kind={} result_chars={} elapsed_ms={:.1f}"
        )
        args: list = [
            self.name,
            "ok" if result_kind == "success" else "check",
            trace_id,
            action,
            target_type,
            status,
            result_kind,
            len(result_text),
            (time.perf_counter() - started) * 1000,
        ]
        if error:
            msg += " error={}"
            args.append(error)
        logger.info(msg, *args)
