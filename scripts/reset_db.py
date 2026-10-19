"""Empty a local development database: delete every row, keep the schema.

    .venv/bin/python scripts/reset_db.py local      # tidewatch_local  (demo runs)
    .venv/bin/python scripts/reset_db.py testnet    # tidewatch_testnet (the long rehearsal)
    .venv/bin/python scripts/reset_db.py test       # tidewatch_test   (pytest)
    ... --yes                                       # skip the typed confirmation

For development on this machine only. It refuses to run unless all of these hold:
  * the target is one of the three names above (never "whatever DATABASE_URL says");
  * the server in DATABASE_URL is localhost;
  * the bot is not running (logs/bot.pid).
Host and credentials come from DATABASE_URL in .env; only the database name is swapped.

It empties the database, not the exchange. Orders the bot placed on the testnet stay there, so
a position wiped here can leave an orphan OCO on the exchange (cancel it by hand first).
The kill-switch pause and the tax ledger are wiped with everything else (`bot_state` keeps
its single seeded row, reset to defaults).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from bot.config.settings import get_settings

TARGETS = {
    "local": "tidewatch_local",
    "testnet": "tidewatch_testnet",
    "test": "tidewatch_test",
}
KEEP = {"alembic_version"}  # the schema's migration marker survives, so no re-migrate is needed
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def bot_is_running() -> bool:
    pid_file = ROOT / "logs" / "bot.pid"
    try:
        os.kill(int(pid_file.read_text().strip()), 0)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


async def reset(target: str, assume_yes: bool) -> int:
    db_name = TARGETS[target]
    url = make_url(get_settings().database_url.get_secret_value())
    if (url.host or "") not in LOCAL_HOSTS:
        print(f"Refusing: DATABASE_URL points at '{url.host}', not this machine.")
        return 1
    if bot_is_running():
        print("Refusing: the bot is running. Stop it first:  ./scripts/local.sh stop")
        return 1

    engine = create_async_engine(url.set(database=db_name))
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("select tablename from pg_tables where schemaname = 'public' order by 1")
            )
            tables = [t for (t,) in rows if t not in KEEP]
            if not tables:
                print(f"{db_name}: no tables. Run `alembic upgrade head` against it first.")
                return 1
            counts = {}
            for t in tables:
                counts[t] = (await conn.execute(text(f'select count(*) from "{t}"'))).scalar_one()

            print(f"Database {db_name} on {url.host}:{url.port or 5432}")
            for t in tables:
                print(f"  {t:<20} {counts[t]:>6} rows")
            if not any(counts.values()):
                print("Already empty.")
                return 0
            if not assume_yes:
                answer = input(f"Delete all of it? Type the database name ({db_name}) to confirm: ")
                if answer.strip() != db_name:
                    print("Cancelled; nothing deleted.")
                    return 1

            names = ", ".join(f'"{t}"' for t in tables)
            await conn.execute(text(f"truncate table {names} restart identity cascade"))
            # Migration 0001 seeds the single bot_state row (the kill-switch pause); put it back.
            await conn.execute(text("insert into bot_state (id) values (1)"))
            await conn.commit()
        print(f"Emptied {len(tables)} tables in {db_name}. The schema is untouched.")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Empty a local development database.")
    parser.add_argument("target", choices=sorted(TARGETS), help="which database")
    parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    args = parser.parse_args()
    return asyncio.run(reset(args.target, args.yes))


if __name__ == "__main__":
    raise SystemExit(main())
