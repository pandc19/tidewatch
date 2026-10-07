"""The engine against the real database and a stateful fake exchange."""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from bot.config.settings import Settings
from bot.database.models import (
    BotState,
    CancelReason,
    DailyPnl,
    ExitReason,
    KillSwitchEvent,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    TaxLedger,
)
from bot.engine.state_machine import (
    ALLOWED_TRANSITIONS,
    EngineObserver,
    EntrySignal,
    IllegalTransition,
    TradingEngine,
    transition,
)
from bot.services.binance_client import ExchangeClient
from bot.services.user_stream import OrderEvent
from tests.fakes import FakeBinance, api_error, order_dict

D = Decimal
SIGNAL = EntrySignal("SOLUSDT", D("120.41"))
FILL_TIME = datetime.fromtimestamp(1_790_000_000, UTC)  # the fake's trade timestamp


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)  # real time: order timestamps come from the database's clock

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Recorder(EngineObserver):
    def __init__(self):
        self.events: list[tuple] = []

    async def entry_placed(self, position, order):
        self.events.append(("entry_placed", position.symbol))

    async def entry_cancelled(self, position, order):
        self.events.append(("entry_cancelled", position.symbol))

    async def exit_placed(self, position, take_profit, stop_loss):
        self.events.append(("exit_placed", take_profit.target_price, stop_loss.target_price))

    async def trade_closed(self, position, result):
        self.events.append(("trade_closed", result.net_profit))

    async def attention(self, message):
        self.events.append(("attention", message))

    async def kill_switch(self, event):
        self.events.append(("kill_switch", event.realized_loss_usd))

    def kinds(self):
        return [e[0] for e in self.events]


@dataclass
class H:
    engine: TradingEngine
    fake: FakeBinance
    exchange: ExchangeClient
    clock: Clock
    obs: Recorder
    session: object

    async def position(self) -> Position:
        self.session.expire_all()
        return (await self.session.scalars(select(Position).order_by(Position.created_at.desc()))).first()

    async def orders(self, side=None) -> list[Order]:
        q = select(Order).order_by(Order.placed_at, Order.client_order_id)
        if side:
            q = q.where(Order.side == side)
        return list((await self.session.scalars(q.execution_options(populate_existing=True))).all())

    async def buy(self) -> Order:
        return (await self.orders(OrderSide.BUY))[-1]

    async def leg(self, order_type) -> Order:
        return next(o for o in await self.orders(OrderSide.SELL) if o.order_type == order_type and o.status != OrderStatus.REJECTED)

    def event(self, order: Order, status: str) -> OrderEvent:
        return OrderEvent(order.symbol, order.binance_order_id, order.client_order_id, order.order_list_id, status, "TRADE")

    async def open_and_fill(self, **fill) -> Order:
        await self.engine.open_position(SIGNAL)
        buy = await self.buy()
        self.fake.fill(buy.binance_order_id, price="120.41", **fill)
        await self.engine.on_stream_event(self.event(buy, "FILLED"))
        return buy


def build(session, *, dry_run=False, source=None, **settings) -> H:
    fake = FakeBinance()
    exchange = ExchangeClient(fake, dry_run=dry_run, order_id_prefix="bot")
    clock, obs = Clock(), Recorder()

    @asynccontextmanager
    async def sessions():
        async with session.begin_nested():  # the test's outer transaction is rolled back at the end
            yield session

    cfg = Settings(_env_file=None, DRY_RUN=str(dry_run), BINANCE_TESTNET="true", **settings)
    engine = TradingEngine(
        settings=cfg, exchange=exchange, session_factory=sessions, entry_source=source,
        observer=obs, clock=clock,
    )
    return H(engine, fake, exchange, clock, obs, session)


@pytest.fixture
def h(session) -> H:
    return build(session)


# --- the happy path -----------------------------------------------------------------------


async def test_a_buy_is_persisted_sized_and_sent(h):
    pos = await h.engine.open_position(SIGNAL)

    assert pos.status == PositionStatus.PENDING_ENTRY
    buy = await h.buy()
    assert buy.quantity == D("0.130")  # 98% of 16 USDT at 120.41, rounded down to the step
    assert buy.target_price == D("120.41")
    assert buy.binance_order_id == 1001 and buy.status == OrderStatus.NEW
    assert buy.expires_at == h.clock.now + timedelta(days=3)
    (sent,) = h.fake.called("create_order")
    assert sent["newClientOrderId"] == buy.client_order_id  # the row existed under this id before sending
    assert h.obs.kinds() == ["entry_placed"]


async def test_a_filled_buy_opens_the_position_and_places_the_oco(h):
    await h.open_and_fill()

    pos = await h.position()
    assert pos.status == PositionStatus.OPEN
    assert pos.quantity == D("0.12987")  # 0.130 minus the 0.1% fee taken in SOL
    assert pos.entry_price == D("120.41")
    tp, sl = await h.leg(OrderType.LIMIT_MAKER), await h.leg(OrderType.STOP_LOSS_LIMIT)
    assert (tp.target_price, sl.stop_price, sl.target_price) == (D("125.22"), D("118.00"), D("117.76"))
    assert tp.quantity == sl.quantity == D("0.129")  # rounded down to the step; the crumb stays
    assert tp.order_list_id == sl.order_list_id is not None
    assert tp.binance_order_id and sl.binance_order_id
    assert h.obs.kinds() == ["entry_placed", "exit_placed"]


async def test_take_profit_closes_the_trade_and_writes_the_ledger(h):
    await h.open_and_fill()
    tp, sl = await h.leg(OrderType.LIMIT_MAKER), await h.leg(OrderType.STOP_LOSS_LIMIT)

    h.fake.fill(tp.binance_order_id, price="125.22")
    await h.engine.on_stream_event(h.event(tp, "FILLED"))

    pos = await h.position()
    assert pos.status == PositionStatus.CLOSED and pos.exit_reason == ExitReason.TAKE_PROFIT
    assert pos.exit_price == D("125.22")
    # sold 0.129 for 16.15338, bought 0.130 for 15.6533, paid 0.01615338 USDT fee on the sell.
    # The 0.1% SOL fee on the buy is already in "sold 0.129, not 0.130": not subtracted twice.
    expected = D("16.15338") - D("15.6533") - D("0.01615338")
    assert pos.realized_pnl_usd == expected
    (row,) = (await h.session.scalars(select(TaxLedger))).all()
    assert (row.amount_operated, row.buy_price_usd, row.sell_price_usd) == (D("0.129"), D("120.41"), D("125.22"))
    assert (row.total_buy_usd, row.total_sell_usd, row.net_profit_usd) == (D("15.6533"), D("16.15338"), expected)
    assert row.fees_usd == D("0.0156533") + D("0.01615338")  # all fees reported
    assert row.sell_timestamp == FILL_TIME
    day = await h.session.get(DailyPnl, FILL_TIME.date())
    assert day.realized_pnl_usd == expected
    assert h.obs.kinds()[-1] == "trade_closed"
    # the exchange cancelled the other leg; it ends terminal and labelled
    await h.session.refresh(sl)
    assert sl.status == OrderStatus.CANCELED and sl.cancel_reason == CancelReason.OCO_COUNTERPART_FILLED


async def test_stop_loss_closes_at_a_loss(h):
    await h.open_and_fill()
    sl = await h.leg(OrderType.STOP_LOSS_LIMIT)

    h.fake.fill(sl.binance_order_id, price="117.90")
    await h.engine.on_stream_event(h.event(sl, "FILLED"))

    pos = await h.position()
    assert pos.status == PositionStatus.CLOSED and pos.exit_reason == ExitReason.STOP_LOSS
    assert pos.realized_pnl_usd < 0
    tp = await h.leg(OrderType.LIMIT_MAKER)
    await h.session.refresh(tp)
    assert tp.cancel_reason == CancelReason.OCO_COUNTERPART_FILLED


async def test_a_second_trade_can_start_after_the_first_closes(h):
    await h.open_and_fill()
    tp = await h.leg(OrderType.LIMIT_MAKER)
    h.fake.fill(tp.binance_order_id, price="125.22")
    await h.engine.on_stream_event(h.event(tp, "FILLED"))
    h.fake.balances["USDT"] = "16.00000000"
    assert await h.engine.open_position(SIGNAL) is not None


# --- idempotence and recovery -------------------------------------------------------------


async def test_repeated_events_never_place_a_second_oco(h):
    buy = await h.open_and_fill()
    for _ in range(3):
        await h.engine.on_stream_event(h.event(buy, "FILLED"))
    await h.engine.tick()
    assert len(h.fake.called("create_oco_order")) == 1
    assert len(h.session.new) == 0 and len(await h.orders(OrderSide.SELL)) == 2


async def test_a_fill_missed_while_disconnected_is_picked_up_on_reconnect(h):
    await h.engine.open_position(SIGNAL)
    buy = await h.buy()
    h.fake.fill(buy.binance_order_id, price="120.41")  # no stream event at all

    await h.engine.on_connected()

    assert (await h.position()).status == PositionStatus.OPEN
    assert len(h.fake.called("create_oco_order")) == 1


async def test_a_restart_resumes_from_the_database_alone(session, h):
    await h.engine.open_position(SIGNAL)
    buy = await h.buy()
    h.fake.fill(buy.binance_order_id, price="120.41")

    fresh = build(session)  # a new process: no memory of anything
    fresh.fake, fresh.exchange = h.fake, h.exchange
    fresh.engine._exchange = h.exchange
    await fresh.engine.on_connected()

    assert (await h.position()).status == PositionStatus.OPEN


async def test_an_orphan_order_blocks_new_entries_until_it_is_gone(h):
    h.fake.open = [order_dict(9001, "bot-orphan", status="NEW")]
    await h.engine.on_connected()
    assert h.engine.trading_blocked and "attention" in h.obs.kinds()
    assert await h.engine.open_position(SIGNAL) is None
    assert h.fake.called("create_order") == []

    h.fake.open = None
    await h.engine.on_connected()
    assert h.engine.trading_blocked is None
    assert await h.engine.open_position(SIGNAL) is not None


async def test_events_for_orders_that_are_not_ours_are_ignored(h, caplog):
    await h.engine.on_stream_event(OrderEvent("SOLUSDT", 777, "web_xyz", None, "FILLED", "TRADE"))
    assert "no row exists" not in caplog.text
    with caplog.at_level(logging.ERROR):
        await h.engine.on_stream_event(OrderEvent("SOLUSDT", 778, "bot-lost-row", None, "NEW", "NEW"))
    assert "no row exists" in caplog.text  # ours by prefix but unknown: loud, no crash


# --- timeouts -----------------------------------------------------------------------------


async def test_an_unfilled_buy_is_cancelled_after_the_timeout_and_the_slot_is_freed(h):
    await h.engine.open_position(SIGNAL)
    h.clock.advance(days=2)
    await h.engine.tick()
    assert (await h.position()).status == PositionStatus.PENDING_ENTRY  # not yet

    h.clock.advance(days=1, minutes=1)
    await h.engine.tick()

    buy = await h.buy()
    assert buy.status == OrderStatus.CANCELED and buy.cancel_reason == CancelReason.ENTRY_TIMEOUT
    assert "3 days" in buy.cancel_detail
    assert (await h.position()).status == PositionStatus.CANCELLED
    assert h.obs.kinds()[-1] == "entry_cancelled"
    assert await h.engine.open_position(SIGNAL) is not None  # capital is free again


async def test_a_partly_filled_buy_keeps_what_filled_and_gets_its_oco(h):
    await h.engine.open_position(SIGNAL)
    buy = await h.buy()
    h.fake.fill(buy.binance_order_id, price="120.41", qty="0.060")
    await h.engine.on_stream_event(h.event(buy, "PARTIALLY_FILLED"))
    assert (await h.position()).status == PositionStatus.PENDING_ENTRY  # still waiting for the rest

    h.clock.advance(days=3, minutes=1)
    await h.engine.tick()

    pos = await h.position()
    assert pos.status == PositionStatus.OPEN and pos.quantity == D("0.05994")
    await h.session.refresh(buy)
    assert buy.status == OrderStatus.CANCELED and buy.cancel_reason == CancelReason.ENTRY_TIMEOUT
    assert buy.filled_qty == D("0.060")
    assert (await h.leg(OrderType.LIMIT_MAKER)).quantity == D("0.059")


async def test_a_buy_that_filled_just_as_it_timed_out_is_not_lost(h):
    await h.engine.open_position(SIGNAL)
    buy = await h.buy()
    h.fake.fill(buy.binance_order_id, price="120.41")  # filled; the bot has not heard yet
    h.clock.advance(days=4)

    await h.engine.tick()  # cancel is refused ("unknown order"), the sync finds the fill

    assert (await h.position()).status == PositionStatus.OPEN
    await h.session.refresh(buy)
    assert buy.status == OrderStatus.FILLED and buy.cancel_reason is None


async def test_the_kill_switch_cancels_entries_but_never_touches_open_positions(session):
    h = build(session, MAX_OPEN_POSITIONS="2")
    await h.open_and_fill()  # position 1: OPEN with an OCO
    h.fake.balances["USDT"] = "16"
    await h.engine.open_position(EntrySignal("BTCUSDT", D("120.41")))  # position 2: pending

    await h.engine.cancel_pending_entries(CancelReason.KILL_SWITCH, "daily loss limit")

    buys = {o.symbol: o for o in await h.orders(OrderSide.BUY)}
    assert buys["SOLUSDT"].status == OrderStatus.FILLED
    cancelled = [o for o in buys.values() if o.status == OrderStatus.CANCELED]
    assert [o.cancel_reason for o in cancelled] == [CancelReason.KILL_SWITCH]
    live_exits = [o for o in await h.orders(OrderSide.SELL) if o.status == OrderStatus.NEW]
    assert len(live_exits) == 2  # the OCO is untouched


# --- failures -----------------------------------------------------------------------------


async def test_a_refused_buy_leaves_nothing_behind(h):
    h.fake.raise_on["create_order"] = api_error(-2010, "Account has insufficient balance")
    assert await h.engine.open_position(SIGNAL) is None
    pos = await h.position()
    buy = await h.buy()
    assert pos.status == PositionStatus.CANCELLED and buy.status == OrderStatus.REJECTED
    assert "insufficient balance" in buy.cancel_detail
    h.fake.raise_on.clear()
    assert await h.engine.open_position(SIGNAL) is not None  # the symbol is not stuck


async def test_a_lost_answer_to_a_buy_that_never_reached_binance_is_cleaned_up(h):
    h.fake.raise_once["create_order"] = [api_error(-1021, "Timestamp outside of recvWindow")]
    pos = await h.engine.open_position(SIGNAL)
    assert pos.status == PositionStatus.PENDING_ENTRY  # outcome unknown: do not assume
    assert (await h.buy()).binance_order_id is None

    h.clock.advance(days=1)  # past the grace period
    await h.engine.tick()

    assert (await h.buy()).status == OrderStatus.REJECTED
    assert (await h.position()).status == PositionStatus.CANCELLED


async def test_a_lost_answer_to_a_buy_that_did_reach_binance_is_found_by_client_id(h):
    h.fake.raise_once["create_order"] = [api_error(-1021, "Timestamp outside of recvWindow")]
    await h.engine.open_position(SIGNAL)
    buy = await h.buy()
    # the order did get placed; only the answer was lost
    h.fake.orders[1500] = order_dict(1500, buy.client_order_id, price="120.41", qty="0.130")

    h.clock.advance(days=1)
    await h.engine.tick()

    await h.session.refresh(buy)
    assert buy.binance_order_id == 1500 and buy.status == OrderStatus.NEW
    assert (await h.position()).status == PositionStatus.PENDING_ENTRY
    assert len(h.fake.called("create_order")) == 1  # and it was never placed twice


async def test_a_lost_answer_to_the_exit_that_did_reach_binance_is_adopted_not_duplicated(h):
    h.fake.raise_once["create_oco_order"] = [api_error(-1021, "Timestamp outside of recvWindow")]
    await h.open_and_fill()
    tp, sl = await h.orders(OrderSide.SELL)
    assert tp.binance_order_id is None and tp.status == OrderStatus.NEW  # outcome unknown: no assumption
    await h.engine.tick()  # inside the grace period: it is not declared missing, and no second OCO is sent
    assert len(h.fake.called("create_oco_order")) == 1

    # the OCO had in fact been placed; only the answer was lost
    for row, oid in ((tp, 2001), (sl, 2002)):
        h.fake.orders[oid] = order_dict(oid, row.client_order_id, side="SELL", price=str(row.target_price),
                                        qty=str(row.quantity), list_id=77,
                                        type_="LIMIT_MAKER" if row is tp else "STOP_LOSS_LIMIT")
    h.clock.advance(hours=1)
    await h.engine.tick()

    legs = await h.orders(OrderSide.SELL)
    assert {o.binance_order_id for o in legs} == {2001, 2002} and {o.order_list_id for o in legs} == {77}
    assert len(h.fake.called("create_oco_order")) == 1  # never placed twice
    assert "attention" not in h.obs.kinds()


async def test_a_lost_answer_to_the_exit_that_never_reached_binance_is_retried(h):
    h.fake.raise_once["create_oco_order"] = [api_error(-1021, "Timestamp outside of recvWindow")]
    await h.open_and_fill()

    h.clock.advance(hours=1)
    await h.engine.tick()  # not found by client id: declared refused, then placed again

    assert len(h.fake.called("create_oco_order")) == 2
    live = [o for o in await h.orders(OrderSide.SELL) if o.status == OrderStatus.NEW]
    assert len(live) == 2 and all(o.binance_order_id for o in live)


async def test_a_take_profit_already_passed_is_moved_just_above_the_market(h):
    h.fake.raise_once["create_oco_order"] = [api_error(-2010, "The relationship of the prices for the orders is not correct.")]
    h.fake.ask = "125.50000000"  # the market ran past the 125.22 target while the buy was filling
    await h.open_and_fill()

    tp = await h.leg(OrderType.LIMIT_MAKER)
    assert tp.target_price == D("125.57")  # best ask + 0.05%, rounded up to the tick
    assert (await h.position()).status == PositionStatus.OPEN
    assert len(h.fake.called("create_oco_order")) == 2
    assert "attention" not in h.obs.kinds()


async def test_an_exit_the_exchange_keeps_refusing_is_flagged_once_and_retried_without_piling_up_rows(h):
    h.fake.raise_on["create_oco_order"] = api_error(-2010, "The relationship of the prices for the orders is not correct.")
    await h.open_and_fill()

    assert (await h.position()).status == PositionStatus.OPEN  # holding coins with no exit: must not look fine
    assert [o.status for o in await h.orders(OrderSide.SELL)] == [OrderStatus.REJECTED] * 2
    assert h.obs.kinds().count("attention") == 1 and len(h.fake.called("create_oco_order")) == 1

    await h.engine.tick()  # inside the retry window: not tried again
    assert len(h.fake.called("create_oco_order")) == 1

    h.clock.advance(seconds=121)
    await h.engine.tick()  # tried again, still refused, not re-announced
    assert len(h.fake.called("create_oco_order")) == 2 and h.obs.kinds().count("attention") == 1
    assert len(await h.orders(OrderSide.SELL)) == 2  # the same two rows, reused

    h.fake.raise_on.clear()
    h.clock.advance(seconds=121)
    await h.engine.tick()  # the market is fine again
    legs = await h.orders(OrderSide.SELL)
    assert len(legs) == 2 and all(o.status == OrderStatus.NEW and o.order_list_id for o in legs)


async def test_a_stop_the_price_already_fell_past_is_replaced_by_an_emergency_stop_below_the_market(h):
    h.fake.raise_once["create_oco_order"] = [api_error(-2010, "The relationship of the prices for the orders is not correct.")]
    h.fake.bid = "116.00000000"  # fell through the planned 118.00 stop before the OCO could be placed
    await h.open_and_fill()

    tp, sl = await h.leg(OrderType.LIMIT_MAKER), await h.leg(OrderType.STOP_LOSS_LIMIT)
    assert tp.target_price == D("125.22")  # the take-profit is untouched
    assert (sl.stop_price, sl.target_price) == (D("113.68"), D("113.45"))  # 2% below the CURRENT bid
    assert len(h.fake.called("create_oco_order")) == 2
    assert h.fake.called("create_oco_order")[-1]["belowStopPrice"] == "113.68"
    assert sl.status == OrderStatus.NEW
    assert (await h.position()).status == PositionStatus.OPEN
    (message,) = [e[1] for e in h.obs.events if e[0] == "attention"]  # the owner is told, once
    assert "116" in message and "118" in message and "113.68" in message and "5." in message


async def test_a_normal_stop_never_triggers_the_emergency_path(h):
    await h.open_and_fill()
    assert (await h.leg(OrderType.STOP_LOSS_LIMIT)).stop_price == D("118.00")
    assert "attention" not in h.obs.kinds()


async def test_a_position_with_nothing_left_to_sell_is_flagged_not_sent_as_a_zero_order(h):
    await h.engine.open_position(SIGNAL)
    buy = await h.buy()
    h.fake.fill(buy.binance_order_id, price="120.41")
    h.fake.balances["SOL"] = "0.0001"  # the coins are gone (moved or sold elsewhere)
    await h.engine.on_stream_event(h.event(buy, "FILLED"))
    await h.engine.tick()
    assert h.fake.called("create_oco_order") == []
    assert h.obs.kinds().count("attention") == 1
    assert (await h.position()).status == PositionStatus.OPEN


# --- the owner sells by hand --------------------------------------------------------------


async def cancel_exits_in_the_app(h):
    tp = await h.leg(OrderType.LIMIT_MAKER)
    h.fake.cancel_externally(tp.binance_order_id)
    await h.engine.on_stream_event(h.event(tp, "CANCELED"))
    await h.engine.tick()


async def test_a_sale_made_by_hand_closes_the_position_as_manual(h):
    await h.open_and_fill()
    await cancel_exits_in_the_app(h)
    assert (await h.position()).status == PositionStatus.OPEN  # cancelled, not sold yet: only flagged
    assert h.obs.kinds().count("attention") == 1

    h.fake.manual_sell("0.129", "123.00")
    await h.engine.tick()

    pos = await h.position()
    assert pos.status == PositionStatus.CLOSED and pos.exit_reason == ExitReason.MANUAL
    assert pos.exit_price == D("123.00") and pos.opened_at <= pos.closed_at
    # 0.129 x 123 = 15.867 back; 0.130 x 120.41 = 15.6533 spent; 0.015867 USDT fee on the sale
    expected = D("15.867") - D("15.6533") - D("0.015867")
    assert pos.realized_pnl_usd == expected
    (row,) = (await h.session.scalars(select(TaxLedger))).all()
    assert (row.amount_operated, row.net_profit_usd) == (D("0.129"), expected)
    manual = [o for o in await h.orders(OrderSide.SELL) if o.client_order_id.startswith("manual-")]
    assert len(manual) == 1 and "outside the bot" in manual[0].cancel_detail
    assert "trade_closed" in h.obs.kinds()


async def test_only_the_positions_share_of_a_bigger_manual_sale_is_booked(h):
    h.fake.balances["SOL"] = "4"  # the owner also holds coins of their own
    await h.open_and_fill()
    await cancel_exits_in_the_app(h)

    h.fake.manual_sell("4.12987", "123.00")  # sells everything, theirs included
    await h.engine.tick()

    (row,) = (await h.session.scalars(select(TaxLedger))).all()
    assert row.amount_operated == D("0.12987")  # exactly what the bot held, not 4.12987
    assert (await h.position()).status == PositionStatus.CLOSED


async def test_a_partial_manual_sale_is_flagged_and_not_guessed_at(h):
    await h.open_and_fill()
    await cancel_exits_in_the_app(h)
    h.fake.manual_sell("0.05", "123.00")
    await h.engine.tick()
    await h.engine.tick()
    assert (await h.position()).status == PositionStatus.OPEN
    assert (await h.session.scalar(select(TaxLedger.id))) is None
    messages = [e[1] for e in h.obs.events if e[0] == "attention"]
    assert sum("Partial manual sales" in m for m in messages) == 1  # said once


async def test_trade_history_is_not_even_fetched_while_the_exits_are_live(h):
    await h.open_and_fill()
    await h.engine.tick()
    await h.engine.tick()
    assert [c for c in h.fake.called("get_my_trades") if "limit" in c] == []  # only per-order fill lookups


# --- the kill-switch inside the engine ----------------------------------------------------


async def test_the_kill_switch_runs_each_tick_cancels_entries_and_pauses_trading(h):
    await h.engine.open_position(SIGNAL)  # a buy resting on the exchange
    h.session.add(Position(
        symbol="BTCUSDT", status=PositionStatus.CLOSED, take_profit_pct=D("0.04"), stop_loss_pct=D("0.02"),
        realized_pnl_usd=D("-0.60"), closed_at=h.clock.now - timedelta(hours=1),
    ))
    await h.session.flush()

    await h.engine.tick()

    buy = await h.buy()
    event = (await h.session.scalars(select(KillSwitchEvent))).one()
    assert buy.status == OrderStatus.CANCELED and buy.cancel_reason == CancelReason.KILL_SWITCH
    assert buy.kill_switch_event_id == event.id  # the cancelled order points at why
    sol = (await h.session.scalars(select(Position).where(Position.symbol == "SOLUSDT"))).one()
    assert sol.status == PositionStatus.CANCELLED  # (the newest position is the injected loss, not this one)
    assert "kill_switch" in h.obs.kinds()
    assert await h.engine.open_position(SIGNAL) is None  # and no new entry while paused
    state = await h.session.get(BotState, 1)
    assert state.trading_paused_until == event.resume_at


async def test_an_oco_cancelled_by_the_owner_is_flagged_not_silently_replaced(h):
    await h.open_and_fill()
    tp = await h.leg(OrderType.LIMIT_MAKER)

    h.fake.cancel_externally(tp.binance_order_id)
    await h.engine.on_stream_event(h.event(tp, "CANCELED"))
    await h.engine.tick()
    await h.engine.tick()

    pos = await h.position()
    assert pos.status == PositionStatus.OPEN
    legs = await h.orders(OrderSide.SELL)
    assert {o.cancel_reason for o in legs} == {CancelReason.EXTERNAL}
    assert len(h.fake.called("create_oco_order")) == 1  # the owner's decision is not overridden
    assert h.obs.kinds().count("attention") == 1  # said once, not every tick


# --- leftovers and fees -------------------------------------------------------------------


async def test_a_leftover_crumb_is_swept_into_the_next_sale(h):
    h.fake.balances["SOL"] = "0.00060000"  # under one 0.001 step, left by an earlier trade
    await h.open_and_fill()
    assert (await h.leg(OrderType.LIMIT_MAKER)).quantity == D("0.130")  # 0.12987 + 0.0006 = 0.13047


async def test_coins_the_owner_holds_separately_are_never_sold(h):
    h.fake.balances["SOL"] = "4.00000000"  # the testnet account (and maybe yours) holds some
    await h.open_and_fill()
    assert (await h.leg(OrderType.LIMIT_MAKER)).quantity == D("0.129")


async def test_a_fee_paid_in_bnb_is_a_real_cost_and_leaves_the_quantity_whole(h):
    h.fake.balances["BNB"] = "1"
    await h.open_and_fill(fee="0.0002", fee_asset="BNB")
    pos = await h.position()
    assert pos.quantity == D("0.130")  # no coin was taken from the purchase
    tp = await h.leg(OrderType.LIMIT_MAKER)
    assert tp.quantity == D("0.130")

    h.fake.fill(tp.binance_order_id, price="125.22")
    await h.engine.on_stream_event(h.event(tp, "FILLED"))

    (row,) = (await h.session.scalars(select(TaxLedger))).all()
    bnb_buy_fee = D("0.0002") * D("120")  # the fake prices every pair at 120
    sell_fee = D("0.130") * D("125.22") * D("0.001")
    assert row.net_profit_usd == row.total_sell_usd - row.total_buy_usd - bnb_buy_fee - sell_fee


# --- gates --------------------------------------------------------------------------------


async def test_no_entry_while_trading_is_paused(h):
    state = await h.session.get(BotState, 1)  # the single row the migration seeds
    state.trading_paused_until = h.clock.now + timedelta(hours=5)
    state.pause_reason = "kill-switch"
    await h.session.flush()
    assert await h.engine.open_position(SIGNAL) is None
    assert h.fake.called("create_order") == []
    h.clock.advance(hours=6)
    assert await h.engine.open_position(SIGNAL) is not None  # the pause is over


async def test_one_position_at_a_time_by_default(h):
    assert await h.engine.open_position(SIGNAL) is not None
    assert await h.engine.open_position(EntrySignal("BTCUSDT", D("120.41"))) is None
    assert len(h.fake.called("create_order")) == 1


async def test_never_two_live_positions_on_the_same_symbol(session):
    h = build(session, MAX_OPEN_POSITIONS="3")
    assert await h.engine.open_position(SIGNAL) is not None
    assert await h.engine.open_position(SIGNAL) is None


async def test_a_wallet_too_small_to_protect_a_trade_opens_nothing(h):
    h.fake.balances["USDT"] = "5.17"  # buys 0.043 SOL, but only 0.042 could be sold after fees
    assert await h.engine.open_position(SIGNAL) is None
    assert h.fake.called("create_order") == []
    assert await h.session.scalar(select(Position.id)) is None  # not even a row


class OneShot:
    def __init__(self, signal):
        self.signals = [signal]

    async def next_entry(self):
        return self.signals.pop(0) if self.signals else None


async def test_the_tick_asks_the_entry_source_and_opens_what_it_returns(session):
    h = build(session, source=OneShot(SIGNAL))
    await h.engine.tick()
    assert (await h.position()).status == PositionStatus.PENDING_ENTRY
    await h.engine.tick()  # the source has nothing more, and a position is live anyway
    assert len(h.fake.called("create_order")) == 1


# --- dry run ------------------------------------------------------------------------------


async def test_dry_run_records_intent_and_never_touches_the_order_endpoints(session):
    h = build(session, dry_run=True)
    pos = await h.engine.open_position(SIGNAL)
    buy = await h.buy()
    assert pos.status == PositionStatus.PENDING_ENTRY and buy.binance_order_id is None

    h.clock.advance(days=3, minutes=1)
    await h.engine.tick()

    assert (await h.buy()).cancel_reason == CancelReason.ENTRY_TIMEOUT
    assert (await h.position()).status == PositionStatus.CANCELLED
    for name in ("create_order", "create_oco_order", "cancel_order", "get_order", "get_my_trades"):
        assert h.fake.called(name) == [], name


# --- state machine ------------------------------------------------------------------------


def test_the_state_graph():
    assert ALLOWED_TRANSITIONS[PositionStatus.PENDING_ENTRY] == {PositionStatus.OPEN, PositionStatus.CANCELLED}
    assert ALLOWED_TRANSITIONS[PositionStatus.OPEN] == {PositionStatus.CLOSED}
    assert not ALLOWED_TRANSITIONS[PositionStatus.CLOSED] and not ALLOWED_TRANSITIONS[PositionStatus.CANCELLED]


@pytest.mark.parametrize(
    "start,target",
    [
        (PositionStatus.CLOSED, PositionStatus.OPEN),
        (PositionStatus.CANCELLED, PositionStatus.OPEN),
        (PositionStatus.OPEN, PositionStatus.PENDING_ENTRY),
        (PositionStatus.OPEN, PositionStatus.CANCELLED),
        (PositionStatus.PENDING_ENTRY, PositionStatus.CLOSED),
    ],
)
def test_illegal_transitions_are_refused(start, target):
    pos = Position(symbol="SOLUSDT", status=start, take_profit_pct=D("0.04"), stop_loss_pct=D("0.02"))
    with pytest.raises(IllegalTransition):
        transition(pos, target)
    assert pos.status == start
