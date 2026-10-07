"""Entrypoint: `python -m bot.main [--check-only]`.

Startup checks (configuration, database, every configured pair against Binance's live
filters, account lookups, strategy sanity), then the engine runs until Ctrl+C or SIGTERM.
With `--check-only` it also reconciles stored orders with the exchange and exits.

With API keys the scanner supplies entries; without them the engine only manages and
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
from bot.engine.state_machine import EntrySource, NoEntries, TradingEngine
from bot.services.binance_client import ExchangeClient, ExchangeError
from bot.services.notifications import NotificationService, build_notifier, send_test_message
from bot.services.reconciliation import reconcile
from bot.services.risk_manager import config_problems
from bot.services.scanner import Scanner

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

        free_quote = None
        if settings.has_binance_credentials:
            free_quote = await _account_checks(settings, exchange)
        else:
            log.warning("no API keys: skipping account checks")
        if not await _config_checks(settings, exchange, free_quote):
            return 1

        if check_only:
            await _check_telegram(settings)
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


async def _check_telegram(settings: Settings) -> None:
    """Sends a test message when Telegram is configured. A failure is logged, not fatal."""
    if not settings.telegram_enabled:
        log.info("telegram: not configured, skipping the test message")
        return
    problem = await send_test_message(settings)
    if problem is None:
        log.info("telegram: test message delivered")
    else:
        log.error("telegram: test message failed: %s", problem)


async def _config_checks(settings: Settings, exchange: ExchangeClient, free_quote) -> bool:
    """Refuse to start on a strategy that cannot work (for example a profit target under the fees)."""
    filters = {s: await exchange.symbol_filters(s) for s in settings.symbols}
    prices = {s: (await exchange.book_ticker(s)).ask for s in settings.symbols}
    errors, warnings = config_problems(settings, filters, prices, free_quote)
    for message in warnings:
        log.warning("config: %s", message)
    for message in errors:
        log.error("config: %s", message)
    return not errors


async def _run_engine(settings: Settings, db: Database, exchange: ExchangeClient) -> int:
    source: EntrySource
    if settings.has_binance_credentials:
        source = Scanner(settings, exchange)
        log.info(
            "scanning %s every %ss on %s candles (RSI<=%s then an EMA(%s) cross-up)%s",
            ",".join(settings.symbols), settings.scan_every_seconds, settings.scan_interval,
            settings.rsi_oversold, settings.ema_period,
            " [DRY RUN: entries are recorded, never sent]" if settings.dry_run else "",
        )
    else:
        source = NoEntries()
        log.warning("no API keys: nothing to scan with, managing existing positions only")
    alerts: NotificationService | None = None
    if settings.telegram_enabled:
        alerts = NotificationService(settings, build_notifier(settings))
        alerts.start()
    else:
        log.warning("telegram is not configured: alerts go to the log only")
    engine = TradingEngine(
        settings=settings, exchange=exchange, session_factory=db.session, entry_source=source,
        observer=alerts,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        if alerts:
            alerts.announce_started()
        await EngineRunner(engine, exchange, settings).run(stop)
        if alerts:
            alerts.announce_stopped()
    finally:
        if alerts:
            await alerts.close()  # flushes what it can, within a time limit
    return 0


async def _account_checks(settings: Settings, exchange: ExchangeClient):
    """Logs the fee rate and balance and returns the free quote balance (None if the lookup failed).

    A failure here is logged, not fatal: these endpoints can differ on the testnet.
    """
    try:
        fee = await exchange.account_fees()
        log.info("account fee rate: maker=%s taker=%s", fee.maker, fee.taker)
        free = await exchange.free_balance(settings.quote_asset)
        log.info("free %s: %s", settings.quote_asset, free)
        return free
    except Exception as exc:  # noqa: BLE001
        log.warning("account lookup failed: %s", exc)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bot.main")
    parser.add_argument("--check-only", action="store_true", help="run the startup checks, then exit")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.check_only)))


if __name__ == "__main__":
    main()
