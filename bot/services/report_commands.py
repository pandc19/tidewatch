"""The `/csv` and `/report` handlers: build a file from the ledger for the listener to send."""

from __future__ import annotations

import io
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config.settings import Settings
from bot.reports.sales_sheet import PeriodError, build_report, parse_period
from bot.reports.tax_exporter import export
from bot.services.commands import Document, Handler, Reply


def csv_handler(settings: Settings, sessions) -> Handler:
    """`/csv [year|all]`: the full ledger (fees, net profit) as a CSV file; the year is by Ecuador days."""

    tz = ZoneInfo(settings.report_timezone)

    async def handle(args: list[str]) -> Reply:
        word = (args[0] if args else str(datetime.now(tz).year)).lower()
        if word == "all":
            year, label = None, "all"
        elif word.isdigit() and 2000 <= int(word) <= datetime.now(tz).year + 1:
            year, label = int(word), word
        else:
            return "Usage: /csv [year|all], for example /csv 2026"
        out = io.StringIO()
        async with sessions() as session:
            summary = await export(session, out, year, tz)
        if summary.rows == 0:
            return f"No closed trades for {label}."
        caption = (
            f"Ledger {label}: {summary.rows} trade(s), net {summary.total_net_profit_usd:+.4f} USD, "
            f"fees {summary.total_fees_usd:.4f} USD. Year by {settings.report_timezone} days, timestamps in UTC."
        )
        return Document(f"tidewatch_ledger_{label}.csv", out.getvalue().encode("utf-8"), caption)

    return handle


def report_handler(settings: Settings, sessions) -> Handler:
    """`/report [year|yyyy-mm]`: the sale rows in the owner's sheet layout (no header, tab-separated)."""
    tz = ZoneInfo(settings.report_timezone)

    async def handle(args: list[str]) -> Reply:
        text = args[0] if args else str(datetime.now(tz).year)
        try:
            period = parse_period(text, tz)
        except PeriodError:
            return "Usage: /report [year|yyyy-mm], for example /report 2026 or /report 2026-10"
        async with sessions() as session:
            body, count = await build_report(session, period, tz, settings.quote_asset)
        if count == 0:
            return f"No sales for {period.label}."
        caption = f"Ventas {period.label}: {count} fila(s), fechas de {settings.report_timezone}. Pega las filas en la hoja."
        return Document(f"ventas_{period.label}.txt", body.encode("utf-8"), caption)

    return handle
