"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hoursx.db.models import Base


class Database:
    """Owns one async engine and hands out sessions.

    Constructed once per process; injected everywhere else. Tests construct their
    own instance against SQLite in-memory/file databases.
    """

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, future=True)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        """Create every table. Dev/test bootstrap; production uses migrations."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """One unit-of-work session; commits on success, rolls back on error."""
        async with self._sessions() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()


_database: Database | None = None


def get_database(url: str | None = None) -> Database:
    """Return the process-wide database, creating it on first use."""
    global _database
    if _database is None:
        if url is None:
            from hoursx.config import get_settings

            url = get_settings().database_url
        _database = Database(url)
    return _database


def set_database(db: Database | None) -> None:
    """Override the process-wide database (used by tests and app startup)."""
    global _database
    _database = db
