"""Schema, migration and DSN contract tests for the PostgreSQL layer.

These tests do not require a live server: they prove the ORM compiles to
PostgreSQL DDL, the shipped revision matches ``SCHEMA_HEAD``, and the DSN is
normalized consistently. Real database behaviour is covered by
``test_storage_integration.py``, which skips unless a test DSN is provided.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from nanoreview.storage.database import (
    MIGRATIONS_DIR,
    SCHEMA_HEAD,
    DatabaseNotConfiguredError,
    resolve_database_url,
)
from nanoreview.storage.models import Base

_EXPECTED_TABLES = {
    "sessions",
    "messages",
    "repositories",
    "pull_requests",
    "review_runs",
    "review_snapshots",
    "review_evidence",
    "reviewer_results",
    "review_findings",
    "review_reports",
}


def test_models_cover_every_contracted_table() -> None:
    assert set(Base.metadata.tables) == _EXPECTED_TABLES


_JSONB_TABLES = _EXPECTED_TABLES - {"repositories", "pull_requests"}


def test_models_compile_to_postgresql_ddl() -> None:
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table.name}" in ddl
        if table.name in _JSONB_TABLES:
            assert "JSONB" in ddl


def test_shipped_revision_matches_schema_head() -> None:
    path = MIGRATIONS_DIR / "versions" / f"{SCHEMA_HEAD}.py"
    assert path.is_file()
    spec = importlib.util.spec_from_file_location("_migration_initial", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == SCHEMA_HEAD
    assert module.down_revision is None
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_resolve_database_url_normalizes_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NANOREVIEW_DATABASE_URL", raising=False)

    assert resolve_database_url("postgres://u:p@host/db") == (
        "postgresql+asyncpg://u:p@host/db"
    )
    assert resolve_database_url("postgresql://u:p@host/db") == (
        "postgresql+asyncpg://u:p@host/db"
    )
    assert resolve_database_url("postgresql+asyncpg://u:p@host/db") == (
        "postgresql+asyncpg://u:p@host/db"
    )


def test_env_overrides_configured_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOREVIEW_DATABASE_URL", "postgresql://env@host/envdb")
    assert resolve_database_url("postgresql://config@host/configdb") == (
        "postgresql+asyncpg://env@host/envdb"
    )


def test_missing_dsn_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NANOREVIEW_DATABASE_URL", raising=False)
    with pytest.raises(DatabaseNotConfiguredError):
        resolve_database_url(None)
    with pytest.raises(DatabaseNotConfiguredError):
        resolve_database_url("   ")


def test_migrations_directory_contains_environment() -> None:
    assert (Path(MIGRATIONS_DIR) / "env.py").is_file()
    assert (Path(MIGRATIONS_DIR) / "script.py.mako").is_file()
