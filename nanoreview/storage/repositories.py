"""Async repositories over the PostgreSQL schema.

Each method owns one transaction. Review tasks run concurrently, so a session
is never shared across tasks; a repository call borrows a fresh ``AsyncSession``
from the :class:`~nanoreview.storage.database.Database` factory and closes it
before returning plain records.

The terminal write (findings + report + run status) is a single transaction:
either the run is settled with a report or it is settled with an error, never
half-published.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import delete, func, select, update

from nanoreview.storage.database import Database
from nanoreview.storage.models import (
    MessageRow,
    PullRequestRow,
    RepositoryRow,
    ReviewerResultRow,
    ReviewEvidenceRow,
    ReviewFindingRow,
    ReviewReportRow,
    ReviewRunRow,
    ReviewSnapshotRow,
    SessionRow,
)
from nanoreview.storage.records import (
    ChangedFile,
    PullRequestRecord,
    RepositoryRecord,
    ReviewReportRecord,
    ReviewRunRecord,
    ReviewSnapshot,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionRepository:
    """Sessions and their message history."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get_or_create(
        self, session_key: str, *, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with self._db.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SessionRow).where(SessionRow.session_key == session_key)
                )
                if row is None:
                    row = SessionRow(
                        id=f"s{uuid.uuid4().hex}",
                        session_key=session_key,
                        metadata_payload=dict(metadata or {}),
                    )
                    session.add(row)
                    await session.flush()
                return dict(row.metadata_payload)

    async def update_metadata(
        self, session_key: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with self._db.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SessionRow)
                    .where(SessionRow.session_key == session_key)
                    .with_for_update()
                )
                if row is None:
                    return None
                payload = dict(row.metadata_payload)
                for key, value in updates.items():
                    if value is None:
                        payload.pop(key, None)
                    else:
                        payload[key] = value
                row.metadata_payload = payload
                row.updated_at = _utcnow()
                return dict(payload)

    async def get_metadata(self, session_key: str) -> dict[str, Any] | None:
        async with self._db.session() as session:
            row = await session.scalar(
                select(SessionRow).where(SessionRow.session_key == session_key)
            )
            return None if row is None else dict(row.metadata_payload)

    async def list_sessions(self, *, limit: int = 200) -> list[dict[str, Any]]:
        async with self._db.session() as session:
            rows = (
                await session.scalars(
                    select(SessionRow).order_by(SessionRow.updated_at.desc()).limit(limit)
                )
            ).all()
            return [
                {
                    "id": row.id,
                    "session_key": row.session_key,
                    "metadata": dict(row.metadata_payload),
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]

    async def delete(self, session_key: str) -> bool:
        async with self._db.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SessionRow).where(SessionRow.session_key == session_key)
                )
                if row is None:
                    return False
                await session.delete(row)
                return True

    async def append_message(
        self, session_key: str, *, role: str, payload: dict[str, Any]
    ) -> None:
        async with self._db.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SessionRow).where(SessionRow.session_key == session_key)
                )
                if row is None:
                    raise KeyError(f"unknown session {session_key!r}")
                next_seq = (
                    await session.scalar(
                        select(func.coalesce(func.max(MessageRow.seq), 0)).where(
                            MessageRow.session_id == row.id
                        )
                    )
                    or 0
                ) + 1
                session.add(
                    MessageRow(session_id=row.id, seq=next_seq, role=role, payload=payload)
                )
                row.updated_at = _utcnow()

    async def load_messages(self, session_key: str) -> list[dict[str, Any]]:
        async with self._db.session() as session:
            row = await session.scalar(
                select(SessionRow).where(SessionRow.session_key == session_key)
            )
            if row is None:
                return []
            messages = (
                await session.scalars(
                    select(MessageRow)
                    .where(MessageRow.session_id == row.id)
                    .order_by(MessageRow.seq)
                )
            ).all()
            return [dict(item.payload) for item in messages]


class RepositoryStore:
    """Repositories and pull requests discovered from GitHub."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def upsert_repository(
        self,
        *,
        owner: str,
        name: str,
        url: str,
        default_branch: str | None = None,
        provider: str = "github",
    ) -> RepositoryRecord:
        async with self._db.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(RepositoryRow).where(
                        RepositoryRow.provider == provider,
                        RepositoryRow.owner == owner,
                        RepositoryRow.name == name,
                    )
                )
                if row is None:
                    row = RepositoryRow(
                        provider=provider,
                        owner=owner,
                        name=name,
                        url=url,
                        default_branch=default_branch,
                    )
                    session.add(row)
                    await session.flush()
                elif default_branch and row.default_branch != default_branch:
                    row.default_branch = default_branch
                return RepositoryRecord(
                    id=row.id,
                    provider=row.provider,
                    owner=row.owner,
                    name=row.name,
                    url=row.url,
                    default_branch=row.default_branch,
                )

    async def get_repository(self, repository_id: int) -> RepositoryRecord | None:
        async with self._db.session() as session:
            row = await session.get(RepositoryRow, repository_id)
            if row is None:
                return None
            return RepositoryRecord(
                id=row.id,
                provider=row.provider,
                owner=row.owner,
                name=row.name,
                url=row.url,
                default_branch=row.default_branch,
            )

    async def upsert_pull_request(
        self,
        *,
        repository_id: int,
        number: int,
        title: str,
        state: str,
        draft: bool,
        author: str | None,
        base_ref: str,
        head_ref: str,
        head_sha: str | None,
        base_sha: str | None,
        html_url: str | None,
        updated_at: datetime | None,
    ) -> PullRequestRecord:
        async with self._db.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(PullRequestRow).where(
                        PullRequestRow.repository_id == repository_id,
                        PullRequestRow.number == number,
                    )
                )
                if row is None:
                    row = PullRequestRow(repository_id=repository_id, number=number)
                    session.add(row)
                row.title = title
                row.state = state
                row.draft = draft
                row.author = author
                row.base_ref = base_ref
                row.head_ref = head_ref
                row.head_sha = head_sha
                row.base_sha = base_sha
                row.html_url = html_url
                row.updated_at = updated_at
                row.fetched_at = _utcnow()
                await session.flush()
                return _pull_request_record(row)

    async def list_pull_requests(
        self, repository_id: int, *, state: str = "open", limit: int = 30, offset: int = 0
    ) -> list[PullRequestRecord]:
        async with self._db.session() as session:
            rows = (
                await session.scalars(
                    select(PullRequestRow)
                    .where(
                        PullRequestRow.repository_id == repository_id,
                        PullRequestRow.state == state,
                    )
                    .order_by(PullRequestRow.number.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return [_pull_request_record(row) for row in rows]

    async def get_pull_request(
        self, repository_id: int, number: int
    ) -> PullRequestRecord | None:
        async with self._db.session() as session:
            row = await session.scalar(
                select(PullRequestRow).where(
                    PullRequestRow.repository_id == repository_id,
                    PullRequestRow.number == number,
                )
            )
            return None if row is None else _pull_request_record(row)


class ReviewRunRepository:
    """Review runs, their snapshots, evidence, results and reports."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def create(
        self,
        *,
        run_id: str,
        session_key: str,
        repository_id: int,
        pull_request_id: int,
        request_id: str | None,
        status: str = "queued",
        phase: str = "prepare",
        base_sha: str | None = None,
        head_sha: str | None = None,
        merge_base_sha: str | None = None,
    ) -> ReviewRunRecord:
        async with self._db.session() as session:
            async with session.begin():
                session_row = await session.scalar(
                    select(SessionRow).where(SessionRow.session_key == session_key)
                )
                if session_row is None:
                    session_row = SessionRow(
                        id=f"s{uuid.uuid4().hex}",
                        session_key=session_key,
                        metadata_payload={},
                    )
                    session.add(session_row)
                    await session.flush()
                row = ReviewRunRow(
                    id=run_id,
                    session_id=session_row.id,
                    repository_id=repository_id,
                    pull_request_id=pull_request_id,
                    request_id=request_id,
                    status=status,
                    phase=phase,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    merge_base_sha=merge_base_sha,
                )
                session.add(row)
                await session.flush()
                return _run_record(row)

    async def get(self, run_id: str) -> ReviewRunRecord | None:
        async with self._db.session() as session:
            row = await session.get(ReviewRunRow, run_id)
            return None if row is None else _run_record(row)

    async def find_by_request_id(self, request_id: str) -> ReviewRunRecord | None:
        async with self._db.session() as session:
            row = await session.scalar(
                select(ReviewRunRow).where(ReviewRunRow.request_id == request_id)
            )
            return None if row is None else _run_record(row)

    async def mark_running(self, run_id: str, *, base_sha: str, head_sha: str, merge_base_sha: str) -> None:
        async with self._db.session() as session:
            async with session.begin():
                await session.execute(
                    update(ReviewRunRow)
                    .where(ReviewRunRow.id == run_id)
                    .values(
                        status="running",
                        phase="prepare",
                        base_sha=base_sha,
                        head_sha=head_sha,
                        merge_base_sha=merge_base_sha,
                        started_at=_utcnow(),
                    )
                )

    async def set_phase(self, run_id: str, phase: str) -> None:
        async with self._db.session() as session:
            async with session.begin():
                await session.execute(
                    update(ReviewRunRow).where(ReviewRunRow.id == run_id).values(phase=phase)
                )

    async def append_snapshot_audit(
        self, run_id: str, sections: dict[str, Any]
    ) -> bool:
        """Merge run-audit sections into the snapshot's metadata.

        The snapshot row keeps the frozen input; the audit sections recorded
        after planning (budget contract, model identity, accepted evidence,
        assignments) are additive metadata, so they are merged rather than
        rewriting ``changed_files``/``patches``.
        """
        async with self._db.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(ReviewSnapshotRow).where(ReviewSnapshotRow.run_id == run_id)
                )
                if row is None:
                    return False
                metadata = dict(row.metadata_payload or {})
                audit = dict(metadata.get("audit") or {})
                audit.update(sections)
                metadata["audit"] = audit
                row.metadata_payload = metadata
                return True

    async def save_snapshot(self, snapshot: ReviewSnapshot) -> None:
        async with self._db.session() as session:
            async with session.begin():
                row = await session.get(ReviewSnapshotRow, snapshot.id)
                if row is None:
                    row = ReviewSnapshotRow(id=snapshot.id, run_id=snapshot.run_id)
                    session.add(row)
                row.repository_id = snapshot.repository_id
                row.pr_number = snapshot.pr_number
                row.base_repo = snapshot.base_repo
                row.head_repo = snapshot.head_repo
                row.base_sha = snapshot.base_sha
                row.head_sha = snapshot.head_sha
                row.merge_base_sha = snapshot.merge_base_sha
                row.metadata_payload = dict(snapshot.metadata)
                row.changed_files = [item.as_payload() for item in snapshot.changed_files]
                row.patches = [
                    {"path": item.path, "patch": item.patch}
                    for item in snapshot.changed_files
                    if item.patch
                ]
                row.input_fingerprint = snapshot.input_fingerprint
                row.gaps = list(snapshot.gaps)
                row.schema_version = snapshot.schema_version
                await session.execute(
                    update(ReviewRunRow)
                    .where(ReviewRunRow.id == snapshot.run_id)
                    .values(
                        snapshot_id=snapshot.id,
                        base_sha=snapshot.base_sha,
                        head_sha=snapshot.head_sha,
                        merge_base_sha=snapshot.merge_base_sha,
                    )
                )

    async def load_snapshot(self, run_id: str) -> ReviewSnapshot | None:
        async with self._db.session() as session:
            row = await session.scalar(
                select(ReviewSnapshotRow).where(ReviewSnapshotRow.run_id == run_id)
            )
            if row is None:
                return None
            return ReviewSnapshot(
                id=row.id,
                run_id=row.run_id,
                repository_id=row.repository_id,
                pr_number=row.pr_number,
                base_repo=row.base_repo,
                head_repo=row.head_repo,
                base_sha=row.base_sha,
                head_sha=row.head_sha,
                merge_base_sha=row.merge_base_sha,
                metadata=dict(row.metadata_payload),
                changed_files=tuple(
                    ChangedFile.from_payload(item) for item in (row.changed_files or [])
                ),
                input_fingerprint=row.input_fingerprint,
                gaps=tuple(row.gaps or ()),
                created_at=row.created_at,
                schema_version=row.schema_version,
            )

    async def replace_evidence(self, run_id: str, evidence: Iterable[dict[str, Any]]) -> None:
        async with self._db.session() as session:
            async with session.begin():
                await session.execute(
                    delete(ReviewEvidenceRow).where(ReviewEvidenceRow.run_id == run_id)
                )
                for item in evidence:
                    session.add(
                        ReviewEvidenceRow(
                            run_id=run_id,
                            evidence_id=str(item["evidence_id"]),
                            kind=str(item.get("kind") or "diff"),
                            path=item.get("path"),
                            side=item.get("side"),
                            start_line=item.get("start_line"),
                            end_line=item.get("end_line"),
                            payload=dict(item.get("payload") or {}),
                        )
                    )

    async def record_reviewer_result(
        self,
        run_id: str,
        *,
        dimension: str,
        status: str,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
        duplicate_reads: int = 0,
        duplicate_searches: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self._db.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(ReviewerResultRow).where(
                        ReviewerResultRow.run_id == run_id,
                        ReviewerResultRow.dimension == dimension,
                    )
                )
                if row is None:
                    row = ReviewerResultRow(run_id=run_id, dimension=dimension)
                    session.add(row)
                row.status = status
                row.error = error
                row.usage = dict(usage or {})
                row.duplicate_reads = duplicate_reads
                row.duplicate_searches = duplicate_searches
                row.payload = dict(payload or {})

    async def commit_terminal(
        self,
        run_id: str,
        *,
        status: str,
        findings: list[dict[str, Any]],
        report_markdown: str | None,
        coverage: dict[str, Any],
        error: str | None = None,
        summary: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Persist findings, report and terminal status in one transaction."""
        async with self._db.session() as session:
            async with session.begin():
                await session.execute(
                    delete(ReviewFindingRow).where(ReviewFindingRow.run_id == run_id)
                )
                for item in findings:
                    session.add(
                        ReviewFindingRow(
                            run_id=run_id,
                            finding_id=str(item.get("finding_id") or item.get("id") or ""),
                            severity=str(item.get("severity") or "medium"),
                            title=str(item.get("title") or ""),
                            path=item.get("file") or item.get("path"),
                            side=item.get("side"),
                            start_line=item.get("line") or item.get("start_line"),
                            end_line=item.get("end_line"),
                            verdict=str(item.get("verdict") or "needs_confirmation"),
                            payload=dict(item),
                        )
                    )
                if report_markdown is not None:
                    report = await session.scalar(
                        select(ReviewReportRow).where(ReviewReportRow.run_id == run_id)
                    )
                    if report is None:
                        report = ReviewReportRow(run_id=run_id, markdown=report_markdown)
                        session.add(report)
                    report.markdown = report_markdown
                    report.findings = findings
                    report.coverage = coverage
                await session.execute(
                    update(ReviewRunRow)
                    .where(ReviewRunRow.id == run_id)
                    .values(
                        status=status,
                        phase="cleanup",
                        error=error,
                        summary=summary,
                        usage=dict(usage or {}),
                        finished_at=_utcnow(),
                    )
                )

    async def get_report(self, run_id: str) -> ReviewReportRecord | None:
        async with self._db.session() as session:
            row = await session.scalar(
                select(ReviewReportRow).where(ReviewReportRow.run_id == run_id)
            )
            if row is None:
                return None
            return ReviewReportRecord(
                run_id=row.run_id,
                markdown=row.markdown,
                findings=tuple(row.findings or ()),
                coverage=dict(row.coverage or {}),
                created_at=row.created_at,
            )

    async def mark_interrupted(self) -> int:
        """Normalize runs left ``running``/``queued`` by a previous process."""
        async with self._db.session() as session:
            async with session.begin():
                result = await session.execute(
                    update(ReviewRunRow)
                    .where(ReviewRunRow.status.in_(("running", "queued")))
                    .values(
                        status="interrupted",
                        phase="cleanup",
                        error="process restarted before the run finished",
                        finished_at=_utcnow(),
                    )
                )
                return int(result.rowcount or 0)

    async def delete(self, run_id: str) -> None:
        """Remove a run row outright.

        Used by admission when a run was registered but never started: the row
        is deleted instead of being left as a zombie ``running`` entry whose
        ``request_id`` would block every retry.
        """
        async with self._db.session() as session:
            async with session.begin():
                await session.execute(
                    delete(ReviewRunRow).where(ReviewRunRow.id == run_id)
                )


def _pull_request_record(row: PullRequestRow) -> PullRequestRecord:
    return PullRequestRecord(
        id=row.id,
        repository_id=row.repository_id,
        number=row.number,
        title=row.title,
        state=row.state,
        draft=row.draft,
        author=row.author,
        base_ref=row.base_ref,
        head_ref=row.head_ref,
        head_sha=row.head_sha,
        base_sha=row.base_sha,
        html_url=row.html_url,
        updated_at=row.updated_at,
    )


def _run_record(row: ReviewRunRow) -> ReviewRunRecord:
    return ReviewRunRecord(
        id=row.id,
        session_id=row.session_id,
        repository_id=row.repository_id,
        pull_request_id=row.pull_request_id,
        status=row.status,
        phase=row.phase,
        request_id=row.request_id,
        base_sha=row.base_sha,
        head_sha=row.head_sha,
        merge_base_sha=row.merge_base_sha,
        snapshot_id=row.snapshot_id,
        error=row.error,
        summary=row.summary,
        usage=dict(row.usage or {}),
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class Storage:
    """Bundle of repositories sharing one engine."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.sessions = SessionRepository(database)
        self.repositories = RepositoryStore(database)
        self.runs = ReviewRunRepository(database)

    async def close(self) -> None:
        await self.database.dispose()

    async def startup(self) -> int:
        """One-time boot preparation: verify the schema, close orphaned runs.

        Runs left ``running``/``queued`` by a crashed process can never finish;
        they are normalized to ``interrupted`` once per process, before any new
        review can be admitted. Never call this while reviews may be running.
        """
        await self.database.verify_schema()
        return await self.runs.mark_interrupted()


__all__ = [
    "RepositoryStore",
    "ReviewRunRepository",
    "SessionRepository",
    "Storage",
]
