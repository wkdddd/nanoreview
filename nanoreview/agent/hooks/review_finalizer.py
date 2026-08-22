"""Review finalization hook."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

from loguru import logger

from nanoreview.agent.hooks.lifecycle import AgentHook, AgentHookContext
from nanoreview.review.input import policy_for_depth
from nanoreview.review.output.finalizer import ReviewFinalizer
from nanoreview.review.output.judge import ReviewJudge
from nanoreview.review.types import GitHubDiffEvidence, ReviewDepth


class ReviewFinalizerHook(AgentHook):
    """Runner hook that renders a fixed review report from subagent outputs."""

    # Matches the task id in spawn tool result text:
    # "Review subagent [<label>] started (id: <task_id>). ..."
    _SPAWN_ID_PATTERN = re.compile(r"started \(id: (\w+)\)")

    def __init__(
        self,
        *,
        workspace: str,
        target_name: str,
        changed_files: list[str] | None = None,
        depth: ReviewDepth = "full",
        judge: ReviewJudge | None = None,
        allowed_dimensions: list[str] | set[str] | None = None,
        can_finalize: Callable[[], bool] | None = None,
        remote_diff: GitHubDiffEvidence | None = None,
    ) -> None:
        super().__init__()
        self._target_name = target_name
        self._policy = policy_for_depth(depth)
        self._finalizer = ReviewFinalizer(
            workspace,
            changed_files,
            policy=self._policy,
            allowed_dimensions=allowed_dimensions,
            remote_diff=remote_diff,
        )
        self._rendered = False
        self._rendered_report: str | None = None
        self._seen_subagent_results: set[str] = set()
        self._judged = False
        self._judge = judge
        self._can_finalize = can_finalize or (lambda: True)

    def set_allowed_dimensions(self, allowed_dimensions: list[str] | set[str] | None) -> None:
        self._finalizer.set_allowed_dimensions(allowed_dimensions)

    def set_validation_context(
        self,
        *,
        workspace: str,
        changed_files: list[str] | None = None,
        local_target: str | None = None,
        remote_diff: GitHubDiffEvidence | None = None,
    ) -> None:
        self._finalizer.set_validation_context(
            workspace=workspace,
            changed_files=changed_files,
            local_target=local_target,
            remote_diff=remote_diff,
        )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._rendered:
            return
        ingested = self._ensure_ingested(context)
        if ingested <= 0:
            return
        self._judged = False
        await self._finalizer.apply_judge(self._judge)
        self._judged = True

    def _ensure_ingested(self, context: AgentHookContext) -> int:
        messages: list[dict[str, Any]] = []
        for message in context.messages:
            meta = ReviewFinalizer._subagent_metadata(message)
            if not meta:
                continue
            raw = ReviewFinalizer._subagent_raw_output(message, meta)
            key = self._subagent_result_key(meta, raw)
            if key in self._seen_subagent_results:
                continue
            self._seen_subagent_results.add(key)
            messages.append(message)
        if not messages:
            return 0
        return self._finalizer.ingest_messages(messages)

    @staticmethod
    def _subagent_result_key(meta: dict[str, Any], raw: str) -> str:
        task_id = meta.get("subagent_task_id")
        if isinstance(task_id, str) and task_id:
            return task_id
        label = str(meta.get("subagent_label") or meta.get("label") or "unknown")
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"{label}:{digest}"

    def _has_unreceived_spawns(self, context: AgentHookContext) -> bool:
        """Return True if any spawn tool result has no matching ingested subagent result.

        扫描 context.messages 中的 spawn tool 结果，提取已启动的 task_id，
        与 _seen_subagent_results 对比，判断是否存在结果尚未到达的子代理。
        用于在 finalize_content 中避免过早渲染空报告，让 runner 的注入机制
        有机会从 _session_results 队列获取已完成的子代理结果。
        """
        for msg in context.messages:
            if msg.get("role") != "tool" or msg.get("name") != "spawn":
                continue
            content_text = msg.get("content", "")
            if not isinstance(content_text, str):
                continue
            match = self._SPAWN_ID_PATTERN.search(content_text)
            if not match:
                continue
            task_id = match.group(1)
            if task_id not in self._seen_subagent_results:
                return True
        return False

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        if self._rendered:
            context.content_replaced = True
            return self._rendered_report or content
        ingested = self._ensure_ingested(context)
        if not self._can_finalize():
            if ingested:
                logger.info(
                    "review.finalizer.defer target={} ingested={} waiting_for_subagents=true",
                    self._target_name,
                    ingested,
                )
            return content
        if ingested == 0 and not self._finalizer.dimensions:
            # 子代理 Task 可能已完成并把结果放入 _session_results 队列，但
            # 尚未通过 _drain_pending 注入到 context.messages。此时渲染空报告
            # 会设置 content_replaced=True，导致 runner 跳过 _try_drain_injections，
            # 子代理结果被推迟到下一个 turn，最终产生第二份报告。
            # 这里检测已 spawn 但结果尚未到达的情况，保留原始 content，让 runner
            # 的注入机制有机会从 _session_results 队列获取结果。
            if self._has_unreceived_spawns(context):
                logger.info(
                    "review.finalizer.defer_for_pending_spawns target={}",
                    self._target_name,
                )
                return content
            logger.warning("review.finalizer.no_subagent_results target={}", self._target_name)
            result = self._finalizer.finalize(self._target_name)
            self._rendered = True
            self._rendered_report = result.report_markdown
            context.content_replaced = True
            logger.info(
                "review.finalizer.rendered target={} dimensions={} needs_confirmation={} errors={}",
                self._target_name,
                len(result.dimensions),
                len(result.needs_confirmation),
                len(result.errors),
            )
            return result.report_markdown
        result = self._finalizer.finalize(self._target_name)
        self._rendered = True
        self._rendered_report = result.report_markdown
        context.content_replaced = True
        logger.info(
            "review.finalizer.rendered target={} dimensions={} needs_confirmation={} errors={}",
            self._target_name,
            len(result.dimensions),
            len(result.needs_confirmation),
            len(result.errors),
        )
        return result.report_markdown
