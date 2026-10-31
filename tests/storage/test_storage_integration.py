"""PostgreSQL integration tests.

Run against a real, empty PostgreSQL database:

    NANOREVIEW_TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/nanoreview_test pytest tests/storage

SQLite is deliberately not supported here: JSONB, ``ON DELETE`` behaviour and
the migration path are part of what this suite verifies.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from nanoreview.storage.database import Database, SCHEMA_HEAD, SchemaNotMigratedError
from nanoreview.storage.records import ChangedFile, ReviewSnapshot
from nanoreview.storage.repositories import Storage

_DSN = os.environ.get("NANOREVIEW_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="set NANOREVIEW_TEST_DATABASE_URL to run PostgreSQL integration tests",
)


@pytest.fixture
async def storage() -> Storage:
    database = Database(_DSN or "", pool_size=2)
    await database.upgrade()
    await database.verify_schema()
    bundle = Storage(database)
    try:
        yield bundle
    finally:
        await bundle.close()


@pytest.fixture
def run_id() -> str:
    return f"run_{uuid.uuid4().hex[:16]}"


async def test_schema_head_is_reported(storage: Storage) -> None:
    await storage.database.verify_schema()
    assert SCHEMA_HEAD == "0001_initial"


async def test_session_metadata_and_messages_round_trip(storage: Storage) -> None:
    key = f"test:session:{uuid.uuid4().hex[:8]}"
    metadata = await storage.sessions.get_or_create(key, metadata={"a": 1})
    assert metadata == {"a": 1}

    updated = await storage.sessions.update_metadata(key, {"b": 2, "a": None})
    assert updated == {"b": 2}

    await storage.sessions.append_message(key, role="user", payload={"content": "hi"})
    await storage.sessions.append_message(key, role="assistant", payload={"content": "yo"})
    messages = await storage.sessions.load_messages(key)
    assert [item["content"] for item in messages] == ["hi", "yo"]

    listed = await storage.sessions.list_sessions()
    assert any(item["session_key"] == key for item in listed)

    assert await storage.sessions.delete(key) is True
    assert await storage.sessions.load_messages(key) == []


async def test_repository_and_pull_request_upsert(storage: Storage) -> None:
    repo = await storage.repositories.upsert_repository(
        owner="acme", name="widgets", url="https://github.com/acme/widgets", default_branch="main"
    )
    again = await storage.repositories.upsert_repository(
        owner="acme", name="widgets", url="https://github.com/acme/widgets", default_branch="trunk"
    )
    assert again.id == repo.id
    assert again.default_branch == "trunk"

    pr = await storage.repositories.upsert_pull_request(
        repository_id=repo.id,
        number=7,
        title="Fix widget",
        state="open",
        draft=False,
        author="octocat",
        base_ref="main",
        head_ref="fix",
        head_sha="a" * 40,
        base_sha="b" * 40,
        html_url="https://github.com/acme/widgets/pull/7",
        updated_at=datetime.now(timezone.utc),
    )
    assert pr.number == 7
    assert pr.head_sha == "a" * 40

    listed = await storage.repositories.list_pull_requests(repo.id, state="open")
    assert [item.number for item in listed] == [7]


async def test_snapshot_and_terminal_commit(storage: Storage, run_id: str) -> None:
    repo = await storage.repositories.upsert_repository(
        owner="acme", name=f"widgets-{uuid.uuid4().hex[:6]}", url="https://github.com/acme/w"
    )
    pr = await storage.repositories.upsert_pull_request(
        repository_id=repo.id,
        number=1,
        title="t",
        state="open",
        draft=False,
        author=None,
        base_ref="main",
        head_ref="f",
        head_sha="c" * 40,
        base_sha="d" * 40,
        html_url=None,
        updated_at=None,
    )
    run = await storage.runs.create(
        run_id=run_id,
        session_key=f"test:run:{run_id}",
        repository_id=repo.id,
        pull_request_id=pr.id,
        request_id=f"req-{run_id}",
        base_sha="d" * 40,
        head_sha="c" * 40,
        merge_base_sha="e" * 40,
    )
    assert run.status == "queued"

    snapshot = ReviewSnapshot(
        id=f"snap_{run_id}",
        run_id=run_id,
        repository_id=repo.id,
        pr_number=1,
        base_repo="acme/widgets",
        head_repo="acme/widgets",
        base_sha="d" * 40,
        head_sha="c" * 40,
        merge_base_sha="e" * 40,
        metadata={"title": "t"},
        changed_files=(
            ChangedFile(
                path="src/a.py",
                old_path=None,
                status="modified",
                additions=1,
                deletions=1,
                patch="@@ -1 +1 @@\n-old\n+new\n",
            ),
        ),
        input_fingerprint="f" * 64,
        gaps=("patch_missing: src/b.py",),
    )
    await storage.runs.save_snapshot(snapshot)
    loaded = await storage.runs.load_snapshot(run_id)
    assert loaded is not None
    assert loaded.head_sha == "c" * 40
    assert loaded.changed_files[0].path == "src/a.py"
    assert loaded.patch_map == {"src/a.py": "@@ -1 +1 @@\n-old\n+new\n"}
    assert loaded.gaps == ("patch_missing: src/b.py",)

    await storage.runs.commit_terminal(
        run_id,
        status="succeeded",
        findings=[
            {
                "finding_id": "f1",
                "severity": "high",
                "title": "bug",
                "file": "src/a.py",
                "line": 1,
                "verdict": "accepted",
            }
        ],
        report_markdown="# Report",
        coverage={"changed_files": ["src/a.py"]},
        summary="1 finding",
        usage={"total_tokens": 10},
    )
    report = await storage.runs.get_report(run_id)
    assert report is not None
    assert report.markdown == "# Report"
    assert report.findings[0]["finding_id"] == "f1"

    stored_run = await storage.runs.get(run_id)
    assert stored_run is not None
    assert stored_run.status == "succeeded"
    assert stored_run.phase == "cleanup"
    assert stored_run.summary == "1 finding"


async def test_request_id_is_unique_and_findable(storage: Storage, run_id: str) -> None:
    repo = await storage.repositories.upsert_repository(
        owner="acme", name=f"uniq-{uuid.uuid4().hex[:6]}", url="https://github.com/acme/u"
    )
    pr = await storage.repositories.upsert_pull_request(
        repository_id=repo.id,
        number=2,
        title="t",
        state="open",
        draft=False,
        author=None,
        base_ref="main",
        head_ref="f",
        head_sha=None,
        base_sha=None,
        html_url=None,
        updated_at=None,
    )
    request_id = f"req-{run_id}"
    await storage.runs.create(
        run_id=run_id,
        session_key=f"test:req:{run_id}",
        repository_id=repo.id,
        pull_request_id=pr.id,
        request_id=request_id,
    )
    found = await storage.runs.find_by_request_id(request_id)
    assert found is not None and found.id == run_id

    with pytest.raises(Exception):
        await storage.runs.create(
            run_id=f"{run_id}_dup",
            session_key=f"test:req:{run_id}",
            repository_id=repo.id,
            pull_request_id=pr.id,
            request_id=request_id,
        )


async def test_restart_marks_interrupted(storage: Storage, run_id: str) -> None:
    repo = await storage.repositories.upsert_repository(
        owner="acme", name=f"interrupt-{uuid.uuid4().hex[:6]}", url="https://github.com/acme/i"
    )
    pr = await storage.repositories.upsert_pull_request(
        repository_id=repo.id,
        number=3,
        title="t",
        state="open",
        draft=False,
        author=None,
        base_ref="main",
        head_ref="f",
        head_sha=None,
        base_sha=None,
        html_url=None,
        updated_at=None,
    )
    await storage.runs.create(
        run_id=run_id,
        session_key=f"test:int:{run_id}",
        repository_id=repo.id,
        pull_request_id=pr.id,
        request_id=None,
        status="running",
    )
    await storage.runs.mark_interrupted()
    stored = await storage.runs.get(run_id)
    assert stored is not None
    assert stored.status == "interrupted"


async def test_verify_schema_rejects_unmigrated_database() -> None:
    separator = "&" if "?" in (_DSN or "") else "?"
    isolated = f"{_DSN}{separator}options=-csearch_path%3Dnanoreview_unmigrated"
    database = Database(isolated, pool_size=1)
    try:
        with pytest.raises(SchemaNotMigratedError):
            await database.verify_schema()
    finally:
        await database.dispose()


__all__ = []
