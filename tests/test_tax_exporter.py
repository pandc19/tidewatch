import csv
import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from bot.database.models import ExitReason, Position, PositionStatus, TaxLedger
from bot.reports.tax_exporter import COLUMNS, default_path, export, main

D = Decimal


async def trade(session, sold: datetime, *, symbol="SOLUSDT", net="0.48392662", bought: datetime | None = None,
                reason=ExitReason.TAKE_PROFIT, fees="0.03180668") -> TaxLedger:
    pos = Position(symbol=symbol, status=PositionStatus.CLOSED, take_profit_pct=D("0.04"), stop_loss_pct=D("0.02"),
                   exit_reason=reason)
    session.add(pos)
    await session.flush()
    row = TaxLedger(
        position_id=pos.id, symbol=symbol, amount_operated=D("0.129"), buy_price_usd=D("120.41000000"),
        sell_price_usd=D("125.22"), total_buy_usd=D("15.6533"), total_sell_usd=D("16.15338"),
        fees_usd=D(fees), net_profit_usd=D(net), buy_timestamp=bought or datetime(2026, 3, 1, 12, tzinfo=UTC),
        sell_timestamp=sold,
    )
    session.add(row)
    await session.flush()
    return row


async def run(session, year=None):
    out = io.StringIO()
    summary = await export(session, out, year)
    return summary, list(csv.reader(io.StringIO(out.getvalue())))


async def test_a_row_is_plain_readable_and_complete(session):
    row = await trade(session, datetime(2026, 3, 2, 14, 30, 5, tzinfo=UTC), bought=datetime(2026, 3, 1, 12, tzinfo=UTC))
    summary, rows = await run(session)

    assert rows[0] == COLUMNS
    (line,) = rows[1:]
    got = dict(zip(COLUMNS, line, strict=True))
    assert got["sell_date_utc"] == "2026-03-02" and got["symbol"] == "SOLUSDT"
    assert got["buy_price_usd"] == "120.41"  # trailing zeros trimmed, no exponent
    assert (got["amount_operated"], got["total_buy_usd"], got["total_sell_usd"]) == ("0.129", "15.6533", "16.15338")
    assert (got["fees_usd"], got["net_profit_usd"]) == ("0.03180668", "0.48392662")
    assert got["buy_timestamp_utc"] == "2026-03-01T12:00:00Z" and got["sell_timestamp_utc"] == "2026-03-02T14:30:05Z"
    assert got["holding_days"] == "1.10"  # 26.5 hours
    assert got["exit_reason"] == "TAKE_PROFIT" and got["position_id"] == str(row.position_id)
    assert summary.rows == 1 and summary.total_net_profit_usd == D("0.48392662")


async def test_the_tax_year_is_the_utc_year_of_the_sale(session):
    await trade(session, datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC), net="1")
    await trade(session, datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC), net="2")
    await trade(session, datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC), net="3")
    await trade(session, datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC), net="4")

    summary, rows = await run(session, year=2026)

    assert [r[COLUMNS.index("net_profit_usd")] for r in rows[1:]] == ["2", "3"]
    assert summary.rows == 2 and summary.total_net_profit_usd == D("5")
    assert (await run(session))[0].rows == 4  # no year: everything


async def test_rows_come_out_oldest_sale_first(session):
    await trade(session, datetime(2026, 5, 1, tzinfo=UTC), symbol="BTCUSDT")
    await trade(session, datetime(2026, 2, 1, tzinfo=UTC), symbol="ETHUSDT")
    await trade(session, datetime(2026, 3, 1, tzinfo=UTC), symbol="SOLUSDT")
    _, rows = await run(session)
    assert [r[COLUMNS.index("symbol")] for r in rows[1:]] == ["ETHUSDT", "SOLUSDT", "BTCUSDT"]


async def test_losses_and_manual_sales_are_labelled(session):
    await trade(session, datetime(2026, 4, 1, tzinfo=UTC), net="-0.35", reason=ExitReason.STOP_LOSS)
    await trade(session, datetime(2026, 4, 2, tzinfo=UTC), net="0.2", reason=ExitReason.MANUAL)
    summary, rows = await run(session)
    assert [(r[COLUMNS.index("net_profit_usd")], r[COLUMNS.index("exit_reason")]) for r in rows[1:]] == [
        ("-0.35", "STOP_LOSS"), ("0.2", "MANUAL"),
    ]
    assert summary.total_net_profit_usd == D("-0.15")


async def test_an_empty_ledger_still_gives_a_valid_file(session):
    summary, rows = await run(session, year=2026)
    assert rows == [COLUMNS] and summary.rows == 0 and summary.total_net_profit_usd == 0


async def test_every_row_has_every_column(session):
    for day in (1, 2, 3):
        await trade(session, datetime(2026, 4, day, tzinfo=UTC))
    _, rows = await run(session)
    assert {len(r) for r in rows} == {len(COLUMNS)}


def test_default_file_names():
    assert default_path(2026) == Path("exports/tax_ledger_2026.csv")
    assert default_path(None) == Path("exports/tax_ledger.csv")


@pytest.mark.parametrize("year", ["1999", "2999", "abc"])
def test_a_nonsense_year_is_refused_before_anything_is_written(year, capsys):
    with pytest.raises(SystemExit) as err:
        main(["--year", year])
    assert err.value.code == 2


def test_the_command_itself_writes_the_file_and_creates_the_folder(tmp_path, capsys):
    out = tmp_path / "nested" / "tax.csv"
    try:
        code = main(["--year", "2001", "--out", str(out)])  # a year with no trades
    except OSError as exc:  # no database reachable here: nothing to test against
        pytest.skip(f"PostgreSQL not reachable: {exc!r}")
    assert code == 0
    assert out.read_text().splitlines() == [",".join(COLUMNS)]
    assert "wrote 0 trade(s)" in capsys.readouterr().err
