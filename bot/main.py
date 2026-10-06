"""Entrypoint: `python -m bot.main`.

Phase 1 only verifies configuration and database connectivity. The trading loop
arrives in phase 3.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from bot.config.settings import get_settings
from bot.database.connection import Database

log = logging.getLogger("bot")


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format=f"%(asctime)s %(levelname)s [{settings.app_name}] %(name)s: %(message)s",
    )
    log.info(
        "starting: testnet=%s dry_run=%s symbols=%s tp=%s sl=%s",
        settings.binance_testnet,
        settings.dry_run,
        ",".join(settings.symbols),
        settings.take_profit_pct,
        settings.stop_loss_pct,
    )
    if not settings.binance_testnet and not settings.dry_run:
        log.warning("LIVE MODE: real orders will be sent to the real Binance account")

    db = Database.from_settings(settings)
    try:
        await db.ping()
        log.info("database reachable")
    except Exception:
        log.exception("database unreachable")
        return 1
    finally:
        await db.dispose()

    log.info("phase 1 foundation only: no trading logic yet, exiting")
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
