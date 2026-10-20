"""Export the tax ledger to CSV.

    python -m bot.reports.tax_exporter --year 2026 --out exports/tax_2026.csv

One row per closed trade, oldest sale first. The year of `--year` is the calendar year of the
*sale* (`sell_timestamp`) in the report time zone (`REPORT_TIMEZONE`, Ecuador by default), so a
sale on the evening of 31 December still belongs to that year. `sell_date_local` shows that day;
`sell_date_utc` and the timestamps stay in UTC. USDT is treated as USD. Numbers are plain decimals (no exponent, trailing
zeros trimmed) and timestamps are ISO 8601 in UTC, so the file opens cleanly in a spreadsheet
or an accountant's tool. This is a record of trades, not tax advice.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.precision import format_decimal
from bot.config.settings import get_settings
from bot.database.connection import Database
from bot.database.models import Position, TaxLedger
from bot.reports.periods import parse_period

COLUMNS = [
    "sell_date_utc",
    "sell_date_local",
    "symbol",
    "amount_operated",
    "buy_price_usd",
    "sell_price_usd",
    "total_buy_usd",
    "total_sell_usd",
    "fees_usd",
    "net_profit_usd",
    "buy_timestamp_utc",
    "sell_timestamp_utc",
    "holding_days",
    "exit_reason",
    "position_id",
]


@dataclass(frozen=True)
class Summary:
    rows: int
    total_net_profit_usd: Decimal
    total_fees_usd: Decimal


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(entry: TaxLedger, exit_reason: str | None, tz: ZoneInfo) -> list[str]:
    held = (entry.sell_timestamp - entry.buy_timestamp).total_seconds() / 86400
    return [
        entry.sell_timestamp.astimezone(UTC).date().isoformat(),
        entry.sell_timestamp.astimezone(tz).date().isoformat(),
        entry.symbol,
        format_decimal(entry.amount_operated),
        format_decimal(entry.buy_price_usd),
        format_decimal(entry.sell_price_usd),
        format_decimal(entry.total_buy_usd),
        format_decimal(entry.total_sell_usd),
        format_decimal(entry.fees_usd),
        format_decimal(entry.net_profit_usd),
        _iso(entry.buy_timestamp),
        _iso(entry.sell_timestamp),
        f"{held:.2f}",
        exit_reason or "",
        str(entry.position_id),
    ]


async def export(session: AsyncSession, out: TextIO, year: int | None = None, tz: tzinfo = UTC) -> Summary:
    """Write the ledger (optionally one calendar year of sales, as days in `tz`) as CSV to `out`."""
    query = (
        select(TaxLedger, Position.exit_reason)
        .join(Position, Position.id == TaxLedger.position_id)
        .order_by(TaxLedger.sell_timestamp, TaxLedger.id)
    )
    if year is not None:
        period = parse_period(str(year), tz)
        query = query.where(TaxLedger.sell_timestamp >= period.start, TaxLedger.sell_timestamp < period.end)
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    count, net, fees = 0, Decimal(0), Decimal(0)
    for entry, reason in (await session.execute(query)).all():
        writer.writerow(_row(entry, reason.value if reason else None, tz))
        count, net, fees = count + 1, net + entry.net_profit_usd, fees + entry.fees_usd
    return Summary(count, net, fees)


def default_path(year: int | None) -> Path:
    return Path("exports") / (f"tax_ledger_{year}.csv" if year else "tax_ledger.csv")


async def _run(year: int | None, out_path: Path) -> Summary:
    settings = get_settings()
    tz = ZoneInfo(settings.report_timezone)
    db = Database.from_settings(settings)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            async with db.session() as session:
                return await export(session, fh, year, tz)
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.reports.tax_exporter", description=__doc__.split("\n")[0])
    parser.add_argument("--year", type=int, help="only sales in this year, by REPORT_TIMEZONE days (default: everything)")
    parser.add_argument("--out", type=Path, help="output file (default: exports/tax_ledger[_YEAR].csv)")
    args = parser.parse_args(argv)
    if args.year is not None and not 2000 <= args.year <= datetime.now(UTC).year + 1:
        parser.error(f"--year {args.year} is not a plausible year")
    out_path = args.out or default_path(args.year)
    summary = asyncio.run(_run(args.year, out_path))
    print(
        f"wrote {summary.rows} trade(s) to {out_path}: "
        f"net profit {format_decimal(summary.total_net_profit_usd)} USD, "
        f"fees {format_decimal(summary.total_fees_usd)} USD",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
