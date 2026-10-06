import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.settings import get_settings
from bot.database.connection import Database


@pytest.fixture
async def session():
    """A session on the real, migrated PostgreSQL, inside a transaction that is rolled back.

    The database stays empty after every test. Tests are skipped when no database is
    reachable at DATABASE_URL.
    """
    db = Database.from_settings(get_settings())
    try:
        await db.ping()
    except Exception as exc:  # noqa: BLE001  any failure to connect means skip, not fail
        await db.dispose()
        pytest.skip(f"PostgreSQL not reachable: {exc!r}")
    async with db.engine.connect() as conn:
        outer = await conn.begin()
        s = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        yield s
        await s.close()
        await outer.rollback()
    await db.dispose()
