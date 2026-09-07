"""Build structured code review plans from metadata and user text."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.agent.context import ContextBuilder
from nanoreview.review.input import (
    apply_policy_to_roles,
    extract_review_target,
    normalize_mode,
    normalize_requested_dimensions,
    normalize_review_action,
    normalize_review_target_type,
    parse_repo_target,
    policy_for_depth,
)
from nanoreview.review.source.utils import (
    parse_github_scoped_target,
    parse_pr_target,
)
from nanoreview.review.types import (
    LocalReviewScope,
    ReviewAction,
    ReviewEvidenceBundle,
    ReviewMetaKey,
    ReviewPlan,
    ReviewTargetType,
)
from nanoreview.session.manager import Session

_LEGACY_REVIEW_SCOPE_KEY = "review_" "target_" "paths"


@dataclass(frozen=True, slots=True)
class ReviewPreparation:
    """Program-owned review inputs prepared before the coordinator runs."""

    plan: ReviewPlan | None
    prompt: str
    evidence: ReviewEvidenceBundle | None = None


def _find_git_root(path: Path) -> Path | None:
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _relative_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _resolve_local_scope(target: str | None) -> tuple[LocalReviewScope | None, str]:
    if not target:
        return None, "missing_target"
    try:
        resolved_target = Path(target).expanduser().resolve()
    except OSError as exc:
        return None, f"target_resolve_failed:{exc}"
    if not resolved_target.exists():
        return None, "target_not_found"

    if resolved_target.is_file():
        git_root = _find_git_root(resolved_target)
        review_root = git_root or resolved_target.parent
        inferred_paths = [_relative_posix(resolved_target, review_root)]
        return (
            LocalReviewScope(
                kind="file",
                review_root=str(review_root),
                scope_paths=inferred_paths,
                target_path=_relative_posix(resolved_target, review_root),
                reason="file_target",
            ),
            "file_target",
        )

    if resolved_target.is_dir():
        review_root = resolved_target
        return (
            LocalReviewScope(
                kind="directory",
                review_root=str(review_root),
                scope_paths=[],
                target_path=".",
                reason="directory_target",
            ),
            "directory_target",
        )
    return None, "target_not_file_or_directory"


def _resolve_action(
    *,
    requested: ReviewAction,
    target: str | None,
    target_type: ReviewTargetType,
    user_content: str,
) -> ReviewAction:
    pr_repo, _ = parse_pr_target(target)
    if pr_repo and requested == ReviewAction.REPO:
        return ReviewAction.DIFF
    return requested


def build_review_plan(
    *,
    target: str | None = None,
    user_content: str = "",
    focus: str | list[str] | None = None,
    depth: Any = "full",
    max_subagents: Any = 4,
    target_type: str | None = None,
    action: str | None = None,
    target_ref: str | None = None,
    prefetch_summary: str | None = None,
) -> ReviewPlan | None:
    trace_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()
    logger.info("review.plan.start trace_id={} target={} action={}", trace_id, target, action or "auto")
    target_name = target
    if not target:
        extracted = extract_review_target(user_content)
        if extracted:
            target, target_name = extracted

    if not target:
        logger.info(
            "review.plan.done trace_id={} status=fallback elapsed_ms={:.1f}",
            trace_id,
            (time.perf_counter() - started) * 1000,
        )
        return None

    roles, routing_mode = normalize_requested_dimensions(focus)
    normalized_type = normalize_review_target_type(target_type, target)
    if normalized_type not in {"github", "local"}:
        normalized_type = normalize_review_target_type(None, target) or "auto"
    target_type_value: ReviewTargetType = normalized_type  # type: ignore[assignment]
    requested_action = normalize_review_action(action)
    resolved_action = _resolve_action(
        requested=requested_action,
        target=target,
        target_type=target_type_value,
        user_content=user_content,
    )
    target_repo, pr_number = parse_pr_target(target)
    normalized_ref: str | None = target_ref.strip() if isinstance(target_ref, str) and target_ref.strip() else None
    target_subpath: str | None = None
    target_subpath_kind: str | None = None
    scoped_target = parse_github_scoped_target(target)
    if scoped_target is not None and target_type_value == "github":
        target_repo = scoped_target.repo
        normalized_ref = scoped_target.ref
        target_subpath = scoped_target.path
        target_subpath_kind = scoped_target.kind
    if target_repo is None and target_type_value == "github":
        target_repo = parse_repo_target(target)

    local_scope: LocalReviewScope | None = None
    scope_reason = ""
    if target_type_value == "local":
        local_scope, scope_reason = _resolve_local_scope(target)

    depth_value = normalize_mode(depth)
    policy = policy_for_depth(depth_value)
    roles = apply_policy_to_roles(
        roles=roles,
        routing_mode=routing_mode,
        policy=policy,
    )

    plan = ReviewPlan(
        target=target,
        target_name=target_name or target,
        target_type=target_type_value,
        action=resolved_action,
        depth=depth_value,
        roles=roles,
        routing_mode=routing_mode,
        user_requirements=user_content.strip(),
        target_repo=target_repo,
        pr_number=pr_number,
        target_ref=normalized_ref,
        target_subpath=target_subpath,
        target_subpath_kind=target_subpath_kind,
        local_scope=local_scope,
        prefetch_summary=prefetch_summary,
    )
    logger.info(
        "review.plan.done trace_id={} action={} target_type={} scope_kind={} scope_reason={} review_root={} target_subpath={} routing_mode={} requested_dimensions={} roles={} allowed_dimensions={} user_requirements={} elapsed_ms={:.1f}",
        trace_id,
        plan.action.value,
        plan.target_type,
        plan.local_scope.kind if plan.local_scope else "",
        scope_reason,
        plan.local_scope.review_root if plan.local_scope else "",
        plan.target_subpath or "",
        plan.routing_mode == "explicit",
        focus,
        [role.name for role in plan.roles],
        [role.name for role in plan.roles],
        plan.user_requirements,
        (time.perf_counter() - started) * 1000,
    )
    return plan


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


async def resolve_code_review_context(
    initial_messages: list[dict[str, Any]],
    session_meta: dict[str, Any],
    progress_callback: Any | None = None,
) -> str:
    """Build the Review-mode system prompt from metadata or the user prompt."""
    preparation = await prepare_code_review_context(
        initial_messages,
        session_meta,
        progress_callback=progress_callback,
    )
    return preparation.prompt


async def prepare_code_review_context(
    initial_messages: list[dict[str, Any]],
    session_meta: dict[str, Any],
    progress_callback: Any | None = None,
) -> ReviewPreparation:
    """Resolve policy, evidence, and the coordinator-only prompt for one review."""
    from nanoreview.review.planning.prefetch import maybe_prefetch_review_context
    from nanoreview.review.planning.prompt import (
        build_review_fallback_prompt,
        render_review_coordinator_prompt,
    )

    user_content = latest_user_text(initial_messages)
    plan = build_review_plan(
        target=session_meta.get(ReviewMetaKey.TARGET) if isinstance(session_meta.get(ReviewMetaKey.TARGET), str) else None,
        user_content=user_content,
        focus=session_meta.get(ReviewMetaKey.REQUESTED_DIMENSIONS),
        depth=session_meta.get(ReviewMetaKey.MODE_VARIANT) or session_meta.get("review_mode_name") or "full",
        max_subagents=session_meta.get(ReviewMetaKey.MAX_CONCURRENT_SUBAGENTS) or 4,
        target_type=session_meta.get(ReviewMetaKey.TARGET_TYPE) if isinstance(session_meta.get(ReviewMetaKey.TARGET_TYPE), str) else None,
        action=session_meta.get(ReviewMetaKey.ACTION) if isinstance(session_meta.get(ReviewMetaKey.ACTION), str) else None,
        target_ref=session_meta.get(ReviewMetaKey.TARGET_REF) if isinstance(session_meta.get(ReviewMetaKey.TARGET_REF), str) else None,
    )
    if plan is None:
        return ReviewPreparation(None, build_review_fallback_prompt())
    session_meta.pop(_LEGACY_REVIEW_SCOPE_KEY, None)
    if plan.local_scope:
        session_meta[ReviewMetaKey.LOCAL_ROOT] = plan.local_scope.review_root
        local_root = Path(plan.local_scope.review_root)
        local_target = (
            local_root / plan.local_scope.target_path
            if plan.local_scope.target_path and plan.local_scope.target_path != "."
            else local_root
        )
        session_meta[ReviewMetaKey.LOCAL_TARGET] = str(local_target.resolve())
        session_meta[ReviewMetaKey.LOCAL_SCOPE_KIND] = plan.local_scope.kind
    else:
        session_meta.pop(ReviewMetaKey.LOCAL_ROOT, None)
        session_meta.pop(ReviewMetaKey.LOCAL_TARGET, None)
        session_meta.pop(ReviewMetaKey.LOCAL_SCOPE_KIND, None)
    prefetch_summary = await maybe_prefetch_review_context(
        plan,
        session_meta,
        progress_callback=progress_callback,
    )
    if prefetch_summary.summary:
        plan = replace(plan, prefetch_summary=prefetch_summary.summary)
        if plan.target_type == "github":
            session_meta[ReviewMetaKey.GITHUB_PREFETCH_READY] = True
    elif prefetch_summary.attempted:
        target_label = "GitHub" if plan.target_type == "github" else "repository"
        if plan.target_type == "github":
            session_meta[ReviewMetaKey.GITHUB_PREFETCH_READY] = True
        detail = f": {prefetch_summary.reason}" if prefetch_summary.reason else ""
        plan = replace(
            plan,
            prefetch_summary=(
                f"{target_label} evidence prefetch was already attempted for this review "
                f"and returned {prefetch_summary.status}{detail}. Do not call "
                f"{'github_review' if plan.target_type == 'github' else 'local_review'} again "
                "for the same target in this turn; continue with the available context and "
                "state any evidence limitations in the review."
            ),
        )
    session_meta[ReviewMetaKey.ALLOWED_DIMENSIONS] = [role.name for role in plan.roles]
    evidence = prefetch_summary.evidence
    return ReviewPreparation(
        plan=plan,
        prompt=render_review_coordinator_prompt(plan, evidence),
        evidence=evidence,
    )


def build_code_review_context(
    *,
    target: str | None = None,
    user_content: str = "",
    focus: str | None = None,
    mode: str = "full",
    max_subagents: int = 4,
    target_type: str | None = None,
    action: str | None = None,
    target_ref: str | None = None,
) -> str:
    from nanoreview.review.planning.prompt import build_review_fallback_prompt, render_review_prompt

    plan = build_review_plan(
        target=target,
        user_content=user_content,
        focus=focus,
        depth=mode,
        max_subagents=max_subagents,
        target_type=target_type,
        action=action,
        target_ref=target_ref,
    )
    if plan is None:
        return build_review_fallback_prompt()
    return render_review_prompt(plan)


def apply_review_metadata_from_message(
    session: Session,
    metadata: dict[str, Any] | None,
) -> bool:
    """Apply structured Review metadata before this turn runs."""
    if not isinstance(metadata, dict):
        return False
    keys = (
        ReviewMetaKey.TARGET,
        ReviewMetaKey.TARGET_TYPE,
        ReviewMetaKey.MODE_VARIANT,
        ReviewMetaKey.ACTION,
        ReviewMetaKey.REQUESTED_DIMENSIONS,
        ReviewMetaKey.TARGET_REF,
    )
    if not any(key in metadata for key in keys):
        return False

    changed = False

    def _set_meta(key: str, value: Any) -> None:
        nonlocal changed
        if session.metadata.get(key) != value:
            session.metadata[key] = value
            changed = True

    def _pop_meta(key: str) -> None:
        nonlocal changed
        if key in session.metadata:
            session.metadata.pop(key, None)
            changed = True

    _set_meta(ReviewMetaKey.MODE, True)

    raw_mode = metadata.get(ReviewMetaKey.MODE_VARIANT)
    if isinstance(raw_mode, str):
        mode = raw_mode.strip().lower()
        if mode in {"quick", "full", "deep"}:
            _set_meta(ReviewMetaKey.MODE_VARIANT, mode)
        else:
            _pop_meta(ReviewMetaKey.MODE_VARIANT)

    raw_target = metadata.get(ReviewMetaKey.TARGET)
    if isinstance(raw_target, str):
        target = raw_target.strip()
        if target:
            _set_meta(ReviewMetaKey.TARGET, target)
        else:
            _pop_meta(ReviewMetaKey.TARGET)

    raw_action = metadata.get(ReviewMetaKey.ACTION)
    if isinstance(raw_action, str):
        try:
            _set_meta(ReviewMetaKey.ACTION, normalize_review_action(raw_action).value)
        except ValueError:
            _pop_meta(ReviewMetaKey.ACTION)

    raw_focus = metadata.get(ReviewMetaKey.REQUESTED_DIMENSIONS)
    if isinstance(raw_focus, (str, list)):
        _set_meta(ReviewMetaKey.REQUESTED_DIMENSIONS, raw_focus)

    _pop_meta(_LEGACY_REVIEW_SCOPE_KEY)

    raw_ref = metadata.get(ReviewMetaKey.TARGET_REF)
    if isinstance(raw_ref, str) and raw_ref.strip():
        _set_meta(ReviewMetaKey.TARGET_REF, raw_ref.strip())
    elif ReviewMetaKey.TARGET_REF in metadata:
        _pop_meta(ReviewMetaKey.TARGET_REF)

    target_type = normalize_review_target_type(
        metadata.get(ReviewMetaKey.TARGET_TYPE) if isinstance(metadata.get(ReviewMetaKey.TARGET_TYPE), str) else None,
        session.metadata.get(ReviewMetaKey.TARGET),
    )
    if target_type:
        _set_meta(ReviewMetaKey.TARGET_TYPE, target_type)
    else:
        _pop_meta(ReviewMetaKey.TARGET_TYPE)

    return changed
