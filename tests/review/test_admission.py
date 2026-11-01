"""Admission boundary tests for pull-request review requests.

Admission is the single place where a review is validated, frozen into a
snapshot, and registered. These tests pin the invariants the transports depend
on: rejection is all-or-nothing (no run, no snapshot, no session metadata), the
review input is a GitHub pull request pinned to the SHAs captured at admission,
and a repeated request id or a running review is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from nanoreview.review.admission import (
    ReviewAdmissionCode,
    ReviewAdmissionError,
    ReviewAdmissionRequest,
    ReviewAdmissionService,
)
from nanoreview.review.input.github import (
    GitHubError,
    GitHubNotFoundError,
    GitHubPullRequest,
    GitHubRepository,
)
from nanoreview.review.types import ReviewMetaKey


def _repo(owner: str = "octo", name: str = "demo") -> GitHubRepository:
    return GitHubRepository(
        owner=owner,
        name=name,
        url=f"https://github.com/{owner}/{name}",
        default_branch="main",
    )


def _pull(number: int = 7) -> GitHubPullRequest:
    return GitHubPullRequest(
        number=number,
        title="Fix boundary",
        state="open",
        draft=False,
        author="octo",
        base_ref="main",
        head_ref="feature",
        base_sha="a" * 40,
        head_sha="b" * 40,
        base_repo="octo/demo",
        head_repo="octo/demo",
        html_url=f"https://github.com/octo/demo/pull/{number}",
        updated_at=None,
    )


class FakeGitHub:
    """resolve/get_pull_request only; snapshot preparation is injected."""

    def __init__(self) -> None:
        self.repository = _repo()
        self.pull = _pull()
        self.fail_resolve: GitHubError | None = None
        self.fail_pull: GitHubError | None = None

    async def resolve_repository(self, target: str) -> GitHubRepository:
        if self.fail_resolve is not None:
            raise self.fail_resolve
        return self.repository

    async def get_pull_request(self, owner: str, name: str, number: int) -> GitHubPullRequest:
        if self.fail_pull is not None:
            raise self.fail_pull
        return self.pull


@dataclass
class FakeRuns:
    created: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[Any] = field(default_factory=list)
    marked: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    by_request: dict[str, Any] = field(default_factory=dict)
    store: dict[str, Any] = field(default_factory=dict)

    async def create(self, **kwargs: Any) -> None:
        self.created.append(kwargs)

    async def save_snapshot(self, snapshot: Any) -> None:
        self.snapshots.append(snapshot)

    async def mark_running(self, run_id: str, **kwargs: Any) -> None:
        self.marked.append({"run_id": run_id, **kwargs})

    async def delete(self, run_id: str) -> None:
        self.deleted.append(run_id)

    async def clear_request_id(self, run_id: str) -> None:
        self.cleared.append(run_id)

    async def get(self, run_id: str) -> Any | None:
        return self.store.get(run_id)

    async def find_by_request_id(self, request_id: str) -> Any | None:
        return self.by_request.get(request_id)


@dataclass
class FakeRepositories:
    repository_id: int = 11
    pull_request_id: int = 22

    async def upsert_repository(self, **kwargs: Any) -> Any:
        @dataclass(frozen=True)
        class _Row:
            id: int
            owner: str
            name: str
            url: str
            full_name: str

        return _Row(
            id=self.repository_id,
            owner=kwargs["owner"],
            name=kwargs["name"],
            url=kwargs["url"],
            full_name=f"{kwargs['owner']}/{kwargs['name']}",
        )

    async def upsert_pull_request(self, **kwargs: Any) -> Any:
        @dataclass(frozen=True)
        class _Row:
            id: int
            number: int

        return _Row(id=self.pull_request_id, number=kwargs["number"])


@dataclass
class FakeSessions:
    stored: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def update_metadata(self, session_key: str, payload: dict[str, Any]) -> None:
        self.stored.setdefault(session_key, {}).update(payload)

    async def get_metadata(self, session_key: str) -> dict[str, Any] | None:
        return self.stored.get(session_key)


class _SnapshotStub:
    def __init__(self, prepare) -> None:
        self._prepare = prepare

    async def prepare(self, **kwargs: Any) -> Any:
        return await self._prepare(**kwargs)


def _snapshot_service(head_sha: str = "b" * 40) -> _SnapshotStub:
    from datetime import datetime, timezone

    from nanoreview.storage.records import ChangedFile, ReviewSnapshot

    async def prepare(*, run_id: str, owner: str, name: str, number: int):
        return ReviewSnapshot(
            id=f"snap_{run_id[-6:]}",
            run_id=run_id,
            repository_id=0,
            pr_number=number,
            base_repo=f"{owner}/{name}",
            head_repo=f"{owner}/{name}",
            base_sha="a" * 40,
            head_sha=head_sha,
            merge_base_sha="c" * 40,
            metadata={"html_url": f"https://github.com/{owner}/{name}/pull/{number}"},
            changed_files=(
                ChangedFile(
                    path="src/app.py",
                    old_path=None,
                    status="modified",
                    patch="@@ -1,1 +1,2 @@\n-old\n+new\n+extra\n",
                ),
            ),
            created_at=datetime.now(timezone.utc),
        )

    return _SnapshotStub(prepare)


def _service(**overrides: Any) -> tuple[ReviewAdmissionService, FakeGitHub, FakeRuns, FakeSessions]:
    github = overrides.pop("github", FakeGitHub())
    runs = overrides.pop("runs", FakeRuns())
    sessions = overrides.pop("sessions", FakeSessions())
    service = ReviewAdmissionService(
        github=github,
        storage=type("S", (), {"repositories": FakeRepositories(), "runs": runs})(),
        sessions=sessions,
        snapshot_service=overrides.pop("snapshot_service", _snapshot_service()),
    )
    return service, github, runs, sessions


@pytest.mark.asyncio
async def test_admit_registers_run_snapshot_and_metadata() -> None:
    service, _github, runs, sessions = _service()
    admission = await service.admit(
        ReviewAdmissionRequest(
            repository="octo/demo",
            pull_request=7,
            focus="bug,general",
            content="check the boundary",
            session_key="review:test",
            request_id="req-1",
        )
    )

    assert admission.target == "octo/demo#7"
    assert admission.run_id.startswith("run-")
    assert [item["run_id"] for item in runs.created] == [admission.run_id]
    assert runs.created[0]["status"] == "running"
    assert runs.snapshots and runs.snapshots[0].run_id == admission.run_id
    assert runs.marked and runs.marked[0]["head_sha"] == "b" * 40

    metadata = sessions.stored["review:test"]
    assert metadata[ReviewMetaKey.RUN_ID] == admission.run_id
    assert metadata[ReviewMetaKey.TARGET_TYPE] == "pull_request"
    assert metadata[ReviewMetaKey.PULL_REQUEST_NUMBER] == 7
    assert metadata[ReviewMetaKey.HEAD_SHA] == "b" * 40
    assert metadata[ReviewMetaKey.SNAPSHOT_REF] == runs.snapshots[0].id
    assert [role.name for role in admission.plan.roles] == ["bug", "general"]
    assert admission.plan.pull_request is not None
    assert admission.plan.pull_request.snapshot_id == runs.snapshots[0].id


@pytest.mark.asyncio
async def test_admit_rejects_missing_pr_number() -> None:
    service, *_ = _service()
    with pytest.raises(ReviewAdmissionError) as exc:
        await service.admit(
            ReviewAdmissionRequest(repository="octo/demo", pull_request=None)
        )
    assert exc.value.code == ReviewAdmissionCode.INVALID_PULL_REQUEST
    assert exc.value.field == "pull_request"


@pytest.mark.asyncio
async def test_admit_rejects_non_github_target() -> None:
    service, *_ = _service()
    with pytest.raises(ReviewAdmissionError) as exc:
        await service.admit(
            ReviewAdmissionRequest(repository="C:/code/demo", pull_request=7)
        )
    assert exc.value.code == ReviewAdmissionCode.INVALID_REPOSITORY
    assert exc.value.field == "repository"


@pytest.mark.asyncio
async def test_admit_maps_github_not_found() -> None:
    github = FakeGitHub()
    github.fail_pull = GitHubNotFoundError("nope")
    service, *_ = _service(github=github)
    with pytest.raises(ReviewAdmissionError) as exc:
        await service.admit(
            ReviewAdmissionRequest(repository="octo/demo", pull_request=7)
        )
    assert exc.value.code == ReviewAdmissionCode.PULL_REQUEST_NOT_FOUND
    assert exc.value.status == 404


@pytest.mark.asyncio
async def test_admit_rejects_duplicate_request_id() -> None:
    runs = FakeRuns()

    @dataclass(frozen=True)
    class _Existing:
        id: str = "run-existing"
        status: str = "running"

    runs.by_request["req-1"] = _Existing()
    service, *_ = _service(runs=runs)
    with pytest.raises(ReviewAdmissionError) as exc:
        await service.admit(
            ReviewAdmissionRequest(
                repository="octo/demo", pull_request=7, request_id="req-1"
            )
        )
    assert exc.value.code == ReviewAdmissionCode.DUPLICATE_REQUEST


@pytest.mark.asyncio
async def test_admit_allows_retry_after_the_process_lost_the_run() -> None:
    """An interrupted run never delivered a result: request_id is reusable.

    The old row's idempotency key must be released first — ``request_id`` is
    unique across runs, so the retry's insert would otherwise fail on the
    constraint.
    """
    runs = FakeRuns()

    @dataclass(frozen=True)
    class _Interrupted:
        id: str = "run-old"
        status: str = "interrupted"

    runs.by_request["req-1"] = _Interrupted()
    service, _github, runs, _sessions = _service(runs=runs)
    admission = await service.admit(
        ReviewAdmissionRequest(
            repository="octo/demo", pull_request=7, request_id="req-1"
        )
    )
    assert admission.run_id != "run-old"
    assert runs.cleared == ["run-old"]


@pytest.mark.asyncio
async def test_admit_rejects_session_with_running_review() -> None:
    sessions = FakeSessions()
    sessions.stored["review:test"] = {
        ReviewMetaKey.RUN_ID: "run-1",
        ReviewMetaKey.STATUS: "running",
    }
    runs = FakeRuns()

    @dataclass(frozen=True)
    class _Running:
        id: str = "run-1"
        status: str = "running"

    runs.store["run-1"] = _Running()
    service, *_ = _service(sessions=sessions, runs=runs)
    with pytest.raises(ReviewAdmissionError) as exc:
        await service.admit(
            ReviewAdmissionRequest(
                repository="octo/demo", pull_request=7, session_key="review:test"
            )
        )
    assert exc.value.code == ReviewAdmissionCode.DUPLICATE_REVIEW


@pytest.mark.asyncio
async def test_admit_repairs_stale_session_metadata_after_a_restart() -> None:
    """A crash orphans the session metadata; the retry repairs, not rejects."""
    sessions = FakeSessions()
    sessions.stored["review:test"] = {
        ReviewMetaKey.RUN_ID: "run-1",
        ReviewMetaKey.STATUS: "running",
    }
    runs = FakeRuns()

    @dataclass(frozen=True)
    class _Interrupted:
        id: str = "run-1"
        status: str = "interrupted"
        phase: str = "cleanup"
        summary: str | None = None
        error: str | None = "process restarted before the run finished"

    runs.store["run-1"] = _Interrupted()
    service, *_ = _service(sessions=sessions, runs=runs)

    admission = await service.admit(
        ReviewAdmissionRequest(
            repository="octo/demo", pull_request=7, session_key="review:test"
        )
    )

    # The stale running entry did not block the retry, and the session now
    # belongs to the new run (the repair write is superseded by the new
    # admission's own metadata).
    assert admission.run_id != "run-1"
    repaired = sessions.stored["review:test"]
    assert repaired[ReviewMetaKey.RUN_ID] == admission.run_id
    assert runs.cleared == ["run-1"]


@pytest.mark.asyncio
async def test_admit_repairs_metadata_when_the_run_finished_but_the_save_failed() -> None:
    """A terminal DB row with stale ``running`` metadata must not block forever.

    The board's terminal transaction commits before the session metadata save;
    if that save fails and the process restarts, the session still claims a
    running review the database knows is finished.
    """
    sessions = FakeSessions()
    sessions.stored["review:test"] = {
        ReviewMetaKey.RUN_ID: "run-1",
        ReviewMetaKey.STATUS: "running",
    }
    runs = FakeRuns()

    @dataclass(frozen=True)
    class _Completed:
        id: str = "run-1"
        status: str = "completed"
        phase: str = "done"
        summary: str = "all clear"
        error: str | None = None

    runs.store["run-1"] = _Completed()
    service, *_ = _service(sessions=sessions, runs=runs)

    admission = await service.admit(
        ReviewAdmissionRequest(
            repository="octo/demo", pull_request=7, session_key="review:test"
        )
    )

    assert admission.run_id != "run-1"
    repaired = sessions.stored["review:test"]
    assert repaired[ReviewMetaKey.RUN_ID] == admission.run_id
    # The interrupted-style synthetic repair was not applied to a real
    # terminal row's identity: no key clearing happened.
    assert runs.cleared == []


@pytest.mark.asyncio
async def test_admit_maps_a_storage_outage_to_a_stable_code() -> None:
    """The database being down is a rejection, not a raw driver traceback."""
    runs = FakeRuns()

    async def down(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("connection refused")

    runs.find_by_request_id = down  # type: ignore[method-assign]
    service, *_ = _service(runs=runs)
    with pytest.raises(ReviewAdmissionError) as exc:
        await service.admit(
            ReviewAdmissionRequest(
                repository="octo/demo", pull_request=7, request_id="req-1"
            )
        )
    assert exc.value.code == ReviewAdmissionCode.PERSISTENCE_FAILED


@pytest.mark.asyncio
async def test_admit_allows_rerun_after_terminal_review() -> None:
    sessions = FakeSessions()
    sessions.stored["review:test"] = {
        ReviewMetaKey.RUN_ID: "run-1",
        ReviewMetaKey.STATUS: "completed",
    }
    service, _github, runs, sessions = _service(sessions=sessions)
    admission = await service.admit(
        ReviewAdmissionRequest(
            repository="octo/demo", pull_request=7, session_key="review:test"
        )
    )
    assert admission.run_id != "run-1"
    assert sessions.stored["review:test"][ReviewMetaKey.RUN_ID] == admission.run_id


@pytest.mark.asyncio
async def test_admit_rejects_empty_pull_request() -> None:
    from nanoreview.storage.records import ReviewSnapshot

    async def prepare(*, run_id: str, owner: str, name: str, number: int):
        return ReviewSnapshot(
            id="snap_x",
            run_id=run_id,
            repository_id=0,
            pr_number=number,
            base_repo=f"{owner}/{name}",
            head_repo=f"{owner}/{name}",
            base_sha="a" * 40,
            head_sha="b" * 40,
            merge_base_sha="c" * 40,
        )

    service, *_ = _service(snapshot_service=_SnapshotStub(prepare))
    with pytest.raises(ReviewAdmissionError) as exc:
        await service.admit(
            ReviewAdmissionRequest(repository="octo/demo", pull_request=7)
        )
    assert exc.value.code == ReviewAdmissionCode.PULL_REQUEST_EMPTY


@pytest.mark.asyncio
async def test_admit_maps_head_moved_to_pull_request_changed() -> None:
    from nanoreview.review.input.pr_snapshot import SnapshotPreparationError

    async def prepare(*, run_id: str, owner: str, name: str, number: int):
        raise SnapshotPreparationError(
            "pull_request_changed", "the pull request changed"
        )

    service, github, runs, _sessions = _service(
        snapshot_service=_SnapshotStub(prepare)
    )
    with pytest.raises(ReviewAdmissionError) as exc:
        await service.admit(
            ReviewAdmissionRequest(repository="octo/demo", pull_request=7)
        )
    assert exc.value.code == ReviewAdmissionCode.PULL_REQUEST_CHANGED
    assert exc.value.status == 409
    # The run row was registered before preparation failed; a run that never
    # started must not stay behind as a zombie ``running`` row — it would
    # block this request_id forever.
    assert len(runs.created) == 1
    assert runs.deleted == [runs.created[0]["run_id"]]
    assert runs.snapshots == []


@pytest.mark.asyncio
async def test_admit_discards_the_run_row_when_persistence_fails() -> None:
    service, _github, runs, _sessions = _service()

    async def failing_save(snapshot: Any) -> None:
        raise OSError("disk full")

    runs.save_snapshot = failing_save  # type: ignore[method-assign]

    with pytest.raises(ReviewAdmissionError) as exc:
        await service.admit(
            ReviewAdmissionRequest(repository="octo/demo", pull_request=7)
        )
    assert exc.value.code == ReviewAdmissionCode.PERSISTENCE_FAILED
    assert runs.deleted == [runs.created[0]["run_id"]]
    # No session metadata either: the rejection is all-or-nothing.
    assert _sessions.stored == {}


@pytest.mark.asyncio
async def test_admission_rejection_writes_nothing() -> None:
    runs = FakeRuns()
    sessions = FakeSessions()
    service, *_ = _service(runs=runs, sessions=sessions)
    with pytest.raises(ReviewAdmissionError):
        await service.admit(
            ReviewAdmissionRequest(repository="not a repo", pull_request=7)
        )
    assert runs.created == []
    assert runs.snapshots == []
    assert sessions.stored == {}
