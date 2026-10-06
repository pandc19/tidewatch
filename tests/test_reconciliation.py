import uuid
from decimal import Decimal

import pytest

from bot.database.models import (
    CancelReason,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
)
from bot.services.binance_client import ExchangeClient
from bot.services.reconciliation import reconcile, sync_order
from tests.fakes import FakeBinance, api_error, order_dict, trade_dict

D = Decimal


def exchange(fake: FakeBinance | None = None, *, dry_run=False) -> tuple[ExchangeClient, FakeBinance]:
    fake = fake or FakeBinance()
    return ExchangeClient(fake, dry_run=dry_run, order_id_prefix="bot"), fake


async def make_position(session, symbol="SOLUSDT") -> Position:
    pos = Position(
        symbol=symbol, status=PositionStatus.PENDING_ENTRY, take_profit_pct=D("0.04"), stop_loss_pct=D("0.02")
    )
    session.add(pos)
    await session.flush()
    return pos


async def make_order(session, pos, *, binance_id=1001, client_id=None, **kw) -> Order:
    defaults = {
        "position_id": pos.id,
        "symbol": pos.symbol,
        "binance_order_id": binance_id,
        "client_order_id": client_id or f"bot-{uuid.uuid4().hex[:24]}",
        "side": OrderSide.BUY,
        "order_type": OrderType.LIMIT,
        "status": OrderStatus.NEW,
        "target_price": D("120.41"),
        "quantity": D("0.130"),
    }
    order = Order(**{**defaults, **kw})
    session.add(order)
    await session.flush()
    return order


def filled(order: Order, fake: FakeBinance, *, qty="0.130", quote="15.6533", status="FILLED", **kw):
    fake.orders[order.binance_order_id] = order_dict(
        order.binance_order_id, order.client_order_id, status=status, executed=qty, quote=quote, **kw
    )


# --- fills --------------------------------------------------------------------------------


async def test_a_filled_buy_records_price_quantity_and_fee(session):
    ex, fake = exchange()
    pos = await make_position(session)
    order = await make_order(session, pos)
    filled(order, fake)
    fake.trades[1001] = [trade_dict(1, 1001, price="120.41", qty="0.130", commission="0.00013", asset="SOL")]

    report = await reconcile(session, ex)

    (change,) = report.changes
    assert (change.old_status, change.new_status) == (OrderStatus.NEW, OrderStatus.FILLED)
    assert change.newly_filled == D("0.130") and change.position_id == pos.id
    assert order.filled_qty == D("0.130")
    assert order.avg_fill_price == D("15.6533") / D("0.130")
    assert (order.commission, order.commission_asset) == (D("0.00013"), "SOL")
    assert report.errors == [] and report.orphans == []


async def test_reconciling_twice_changes_nothing_the_second_time(session):
    ex, fake = exchange()
    order = await make_order(session, await make_position(session))
    filled(order, fake)
    assert len((await reconcile(session, ex)).changes) == 1
    assert (await reconcile(session, ex)).changes == []
    assert len(fake.called("get_order")) == 1  # a finished order is not re-read


async def test_partial_fills_report_only_the_new_quantity(session):
    ex, fake = exchange()
    order = await make_order(session, await make_position(session))

    filled(order, fake, qty="0.050", quote="6.0205", status="PARTIALLY_FILLED")
    (c1,) = (await reconcile(session, ex)).changes
    assert c1.new_status == OrderStatus.PARTIALLY_FILLED and c1.newly_filled == D("0.050")

    filled(order, fake, qty="0.090", quote="10.8369", status="PARTIALLY_FILLED")
    (c2,) = (await reconcile(session, ex)).changes
    assert c2.newly_filled == D("0.040") and c2.filled_qty == D("0.090")  # stays live, same status


async def test_a_pending_cancel_does_not_change_the_status(session):
    ex, fake = exchange()
    order = await make_order(session, await make_position(session))
    fake.orders[1001] = order_dict(1001, order.client_order_id, status="PENDING_CANCEL")
    assert (await reconcile(session, ex)).changes == []
    assert order.status == OrderStatus.NEW


# --- orders that never got an id ----------------------------------------------------------


async def test_a_lost_response_is_recovered_by_client_order_id(session):
    ex, fake = exchange()
    order = await make_order(session, await make_position(session), binance_id=None, client_id="bot-lost")
    fake.orders[555] = order_dict(555, "bot-lost", status="NEW")
    await reconcile(session, ex)
    assert order.binance_order_id == 555 and order.status == OrderStatus.NEW


async def test_an_order_binance_never_saw_is_marked_rejected(session):
    ex, _ = exchange()
    order = await make_order(session, await make_position(session), binance_id=None, client_id="bot-never")
    (change,) = (await reconcile(session, ex)).changes
    assert change.new_status == OrderStatus.REJECTED
    assert order.cancel_reason is None and "not found" in order.cancel_detail


async def test_a_known_order_that_vanishes_is_an_error_not_a_guess(session):
    ex, fake = exchange()
    gone = await make_order(session, await make_position(session, "SOLUSDT"), binance_id=1001)
    ok = await make_order(session, await make_position(session, "BTCUSDT"), binance_id=1002, symbol="BTCUSDT")
    filled(ok, fake, symbol="BTCUSDT")

    report = await reconcile(session, ex)

    assert [oid for oid, _ in report.errors] == [gone.id]
    assert gone.status == OrderStatus.NEW  # untouched
    assert [c.order_id for c in report.changes] == [ok.id]  # the healthy order was still synced


async def test_an_unknown_exchange_status_is_reported_per_order(session):
    ex, fake = exchange()
    order = await make_order(session, await make_position(session))
    fake.orders[1001] = order_dict(1001, order.client_order_id, status="SOMETHING_NEW")
    report = await reconcile(session, ex)
    assert report.changes == [] and len(report.errors) == 1


# --- cancel reasons -----------------------------------------------------------------------


async def test_a_cancel_nobody_asked_for_is_labelled_external(session):
    ex, fake = exchange()
    order = await make_order(session, await make_position(session))
    fake.orders[1001] = order_dict(1001, order.client_order_id, status="CANCELED")
    await reconcile(session, ex)
    assert order.status == OrderStatus.CANCELED
    assert order.cancel_reason == CancelReason.EXTERNAL


async def test_the_engine_can_pass_the_real_reason(session):
    ex, fake = exchange()
    order = await make_order(session, await make_position(session))
    fake.orders[1001] = order_dict(1001, order.client_order_id, status="CANCELED")
    await sync_order(
        session, ex, order, cancel_reason=CancelReason.ENTRY_TIMEOUT, cancel_detail="not filled in 3 days"
    )
    assert order.cancel_reason == CancelReason.ENTRY_TIMEOUT
    assert order.cancel_detail == "not filled in 3 days"


async def test_a_reason_is_never_overwritten(session):
    ex, fake = exchange()
    order = await make_order(session, await make_position(session))
    fake.orders[1001] = order_dict(1001, order.client_order_id, status="CANCELED")
    await sync_order(session, ex, order, cancel_reason=CancelReason.KILL_SWITCH)
    order.status = OrderStatus.CANCELED
    again = await sync_order(session, ex, order, cancel_reason=CancelReason.ENTRY_TIMEOUT)
    assert order.cancel_reason == CancelReason.KILL_SWITCH and again is None


@pytest.mark.parametrize("sync_filled_first", [True, False])
@pytest.mark.parametrize("leg_status", ["CANCELED", "EXPIRED"])
async def test_the_losing_oco_leg_is_labelled_whichever_syncs_first(session, sync_filled_first, leg_status):
    ex, fake = exchange()
    pos = await make_position(session)
    common = {"side": OrderSide.SELL, "order_list_id": 55, "quantity": D("0.129")}
    tp = await make_order(session, pos, binance_id=2001, order_type=OrderType.LIMIT_MAKER, **common)
    sl = await make_order(session, pos, binance_id=2002, order_type=OrderType.STOP_LOSS_LIMIT, **common)
    fake.orders[2001] = order_dict(
        2001, tp.client_order_id, side="SELL", type_="LIMIT_MAKER", status="FILLED",
        executed="0.129", quote="16.1538", list_id=55,
    )
    fake.orders[2002] = order_dict(
        2002, sl.client_order_id, side="SELL", type_="STOP_LOSS_LIMIT", status=leg_status, list_id=55
    )

    for leg in (tp, sl) if sync_filled_first else (sl, tp):
        await sync_order(session, ex, leg)

    assert tp.status == OrderStatus.FILLED
    assert sl.status.value == leg_status
    assert sl.cancel_reason == CancelReason.OCO_COUNTERPART_FILLED


async def test_a_cancelled_oco_pair_with_no_fill_stays_external(session):
    ex, fake = exchange()
    pos = await make_position(session)
    legs = [
        await make_order(session, pos, binance_id=i, side=OrderSide.SELL, order_list_id=55,
                         order_type=t, quantity=D("0.129"))
        for i, t in ((2001, OrderType.LIMIT_MAKER), (2002, OrderType.STOP_LOSS_LIMIT))
    ]
    for leg in legs:
        fake.orders[leg.binance_order_id] = order_dict(leg.binance_order_id, leg.client_order_id, status="CANCELED", list_id=55)
        await sync_order(session, ex, leg)
    assert all(leg.cancel_reason == CancelReason.EXTERNAL for leg in legs)


# --- orphans / dry-run --------------------------------------------------------------------


async def test_orders_open_on_binance_but_missing_from_the_database_are_reported(session):
    ex, fake = exchange()
    known = await make_order(session, await make_position(session), client_id="bot-known")
    fake.orders[1001] = order_dict(1001, "bot-known", status="NEW")
    fake.open = [
        order_dict(1001, "bot-known"),
        order_dict(3001, "bot-orphan"),  # ours, but we have no row: crash between send and save
        order_dict(3002, "web_abc123"),  # placed by hand in the Binance app: not ours, ignored
    ]
    report = await reconcile(session, ex)
    assert [o.client_order_id for o in report.orphans] == ["bot-orphan"]
    assert known.status == OrderStatus.NEW


async def test_dry_run_never_calls_binance(session):
    ex, fake = exchange(dry_run=True)
    await make_order(session, await make_position(session), binance_id=None)
    report = await reconcile(session, ex)
    assert report.skipped_dry_run and report.changes == []
    assert fake.calls == []


async def test_commission_in_two_assets_is_kept_as_the_larger_one_and_logged(session, caplog):
    ex, fake = exchange()
    order = await make_order(session, await make_position(session))
    filled(order, fake)
    fake.trades[1001] = [
        trade_dict(1, 1001, price="120.41", qty="0.100", commission="0.0001", asset="SOL"),
        trade_dict(2, 1001, price="120.41", qty="0.030", commission="0.00002", asset="BNB"),
        trade_dict(3, 1001, price="120.41", qty="0.000", commission="0.00001", asset="SOL"),
    ]
    await reconcile(session, ex)
    assert order.commission_asset == "SOL" and order.commission == D("0.00011")
    assert "several assets" in caplog.text


async def test_other_exchange_errors_are_reported_not_raised(session):
    ex, fake = exchange()
    order = await make_order(session, await make_position(session))
    fake.raise_on["get_order"] = api_error(-1021, "timestamp")
    report = await reconcile(session, ex)
    assert [oid for oid, _ in report.errors] == [order.id]
