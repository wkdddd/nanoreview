"""Async PostgreSQL engine, schema verification and migrations.

The database is the single persistence boundary for sessions, review runs,
snapshots and reports. ``schema`` is created and upgraded only by the Alembic
migrations in :mod:`nanoreview.storage.migrations`; this module never calls
``metadata.create_all``, so a missing or stale schema fails loudly at startup
instead of being silently patched at runtime.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

#: Revision expected by this build. Bump together with a new alembic revision.
SCHEMA_HEAD = "0001_initial"

_DATABASE_URL_ENV = "NANOREVIEW_DATABASE_URL"
_DEFAULT_DSN = "postgresql+asyncpg://nanoreview:nanoreview@127.0.0.1:5432/nanoreview"


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when a database-backed path runs without a configured DSN."""


class SchemaNotMigratedError(RuntimeError):
    """Raised when the connected database is missing or behind the expected schema."""


def resolve_database_url(configured: str | None) -> str:
    """Return the effective DSN, letting the server-side env override win."""
    url = (os.environ.get(_DATABASE_URL_ENV) or configured or "").strip()
    if not url:
        raise DatabaseNotConfiguredError(
            f"database.url is not configured; set it in config.json or {_DATABASE_URL_ENV}."
        )
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


class Database:
    """Owns the async engine and session factory for one process."""

    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 5) -> None:
        self.url = url
        self._engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            pool_size=pool_size,
            max_overflow=pool_size,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @classmethod
    def from_config(cls, config: Any) -> "Database":
        database = getattr(config, "database", None)
        url = resolve_database_url(getattr(database, "url", None))
        return cls(
            url,
            echo=bool(getattr(database, "echo", False)),
            pool_size=int(getattr(database, "pool_size", 5)),
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def session(self) -> AsyncSession:
        """Create a session. Concurrent review tasks never share one."""
        return self._session_factory()

    async def verify_schema(self) -> None:
        """Fail fast when migrations have not been applied to this database."""
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(text("SELECT version_num FROM alembic_version"))
                revision = result.scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 - driver errors vary by deployment
            raise SchemaNotMigratedError(
                "cannot read alembic_version; run the database migrations before starting "
                f"nanoreview ({exc.__class__.__name__}: {exc})"
            ) from exc
        if revision != SCHEMA_HEAD:
            raise SchemaNotMigratedError(
                f"database schema is at {revision!r}, expected {SCHEMA_HEAD!r}; "
                "run `nanoreview db upgrade` before starting"
            )
        logger.info("storage.schema.verified revision={}", revision)

    async def upgrade(self) -> None:
        """Apply Alembic migrations in a worker thread (Alembic is sync)."""
        await asyncio.to_thread(_upgrade_sync, self.url)
        logger.info("storage.schema.upgraded revision={}", SCHEMA_HEAD)

    async def dispose(self) -> None:
        await self._engine.dispose()


def _upgrade_sync(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


__all__ = [
    "Database",
    "DatabaseNotConfiguredError",
    "MIGRATIONS_DIR",
    "SCHEMA_HEAD",
    "SchemaNotMigratedError",
    "resolve_database_url",
]
