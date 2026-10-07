"""Technical indicators on closing prices. Pure `Decimal` maths, no I/O.

Both match the standard definitions (checked against StockCharts' published worked
examples in tests/test_indicators.py):

* RSI uses Wilder's smoothing: the first average is the plain mean of the first
  `period` price changes, then ``avg = (prev_avg * (period - 1) + current) / period``.
* EMA is seeded with the simple average of the first `period` closes, then
  ``ema = close * k + prev * (1 - k)`` with ``k = 2 / (period + 1)``.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

ZERO = Decimal(0)
HUNDRED = Decimal(100)


def rsi_series(closes: list[Decimal], period: int = 14) -> list[Decimal]:
    """RSI for every close from index `period` onward (so `len(closes) - period` values)."""
    if period < 2:
        raise ValueError("period must be at least 2")
    if len(closes) <= period:
        return []
    changes = [b - a for a, b in pairwise(closes)]
    gains = [max(c, ZERO) for c in changes]
    losses = [max(-c, ZERO) for c in changes]
    avg_gain = sum(gains[:period], ZERO) / period
    avg_loss = sum(losses[:period], ZERO) / period
    out = [_rsi(avg_gain, avg_loss)]
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out.append(_rsi(avg_gain, avg_loss))
    return out


def _rsi(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        return HUNDRED if avg_gain > 0 else Decimal(50)  # no movement at all: neutral
    return HUNDRED - HUNDRED / (1 + avg_gain / avg_loss)


def ema_series(closes: list[Decimal], period: int) -> list[Decimal]:
    """EMA for every close from index `period - 1` onward (so `len(closes) - period + 1` values)."""
    if period < 2:
        raise ValueError("period must be at least 2")
    if len(closes) < period:
        return []
    k = Decimal(2) / (period + 1)
    ema = sum(closes[:period], ZERO) / period
    out = [ema]
    for close in closes[period:]:
        ema = close * k + ema * (1 - k)
        out.append(ema)
    return out
