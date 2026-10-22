"""Summaries of a list of replayed trades, with the uncertainty shown."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bot.replay.simulate import Trade

ZERO = Decimal(0)
Z95 = Decimal("1.96")
MIN_TRADES = 30  # below this the range is too wide to say anything


@dataclass(frozen=True)
class Stats:
    trades: int
    wins: int
    mean: Decimal  # net return per trade, as a fraction
    stderr: Decimal  # standard error of that mean
    worst_streak: int  # most losing trades in a row

    @property
    def win_rate(self) -> Decimal:
        return Decimal(self.wins) / self.trades if self.trades else ZERO

    @property
    def low(self) -> Decimal:
        return self.mean - Z95 * self.stderr

    @property
    def high(self) -> Decimal:
        return self.mean + Z95 * self.stderr

    @property
    def verdict(self) -> str:
        if self.trades < MIN_TRADES:
            return "too few trades"
        if self.low > 0:
            return "above zero"
        if self.high < 0:
            return "below zero"
        return "not distinguishable from zero"


def summarize(trades: list[Trade]) -> Stats:
    n = len(trades)
    if n == 0:
        return Stats(0, 0, ZERO, ZERO, 0)
    returns = [t.net_return for t in trades]
    mean = sum(returns, ZERO) / n
    variance = sum(((r - mean) ** 2 for r in returns), ZERO) / (n - 1) if n > 1 else ZERO
    streak = worst = 0
    for r in returns:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    return Stats(n, sum(1 for r in returns if r > 0), mean, (variance / n).sqrt(), worst)


def split_at(trades: list[Trade], boundary_ms: int) -> tuple[list[Trade], list[Trade]]:
    """(entered before the boundary, entered from it on)."""
    return [t for t in trades if t.entry_time < boundary_ms], [t for t in trades if t.entry_time >= boundary_ms]
