"""One engine per database, and the heartbeat the health check reads."""

import asyncio
import os
import time

import pytest
from sqlalchemy import text

from bot.config.settings import normalize_database_url
from bot.database.connection import Database
from bot.instance import Heartbeat, InstanceLock, main
from tests.conftest import _skip_or_fail, _test_database_url


@pytest.fixture
async def db():
    raw = _test_database_url()
    if not raw:
        _skip_or_fail("TEST_DATABASE_URL is not set")
    database = Database(normalize_database_url(raw))
    try:
        await database.ping()
    except Exception as exc:  # noqa: BLE001
        await database.dispose()
        _skip_or_fail(f"PostgreSQL not reachable: {exc!r}")
    yield database
    await database.dispose()


# --- the lock (real PostgreSQL: two connections) ------------------------------------------


async def test_a_second_instance_cannot_take_the_lock(db):
    first, second = InstanceLock(db.engine), InstanceLock(db.engine)
    try:
        assert await first.acquire(wait_seconds=0)
        assert not await second.acquire(wait_seconds=0)
    finally:
        await first.release()
        await second.release()


async def test_the_lock_is_free_once_the_first_instance_is_gone(db):
    first, second = InstanceLock(db.engine), InstanceLock(db.engine)
    try:
        assert await first.acquire(wait_seconds=0)
        await first.release()
        assert await second.acquire(wait_seconds=0)
    finally:
        await first.release()
        await second.release()


async def test_a_waiting_instance_gets_the_lock_when_the_first_stops(db, monkeypatch):
    monkeypatch.setattr("bot.instance.RETRY_SECONDS", 0.05)
    first, second = InstanceLock(db.engine), InstanceLock(db.engine)
    try:
        assert await first.acquire(wait_seconds=0)
        waiter = asyncio.ensure_future(second.acquire(wait_seconds=5))
        await asyncio.sleep(0.2)
        assert not waiter.done()  # still waiting while the first runs
        await first.release()
        assert await asyncio.wait_for(waiter, timeout=3)
    finally:
        await first.release()
        await second.release()


async def test_waiting_gives_up_after_the_limit(db, monkeypatch):
    monkeypatch.setattr("bot.instance.RETRY_SECONDS", 0.05)
    first, second = InstanceLock(db.engine), InstanceLock(db.engine)
    try:
        assert await first.acquire(wait_seconds=0)
        assert not await second.acquire(wait_seconds=0.3)
    finally:
        await first.release()
        await second.release()


async def test_the_waiting_instance_keeps_beating(db, tmp_path, monkeypatch):
    monkeypatch.setattr("bot.instance.RETRY_SECONDS", 0.05)
    beat = Heartbeat(tmp_path / "hb")
    first, second = InstanceLock(db.engine), InstanceLock(db.engine)
    try:
        assert await first.acquire(wait_seconds=0)
        assert not await second.acquire(wait_seconds=0.3, heartbeat=beat)
        assert beat.age_seconds() is not None and beat.age_seconds() < 5
    finally:
        await first.release()
        await second.release()


@pytest.mark.parametrize("how", ["server_kills_it", "silent_reconnect"])
async def test_losing_the_lock_stops_the_run(db, monkeypatch, how):
    monkeypatch.setattr("bot.instance.WATCH_SECONDS", 0.05)
    lock = InstanceLock(db.engine)
    assert await lock.acquire(wait_seconds=0)
    stop = asyncio.Event()
    watcher = asyncio.ensure_future(lock.watch(stop))
    await asyncio.sleep(0.2)
    assert not stop.is_set() and not lock.lost  # healthy: the watcher leaves it alone
    if how == "server_kills_it":  # as in a database restart or a dropped network
        async with db.engine.connect() as other:
            await other.execute(text("select pg_terminate_backend(:p)"), {"p": lock._pid})
    else:  # the driver reconnects by itself, on a session that does not hold the lock
        await lock._conn.invalidate()
    await asyncio.wait_for(stop.wait(), timeout=3)
    assert lock.lost
    await watcher
    await lock.release()


# --- the heartbeat -------------------------------------------------------------------------


def test_no_heartbeat_file_means_unhealthy(tmp_path, capsys):
    assert Heartbeat(tmp_path / "missing").age_seconds() is None
    assert main_with(tmp_path / "missing", 60) == 1


def test_a_fresh_heartbeat_is_healthy_and_a_stale_one_is_not(tmp_path):
    path = tmp_path / "hb"
    Heartbeat(path).beat()
    assert main_with(path, 60) == 0
    old = time.time() - 600
    os.utime(path, (old, old))
    assert main_with(path, 60) == 1


def test_an_unwritable_heartbeat_does_not_raise(tmp_path):
    Heartbeat(tmp_path / "no-such-dir" / "hb").beat()  # logged, not raised


def main_with(path, max_age) -> int:
    import sys

    argv, sys.argv = sys.argv, ["bot.instance", "--path", str(path), "--max-age", str(max_age)]
    try:
        return main()
    finally:
        sys.argv = argv
