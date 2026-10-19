import os

import pytest
from dotenv import dotenv_values
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.settings import get_settings, normalize_database_url
from bot.database.connection import Database


def _skip_or_fail(reason: str) -> None:
    """Skip locally; fail in CI (GitHub sets CI=true) so a missing database cannot hide the tests."""
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


def _test_database_url() -> str | None:
    """TEST_DATABASE_URL from the environment (CI) or .env (local), never DATABASE_URL."""
    return os.environ.get("TEST_DATABASE_URL") or dotenv_values(".env").get("TEST_DATABASE_URL") or None


@pytest.fixture
async def session():
    """A session on a throwaway, migrated PostgreSQL, inside a transaction that is rolled back.

    The database is TEST_DATABASE_URL, never the one the bot runs on, so a running bot or a
    rehearsal cannot change what the tests see. Migrate it first:
    `DATABASE_URL=$TEST_DATABASE_URL alembic upgrade head`. Tests are skipped when
    TEST_DATABASE_URL is unset or the database is unreachable.
    """
    raw = _test_database_url()
    if not raw:
        _skip_or_fail("TEST_DATABASE_URL is not set (see GETTING_STARTED.md, step 3)")
    url = normalize_database_url(raw)
    if url == get_settings().database_url.get_secret_value():
        pytest.fail("TEST_DATABASE_URL must not be the same database as DATABASE_URL")
    db = Database(url)
    try:
        await db.ping()
    except Exception as exc:  # noqa: BLE001  any failure to connect means skip, not fail
        await db.dispose()
        _skip_or_fail(f"PostgreSQL not reachable: {exc!r}")
    async with db.engine.connect() as conn:
        outer = await conn.begin()
        s = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        yield s
        await s.close()
        await outer.rollback()
    await db.dispose()
