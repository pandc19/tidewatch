"""Replay the bot's entry rule over history with a chosen exit rule. Pure: no network, no database.

One position at a time, like the bot (`MAX_OPEN_POSITIONS=1`): while a trade is open, signals are
ignored; when several pairs signal on the same candle, the lowest RSI wins. The entry is assumed
filled at the close of the signal candle (the bot rests a limit at the bid, which may not fill:
this flatters the result a little). Fees are charged on both sides.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from bot.replay.exits import OPEN_AT_END, Costs, ExitRule
from bot.services.binance_client import Candle
from bot.services.scanner import CANDLES, evaluate_signal

ONE = Decimal(1)


@dataclass(frozen=True)
class EntryParams:
    rsi_period: int
    rsi_oversold: Decimal
    rsi_lookback: int
    ema_period: int


@dataclass(frozen=True)
class Signal:
    symbol: str
    index: int  # of the signal candle in that pair's series
    time: int  # open time of the signal candle
    rsi: Decimal
    price: Decimal  # its close


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_time: int
    entry_price: Decimal
    exit_time: int
    exit_price: Decimal
    reason: str
    bars: int
    rsi: Decimal
    net_return: Decimal  # after fees on both sides, as a fraction of the cash spent


def net_return(entry: Decimal, exit_price: Decimal, fee: Decimal) -> Decimal:
    return exit_price * (ONE - fee) / (entry * (ONE + fee)) - ONE


def find_signals(series: dict[str, list[Candle]], params: EntryParams, *, window: int = CANDLES) -> list[Signal]:
    """Every candle on which the bot's own `evaluate_signal` fires, looking at the last `window` closes."""
    found: list[Signal] = []
    for symbol, candles in series.items():
        closes = [c.close for c in candles]
        for i in range(window - 1, len(candles)):
            rsi = evaluate_signal(
                closes[i - window + 1: i + 1], rsi_period=params.rsi_period, rsi_oversold=params.rsi_oversold,
                rsi_lookback=params.rsi_lookback, ema_period=params.ema_period,
            )
            if rsi is not None:
                found.append(Signal(symbol, i, candles[i].open_time, rsi, candles[i].close))
    return sorted(found, key=lambda s: (s.time, s.rsi))


def run(signals: list[Signal], series: dict[str, list[Candle]], rule: ExitRule, costs: Costs) -> list[Trade]:
    by_time: dict[int, list[Signal]] = defaultdict(list)
    for s in signals:
        by_time[s.time].append(s)
    trades: list[Trade] = []
    free_at = -1  # the open time of the candle in which the last trade ended
    for time in sorted(by_time):
        if time < free_at:
            continue
        pick = min(by_time[time], key=lambda s: s.rsi)
        candles = series[pick.symbol]
        future = candles[pick.index + 1:]
        exit_ = rule.run(pick.price, candles[max(0, pick.index - 30): pick.index + 1], future, costs)
        ended = future[exit_.bars - 1] if exit_.bars else candles[pick.index]
        trades.append(Trade(
            pick.symbol, pick.time, pick.price, ended.open_time, exit_.price, exit_.reason,
            exit_.bars, pick.rsi, net_return(pick.price, exit_.price, costs.fee),
        ))
        if exit_.reason.endswith(OPEN_AT_END):
            break  # the data ends with this trade still open
        free_at = ended.open_time
    return trades
