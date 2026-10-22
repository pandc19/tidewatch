"""Replay the entry rule over history with different exit rules. Reads public data, trades nothing.

    .venv/bin/python -m bot.replay --days 250
    .venv/bin/python -m bot.replay --variant fixed:0.04:0.02 --variant trailing:0.02:0.03 --per-pair

No database and no API key. The first run downloads the candles from Binance's public market
data (mainnet, never the testnet: its prices are synthetic) into `data/candles/`; later runs
read the cache. The entry settings default to your `.env` (RSI_OVERSOLD and the rest). See
docs/guides/replay.md for how to read the output. This is research, not advice.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

from binance import AsyncClient

from bot.config.settings import get_settings
from bot.replay import report
from bot.replay.candles import INTERVAL_MS, history
from bot.replay.exits import DEFAULT_VARIANTS, SPEC_HELP, Costs, parse_variant
from bot.replay.simulate import EntryParams, find_signals, run
from bot.services.binance_client import ExchangeClient

DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT"


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def _load(symbols: list[str], interval: str, days: int, cache_dir: Path, refresh: bool) -> dict:
    client = await AsyncClient.create(testnet=False)  # public data only, no keys
    exchange = ExchangeClient(client, dry_run=True, has_credentials=False)
    try:
        series = {}
        for symbol in symbols:
            series[symbol] = await history(exchange, symbol, interval, days, cache_dir, refresh=refresh)
            _log(f"{symbol}: {len(series[symbol])} candles")
        return series
    finally:
        await exchange.close()


def main(argv: list[str] | None = None) -> int:
    s = get_settings()
    p = argparse.ArgumentParser(prog="python -m bot.replay", description=__doc__.split("\n")[0])
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--days", type=int, default=250)
    p.add_argument("--interval", default=s.scan_interval, choices=sorted(INTERVAL_MS, key=INTERVAL_MS.get))
    p.add_argument("--variant", action="append", help=f"an exit rule, repeatable ({SPEC_HELP}); default: {len(DEFAULT_VARIANTS)} built-in")
    p.add_argument("--rsi-oversold", type=Decimal, default=s.rsi_oversold)
    p.add_argument("--rsi-period", type=int, default=s.rsi_period)
    p.add_argument("--rsi-lookback", type=int, default=s.rsi_lookback)
    p.add_argument("--ema-period", type=int, default=s.ema_period)
    p.add_argument("--fee", type=Decimal, default=Decimal("0.001"), help="per side (default 0.1%%)")
    p.add_argument("--slippage", type=Decimal, default=Decimal("0.0005"), help="extra loss on stop fills (default 0.05%%)")
    p.add_argument("--holdout", type=Decimal, default=Decimal("0.3"), help="share of the period kept aside (default 0.3)")
    p.add_argument("--per-pair", action="store_true")
    p.add_argument("--trades-csv", type=Path, help="write every simulated trade to this file")
    p.add_argument("--cache-dir", type=Path, default=Path("data/candles"))
    p.add_argument("--refresh", action="store_true", help="download everything again")
    args = p.parse_args(argv)
    if not Decimal(0) < args.holdout < 1:
        p.error("--holdout must be between 0 and 1")
    try:
        rules = [parse_variant(v) for v in (args.variant or DEFAULT_VARIANTS)]
    except ValueError as exc:
        p.error(str(exc))
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]

    series = asyncio.run(_load(symbols, args.interval, args.days, args.cache_dir, args.refresh))
    series = {k: v for k, v in series.items() if v}
    if not series:
        p.error("no candles")
    params = EntryParams(args.rsi_period, args.rsi_oversold, args.rsi_lookback, args.ema_period)
    _log(f"entry: RSI({params.rsi_period}) <= {params.rsi_oversold} within {params.rsi_lookback} candles, "
         f"then a cross up through EMA({params.ema_period}) on {args.interval} candles")
    _log("finding signals (a minute or so for a long history)...")
    signals = find_signals(series, params)
    _log(f"{len(signals)} signal candle(s) across {len(series)} pair(s)")

    costs = Costs(args.fee, args.slippage)
    results = {rule.name: run(signals, series, rule, costs) for rule in rules}
    first = min(c[0].open_time for c in series.values())
    last = max(c[-1].open_time for c in series.values())
    boundary = first + int((last - first) * (1 - args.holdout))
    print(report.full_report(results, boundary, pairs=args.per_pair, tried=len(rules)))
    if args.trades_csv:
        args.trades_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.trades_csv.open("w", newline="", encoding="utf-8") as fh:
            report.write_trades(fh, results)
        _log(f"wrote the simulated trades to {args.trades_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
