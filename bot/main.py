"""Entrypoint: `python -m bot.main`.

Runs the startup checks: configuration, database, every configured pair against
Binance's live filters, and (with API keys) a reconciliation of stored orders
with the exchange. The trading loop arrives in phase 3.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from bot.config.settings import get_settings
from bot.database.connection import Database
from bot.services.binance_client import ExchangeClient, ExchangeError
from bot.services.reconciliation import reconcile

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
    exchange: ExchangeClient | None = None
    try:
        await db.ping()
        log.info("database reachable")

        exchange = await ExchangeClient.create(settings)
        for symbol in settings.symbols:
            filters = await exchange.symbol_filters(symbol)  # raises if unknown or halted
            if not filters.oco_allowed:
                raise ExchangeError(f"{symbol} does not allow OCO orders")
        log.info("all %d pairs are tradable and allow OCO", len(settings.symbols))

        if not settings.has_binance_credentials:
            log.warning("no API keys: skipping account checks and reconciliation")
        else:
            await _account_checks(settings, exchange)
            async with db.session() as session:
                report = await reconcile(session, exchange)
            log.info(
                "reconciled: %d changes, %d orphans, %d errors%s",
                len(report.changes), len(report.orphans), len(report.errors),
                " (dry run: nothing to compare)" if report.skipped_dry_run else "",
            )
            if report.errors or report.orphans:
                log.error("reconciliation found problems; refusing to continue")
                return 1
    except Exception:
        log.exception("startup check failed")
        return 1
    finally:
        if exchange is not None:
            await exchange.close()
        await db.dispose()

    log.info("startup checks passed. No trading loop yet (phase 3), exiting")
    return 0


async def _account_checks(settings, exchange: ExchangeClient) -> None:
    """Informational: a failure here is logged, not fatal (these endpoints differ on testnet)."""
    try:
        fee = await exchange.account_fees()
        log.info("account fee rate: maker=%s taker=%s", fee.maker, fee.taker)
        log.info("free %s: %s", settings.quote_asset, await exchange.free_balance(settings.quote_asset))
    except Exception as exc:  # noqa: BLE001
        log.warning("account lookup failed: %s", exc)


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
