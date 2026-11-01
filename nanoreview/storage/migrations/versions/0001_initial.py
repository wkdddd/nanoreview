"""initial review persistence schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-31 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("session_key", sa.String(length=512), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("session_key", name="uq_sessions_session_key"),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("session_id", "seq", name="uq_messages_session_seq"),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"])

    op.create_table(
        "repositories",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="github"),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("provider", "owner", "name", name="uq_repositories_provider_owner_name"),
    )

    op.create_table(
        "pull_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "repository_id",
            sa.BigInteger(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("draft", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("author", sa.String(length=255)),
        sa.Column("base_ref", sa.String(length=255), nullable=False),
        sa.Column("head_ref", sa.String(length=255), nullable=False),
        sa.Column("head_sha", sa.String(length=64)),
        sa.Column("base_sha", sa.String(length=64)),
        sa.Column("html_url", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("repository_id", "number", name="uq_pull_requests_repository_number"),
    )
    op.create_index("ix_pull_requests_repository_id", "pull_requests", ["repository_id"])

    op.create_table(
        "review_runs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "repository_id",
            sa.BigInteger(),
            sa.ForeignKey("repositories.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "pull_request_id",
            sa.BigInteger(),
            sa.ForeignKey("pull_requests.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(length=128), unique=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("base_sha", sa.String(length=64)),
        sa.Column("head_sha", sa.String(length=64)),
        sa.Column("merge_base_sha", sa.String(length=64)),
        sa.Column("snapshot_id", sa.String(length=64)),
        sa.Column("error", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_review_runs_session_id", "review_runs", ["session_id"])
    op.create_index("ix_review_runs_repository_id", "review_runs", ["repository_id"])
    op.create_index("ix_review_runs_pull_request_id", "review_runs", ["pull_request_id"])
    op.create_index("ix_review_runs_status", "review_runs", ["status"])

    op.create_table(
        "review_snapshots",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("review_runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("base_repo", sa.String(length=512), nullable=False),
        sa.Column("head_repo", sa.String(length=512), nullable=False),
        sa.Column("base_sha", sa.String(length=64), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("merge_base_sha", sa.String(length=64), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("changed_files", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("patches", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("gaps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "review_evidence",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("review_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("path", sa.Text()),
        sa.Column("side", sa.String(length=8)),
        sa.Column("start_line", sa.Integer()),
        sa.Column("end_line", sa.Integer()),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("run_id", "evidence_id", name="uq_review_evidence_run_evidence"),
    )
    op.create_index("ix_review_evidence_run_id", "review_evidence", ["run_id"])

    op.create_table(
        "reviewer_results",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("review_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dimension", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("duplicate_reads", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_searches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("run_id", "dimension", name="uq_reviewer_results_run_dimension"),
    )
    op.create_index("ix_reviewer_results_run_id", "reviewer_results", ["run_id"])

    op.create_table(
        "review_findings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("review_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("finding_id", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("path", sa.Text()),
        sa.Column("side", sa.String(length=8)),
        sa.Column("start_line", sa.Integer()),
        sa.Column("end_line", sa.Integer()),
        sa.Column("verdict", sa.String(length=24), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("run_id", "finding_id", name="uq_review_findings_run_finding"),
    )
    op.create_index("ix_review_findings_run_id", "review_findings", ["run_id"])

    op.create_table(
        "review_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("review_runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("findings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("review_reports")
    op.drop_index("ix_review_findings_run_id", table_name="review_findings")
    op.drop_table("review_findings")
    op.drop_index("ix_reviewer_results_run_id", table_name="reviewer_results")
    op.drop_table("reviewer_results")
    op.drop_index("ix_review_evidence_run_id", table_name="review_evidence")
    op.drop_table("review_evidence")
    op.drop_table("review_snapshots")
    op.drop_index("ix_review_runs_status", table_name="review_runs")
    op.drop_index("ix_review_runs_pull_request_id", table_name="review_runs")
    op.drop_index("ix_review_runs_repository_id", table_name="review_runs")
    op.drop_index("ix_review_runs_session_id", table_name="review_runs")
    op.drop_table("review_runs")
    op.drop_index("ix_pull_requests_repository_id", table_name="pull_requests")
    op.drop_table("pull_requests")
    op.drop_table("repositories")
    op.drop_index("ix_messages_session_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("sessions")
