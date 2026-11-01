"""SQLAlchemy models for NanoReview's PostgreSQL persistence.

Design rules from the current plan:

- Relational columns carry identity, foreign keys, status, timestamps and
  lookup keys; complex payloads live in JSONB with a ``schema_version`` so a
  shape change is explicit instead of silently reinterpreted.
- ORM rows are private to this package. Callers receive the dataclasses from
  :mod:`nanoreview.storage.records`, never live ORM instances bound to a
  session that closed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    metadata_payload: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["MessageRow"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_messages_session_seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    session: Mapped[SessionRow] = relationship(back_populates="messages")


class RepositoryRow(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("provider", "owner", "name", name="uq_repositories_provider_owner_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="github")
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PullRequestRow(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repository_id", "number", name="uq_pull_requests_repository_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    author: Mapped[str | None] = mapped_column(String(255))
    base_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    head_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    head_sha: Mapped[str | None] = mapped_column(String(64))
    base_sha: Mapped[str | None] = mapped_column(String(64))
    html_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReviewRunRow(Base):
    __tablename__ = "review_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    pull_request_id: Mapped[int] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    request_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    base_sha: Mapped[str | None] = mapped_column(String(64))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    merge_base_sha: Mapped[str | None] = mapped_column(String(64))
    snapshot_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewSnapshotRow(Base):
    __tablename__ = "review_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    base_repo: Mapped[str] = mapped_column(String(512), nullable=False)
    head_repo: Mapped[str] = mapped_column(String(512), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    merge_base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_payload: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    changed_files: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    patches: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    gaps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReviewEvidenceRow(Base):
    __tablename__ = "review_evidence"
    __table_args__ = (
        UniqueConstraint("run_id", "evidence_id", name="uq_review_evidence_run_evidence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str | None] = mapped_column(Text)
    side: Mapped[str | None] = mapped_column(String(8))
    start_line: Mapped[int | None] = mapped_column(Integer)
    end_line: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReviewerResultRow(Base):
    __tablename__ = "reviewer_results"
    __table_args__ = (
        UniqueConstraint("run_id", "dimension", name="uq_reviewer_results_run_dimension"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dimension: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    duplicate_reads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_searches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReviewFindingRow(Base):
    __tablename__ = "review_findings"
    __table_args__ = (
        UniqueConstraint("run_id", "finding_id", name="uq_review_findings_run_finding"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    finding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str | None] = mapped_column(Text)
    side: Mapped[str | None] = mapped_column(String(8))
    start_line: Mapped[int | None] = mapped_column(Integer)
    end_line: Mapped[int | None] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReviewReportRow(Base):
    __tablename__ = "review_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    findings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "Base",
    "MessageRow",
    "PullRequestRow",
    "RepositoryRow",
    "ReviewEvidenceRow",
    "ReviewFindingRow",
    "ReviewReportRow",
    "ReviewRunRow",
    "ReviewSnapshotRow",
    "ReviewerResultRow",
    "SessionRow",
]
