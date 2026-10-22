"""Text tables for the replay."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal
from typing import TextIO

from bot.replay.simulate import Trade
from bot.replay.stats import Stats, split_at, summarize


def _pct(value: Decimal) -> str:
    return f"{value * 100:+.2f}%"


def _row(label: str, s: Stats, width: int) -> str:
    if s.trades == 0:
        return f"{label:<{width}}  {0:>6}"
    return (
        f"{label:<{width}}  {s.trades:>6}  {s.win_rate:>5.0%}  {_pct(s.mean):>8}  "
        f"{_pct(s.low):>8} .. {_pct(s.high):<8}  {s.worst_streak:>6}  {s.verdict}"
    )


HEADER = "trades   win%      mean  95% range (of the mean)     losses in a row  vs zero"


def table(title: str, results: dict[str, list[Trade]]) -> str:
    width = max(len(name) for name in results)
    lines = [title, f"{'':<{width}}  {HEADER}"]
    lines += [_row(name, summarize(trades), width) for name, trades in results.items()]
    return "\n".join(lines)


def per_pair(results: dict[str, list[Trade]]) -> str:
    lines = ["Per pair (all trades; each pair has few, so read it as colour, not proof)"]
    for name, trades in results.items():
        cells = []
        for symbol in sorted({t.symbol for t in trades}):
            s = summarize([t for t in trades if t.symbol == symbol])
            cells.append(f"{symbol} {s.trades}x {s.win_rate:.0%} {_pct(s.mean)}")
        lines.append(f"{name}: " + (", ".join(cells) or "no trades"))
    return "\n".join(lines)


def full_report(results: dict[str, list[Trade]], boundary_ms: int, *, pairs: bool, tried: int) -> str:
    tuning = {n: split_at(t, boundary_ms)[0] for n, t in results.items()}
    held_out = {n: split_at(t, boundary_ms)[1] for n, t in results.items()}
    day = datetime.fromtimestamp(boundary_ms / 1000, UTC).strftime("%Y-%m-%d")
    parts = [
        table(f"Before {day} (the period you would tune on)", tuning),
        table(f"From {day} on (held out: judge by this one)", held_out),
        (f"Variants tried: {tried}. The more you try, the likelier one looks good by luck alone; "
         "a variant is only interesting if it is above zero in BOTH periods."),
    ]
    if pairs:
        parts.insert(2, per_pair(results))
    return "\n\n".join(parts)


def write_trades(out: TextIO, results: dict[str, list[Trade]]) -> None:
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["variant", "symbol", "entry_time_utc", "entry_price", "exit_time_utc", "exit_price",
                     "reason", "candles_held", "rsi_at_entry", "net_return"])
    for name, trades in results.items():
        for t in trades:
            writer.writerow([
                name, t.symbol, _iso(t.entry_time), f"{t.entry_price:f}", _iso(t.exit_time),
                f"{t.exit_price:f}", t.reason, t.bars, f"{t.rsi:.1f}", f"{t.net_return:.6f}",
            ])


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
