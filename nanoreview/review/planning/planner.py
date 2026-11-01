"""Build structured review plans for a pull request.

This module is purely programmatic: it turns the admitted pull request, the
user's reviewer selection and the frozen patches into a ``ReviewPlan`` plus the
evidence the reviewers will receive. It performs no model call and makes no
triage decision — the reviewer set is exactly what the user selected.

There is no local target resolution here: a review input is the PR snapshot
captured at admission, so nothing in this module reads a working tree, a `.git`
directory or a branch.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from loguru import logger

from nanoreview.agent.context import ContextBuilder
from nanoreview.review.input.normalizers import (
    normalize_requested_dimensions,
    normalize_review_action,
    normalize_review_target_type,
)
from nanoreview.review.types import (
    PULL_REQUEST_TARGET_TYPE,
    PullRequestTarget,
    ReviewEvidenceBundle,
    ReviewMetaKey,
    ReviewPlan,
    ReviewTargetType,
)
from nanoreview.session.manager import Session


@dataclass(frozen=True, slots=True)
class ReviewPreparation:
    """Program-owned review inputs prepared before the reviewers run.

    The preparation deliberately carries no prompt: reviewers build their own
    task from the profile template and the frozen evidence, so rendering a
    coordinator prompt here would be work nobody consumes.
    """

    plan: ReviewPlan | None
    evidence: ReviewEvidenceBundle | None = None


def pull_request_from_metadata(
    metadata: dict[str, Any],
) -> PullRequestTarget | None:
    """Reconstruct the frozen PR target from persisted session metadata."""
    number = metadata.get(ReviewMetaKey.PULL_REQUEST_NUMBER)
    repository = metadata.get(ReviewMetaKey.BASE_REPO)
    base_sha = metadata.get(ReviewMetaKey.BASE_SHA)
    head_sha = metadata.get(ReviewMetaKey.HEAD_SHA)
    merge_base_sha = metadata.get(ReviewMetaKey.MERGE_BASE_SHA)
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    if not all(isinstance(item, str) and item for item in (repository, base_sha, head_sha, merge_base_sha)):
        return None
    head_repo = metadata.get(ReviewMetaKey.HEAD_REPO)
    return PullRequestTarget(
        repository=str(repository),
        number=number,
        base_repo=str(repository),
        head_repo=str(head_repo) if isinstance(head_repo, str) and head_repo else str(repository),
        base_sha=str(base_sha),
        head_sha=str(head_sha),
        merge_base_sha=str(merge_base_sha),
        snapshot_id=str(metadata.get(ReviewMetaKey.SNAPSHOT_REF) or ""),
        url=str(metadata.get(ReviewMetaKey.TARGET) or "") or None,
    )


def build_review_plan(
    *,
    target: str | None = None,
    user_content: str = "",
    focus: str | list[str] | None = None,
    max_subagents: Any = 4,
    target_type: str | None = None,
    action: str | None = None,
    pull_request: PullRequestTarget | None = None,
) -> ReviewPlan | None:
    trace_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()
    logger.info(
        "review.plan.start trace_id={} target={} action={}",
        trace_id,
        target,
        action or "auto",
    )
    try:
        roles = normalize_requested_dimensions(focus)
        normalized_type = normalize_review_target_type(target_type, target)
        resolved_action = normalize_review_action(action)
    except ValueError as exc:
        logger.warning("review.plan.invalid trace_id={} reason={}", trace_id, exc)
        return None
    if pull_request is None:
        logger.info("review.plan.done trace_id={} status=no_pull_request", trace_id)
        return None

    plan = ReviewPlan(
        target=target or pull_request.url or pull_request.label,
        target_name=pull_request.label,
        target_type=normalized_type or PULL_REQUEST_TARGET_TYPE,  # type: ignore[arg-type]
        action=resolved_action,
        roles=roles,
        user_requirements=user_content.strip(),
        pull_request=pull_request,
    )
    logger.info(
        "review.plan.done trace_id={} action={} target_type={} pr={} requested_dimensions={} roles={} user_requirements={} elapsed_ms={:.1f}",
        trace_id,
        plan.action.value,
        plan.target_type,
        pull_request.label,
        focus,
        [role.name for role in plan.roles],
        plan.user_requirements,
        (time.perf_counter() - started) * 1000,
    )
    return plan


def _as_target_type(value: Any) -> ReviewTargetType | None:
    try:
        return normalize_review_target_type(value if isinstance(value, str) else None)  # type: ignore[return-value]
    except ValueError:
        return None


def latest_user_text(messages: list[dict[str, Any]]) -> str:
    """Return the latest user text, without the appended runtime metadata."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block, str):
                    parts.append(block)
            text = "\n".join(parts)
        else:
            text = ""
        if ContextBuilder._RUNTIME_CONTEXT_TAG in text:
            text = text.split(ContextBuilder._RUNTIME_CONTEXT_TAG, 1)[0]
        return text.strip()
    return ""


async def prepare_code_review_context(
    initial_messages: list[dict[str, Any]],
    session_meta: dict[str, Any],
    progress_callback: Any | None = None,
) -> ReviewPreparation:
    """Resolve the plan and the frozen evidence for one review.

    No prompt is rendered here: the review path consumes ``plan`` and
    ``evidence`` only, and every reviewer builds its own task from the profile
    template plus the frozen evidence.
    """
    from nanoreview.review.planning.prefetch import maybe_prefetch_review_context

    user_content = latest_user_text(initial_messages)
    pull_request = pull_request_from_metadata(session_meta)
    plan = build_review_plan(
        target=session_meta.get(ReviewMetaKey.TARGET)
        if isinstance(session_meta.get(ReviewMetaKey.TARGET), str)
        else None,
        user_content=user_content,
        focus=session_meta.get(ReviewMetaKey.REQUESTED_DIMENSIONS),
        max_subagents=session_meta.get(ReviewMetaKey.MAX_CONCURRENT_SUBAGENTS) or 4,
        target_type=(
            session_meta.get(ReviewMetaKey.TARGET_TYPE)
            if isinstance(session_meta.get(ReviewMetaKey.TARGET_TYPE), str)
            else None
        ),
        action=(
            session_meta.get(ReviewMetaKey.ACTION)
            if isinstance(session_meta.get(ReviewMetaKey.ACTION), str)
            else None
        ),
        pull_request=pull_request,
    )
    if plan is None:
        return ReviewPreparation(None)
    session_meta[ReviewMetaKey.ALLOWED_DIMENSIONS] = [role.name for role in plan.roles]
    prefetch_result = await maybe_prefetch_review_context(
        plan,
        session_meta,
        progress_callback=progress_callback,
    )
    return ReviewPreparation(plan=plan, evidence=prefetch_result.evidence)


def apply_review_metadata_from_message(
    session: Session,
    metadata: dict[str, Any] | None,
) -> bool:
    """Apply structured pull-request review metadata before this turn runs.

    A review turn is admitted by :class:`ReviewAdmissionService`, which owns the
    snapshot and the persisted target; a message carrying partial metadata can
    therefore only refresh the reviewer selection, never create a target.
    """
    if not isinstance(metadata, dict):
        return False
    changed = False
    raw_focus = metadata.get(ReviewMetaKey.REQUESTED_DIMENSIONS)
    if isinstance(raw_focus, (str, list)):
        if session.metadata.get(ReviewMetaKey.REQUESTED_DIMENSIONS) != raw_focus:
            session.metadata[ReviewMetaKey.REQUESTED_DIMENSIONS] = raw_focus
            changed = True
    raw_action = metadata.get(ReviewMetaKey.ACTION)
    if isinstance(raw_action, str):
        try:
            action = normalize_review_action(raw_action).value
        except ValueError:
            return changed
        if session.metadata.get(ReviewMetaKey.ACTION) != action:
            session.metadata[ReviewMetaKey.ACTION] = action
            changed = True
    return changed


__all__ = [
    "ReviewPreparation",
    "apply_review_metadata_from_message",
    "build_review_plan",
    "latest_user_text",
    "prepare_code_review_context",
    "pull_request_from_metadata",
]
