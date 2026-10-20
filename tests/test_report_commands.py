"""/csv and /report: what they answer and what file they hand to the listener."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bot.config.settings import Settings
from bot.database.models import ExitReason, Position, PositionStatus, TaxLedger
from bot.services.commands import Document
from bot.services.report_commands import csv_handler, report_handler

D = Decimal


def factory(session):
    @asynccontextmanager
    async def sessions():
        yield session

    return sessions


def cfg(**env) -> Settings:
    return Settings(_env_file=None, **env)


async def sale(session, sold, amount="0.128", buy="118.2", sell="121.5", symbol="SOLUSDT"):
    pos = Position(symbol=symbol, status=PositionStatus.CLOSED, take_profit_pct=D("0.04"),
                   stop_loss_pct=D("0.02"), exit_reason=ExitReason.TAKE_PROFIT)
    session.add(pos)
    await session.flush()
    session.add(TaxLedger(
        position_id=pos.id, symbol=symbol, amount_operated=D(amount), buy_price_usd=D(buy),
        sell_price_usd=D(sell), total_buy_usd=D(amount) * D(buy), total_sell_usd=D(amount) * D(sell),
        fees_usd=D("0.03"), net_profit_usd=D("0.39"), buy_timestamp=sold - timedelta(days=1), sell_timestamp=sold,
    ))
    await session.flush()


async def test_report_sends_the_sheet_rows_for_the_month(session):
    await sale(session, datetime(2026, 10, 5, 3, 30, tzinfo=UTC))
    await sale(session, datetime(2026, 9, 29, 12, 0, tzinfo=UTC), symbol="ETHUSDT")  # another month
    handle = report_handler(cfg(), factory(session))
    result = await handle(["2026-10"])
    assert isinstance(result, Document) and result.filename == "ventas_2026-10.txt"
    assert result.content.decode() == "04/10/2026\tBinance\tVenta\tSOL\t\t0,128\t$118,2000\t$121,5000\t$15,55\t$0,42\n"
    assert "1 fila" in result.caption and "America/Guayaquil" in result.caption


async def test_report_defaults_to_this_year_in_ecuador(session):
    await sale(session, datetime.now(UTC))
    result = await report_handler(cfg(), factory(session))([])
    assert isinstance(result, Document) and result.filename == f"ventas_{datetime.now(UTC).year}.txt"


async def test_report_says_so_when_there_is_nothing_and_explains_a_bad_argument(session):
    handle = report_handler(cfg(), factory(session))
    assert await handle(["2021"]) == "No sales for 2021."
    assert (await handle(["october"])).startswith("Usage: /report")
    assert (await handle(["2026-13"])).startswith("Usage: /report")


async def test_csv_sends_the_full_ledger_with_fees_and_net_profit(session):
    await sale(session, datetime(2026, 10, 5, 3, 30, tzinfo=UTC))
    result = await csv_handler(cfg(), factory(session))(["2026"])
    assert isinstance(result, Document) and result.filename == "tidewatch_ledger_2026.csv"
    lines = result.content.decode().splitlines()
    assert lines[0].startswith("sell_date_utc,symbol") and "fees_usd" in lines[0] and "net_profit_usd" in lines[0]
    assert len(lines) == 2 and lines[1].startswith("2026-10-05,SOLUSDT")  # UTC date, unlike the report
    assert "1 trade(s)" in result.caption and "UTC" in result.caption


async def test_csv_all_defaults_and_errors(session):
    await sale(session, datetime(2025, 3, 1, tzinfo=UTC))
    handle = csv_handler(cfg(), factory(session))
    assert isinstance(await handle(["all"]), Document)
    assert await handle(["2020"]) == "No closed trades for 2020."
    assert (await handle(["soon"])).startswith("Usage: /csv")
    assert (await handle(["1999"])).startswith("Usage: /csv")
    assert await handle([]) == f"No closed trades for {datetime.now(UTC).year}."  # this year, none yet
