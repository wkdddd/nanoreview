"""Shared pull-request review admission for every review entry point.

CLI, the structured API and the WebUI all funnel through
:class:`ReviewAdmissionService`, so a review is validated, frozen into a
snapshot and registered exactly once, before any transport delivers an
execution task. Transports keep only protocol parsing, delivery and status
reads.

Admission is *all-or-nothing*:

* a rejected request creates no run, no snapshot and no session metadata;
* the review input is a GitHub pull request pinned to the SHAs captured now,
  so a later push cannot change what is reviewed;
* the run row is written before the snapshot, so a crash between the two
  leaves a ``queued`` run that startup normalizes to ``interrupted`` rather
  than a snapshot no run can reach.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from loguru import logger

from nanoreview.agent.review_state import (
    INTERRUPTED_RUN_REASON,
    REVIEW_TERMINAL_STATUSES,
    ReviewPhase,
    ReviewRunStatus,
    new_review_run_id,
)
from nanoreview.review.input.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRepository,
    GitHubUnavailableError,
    parse_repository_target,
)
from nanoreview.review.input.normalizers import (
    normalize_requested_dimensions,
    normalize_review_action,
)
from nanoreview.review.input.pr_snapshot import (
    PullRequestSnapshotService,
    SnapshotPreparationError,
)
from nanoreview.review.types import (
    PULL_REQUEST_TARGET_TYPE,
    PullRequestTarget,
    ReviewAction,
    ReviewMetaKey,
    ReviewPlan,
)
from nanoreview.storage.records import (
    PullRequestRecord,
    RepositoryRecord,
    ReviewSnapshot,
)


class ReviewAdmissionCode(StrEnum):
    """Stable, renderable rejection codes for review admission."""

    INVALID_ACTION = "invalid_action"
    INVALID_TARGET_TYPE = "invalid_target_type"
    INVALID_DIMENSIONS = "invalid_dimensions"
    INVALID_REPOSITORY = "invalid_repository"
    INVALID_PULL_REQUEST = "invalid_pull_request"
    REPOSITORY_NOT_FOUND = "repository_not_found"
    PULL_REQUEST_NOT_FOUND = "pull_request_not_found"
    GITHUB_AUTH_FAILED = "github_auth_failed"
    GITHUB_RATE_LIMITED = "github_rate_limited"
    GITHUB_UNAVAILABLE = "github_unavailable"
    PULL_REQUEST_EMPTY = "pull_request_empty"
    PULL_REQUEST_CHANGED = "pull_request_changed"
    SNAPSHOT_FAILED = "snapshot_failed"
    DUPLICATE_REVIEW = "duplicate_review"
    DUPLICATE_REQUEST = "duplicate_request"
    PERSISTENCE_FAILED = "persistence_failed"


#: HTTP status that best expresses each rejection code to structured clients.
_CODE_STATUS: dict[ReviewAdmissionCode, int] = {
    ReviewAdmissionCode.INVALID_REPOSITORY: 400,
    ReviewAdmissionCode.INVALID_PULL_REQUEST: 400,
    ReviewAdmissionCode.REPOSITORY_NOT_FOUND: 404,
    ReviewAdmissionCode.PULL_REQUEST_NOT_FOUND: 404,
    ReviewAdmissionCode.PULL_REQUEST_EMPTY: 422,
    ReviewAdmissionCode.GITHUB_AUTH_FAILED: 502,
    ReviewAdmissionCode.GITHUB_RATE_LIMITED: 429,
    ReviewAdmissionCode.GITHUB_UNAVAILABLE: 502,
    ReviewAdmissionCode.DUPLICATE_REVIEW: 409,
    ReviewAdmissionCode.DUPLICATE_REQUEST: 409,
    ReviewAdmissionCode.PULL_REQUEST_CHANGED: 409,
    ReviewAdmissionCode.SNAPSHOT_FAILED: 502,
    ReviewAdmissionCode.PERSISTENCE_FAILED: 500,
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
    """Transport-independent pull-request review request."""

    repository: str | None = None
    pull_request: int | None = None
    action: str | None = None
    focus: str | list[str] | None = None
    content: str = ""
    session_key: str | None = None
    #: Client-supplied idempotency key; it only prevents a duplicate submit.
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewAdmission:
    """Accepted review request, ready to be registered and executed."""

    session_key: str
    run_id: str
    request_id: str | None
    repository_id: int
    pull_request_id: int
    pull_request: PullRequestTarget
    action: ReviewAction
    snapshot: ReviewSnapshot
    plan: ReviewPlan
    input_fingerprint: str
    metadata: dict[str, Any]

    @property
    def target(self) -> str:
        return self.pull_request.label

    @property
    def target_type(self) -> str:
        return PULL_REQUEST_TARGET_TYPE

    @property
    def snapshot_ref(self) -> str:
        return self.snapshot.id


class SessionMetadataWriter(Protocol):
    """Persist a session's review navigation metadata."""

    async def update_metadata(self, session_key: str, payload: dict[str, Any]) -> None: ...

    async def get_metadata(self, session_key: str) -> dict[str, Any] | None: ...


class FileSessionMetadataWriter:
    """Adapter over the file-backed :class:`SessionManager`.

    The review domain never touches a ``Session`` object; it only needs the
    metadata of one session key. Keeping that behind this adapter means the
    migration of session storage changes one class, not every admission call.
    """

    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    async def update_metadata(self, session_key: str, payload: dict[str, Any]) -> None:
        def _write() -> None:
            session = self._sessions.get_or_create(session_key)
            session.metadata.update(payload)
            self._sessions.save(session)

        await asyncio.to_thread(_write)

    async def get_metadata(self, session_key: str) -> dict[str, Any] | None:
        def _read() -> dict[str, Any]:
            session = self._sessions.get_or_create(session_key)
            return dict(session.metadata)

        return await asyncio.to_thread(_read)


def _github_code(exc: GitHubError) -> ReviewAdmissionCode:
    if isinstance(exc, GitHubNotFoundError):
        return ReviewAdmissionCode.REPOSITORY_NOT_FOUND
    if isinstance(exc, GitHubAuthError):
        return ReviewAdmissionCode.GITHUB_AUTH_FAILED
    if isinstance(exc, GitHubRateLimitError):
        return ReviewAdmissionCode.GITHUB_RATE_LIMITED
    if isinstance(exc, GitHubUnavailableError):
        return ReviewAdmissionCode.GITHUB_UNAVAILABLE
    return ReviewAdmissionCode.GITHUB_UNAVAILABLE


class ReviewAdmissionService:
    """Validate, snapshot and register one pull-request review run."""

    def __init__(
        self,
        *,
        github: GitHubClient,
        storage: Any,
        sessions: Any,
        snapshot_service: PullRequestSnapshotService | None = None,
    ) -> None:
        self._github = github
        self._storage = storage
        self._sessions: SessionMetadataWriter = (
            sessions if hasattr(sessions, "update_metadata") else FileSessionMetadataWriter(sessions)
        )
        self._snapshots = snapshot_service or PullRequestSnapshotService(github)

    # -- public API ---------------------------------------------------------

    async def admit(self, request: ReviewAdmissionRequest) -> ReviewAdmission:
        """Validate *request* and return an accepted admission, or raise.

        Every rejection is a :class:`ReviewAdmissionError` with a stable code —
        including storage failures (database down, schema not migrated), so a
        rejecting transport never leaks a raw driver traceback.
        """
        try:
            return await self._admit(request)
        except ReviewAdmissionError:
            raise
        except Exception as exc:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.PERSISTENCE_FAILED,
                f"Review admission failed: {exc}",
            ) from exc

    async def _admit(self, request: ReviewAdmissionRequest) -> ReviewAdmission:
        action = self._resolve_action(request)
        roles = self._resolve_roles(request)
        owner, name, number = self._resolve_target(request)
        session_key = (request.session_key or "").strip() or f"review:{uuid.uuid4().hex[:12]}"
        await self._reject_unavailable(session_key, request.request_id)

        repository_row = await self._resolve_repository(f"{owner}/{name}")
        pull_row, pull_number = await self._resolve_pull_request(repository_row, number)
        run_id = new_review_run_id()
        try:
            await self._storage.runs.create(
                run_id=run_id,
                session_key=session_key,
                repository_id=repository_row.id,
                pull_request_id=pull_row.id,
                request_id=request.request_id,
                status=ReviewRunStatus.RUNNING.value,
                phase=ReviewPhase.PREPARE.value,
            )
        except Exception as exc:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.PERSISTENCE_FAILED,
                f"Cannot register the review run: {exc}",
            ) from exc

        # Everything after the run row exists is one admission transaction in
        # spirit: if any step fails, the run is discarded outright. A run that
        # never started must not stay behind as a zombie ``running`` row — it
        # would block this request_id forever and report a review in progress
        # that does not exist.
        try:
            snapshot = await self._prepare_snapshot(
                run_id=run_id,
                owner=owner,
                name=name,
                number=pull_number,
                repository_id=repository_row.id,
            )
            await self._storage.runs.save_snapshot(snapshot)
            await self._storage.runs.mark_running(
                run_id,
                base_sha=snapshot.base_sha,
                head_sha=snapshot.head_sha,
                merge_base_sha=snapshot.merge_base_sha,
            )

            target = PullRequestTarget(
                repository=repository_row.full_name,
                number=number,
                base_repo=snapshot.base_repo,
                head_repo=snapshot.head_repo,
                base_sha=snapshot.base_sha,
                head_sha=snapshot.head_sha,
                merge_base_sha=snapshot.merge_base_sha,
                snapshot_id=snapshot.id,
                url=str(snapshot.metadata.get("html_url") or "") or None,
            )
            plan = ReviewPlan(
                target=str(target.url or target.label),
                target_name=target.label,
                target_type=PULL_REQUEST_TARGET_TYPE,
                action=action,
                roles=roles,
                user_requirements=(request.content or "").strip(),
                pull_request=target,
            )
            metadata = self._metadata_payload(
                admission_target=target,
                plan=plan,
                repository_id=repository_row.id,
                pull_request_id=pull_row.id,
                run_id=run_id,
                snapshot=snapshot,
                focus=request.focus,
            )
            await self._sessions.update_metadata(session_key, metadata)
        except ReviewAdmissionError:
            await self._discard_run(run_id)
            raise
        except Exception as exc:
            await self._discard_run(run_id)
            raise ReviewAdmissionError(
                ReviewAdmissionCode.PERSISTENCE_FAILED,
                f"Cannot persist the review snapshot: {exc}",
            ) from exc

        logger.info(
            "review.admission.accepted session={} run_id={} pr={} head={} files={} gaps={}",
            session_key,
            run_id,
            target.label,
            snapshot.head_sha[:12],
            len(snapshot.changed_files),
            len(snapshot.gaps),
        )
        return ReviewAdmission(
            session_key=session_key,
            run_id=run_id,
            request_id=request.request_id,
            repository_id=repository_row.id,
            pull_request_id=pull_row.id,
            pull_request=target,
            action=action,
            snapshot=snapshot,
            plan=plan,
            input_fingerprint=snapshot.input_fingerprint,
            metadata=metadata,
        )

    # -- validation ---------------------------------------------------------

    @staticmethod
    def _resolve_action(request: ReviewAdmissionRequest) -> ReviewAction:
        try:
            return normalize_review_action(request.action)
        except ValueError as exc:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.INVALID_ACTION, str(exc), field="action"
            ) from exc

    @staticmethod
    def _resolve_roles(request: ReviewAdmissionRequest) -> list[Any]:
        try:
            return normalize_requested_dimensions(request.focus)
        except ValueError as exc:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.INVALID_DIMENSIONS, str(exc), field="focus"
            ) from exc

    @staticmethod
    def _resolve_target(request: ReviewAdmissionRequest) -> tuple[str, str, int]:
        raw_repository = (request.repository or "").strip()
        if not raw_repository:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.INVALID_REPOSITORY,
                "A GitHub repository is required.",
                field="repository",
            )
        try:
            owner, name = parse_repository_target(raw_repository)
        except GitHubError as exc:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.INVALID_REPOSITORY, exc.message, field="repository"
            ) from exc
        number = request.pull_request
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.INVALID_PULL_REQUEST,
                "A positive pull request number is required.",
                field="pull_request",
            )
        return owner, name, number

    async def _reject_unavailable(self, session_key: str, request_id: str | None) -> None:
        """Refuse a repeated submit or a session whose review is still running.

        A run left ``interrupted`` (process restarted before it finished) never
        delivered a result, so both its request_id and its session are
        reusable: the stale session metadata is normalized here — the same
        repair the conversation gate applies — instead of blocking the retry.
        """
        if request_id:
            existing = await self._storage.runs.find_by_request_id(request_id)
            if existing is not None and existing.status != "interrupted":
                raise ReviewAdmissionError(
                    ReviewAdmissionCode.DUPLICATE_REQUEST,
                    f"Request '{request_id}' was already submitted as run '{existing.id}'.",
                    field="request_id",
                )
        metadata = await self._sessions.get_metadata(session_key) or {}
        run_id = metadata.get(ReviewMetaKey.RUN_ID)
        if not run_id:
            return
        status = str(metadata.get(ReviewMetaKey.STATUS) or "")
        terminal = {item.value for item in REVIEW_TERMINAL_STATUSES}
        if status and status not in terminal:
            row = await self._storage.runs.get(str(run_id))
            if row is not None and row.status != "interrupted":
                raise ReviewAdmissionError(
                    ReviewAdmissionCode.DUPLICATE_REVIEW,
                    (
                        f"Session '{session_key}' already owns review run '{run_id}' "
                        f"with status '{status}'. Wait for it to finish or cancel it."
                    ),
                )
            # The executor of this run is gone; the DB row confirms it. Repair
            # the stale metadata so the gate opens for the retry.
            await self._sessions.update_metadata(
                session_key,
                {
                    ReviewMetaKey.STATUS: ReviewRunStatus.ERROR.value,
                    ReviewMetaKey.PHASE: ReviewPhase.DONE.value,
                    ReviewMetaKey.SUMMARY: INTERRUPTED_RUN_REASON,
                    ReviewMetaKey.ERROR: INTERRUPTED_RUN_REASON,
                },
            )

    async def _discard_run(self, run_id: str) -> None:
        """Best-effort removal of a run row whose admission did not complete.

        Secondary failures here are logged and swallowed: the original
        admission error is the one worth surfacing.
        """
        try:
            await self._storage.runs.delete(run_id)
        except Exception as exc:  # noqa: BLE001 - cleanup must mask nothing
            logger.warning("review.admission.discard_failed run_id={} reason={}", run_id, exc)

    # -- GitHub -------------------------------------------------------------

    async def _resolve_repository(self, repository: str) -> RepositoryRecord:
        try:
            resolved: GitHubRepository = await self._github.resolve_repository(repository)
        except GitHubError as exc:
            raise ReviewAdmissionError(
                _github_code(exc), exc.message, field="repository"
            ) from exc
        return await self._storage.repositories.upsert_repository(
            owner=resolved.owner,
            name=resolved.name,
            url=resolved.url,
            default_branch=resolved.default_branch,
        )

    async def _resolve_pull_request(
        self, repository: RepositoryRecord, number: int
    ) -> tuple[PullRequestRecord, int]:
        try:
            pull = await self._github.get_pull_request(
                repository.owner, repository.name, number
            )
        except GitHubNotFoundError as exc:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.PULL_REQUEST_NOT_FOUND,
                f"Pull request #{number} was not found in {repository.full_name}.",
                field="pull_request",
            ) from exc
        except GitHubError as exc:
            raise ReviewAdmissionError(_github_code(exc), exc.message) from exc
        row = await self._storage.repositories.upsert_pull_request(
            repository_id=repository.id,
            number=pull.number,
            title=pull.title,
            state=pull.state,
            draft=pull.draft,
            author=pull.author,
            base_ref=pull.base_ref,
            head_ref=pull.head_ref,
            head_sha=pull.head_sha,
            base_sha=pull.base_sha,
            html_url=pull.html_url,
            updated_at=pull.updated_at,
        )
        return row, pull.number

    async def _prepare_snapshot(
        self,
        *,
        run_id: str,
        owner: str,
        name: str,
        number: int,
        repository_id: int,
    ) -> ReviewSnapshot:
        try:
            snapshot = await self._snapshots.prepare(
                run_id=run_id, owner=owner, name=name, number=number
            )
        except SnapshotPreparationError as exc:
            code = (
                ReviewAdmissionCode.PULL_REQUEST_CHANGED
                if exc.code == "pull_request_changed"
                else ReviewAdmissionCode.SNAPSHOT_FAILED
            )
            raise ReviewAdmissionError(code, exc.message) from exc
        except GitHubError as exc:
            raise ReviewAdmissionError(_github_code(exc), exc.message) from exc
        except Exception as exc:  # preparation failures are input problems, not crashes
            raise ReviewAdmissionError(
                ReviewAdmissionCode.SNAPSHOT_FAILED,
                f"Cannot capture the pull request snapshot: {exc}",
            ) from exc
        snapshot = replace(snapshot, repository_id=repository_id)
        if not snapshot.changed_files:
            raise ReviewAdmissionError(
                ReviewAdmissionCode.PULL_REQUEST_EMPTY,
                (
                    f"Pull request #{number} has no reviewable file changes at "
                    f"{snapshot.head_sha[:12]}."
                ),
                field="pull_request",
            )
        return snapshot

    # -- persistence --------------------------------------------------------

    @staticmethod
    def _metadata_payload(
        *,
        admission_target: PullRequestTarget,
        plan: ReviewPlan,
        repository_id: int,
        pull_request_id: int,
        run_id: str,
        snapshot: ReviewSnapshot,
        focus: str | list[str] | None,
    ) -> dict[str, Any]:
        return {
            ReviewMetaKey.TARGET: plan.target,
            ReviewMetaKey.TARGET_TYPE: PULL_REQUEST_TARGET_TYPE,
            ReviewMetaKey.ACTION: plan.action.value,
            ReviewMetaKey.REQUESTED_DIMENSIONS: focus,
            ReviewMetaKey.ALLOWED_DIMENSIONS: [role.name for role in plan.roles],
            ReviewMetaKey.REPOSITORY_ID: repository_id,
            ReviewMetaKey.PULL_REQUEST_ID: pull_request_id,
            ReviewMetaKey.PULL_REQUEST_NUMBER: admission_target.number,
            ReviewMetaKey.BASE_REPO: snapshot.base_repo,
            ReviewMetaKey.HEAD_REPO: snapshot.head_repo,
            ReviewMetaKey.BASE_SHA: snapshot.base_sha,
            ReviewMetaKey.HEAD_SHA: snapshot.head_sha,
            ReviewMetaKey.MERGE_BASE_SHA: snapshot.merge_base_sha,
            ReviewMetaKey.SNAPSHOT_REF: snapshot.id,
            ReviewMetaKey.RUN_ID: run_id,
            ReviewMetaKey.STATUS: ReviewRunStatus.RUNNING.value,
            ReviewMetaKey.PHASE: ReviewPhase.PREPARE.value,
            ReviewMetaKey.INPUT_FINGERPRINT: snapshot.input_fingerprint,
        }


__all__ = [
    "REVIEW_TERMINAL_STATUSES",
    "FileSessionMetadataWriter",
    "ReviewAdmission",
    "ReviewAdmissionCode",
    "ReviewAdmissionError",
    "ReviewAdmissionRequest",
    "ReviewAdmissionService",
    "SessionMetadataWriter",
]
