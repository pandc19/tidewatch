"""Entrypoint: `python -m bot.main [--check-only]`.

Startup checks (configuration, database, every configured pair against Binance's live
filters, account lookups), then the engine runs until Ctrl+C or SIGTERM. With
`--check-only` it also reconciles stored orders with the exchange and exits.

Until the scanner exists (phase 4) the engine opens no new positions: it manages and
recovers whatever is already in the database.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from bot.config.settings import Settings, get_settings
from bot.database.connection import Database
from bot.engine.runner import EngineRunner
from bot.engine.state_machine import TradingEngine
from bot.services.binance_client import ExchangeClient, ExchangeError
from bot.services.reconciliation import reconcile

log = logging.getLogger("bot")


async def run(check_only: bool) -> int:
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

        if settings.has_binance_credentials:
            await _account_checks(settings, exchange)
        else:
            log.warning("no API keys: skipping account checks")

        if check_only:
            return await _check_reconciliation(settings, db, exchange)
        return await _run_engine(settings, db, exchange)
    except Exception:
        log.exception("startup check failed")
        return 1
    finally:
        if exchange is not None:
            await exchange.close()
        await db.dispose()


async def _check_reconciliation(settings: Settings, db: Database, exchange: ExchangeClient) -> int:
    if not settings.has_binance_credentials:
        log.info("startup checks passed (no keys: reconciliation skipped)")
        return 0
    async with db.session() as session:
        report = await reconcile(session, exchange)
    log.info(
        "reconciled: %d changes, %d orphans, %d errors%s",
        len(report.changes), len(report.orphans), len(report.errors),
        " (dry run: nothing to compare)" if report.skipped_dry_run else "",
    )
    if report.errors or report.orphans:
        log.error("reconciliation found problems")
        return 1
    log.info("startup checks passed")
    return 0


async def _run_engine(settings: Settings, db: Database, exchange: ExchangeClient) -> int:
    engine = TradingEngine(settings=settings, exchange=exchange, session_factory=db.session)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("no entry source yet (phase 4): managing existing positions only")
    await EngineRunner(engine, exchange, settings).run(stop)
    return 0


async def _account_checks(settings: Settings, exchange: ExchangeClient) -> None:
    """Informational: a failure here is logged, not fatal (these endpoints differ on testnet)."""
    try:
        fee = await exchange.account_fees()
        log.info("account fee rate: maker=%s taker=%s", fee.maker, fee.taker)
        log.info("free %s: %s", settings.quote_asset, await exchange.free_balance(settings.quote_asset))
    except Exception as exc:  # noqa: BLE001
        log.warning("account lookup failed: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bot.main")
    parser.add_argument("--check-only", action="store_true", help="run the startup checks, then exit")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.check_only)))


if __name__ == "__main__":
    main()
