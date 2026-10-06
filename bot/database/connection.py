"""Async PostgreSQL connection pool (SQLAlchemy + asyncpg)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.config.settings import Settings


class Database:
    """Owns the engine and hands out sessions. Create once at startup, dispose at shutdown."""

    def __init__(self, url: str, pool_size: int = 5, max_overflow: int = 5):
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,  # survive a Postgres restart without failing the first query
        )
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        return cls(
            settings.database_url.get_secret_value(),
            settings.db_pool_size,
            settings.db_max_overflow,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """One transaction: commits on success, rolls back on any exception."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def ping(self) -> None:
        """Raises if the database is unreachable."""
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()
