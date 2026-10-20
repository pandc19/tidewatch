"""The sales report in the owner's sheet layout: Spanish format, Ecuador dates, no fees."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from bot.database.models import ExitReason, Position, PositionStatus, TaxLedger
from bot.reports.sales_sheet import (
    HEADER,
    PeriodError,
    build_report,
    main,
    money,
    parse_period,
    price,
    quantity,
    render,
    sale_row,
)

D = Decimal
GYE = ZoneInfo("America/Guayaquil")


def entry(symbol="SOLUSDT", amount="0.128", buy="118.2", sell="121.5", sold=None, **kw) -> TaxLedger:
    return TaxLedger(
        symbol=symbol, amount_operated=D(amount), buy_price_usd=D(buy), sell_price_usd=D(sell),
        total_buy_usd=D(amount) * D(buy), total_sell_usd=D(amount) * D(sell), fees_usd=D("0.03"),
        net_profit_usd=D("0.39"), buy_timestamp=datetime(2026, 10, 1, tzinfo=UTC),
        sell_timestamp=sold or datetime(2026, 10, 5, 3, 30, tzinfo=UTC), **kw,
    )


# --- the owner's two real rows --------------------------------------------------------------


def test_the_sol_row_matches_the_sheet_exactly():
    row = sale_row(entry(), GYE, "USDT")  # sold 2026-10-05 03:30 UTC = 2026-10-04 22:30 in Ecuador
    assert row == ["04/10/2026", "Binance", "Venta", "SOL", "", "0,128", "$118,2000", "$121,5000", "$15,55", "$0,42"]


def test_the_gtc_row_matches_the_sheet_exactly():
    gtc = entry("GTCUSDT", "71.8", "0.2180", "0.2223", sold=datetime(2026, 10, 5, 14, 0, tzinfo=UTC))
    row = sale_row(gtc, GYE, "USDT")
    assert row == ["05/10/2026", "Binance", "Venta", "GTC", "", "71,8", "$0,2180", "$0,2223", "$15,96", "$0,31"]


def test_rows_are_tab_separated_without_a_header_unless_asked():
    text = render([entry()], GYE, "USDT")
    assert text == "04/10/2026\tBinance\tVenta\tSOL\t\t0,128\t$118,2000\t$121,5000\t$15,55\t$0,42\n"
    with_header = render([entry()], GYE, "USDT", header=True).splitlines()
    assert with_header[0].split("\t") == HEADER and len(with_header) == 2
    assert render([], GYE, "USDT") == ""


def test_fees_never_appear_and_profit_is_gross():
    # The ledger's net profit (0.39) subtracts fees; the sheet's Monto is quantity x (sell - buy).
    assert sale_row(entry(), GYE, "USDT")[-1] == "$0,42"


# --- dates ----------------------------------------------------------------------------------


def test_the_date_is_the_ecuador_day_not_the_utc_day():
    late = entry(sold=datetime(2026, 12, 31, 23, 0, tzinfo=UTC))  # 18:00 on 31 Dec in Ecuador
    early = entry(sold=datetime(2027, 1, 1, 4, 59, tzinfo=UTC))  # 23:59 on 31 Dec in Ecuador
    after = entry(sold=datetime(2027, 1, 1, 5, 0, tzinfo=UTC))  # midnight, 1 Jan in Ecuador
    assert [sale_row(e, GYE, "USDT")[0] for e in (late, early, after)] == ["31/12/2026", "31/12/2026", "01/01/2027"]


def test_periods_are_whole_ecuador_days_in_utc():
    year = parse_period("2026", GYE)
    assert year.start == datetime(2026, 1, 1, 5, 0, tzinfo=UTC) and year.end == datetime(2027, 1, 1, 5, 0, tzinfo=UTC)
    month = parse_period("2026-10", GYE)
    assert (month.start, month.end, month.label) == (
        datetime(2026, 10, 1, 5, 0, tzinfo=UTC), datetime(2026, 11, 1, 5, 0, tzinfo=UTC), "2026-10",
    )
    assert parse_period("2026-12", GYE).end == datetime(2027, 1, 1, 5, 0, tzinfo=UTC)
    assert parse_period("2026-1", GYE).label == "2026-01"


@pytest.mark.parametrize("text", ["", "abc", "2026-13", "2026-00", "1999", "2999", "2026-10-05", "26"])
def test_bad_periods_are_refused(text):
    with pytest.raises(PeriodError):
        parse_period(text, GYE)


# --- numbers --------------------------------------------------------------------------------


def test_money_and_quantities_use_the_spanish_format():
    assert money(D("15.555")) == "$15,56"  # half up, not banker's rounding
    assert money(D("-0.314")) == "-$0,31"
    assert money(D("1234567.891")) == "$1.234.567,89"
    assert money(D("0")) == "$0,00"
    assert money(D("-0.001")) == "$0,00"  # no "-$0,00"
    assert quantity(D("0.12800000")) == "0,128" and quantity(D("71.8")) == "71,8" and quantity(D("2")) == "2"
    assert quantity(D("1500.25")) == "1.500,25"


def test_prices_have_four_decimals_unless_that_would_show_zero():
    assert price(D("118.2")) == "$118,2000" and price(D("0.2223")) == "$0,2223"
    assert price(D("0.00001234")) == "$0,00001234"


def test_a_losing_trade_shows_a_negative_profit():
    row = sale_row(entry(buy="125", sell="121.5"), GYE, "USDT")
    assert row[-2:] == ["$15,55", "-$0,45"]


# --- the query ------------------------------------------------------------------------------


async def ledger_row(session, **kw):
    pos = Position(symbol="SOLUSDT", status=PositionStatus.CLOSED, take_profit_pct=D("0.04"),
                   stop_loss_pct=D("0.02"), exit_reason=ExitReason.TAKE_PROFIT)
    session.add(pos)
    await session.flush()
    row = entry(**kw)
    row.position_id = pos.id
    session.add(row)
    await session.flush()
    return row


async def test_only_sales_inside_the_period_come_back_in_order(session):
    base = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    await ledger_row(session, sold=base + timedelta(days=1), amount="2")
    await ledger_row(session, sold=base, amount="1")
    await ledger_row(session, sold=datetime(2026, 11, 1, 4, 59, tzinfo=UTC), amount="3")  # 31 Oct 23:59 Ecuador
    await ledger_row(session, sold=datetime(2026, 11, 1, 5, 0, tzinfo=UTC), amount="4")  # 1 Nov Ecuador
    text, count = await build_report(session, parse_period("2026-10", GYE), GYE, "USDT")
    assert count == 3
    assert [line.split("\t")[5] for line in text.splitlines()] == ["1", "2", "3"]


async def test_an_empty_period_gives_no_rows(session):
    assert await build_report(session, parse_period("2020", GYE), GYE, "USDT") == ("", 0)


def test_the_command_line_refuses_a_bad_month():
    with pytest.raises(SystemExit) as err:
        main(["--year", "2026", "--month", "13"])
    assert err.value.code == 2
