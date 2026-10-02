"""Shared review admission boundary for every review entry point.

CLI, the structured API, and WebUI submissions all funnel through
:class:`ReviewAdmissionService` so that a review is validated, snapshotted,
and registered exactly once, before any transport delivers an execution task.
Transports keep only protocol parsing, delivery, and status reads.

Admission is *all-or-nothing*: a rejected request creates no session, no run,
no snapshot, and no history. A rejected submitter gets a stable
:class:`ReviewAdmissionError` code it can render and correct against.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from loguru import logger

from nanoreview.agent.review_state import (
    REVIEW_TERMINAL_STATUSES,
    ReviewPhase,
    ReviewRunState,
    ReviewRunStatus,
    compute_input_fingerprint,
    new_review_run_id,
)
from nanoreview.review.input.local_git import (
    GitUnavailableError,
    NetDiff,
    collect_net_diff,
    find_git_root,
)
from nanoreview.review.input.normalizers import (
    normalize_requested_dimensions,
    normalize_review_action,
    normalize_review_target_type,
)
from nanoreview.review.input.snapshot import (
    ReviewSnapshotError,
    ReviewSnapshotStore,
    build_snapshot,
    collect_repo_content,
)
from nanoreview.review.input.targets import parse_repo_target
from nanoreview.review.source.utils import clean_scope_paths
from nanoreview.review.types import (
    LocalReviewScope,
    ReviewAction,
    ReviewMetaKey,
    ReviewPlan,
)


class ReviewAdmissionCode(StrEnum):
    """Stable, renderable rejection codes for review admission."""

    INVALID_ACTION = "invalid_action"
    INVALID_TARGET_TYPE = "invalid_target_type"
    INVALID_DIMENSIONS = "invalid_dimensions"
    TARGET_REQUIRED = "target_required"
    RELATIVE_PATH_NOT_ALLOWED = "relative_path_not_allowed"
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_NOT_FILE_OR_DIRECTORY = "target_not_file_or_directory"
    SCOPE_OUTSIDE_TARGET = "scope_outside_target"
    SCOPE_PATH_NOT_FOUND = "scope_path_not_found"
    NOT_A_GIT_REPO = "not_a_git_repo"
    EMPTY_DIFF = "empty_diff"
    SCOPE_NO_CHANGES = "scope_no_changes"
    DUPLICATE_REVIEW = "duplicate_review"
    SNAPSHOT_FAILED = "snapshot_failed"


#: HTTP status that best expresses each rejection code to structured clients.
_CODE_STATUS: dict[ReviewAdmissionCode, int] = {
    ReviewAdmissionCode.TARGET_NOT_FOUND: 404,
    ReviewAdmissionCode.SCOPE_PATH_NOT_FOUND: 404,
    ReviewAdmissionCode.DUPLICATE_REVIEW: 409,
    ReviewAdmissionCode.SNAPSHOT_FAILED: 500,
}


class ReviewAdmissionError(RuntimeError):
    """A rejected review request carrying a stable code and wire status."""

    def __init__(
        self,
        code: ReviewAdmissionCode,
        message: str,
        *,
        field: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.status = status if status is not None else _CODE_STATUS.get(code, 400)

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": "review_not_admitted",
            "code": self.code.value,
            "message": self.message,
        }
        if self.field:
            payload["field"] = self.field
        return payload

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


@dataclass(frozen=True, slots=True)
class ReviewAdmissionRequest:
    """Transport-independent review request presented to admission."""

    target: str | None = None
    target_type: str | None = None
    action: str | None = None
    scope: list[str] | None = None
    focus: str | list[str] | None = None
    content: str = ""
    session_key: str | None = None
    cwd: str | None = None
    max_concurrent_subagents: int | None = None
    #: WebUI/API submissions must pass server-local absolute paths.
    enforce_absolute: bool = False


@dataclass(frozen=True, slots=True)
class ReviewAdmission:
    """Accepted review request, ready to be registered and executed."""

    session_key: str
    run_id: str
    input_fingerprint: str
    action: ReviewAction
    target_type: str
    target: str
    content: str
    plan: ReviewPlan | None
    scope: LocalReviewScope | None
    snapshot_ref: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _is_absolute_target(value: str) -> bool:
    return Path(value).expanduser().is_absolute()


class ReviewAdmissionService:
    """Validate, snapshot, and register one local review run."""

    def __init__(
        self,
        *,
        sessions: Any,
        workspace: Path,
        store: ReviewSnapshotStore | None = None,
    ) -> None:
        self._sessions = sessions
        self._workspace = Path(workspace)
        self._store = store or ReviewSnapshotStore(self._workspace)

    # -- public API ---------------------------------------------------------

    def admit(self, request: ReviewAdmissionRequest) -> ReviewAdmission:
        """Validate *request* and return an accepted admission, or raise."""
        try:
            action = normalize_review_action(request.action)
        except ValueError as exc:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.INVALID_ACTION, str(exc), field="action"
            ) from exc
        try:
            roles, routing_mode = normalize_requested_dimensions(request.focus)
        except ValueError as exc:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.INVALID_DIMENSIONS, str(exc), field="focus"
            ) from exc

        target = (request.target or "").strip()
        if not target:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.TARGET_REQUIRED,
                "A review target is required.",
                field="target",
            )
        if request.target_type and request.target_type.strip().lower() not in {
            "auto", "local", "github", "",
        }:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.INVALID_TARGET_TYPE,
                f"Unknown target type '{request.target_type}'.",
                field="target_type",
            )
        target_type = normalize_review_target_type(request.target_type, target) or "local"
        session_key = request.session_key or f"review:{uuid.uuid4().hex[:12]}"

        self._reject_duplicate(session_key)

        if target_type == "github":
            # Remote targets are out of scope for this round; admission only
            # records the request so the existing planning path is unchanged.
            return self._persist(
                self._admit_remote(
                    request=request,
                    session_key=session_key,
                    action=action,
                    roles=roles,
                    routing_mode=routing_mode,
                    target=target,
                )
            )

        return self._persist(
            self._admit_local(
                request=request,
                session_key=session_key,
                action=action,
                roles=roles,
                routing_mode=routing_mode,
                target=target,
            )
        )

    # -- persistence --------------------------------------------------------

    def _persist(self, admission: ReviewAdmission) -> ReviewAdmission:
        """Persist the accepted run's navigation metadata on its session.

        Admission owns this write: the run is created, the snapshot stored,
        and the session metadata saved inside one boundary, so a crash between
        delivery and registration cannot leave a session pointing at a run
        that was never recorded.
        """
        session = self._sessions.get_or_create(admission.session_key)
        session.metadata.update(admission.metadata)
        self._sessions.save(session)
        return admission

    # -- rejection helpers --------------------------------------------------

    def _reject_duplicate(self, session_key: str) -> None:
        """Refuse a second review for a session that already owns one."""
        session = self._sessions.get_or_create(session_key)
        existing_run = session.metadata.get(ReviewMetaKey.RUN_ID)
        if not existing_run:
            return
        status = str(session.metadata.get(ReviewMetaKey.STATUS) or "running")
        raise ReviewAdmissionError(
            ReviewAdmissionCode.DUPLICATE_REVIEW,
            (
                f"Session '{session_key}' already owns review run '{existing_run}' "
                f"with status '{status}'. Start a new review session instead."
            ),
        )

    # -- admission paths ----------------------------------------------------

    def _admit_remote(
        self,
        *,
        request: ReviewAdmissionRequest,
        session_key: str,
        action: ReviewAction,
        roles: list[Any],
        routing_mode: str,
        target: str,
    ) -> ReviewAdmission:
        repo = parse_repo_target(target)
        plan = ReviewPlan(
            target=target,
            target_name=target,
            target_type="github",
            action=action,
            roles=roles,
            routing_mode=routing_mode,  # type: ignore[arg-type]
            user_requirements=(request.content or "").strip(),
            target_repo=repo,
        )
        run_id = new_review_run_id()
        fingerprint = compute_input_fingerprint(
            target=target,
            target_type="github",
            action=action.value,
            roles=[role.name for role in roles],
            scope={"repo": repo, "session": session_key},
        )
        snapshot_ref = self._write_snapshot(
            build_snapshot(
                run_id=run_id,
                session_key=session_key,
                action=action.value,
                target_type="github",
                target=target,
                input_fingerprint=fingerprint,
                extra_metadata={"repo": repo},
            )
        )
        return ReviewAdmission(
            session_key=session_key,
            run_id=run_id,
            input_fingerprint=fingerprint,
            action=action,
            target_type="github",
            target=target,
            content=request.content.strip() or f"Review {target}",
            plan=plan,
            scope=None,
            snapshot_ref=snapshot_ref,
            metadata=self._metadata_payload(
                plan=plan,
                run_id=run_id,
                fingerprint=fingerprint,
                snapshot_ref=snapshot_ref,
                focus=request.focus,
                max_concurrent_subagents=request.max_concurrent_subagents,
            ),
        )

    def _admit_local(
        self,
        *,
        request: ReviewAdmissionRequest,
        session_key: str,
        action: ReviewAction,
        roles: list[Any],
        routing_mode: str,
        target: str,
    ) -> ReviewAdmission:
        resolved_target = self._resolve_local_target(request, target)
        scope = self._resolve_local_scope(request, resolved_target)
        review_root = Path(scope.review_root)

        net_diff: NetDiff | None = None
        repo_content = None
        if action is ReviewAction.DIFF:
            net_diff = self._collect_diff(review_root, scope)
        else:
            repo_content = collect_repo_content(
                review_root,
                scope_paths=scope.scope_paths or ([scope.target_path] if scope.target_path not in (None, ".") else []),
            )

        plan = ReviewPlan(
            target=str(resolved_target),
            target_name=target,
            target_type="local",
            action=action,
            roles=roles,
            routing_mode=routing_mode,  # type: ignore[arg-type]
            user_requirements=(request.content or "").strip(),
            local_scope=scope,
        )
        run_id = new_review_run_id()
        fingerprint = compute_input_fingerprint(
            target=str(resolved_target),
            target_type="local",
            action=action.value,
            roles=[role.name for role in roles],
            scope={
                "kind": scope.kind,
                "review_root": scope.review_root,
                "scope_paths": sorted(scope.scope_paths),
                "target_path": scope.target_path,
            },
            evidence_manifest=(
                [{"path": path} for path in sorted(net_diff.patches)] if net_diff else []
            ),
            extra_metadata={"session": session_key, "git_head": net_diff.head_sha if net_diff else None},
        )
        snapshot_ref = self._write_snapshot(
            build_snapshot(
                run_id=run_id,
                session_key=session_key,
                action=action.value,
                target_type="local",
                target=str(resolved_target),
                input_fingerprint=fingerprint,
                local_scope={
                    "kind": scope.kind,
                    "review_root": scope.review_root,
                    "scope_paths": list(scope.scope_paths),
                    "target_path": scope.target_path,
                },
                git_head=net_diff.head_sha if net_diff else None,
                net_diff=net_diff.patches if net_diff else None,
                changed_files=net_diff.changed_files if net_diff else None,
                scope_files=sorted(repo_content.files) if repo_content else None,
                repo_content=repo_content,
                extra_metadata=(
                    {
                        "git_skipped": net_diff.skipped,
                        "outside_scope_files": net_diff.outside_scope_files,
                    }
                    if net_diff
                    else {}
                ),
            )
        )
        logger.info(
            "review.admission.accepted session={} run_id={} action={} scope_kind={} scope_paths={}",
            session_key,
            run_id,
            action.value,
            scope.kind,
            len(scope.scope_paths),
        )
        return ReviewAdmission(
            session_key=session_key,
            run_id=run_id,
            input_fingerprint=fingerprint,
            action=action,
            target_type="local",
            target=str(resolved_target),
            content=request.content.strip() or f"Review {resolved_target}",
            plan=plan,
            scope=scope,
            snapshot_ref=snapshot_ref,
            metadata=self._metadata_payload(
                plan=plan,
                run_id=run_id,
                fingerprint=fingerprint,
                snapshot_ref=snapshot_ref,
                focus=request.focus,
                max_concurrent_subagents=request.max_concurrent_subagents,
            ),
        )

    # -- validation ---------------------------------------------------------

    def _resolve_local_target(
        self, request: ReviewAdmissionRequest, target: str
    ) -> Path:
        if request.enforce_absolute and not _is_absolute_target(target):
            raise ReviewAdmissionError(
                ReviewAdmissionCode.RELATIVE_PATH_NOT_ALLOWED,
                f"Review target must be an absolute local path: '{target}'.",
                field="target",
            )
        base = Path(request.cwd).expanduser() if request.cwd else Path.cwd()
        candidate = Path(target).expanduser()
        resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
        if not resolved.exists():
            raise ReviewAdmissionError(
                ReviewAdmissionCode.TARGET_NOT_FOUND,
                f"Review target does not exist: {resolved}",
                field="target",
            )
        if not (resolved.is_file() or resolved.is_dir()):
            raise ReviewAdmissionError(
                ReviewAdmissionCode.TARGET_NOT_FILE_OR_DIRECTORY,
                f"Review target is neither a file nor a directory: {resolved}",
                field="target",
            )
        return resolved

    def _resolve_local_scope(
        self, request: ReviewAdmissionRequest, resolved_target: Path
    ) -> LocalReviewScope:
        """Resolve the review root and scope paths for a local target."""
        base = Path(request.cwd).expanduser() if request.cwd else Path.cwd()
        explicit = clean_scope_paths(request.scope)

        if resolved_target.is_file():
            git_root = find_git_root(resolved_target)
            review_root = git_root or resolved_target.parent
            target_rel = resolved_target.relative_to(review_root).as_posix()
            scope_paths = self._validate_scope_paths(
                explicit, base=base, review_root=review_root, fallback=[target_rel]
            )
            return LocalReviewScope(
                kind="file",
                review_root=str(review_root),
                scope_paths=scope_paths,
                target_path=target_rel,
                reason="file_target",
            )

        review_root = resolved_target
        scope_paths = self._validate_scope_paths(
            explicit, base=base, review_root=review_root, fallback=[]
        )
        kind = "directory" if not scope_paths else "directory"
        return LocalReviewScope(
            kind=kind,  # type: ignore[arg-type]
            review_root=str(review_root),
            scope_paths=scope_paths,
            target_path=".",
            reason="directory_target",
        )

    def _validate_scope_paths(
        self,
        scope: list[str],
        *,
        base: Path,
        review_root: Path,
        fallback: list[str],
    ) -> list[str]:
        if not scope:
            return list(fallback)
        root = review_root.resolve()
        validated: list[str] = []
        for raw in scope:
            candidate = Path(raw).expanduser()
            absolute = (candidate if candidate.is_absolute() else base / candidate).resolve()
            if not absolute.exists():
                raise ReviewAdmissionError(
                    ReviewAdmissionCode.SCOPE_PATH_NOT_FOUND,
                    f"Review scope path does not exist: {absolute}",
                    field="scope",
                )
            try:
                rel = absolute.relative_to(root).as_posix()
            except ValueError as exc:
                raise ReviewAdmissionError(
                    ReviewAdmissionCode.SCOPE_OUTSIDE_TARGET,
                    f"Review scope path is outside the review target: {absolute}",
                    field="scope",
                ) from exc
            if rel not in validated:
                validated.append(rel)
        return validated

    def _collect_diff(self, review_root: Path, scope: LocalReviewScope) -> NetDiff:
        try:
            net_diff = collect_net_diff(
                review_root, scope_paths=scope.scope_paths or None
            )
        except GitUnavailableError as exc:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.NOT_A_GIT_REPO,
                (
                    "Diff review requires a Git repository; "
                    f"'{review_root}' is not inside a Git worktree."
                ),
                field="target",
            ) from exc

        if net_diff.is_empty:
            if net_diff.changed_files and scope.scope_paths:
                raise ReviewAdmissionError(
                    ReviewAdmissionCode.SCOPE_NO_CHANGES,
                    (
                        "The selected scope has no changes relative to HEAD. "
                        f"Changed files outside scope: {', '.join(net_diff.outside_scope_files)}"
                    ),
                    field="scope",
                )
            if not net_diff.has_changes:
                raise ReviewAdmissionError(
                    ReviewAdmissionCode.EMPTY_DIFF,
                    "The workspace has no changes relative to HEAD; nothing to review.",
                    field="target",
                )
            raise ReviewAdmissionError(
                ReviewAdmissionCode.SCOPE_NO_CHANGES,
                "The selected scope has no reviewable changes relative to HEAD.",
                field="scope",
            )
        return net_diff

    # -- persistence --------------------------------------------------------

    def _write_snapshot(self, snapshot: dict[str, Any]) -> str:
        try:
            return self._store.write(snapshot)
        except ReviewSnapshotError as exc:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.SNAPSHOT_FAILED,
                f"Cannot capture the review input snapshot: {exc}",
            ) from exc

    @staticmethod
    def _metadata_payload(
        *,
        plan: ReviewPlan,
        run_id: str,
        fingerprint: str,
        snapshot_ref: str,
        focus: str | list[str] | None,
        max_concurrent_subagents: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            ReviewMetaKey.TARGET: plan.target,
            ReviewMetaKey.TARGET_TYPE: plan.target_type,
            ReviewMetaKey.ACTION: plan.action.value,
            ReviewMetaKey.REQUESTED_DIMENSIONS: focus,
            ReviewMetaKey.ALLOWED_DIMENSIONS: [role.name for role in plan.roles],
            ReviewMetaKey.SNAPSHOT_REF: snapshot_ref,
            ReviewMetaKey.RUN_ID: run_id,
            ReviewMetaKey.STATUS: ReviewRunStatus.RUNNING.value,
            ReviewMetaKey.PHASE: ReviewPhase.PREPARE.value,
            ReviewMetaKey.INPUT_FINGERPRINT: fingerprint,
        }
        if max_concurrent_subagents:
            payload[ReviewMetaKey.MAX_CONCURRENT_SUBAGENTS] = max_concurrent_subagents
        if plan.local_scope is not None:
            payload[ReviewMetaKey.LOCAL_ROOT] = plan.local_scope.review_root
            payload[ReviewMetaKey.LOCAL_SCOPE_KIND] = plan.local_scope.kind
            root = Path(plan.local_scope.review_root)
            target_path = plan.local_scope.target_path
            local_target = (
                root / target_path if target_path and target_path != "." else root
            )
            payload[ReviewMetaKey.LOCAL_TARGET] = str(local_target.resolve())
        return payload


def register_review_run(admission: ReviewAdmission) -> ReviewRunState:
    """Create the authoritative in-process run state for *admission*."""
    return ReviewRunState(
        run_id=admission.run_id,
        session_key=admission.session_key,
        input_fingerprint=admission.input_fingerprint,
        plan=admission.plan,
        snapshot_ref=admission.snapshot_ref,
    )


__all__ = [
    "ReviewAdmission",
    "ReviewAdmissionCode",
    "ReviewAdmissionError",
    "ReviewAdmissionRequest",
    "ReviewAdmissionService",
    "REVIEW_TERMINAL_STATUSES",
    "register_review_run",
]
