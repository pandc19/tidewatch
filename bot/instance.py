"""One engine per database, and a heartbeat the container's health check reads.

Why: a deploy that starts the new container while the old one still runs would otherwise put two
engines on the same database, both able to place orders. The new one waits for the old to exit
(`InstanceLock.acquire`), and keeps beating the whole time, so the platform sees it as alive and
stops the old container.

The lock is a PostgreSQL advisory lock held on a connection of its own for the life of the process.
PostgreSQL releases it when that connection ends, however the process died. It is per database:
two bots on different databases are not stopped by it (reconciliation flags their orders instead).

Run as `python -m bot.instance --max-age N` it is the Docker HEALTHCHECK: exit 0 if the heartbeat
file is younger than N seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = logging.getLogger(__name__)

# Arbitrary, fixed. Advisory locks are keyed by a bigint and shared with any other user of them
# in the same database.
LOCK_KEY = 7_240_113_501
RETRY_SECONDS = 2.0
WATCH_SECONDS = 10.0


class Heartbeat:
    """A file whose modification time says the process is alive and its loop is turning."""

    def __init__(self, path: Path):
        self.path = path

    def beat(self) -> None:
        try:
            self.path.touch()
        except OSError as exc:  # a read-only disk must not stop the bot, only its health check
            log.warning("cannot write the heartbeat file %s: %s", self.path, exc)

    def age_seconds(self) -> float | None:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return None


class InstanceLock:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine
        self._conn: AsyncConnection | None = None
        self._pid: int | None = None  # the PostgreSQL backend that holds the lock
        self.lost = False

    async def _try(self) -> bool:
        if self._conn is None:
            self._conn = await self._engine.connect()
        got = (await self._conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": LOCK_KEY})).scalar_one()
        pid = (await self._conn.execute(text("select pg_backend_pid()"))).scalar_one()
        await self._conn.commit()  # the lock is session-level: committing does not release it
        if not got:
            await self.release()
            return False
        self._pid = pid
        return True

    async def acquire(self, wait_seconds: float, heartbeat: Heartbeat | None = None) -> bool:
        """True once this process is the only engine on the database; False after `wait_seconds`."""
        deadline = time.monotonic() + wait_seconds
        warned = False
        while True:
            if heartbeat:
                heartbeat.beat()
            if await self._try():
                return True
            if not warned:
                log.warning("another instance is running on this database: waiting for it to stop")
                warned = True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(RETRY_SECONDS)

    async def watch(self, stop: asyncio.Event) -> None:
        """Ends the run if the lock's connection dies (a database restart): without the lock another
        instance could start, so this one must not keep trading."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=WATCH_SECONDS)
            except TimeoutError:
                pass
            if stop.is_set() or self._conn is None:
                return
            try:
                # The lock lives exactly as long as its backend session. SQLAlchemy reconnects a
                # dropped connection silently, so "still answers" is not enough: it must be the
                # same backend process.
                pid = (await self._conn.execute(text("select pg_backend_pid()"))).scalar_one()
                await self._conn.commit()
                held = pid == self._pid
            except Exception as exc:  # noqa: BLE001
                log.error("instance lock connection failed: %s", exc)
                held = False
            if not held:
                log.error("the instance lock is lost: stopping, another instance may start")
                self.lost = True
                stop.set()
                return

    async def release(self) -> None:
        conn, self._conn, self._pid = self._conn, None, None
        if conn is not None:
            try:
                await conn.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("closing the lock connection: %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.instance", description="Heartbeat health check")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--max-age", type=float, required=True, help="seconds")
    args = parser.parse_args()
    age = Heartbeat(args.path).age_seconds()
    if age is None:
        print(f"no heartbeat file at {args.path}", file=sys.stderr)
        return 1
    if age > args.max_age:
        print(f"heartbeat is {age:.0f}s old (limit {args.max_age:.0f}s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
