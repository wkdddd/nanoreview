"""Build structured code review plans from metadata and user text."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoreview.agent.context import ContextBuilder
from nanoreview.review.input import (
    extract_review_target,
    normalize_requested_dimensions,
    normalize_review_action,
    normalize_review_target_type,
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

if TYPE_CHECKING:
    from nanoreview.review.planning.manifest import EvidenceManifest


@dataclass(frozen=True, slots=True)
class ReviewPreparation:
    """Program-owned review inputs prepared before the coordinator runs."""

    plan: ReviewPlan | None
    prompt: str
    evidence: ReviewEvidenceBundle | None = None
    #: The single budgeted planner manifest the prompt was rendered from.
    manifest: "EvidenceManifest | None" = None


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
    return requested


def build_review_plan(
    *,
    target: str | None = None,
    user_content: str = "",
    focus: str | list[str] | None = None,
    max_subagents: Any = 4,
    target_type: str | None = None,
    action: str | None = None,
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

    roles, mode = normalize_requested_dimensions(focus)
    normalized_type = normalize_review_target_type(target_type, target)
    if normalized_type not in {"local", "auto"}:
        normalized_type = normalize_review_target_type(None, target) or "auto"
    target_type_value: ReviewTargetType = normalized_type  # type: ignore[assignment]
    requested_action = normalize_review_action(action)
    resolved_action = _resolve_action(
        requested=requested_action,
        target=target,
        target_type=target_type_value,
        user_content=user_content,
    )

    local_scope: LocalReviewScope | None = None
    scope_reason = ""
    if target_type_value == "local":
        local_scope, scope_reason = _resolve_local_scope(target)

    plan = ReviewPlan(
        target=target,
        target_name=target_name or target,
        target_type=target_type_value,
        action=resolved_action,
        roles=roles,
        mode=mode,
        user_requirements=user_content.strip(),
        local_scope=local_scope,
        prefetch_summary=prefetch_summary,
    )
    logger.info(
        "review.plan.done trace_id={} action={} target_type={} scope_kind={} scope_reason={} review_root={} scope_paths={} mode={} requested_dimensions={} roles={} allowed_dimensions={} user_requirements={} elapsed_ms={:.1f}",
        trace_id,
        plan.action.value,
        plan.target_type,
        plan.local_scope.kind if plan.local_scope else "",
        scope_reason,
        plan.local_scope.review_root if plan.local_scope else "",
        len(plan.local_scope.scope_paths) if plan.local_scope else 0,
        plan.mode,
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
    from nanoreview.review.planning.manifest import build_evidence_manifest
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
        max_subagents=session_meta.get(ReviewMetaKey.MAX_CONCURRENT_SUBAGENTS) or 4,
        target_type=session_meta.get(ReviewMetaKey.TARGET_TYPE) if isinstance(session_meta.get(ReviewMetaKey.TARGET_TYPE), str) else None,
        action=session_meta.get(ReviewMetaKey.ACTION) if isinstance(session_meta.get(ReviewMetaKey.ACTION), str) else None,
    )
    if plan is None:
        return ReviewPreparation(None, build_review_fallback_prompt())
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
    elif prefetch_summary.attempted:
        detail = f": {prefetch_summary.reason}" if prefetch_summary.reason else ""
        plan = replace(
            plan,
            prefetch_summary=(
                "Repository evidence prefetch was already attempted for this review "
                f"and returned {prefetch_summary.status}{detail}. Do not repeat broad "
                "evidence retrieval for the same target in this turn; continue with the "
                "available context and state any evidence limitations in the review."
            ),
        )
    session_meta[ReviewMetaKey.ALLOWED_DIMENSIONS] = [role.name for role in plan.roles]
    evidence = prefetch_summary.evidence
    # The planner sees exactly one structured manifest, built from the same
    # references the reviewer assignments use and budgeted for the review window.
    manifest = build_evidence_manifest(evidence)
    return ReviewPreparation(
        plan=plan,
        prompt=render_review_coordinator_prompt(plan, evidence, manifest=manifest),
        evidence=evidence,
        manifest=manifest,
    )


def build_code_review_context(
    *,
    target: str | None = None,
    user_content: str = "",
    focus: str | None = None,
    max_subagents: int = 4,
    target_type: str | None = None,
    action: str | None = None,
) -> str:
    from nanoreview.review.planning.prompt import build_review_fallback_prompt, render_review_prompt

    plan = build_review_plan(
        target=target,
        user_content=user_content,
        focus=focus,
        max_subagents=max_subagents,
        target_type=target_type,
        action=action,
    )
    if plan is None:
        return build_review_fallback_prompt()
    return render_review_prompt(plan)


def apply_review_metadata_from_message(
    session: Session,
    metadata: dict[str, Any] | None,
) -> bool:
    """Apply structured Review metadata before this turn runs.

    Review activation is decided solely by the presence of a valid
    ``review_target``; the legacy toggle/depth variant has been removed.
    """
    if not isinstance(metadata, dict):
        return False
    keys = (
        ReviewMetaKey.TARGET,
        ReviewMetaKey.TARGET_TYPE,
        ReviewMetaKey.ACTION,
        ReviewMetaKey.REQUESTED_DIMENSIONS,
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

    target_type = normalize_review_target_type(
        metadata.get(ReviewMetaKey.TARGET_TYPE) if isinstance(metadata.get(ReviewMetaKey.TARGET_TYPE), str) else None,
        session.metadata.get(ReviewMetaKey.TARGET),
    )
    if target_type:
        _set_meta(ReviewMetaKey.TARGET_TYPE, target_type)
    else:
        _pop_meta(ReviewMetaKey.TARGET_TYPE)

    return changed
