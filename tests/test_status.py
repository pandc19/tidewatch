"""The /status report: gathered from the database, rendered as Telegram HTML."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bot.config.settings import Settings
from bot.database.models import (
    BotState,
    ExitReason,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    TaxLedger,
)
from bot.services.binance_client import BookTicker
from bot.services.status import PositionLine, StatusData, format_status, gather_status

D = Decimal
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class StubExchange:
    def __init__(self, bid="2430", free="9980.1", fail=False):
        self._bid, self._free, self._fail = bid, free, fail

    async def book_ticker(self, symbol):
        if self._fail:
            raise RuntimeError("down")
        return BookTicker(symbol, D(self._bid), D(self._bid) + 1)

    async def free_balance(self, asset):
        if self._fail:
            raise RuntimeError("down")
        return D(self._free)


def cfg(**env) -> Settings:
    return Settings(_env_file=None, BINANCE_API_KEY="k", BINANCE_API_SECRET="s", **env)


def factory(session):
    @asynccontextmanager
    async def sessions():
        yield session

    return sessions


async def open_position(session):
    pos = Position(symbol="ETHUSDT", status=PositionStatus.OPEN, quantity=D("0.0081"), entry_price=D("2424.95"),
                   take_profit_pct=D("0.04"), stop_loss_pct=D("0.02"), opened_at=NOW - timedelta(hours=3))
    session.add(pos)
    await session.flush()
    for kind, side, target, stop in [
        (OrderType.LIMIT, OrderSide.BUY, D("2424.95"), None),
        (OrderType.LIMIT_MAKER, OrderSide.SELL, D("2522"), None),
        (OrderType.STOP_LOSS_LIMIT, OrderSide.SELL, D("2375"), D("2376.4")),
    ]:
        done = kind == OrderType.LIMIT
        session.add(Order(position_id=pos.id, symbol="ETHUSDT", client_order_id=f"bot-x-{kind.value}", side=side,
                          order_type=kind, status=OrderStatus.FILLED if done else OrderStatus.NEW,
                          target_price=target, stop_price=stop, quantity=D("0.0081"), filled_qty=D("0"),
                          commission=D("0")))
    await session.flush()
    return pos


async def trade(session, net, sold):
    pos = Position(symbol="SOLUSDT", status=PositionStatus.CLOSED, take_profit_pct=D("0.04"),
                   stop_loss_pct=D("0.02"), exit_reason=ExitReason.TAKE_PROFIT)
    session.add(pos)
    await session.flush()
    session.add(TaxLedger(position_id=pos.id, symbol="SOLUSDT", amount_operated=D("1"), buy_price_usd=D("100"),
                          sell_price_usd=D("104"), total_buy_usd=D("100"), total_sell_usd=D("104"),
                          fees_usd=D("0.2"), net_profit_usd=D(net), buy_timestamp=sold - timedelta(hours=1),
                          sell_timestamp=sold))


async def report(session, exchange=None, **over):
    args = {
        "settings": cfg(), "sessions": factory(session), "exchange": exchange or StubExchange(),
        "trading_blocked": None, "tick_age_seconds": 12.0, "uptime_seconds": 90061, "stream_on": True, "now": NOW,
        **over,
    }
    return await gather_status(**args)


async def test_an_open_position_shows_its_exits_and_the_unrealised_change(session):
    await open_position(session)
    data = await report(session)
    (line,) = data.positions
    assert (line.take_profit, line.stop_trigger, line.bid) == (D("2522"), D("2376.4"), D("2430"))
    text = format_status(data)
    assert "[DRY RUN]" in text  # the default settings are a dry run, and dry run wins over testnet
    assert "ETH" in text and "2424.95" in text and "2522" in text and "2376.4" in text
    assert "+0.21%" in text  # (2430 - 2424.95) / 2424.95
    assert "1d 01h 01m" in text  # uptime 90061 s
    assert "9,980.10" in text


async def test_results_count_only_the_last_24_hours_in_the_short_line(session):
    await trade(session, "1.50", NOW - timedelta(hours=2))
    await trade(session, "-0.50", NOW - timedelta(hours=30))
    data = await report(session)
    assert (data.closed_24h, data.net_24h) == (1, D("1.50"))
    assert (data.closed_total, data.net_total) == (2, D("1.00"))
    text = format_status(data)
    assert "+1.5000 USD" in text and "+1.0000 USD" in text


async def test_no_position_and_a_quiet_account(session):
    text = format_status(await report(session))
    assert "No open position." in text and "active" in text


async def test_a_future_pause_is_shown_and_a_past_one_is_not(session):
    state = await session.get(BotState, 1) or BotState(id=1)
    session.add(state)
    state.trading_paused_until = NOW + timedelta(hours=5)
    state.pause_reason = "daily loss limit"
    await session.flush()
    text = format_status(await report(session))
    assert "paused (kill-switch)" in text and "daily loss limit" in text and "2026-10-20 17:00 UTC" in text
    state.trading_paused_until = NOW - timedelta(hours=1)
    await session.flush()
    assert "paused" not in format_status(await report(session))


async def test_a_blocked_engine_and_a_stale_tick_are_loud(session):
    text = format_status(await report(session, trading_blocked="reconciliation: 0 unreadable orders, 2 unknown orders", tick_age_seconds=600))
    assert "BLOCKED: reconciliation" in text and "STALE" in text


async def test_exchange_failures_leave_fields_out_instead_of_failing(session):
    await open_position(session)
    data = await report(session, exchange=StubExchange(fail=True))
    assert data.free_quote is None and data.positions[0].bid is None
    text = format_status(data)
    assert "Free USDT" not in text and "Best bid now" not in text and "2522" in text


async def test_an_open_position_without_exit_orders_warns():
    line = PositionLine("ETHUSDT", PositionStatus.OPEN, D("1"), D("100"), None, None, None, None, None)
    data = StatusData("TESTNET", "USDT", 5, None, False, None, None, None, None, 0, D(0), 0, D(0), [line])
    text = format_status(data)
    assert "NONE" in text and "exit orders not live" in text and "no heartbeat yet" in text


async def test_text_is_escaped():
    data = StatusData("TESTNET", "USDT", 5, 1, True, "<script>&", None, None, None, 0, D(0), 0, D(0))
    text = format_status(data)
    assert "<script>" not in text and "&lt;script&gt;&amp;" in text
