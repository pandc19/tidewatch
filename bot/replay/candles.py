"""Candle history for the replay: downloaded once from Binance's public market data, cached as CSV.

Only finished candles are kept. No API key is used or needed (klines are public).
"""

from __future__ import annotations

import csv
import time
from decimal import Decimal
from pathlib import Path

from bot.services.binance_client import Candle, ExchangeClient

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}
DAY_MS = 86_400_000
FIELDS = ["open_time", "open", "high", "low", "close", "close_time"]


def cache_path(cache_dir: Path, symbol: str, interval: str) -> Path:
    return cache_dir / f"{symbol}_{interval}.csv"


def load(path: Path) -> list[Candle]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [
            Candle(int(r["open_time"]), Decimal(r["open"]), Decimal(r["high"]), Decimal(r["low"]),
                   Decimal(r["close"]), int(r["close_time"]))
            for r in csv.DictReader(fh)
        ]


def save(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(FIELDS)
        for c in candles:
            writer.writerow([c.open_time, f"{c.open:f}", f"{c.high:f}", f"{c.low:f}", f"{c.close:f}", c.close_time])


async def _download(exchange: ExchangeClient, symbol: str, interval: str, start_ms: int, now_ms: int) -> list[Candle]:
    out: list[Candle] = []
    cursor = start_ms
    while cursor < now_ms:
        page = await exchange.candles(symbol, interval, start_ms=cursor, end_ms=now_ms)
        if not page:
            break
        out.extend(c for c in page if c.close_time < now_ms)  # the candle still forming is not history
        cursor = page[-1].open_time + 1
        if len(page) < 1000:
            break
    return out


async def history(
    exchange: ExchangeClient, symbol: str, interval: str, days: int, cache_dir: Path,
    *, refresh: bool = False, now_ms: int | None = None,
) -> list[Candle]:
    """Finished candles of the last `days` days, oldest first. Only the missing tail is downloaded."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    step = INTERVAL_MS[interval]
    start_ms = now_ms - days * DAY_MS
    path = cache_path(cache_dir, symbol, interval)
    cached = [] if refresh else load(path)
    if cached and cached[0].open_time <= start_ms + step:
        fresh = await _download(exchange, symbol, interval, cached[-1].open_time + 1, now_ms)
        merged = {c.open_time: c for c in [*cached, *fresh]}
    else:  # nothing cached, or the cache does not reach back far enough
        merged = {c.open_time: c for c in await _download(exchange, symbol, interval, start_ms, now_ms)}
    candles = [merged[k] for k in sorted(merged)]
    if candles:
        save(path, candles)
    return [c for c in candles if c.open_time >= start_ms]
