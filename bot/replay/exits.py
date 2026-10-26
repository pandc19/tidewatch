"""Exit rules for the replay. Each takes the entry and the candles after it and says where it ends.

Intra-candle order is unknown from a candle's high and low, so every rule is conservative:

* A candle that touches the stop and the target counts as a stop (a loss).
* A stop that moves (break-even, trailing) only applies from the next candle on.
* A stop is a market-like sale: it fills at the stop, or at the open if the candle gapped through
  it, and then `slippage` worse. A take-profit is a resting limit order: it fills at its price.
* A position still open when the data ends is sold at the last close (`OPEN_AT_END`).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from bot.services.binance_client import Candle

ZERO = Decimal(0)
ONE = Decimal(1)
OPEN_AT_END = "OPEN_AT_END"


@dataclass(frozen=True)
class Costs:
    fee: Decimal  # per side, as a fraction (0.001 = 0.1%)
    slippage: Decimal  # extra loss on a stop fill


@dataclass(frozen=True)
class Exit:
    price: Decimal  # average sale price per coin bought, before fees
    bars: int  # candles after the entry up to the last sale (0 = none, the data ended)
    reason: str


def _stop_fill(stop: Decimal, candle: Candle, costs: Costs) -> Decimal:
    return min(candle.open, stop) * (ONE - costs.slippage)


def _open_at_end(entry: Decimal, future: list[Candle]) -> Exit:
    return Exit(future[-1].close if future else entry, len(future), OPEN_AT_END)


@dataclass(frozen=True)
class Fixed:
    """Take-profit and stop-loss at fixed percentages of the entry (what the bot does now)."""

    take_profit: Decimal
    stop_loss: Decimal

    @property
    def name(self) -> str:
        return f"fixed tp {self.take_profit:.1%} / sl {self.stop_loss:.1%}"

    def run(self, entry: Decimal, history: list[Candle], future: list[Candle], costs: Costs) -> Exit:
        target, stop = entry * (ONE + self.take_profit), entry * (ONE - self.stop_loss)
        for k, c in enumerate(future, 1):
            if c.low <= stop:
                return Exit(_stop_fill(stop, c, costs), k, "STOP_LOSS")
            if c.high >= target:
                return Exit(target, k, "TAKE_PROFIT")
        return _open_at_end(entry, future)


@dataclass(frozen=True)
class Atr:
    """Fixed levels sized from the pair's recent volatility: multiples of its average candle range."""

    take_profit_atr: Decimal
    stop_loss_atr: Decimal
    period: int = 14

    @property
    def name(self) -> str:
        return f"atr tp {self.take_profit_atr}x / sl {self.stop_loss_atr}x"

    def run(self, entry: Decimal, history: list[Candle], future: list[Candle], costs: Costs) -> Exit:
        recent = history[-(self.period + 1):]
        ranges = [
            max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
            for p, c in pairwise(recent)
        ]
        atr_pct = (sum(ranges, ZERO) / len(ranges)) / entry if ranges else ZERO
        if atr_pct == 0:
            return _open_at_end(entry, future)
        fixed = Fixed(self.take_profit_atr * atr_pct, self.stop_loss_atr * atr_pct)
        return fixed.run(entry, history, future, costs)


@dataclass(frozen=True)
class BreakEven:
    """Fixed levels, but once the price is up `trigger` the stop moves to the entry plus the fees."""

    take_profit: Decimal
    stop_loss: Decimal
    trigger: Decimal

    @property
    def name(self) -> str:
        return f"break-even tp {self.take_profit:.1%} / sl {self.stop_loss:.1%} / at +{self.trigger:.1%}"

    def run(self, entry: Decimal, history: list[Candle], future: list[Candle], costs: Costs) -> Exit:
        target, initial = entry * (ONE + self.take_profit), entry * (ONE - self.stop_loss)
        armed, stop = entry * (ONE + self.trigger), initial
        for k, c in enumerate(future, 1):
            if c.low <= stop:
                return Exit(_stop_fill(stop, c, costs), k, "STOP_LOSS" if stop == initial else "BREAKEVEN_STOP")
            if c.high >= target:
                return Exit(target, k, "TAKE_PROFIT")
            if c.high >= armed:
                stop = max(stop, entry * (ONE + 2 * costs.fee))
        return _open_at_end(entry, future)


@dataclass(frozen=True)
class Trailing:
    """A stop that follows the highest price by `trail`, starting `stop_loss` below the entry. No target."""

    stop_loss: Decimal
    trail: Decimal

    @property
    def name(self) -> str:
        return f"trailing sl {self.stop_loss:.1%} / trail {self.trail:.1%}"

    def run(self, entry: Decimal, history: list[Candle], future: list[Candle], costs: Costs) -> Exit:
        initial = entry * (ONE - self.stop_loss)
        stop, peak = initial, entry
        for k, c in enumerate(future, 1):
            if c.low <= stop:
                return Exit(_stop_fill(stop, c, costs), k, "STOP_LOSS" if stop == initial else "TRAILING_STOP")
            peak = max(peak, c.high)
            stop = max(stop, peak * (ONE - self.trail))
        return _open_at_end(entry, future)


@dataclass(frozen=True)
class Partial:
    """Half is sold at the target; the rest is protected at the entry plus fees and then trailed."""

    take_profit: Decimal
    stop_loss: Decimal
    trail: Decimal
    fraction: Decimal = Decimal("0.5")

    @property
    def name(self) -> str:
        return f"partial tp {self.take_profit:.1%} / sl {self.stop_loss:.1%} / trail {self.trail:.1%}"

    def run(self, entry: Decimal, history: list[Candle], future: list[Candle], costs: Costs) -> Exit:
        target, stop = entry * (ONE + self.take_profit), entry * (ONE - self.stop_loss)
        for k, c in enumerate(future, 1):
            if c.low <= stop:
                return Exit(_stop_fill(stop, c, costs), k, "STOP_LOSS")
            if c.high >= target:
                return self._trail_the_rest(entry, target, c, future[k:], k, costs)
        return _open_at_end(entry, future)

    def _trail_the_rest(
        self, entry: Decimal, target: Decimal, hit: Candle, rest: list[Candle], k: int, costs: Costs
    ) -> Exit:
        stop = entry * (ONE + 2 * costs.fee)
        peak = hit.high
        stop = max(stop, peak * (ONE - self.trail))
        for j, c in enumerate(rest, k + 1):
            if c.low <= stop:
                return self._blend(target, _stop_fill(stop, c, costs), j, "PARTIAL_THEN_STOP")
            peak = max(peak, c.high)
            stop = max(stop, peak * (ONE - self.trail))
        last = rest[-1].close if rest else hit.close
        return self._blend(target, last, k + len(rest), "PARTIAL_THEN_" + OPEN_AT_END)

    def _blend(self, target: Decimal, rest_price: Decimal, bars: int, reason: str) -> Exit:
        return Exit(self.fraction * target + (ONE - self.fraction) * rest_price, bars, reason)


ExitRule = Fixed | Atr | BreakEven | Trailing | Partial

SPEC_HELP = (
    "fixed:TP:SL  atr:TP_MULT:SL_MULT  breakeven:TP:SL:TRIGGER  trailing:SL:TRAIL  "
    "partial:TP:SL:TRAIL (percentages as fractions, 0.04 = 4%)"
)
_KINDS = {"fixed": (Fixed, 2), "atr": (Atr, 2), "breakeven": (BreakEven, 3), "trailing": (Trailing, 2), "partial": (Partial, 3)}


def parse_variant(spec: str) -> ExitRule:
    kind, *numbers = spec.split(":")
    if kind not in _KINDS or len(numbers) != _KINDS[kind][1]:
        raise ValueError(f"bad variant {spec!r}; use {SPEC_HELP}")
    try:
        values = [Decimal(n) for n in numbers]
    except ArithmeticError as exc:
        raise ValueError(f"bad number in {spec!r}") from exc
    if any(v <= 0 for v in values):
        raise ValueError(f"every number in {spec!r} must be above zero")
    return _KINDS[kind][0](*values)


DEFAULT_VARIANTS = [
    "fixed:0.04:0.02",  # what the bot does now
    "fixed:0.03:0.015",
    "fixed:0.06:0.03",
    "atr:6:3",
    "atr:8:4",
    "breakeven:0.04:0.02:0.02",
    "trailing:0.02:0.02",
    "trailing:0.02:0.03",
    "partial:0.04:0.02:0.02",
]
