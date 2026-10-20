"""The sales report in the owner's spreadsheet layout, one `Venta` row per closed trade.

Tab-separated, Spanish number format (decimal comma, `$` before money), dates in the report
time zone (Ecuador, `REPORT_TIMEZONE`). Paste the rows straight into the sheet. Columns:

    Fecha | Plataforma | Tipo | Motivo | Observaciones | Cantidad Operada |
    Precio Unitario de Compra | Precio Unitario de Venta | Monto Directo | Monto

No fees anywhere, like the sheet: `Monto Directo` is quantity x sell price and `Monto` is the
gross profit, quantity x (sell price - buy price). The CSV export (`tax_exporter`) keeps the
fees and the net profit. Deposits are not in this report.

    python -m bot.reports.sales_sheet --year 2026 [--month 10] [--header] [--out FILE]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.settings import get_settings
from bot.database.connection import Database
from bot.database.models import TaxLedger
from bot.reports.periods import Period, PeriodError, parse_period

HEADER = [
    "Fecha",
    "Plataforma",
    "Tipo",
    "Motivo",
    "Observaciones",
    "Cantidad Operada",
    "Precio Unitario de Compra",
    "Precio Unitario de Venta",
    "Monto Directo",
    "Monto",
]
PLATFORM = "Binance"
KIND = "Venta"
# --- Spanish number format ------------------------------------------------------------------


def _es(value: Decimal, places: int) -> str:
    """1234.5 -> '1.234,50' (thousands dot, decimal comma), rounded half up."""
    quantized = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    whole, _, frac = f"{abs(quantized):.{places}f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    return f"{grouped},{frac}" if places else grouped


def money(value: Decimal, places: int = 2) -> str:
    """'$15,55' or '-$0,31'."""
    text = "$" + _es(value, places)
    negative = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP) < 0
    return "-" + text if negative else text


def price(value: Decimal) -> str:
    """Four decimals like the sheet; more only if four would show a price as zero."""
    places = 4 if value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) != 0 else 8
    return money(value, places)


def quantity(value: Decimal) -> str:
    """The traded amount without trailing zeros: 0.128 -> '0,128', 71.8 -> '71,8'."""
    text = format(value.normalize(), "f")
    whole, _, frac = text.partition(".")
    return f"{int(whole):,}".replace(",", ".") + (f",{frac}" if frac else "")


# --- the rows -------------------------------------------------------------------------------


def sale_row(entry: TaxLedger, tz: ZoneInfo, quote_asset: str) -> list[str]:
    asset = entry.symbol.removesuffix(quote_asset)
    amount, buy, sell = entry.amount_operated, entry.buy_price_usd, entry.sell_price_usd
    return [
        entry.sell_timestamp.astimezone(tz).strftime("%d/%m/%Y"),
        PLATFORM,
        KIND,
        asset,
        "",
        quantity(amount),
        price(buy),
        price(sell),
        money(amount * sell),
        money(amount * (sell - buy)),
    ]


def render(entries: list[TaxLedger], tz: ZoneInfo, quote_asset: str, *, header: bool = False) -> str:
    rows = ([HEADER] if header else []) + [sale_row(e, tz, quote_asset) for e in entries]
    return "\n".join("\t".join(row) for row in rows) + ("\n" if rows else "")


async def fetch_sales(session: AsyncSession, period: Period) -> list[TaxLedger]:
    query = (
        select(TaxLedger)
        .where(TaxLedger.sell_timestamp >= period.start, TaxLedger.sell_timestamp < period.end)
        .order_by(TaxLedger.sell_timestamp, TaxLedger.id)
    )
    return list((await session.scalars(query)).all())


async def build_report(session: AsyncSession, period: Period, tz: ZoneInfo, quote_asset: str, *, header: bool = False) -> tuple[str, int]:
    """(text, number of sales)."""
    entries = await fetch_sales(session, period)
    return render(entries, tz, quote_asset, header=header), len(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.reports.sales_sheet", description=__doc__.split("\n")[0])
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--month", type=int, help="only this month (1-12) of the year")
    parser.add_argument("--header", action="store_true", help="add the header row (off by default: you paste rows)")
    parser.add_argument("--out", type=Path, help="write to this file instead of the terminal")
    args = parser.parse_args(argv)
    settings = get_settings()
    tz = ZoneInfo(settings.report_timezone)
    try:
        period = parse_period(f"{args.year}-{args.month}" if args.month else str(args.year), tz)
    except PeriodError as exc:
        parser.error(str(exc))

    async def run() -> tuple[str, int]:
        db = Database.from_settings(settings)
        try:
            async with db.session() as session:
                return await build_report(session, period, tz, settings.quote_asset, header=args.header)
        finally:
            await db.dispose()

    text, count = asyncio.run(run())
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {count} sale(s) for {period.label} to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
        print(f"{count} sale(s) for {period.label} ({settings.report_timezone})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
