"""Entry signal: RSI oversold, then the price crosses back up through its EMA.

This is a *starting point*, not a backtested strategy. A symbol signals when, on closed
candles only:

1. RSI was at or below `RSI_OVERSOLD` on at least one of the last `RSI_LOOKBACK` candles
   (the latest included), and
2. the latest close is above the EMA while the close before it was at or below it (the cross
   happens on exactly one candle, so a signal fires once per dip).

Of the symbols that signal and have a tight enough spread, the one with the lowest RSI wins.
The entry is a limit buy at the best bid (optionally `ENTRY_DISCOUNT_PCT` below), which rests
as a maker order.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from bot.config.settings import Settings
from bot.engine.state_machine import EntrySignal
from bot.services.binance_client import ExchangeClient
from bot.services.indicators import ema_series, rsi_series
from bot.services.risk_manager import spread_ok
from bot.services.universe import Universe

log = logging.getLogger(__name__)

# More history than the indicators strictly need: Wilder's RSI converges as it smooths.
CANDLES = 200
# How often the scanner says what it saw even when nothing signalled.
HEARTBEAT_SECONDS = 3600


def evaluate_signal(
    closes: list[Decimal], *, rsi_period: int, rsi_oversold: Decimal, rsi_lookback: int, ema_period: int
) -> Decimal | None:
    """The latest RSI if the closes show the signal, else None."""
    rsis = rsi_series(closes, rsi_period)
    emas = ema_series(closes, ema_period)
    if len(rsis) < rsi_lookback or len(emas) < 2:
        return None
    was_oversold = min(rsis[-rsi_lookback:]) <= rsi_oversold
    crossed_up = closes[-1] > emas[-1] and closes[-2] <= emas[-2]
    return rsis[-1] if was_oversold and crossed_up else None


def lowest_recent_rsi(closes: list[Decimal], *, rsi_period: int, rsi_lookback: int) -> Decimal | None:
    """The lowest RSI over the last `rsi_lookback` candles: how close the pair came to oversold."""
    rsis = rsi_series(closes, rsi_period)
    return min(rsis[-rsi_lookback:]) if rsis else None


@dataclass(frozen=True)
class Candidate:
    symbol: str
    rsi: Decimal
    price: Decimal
    spread_pct: Decimal


class Scanner:
    """An `EntrySource`: scans at most every `SCAN_EVERY_SECONDS` however often it is asked."""

    def __init__(
        self,
        settings: Settings,
        exchange: ExchangeClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        universe: Universe | None = None,
    ):
        self._s = settings
        self._exchange = exchange
        self._universe = universe or Universe(settings, exchange, clock=clock)
        self._clock = clock
        self._last_scan: float | None = None
        self._last_heartbeat: float | None = None
        self._lows: dict[str, Decimal] = {}
        self._failed = 0

    async def next_entry(self) -> EntrySignal | None:
        now = self._clock()
        if self._last_scan is not None and now - self._last_scan < self._s.scan_every_seconds:
            return None
        self._last_scan = now
        candidates = await self.scan()
        if not candidates:
            return None
        best = candidates[0]
        log.info(
            "entry signal: %s RSI %.1f, spread %.4f%% (%d candidate(s): %s)",
            best.symbol, best.rsi, best.spread_pct * 100, len(candidates),
            ", ".join(f"{c.symbol} {c.rsi:.1f}" for c in candidates),
        )
        return EntrySignal(best.symbol, best.price)

    async def scan(self) -> list[Candidate]:
        """Every symbol that signals and passes the spread filter, lowest RSI first."""
        found: list[Candidate] = []
        await self._universe.refresh()
        self._lows, self._failed = {}, 0
        for symbol in self._universe.symbols:
            try:
                candidate = await self._check(symbol)
            except Exception:
                self._failed += 1
                log.exception("scan of %s failed", symbol)
                continue
            if candidate is not None:
                found.append(candidate)
        self._heartbeat(len(self._universe.symbols), len(found))
        return sorted(found, key=lambda c: c.rsi)

    def _heartbeat(self, pairs: int, signals: int) -> None:
        """One line per `HEARTBEAT_SECONDS`, so a quiet log can be told from a stuck scanner."""
        now = self._clock()
        if self._last_heartbeat is not None and now - self._last_heartbeat < HEARTBEAT_SECONDS:
            return
        self._last_heartbeat = now
        closest = "no RSI readings"
        if self._lows:
            symbol, low = min(self._lows.items(), key=lambda kv: kv[1])
            closest = f"closest to oversold: {symbol} lowest RSI {low:.1f} of the last {self._s.rsi_lookback} candles"
        log.info("scan: %d pair(s) checked, %d unreadable, %d signal(s); %s (signal needs <= %s, then a cross up through the EMA)",
                 pairs, self._failed, signals, closest, self._s.rsi_oversold)

    async def _check(self, symbol: str) -> Candidate | None:
        s = self._s
        closes = await self._exchange.closed_candle_closes(symbol, s.scan_interval, CANDLES)
        low = lowest_recent_rsi(closes, rsi_period=s.rsi_period, rsi_lookback=s.rsi_lookback)
        if low is not None:
            self._lows[symbol] = low
        rsi = evaluate_signal(
            closes, rsi_period=s.rsi_period, rsi_oversold=s.rsi_oversold,
            rsi_lookback=s.rsi_lookback, ema_period=s.ema_period,
        )
        if rsi is None:
            return None
        ticker = await self._exchange.book_ticker(symbol)
        if not spread_ok(ticker.spread_pct, s.max_spread_pct):
            log.info("%s signals (RSI %.1f) but the spread is %.3f%% (limit %.3f%%): skipped",
                     symbol, rsi, ticker.spread_pct * 100, s.max_spread_pct * 100)
            return None
        filters = await self._exchange.symbol_filters(symbol)
        price = filters.round_price(ticker.bid * (1 - s.entry_discount_pct))
        return Candidate(symbol, rsi, price, ticker.spread_pct)
