from decimal import Decimal

import pytest

from bot.config.precision import FilterViolation
from bot.database.models import OrderStatus
from bot.services.binance_client import (
    BookTicker,
    ExchangeClient,
    ExchangeError,
    ExchangeOrder,
    Fill,
    OrderNotFound,
    OrderRejected,
    to_order_status,
)
from tests.fakes import FakeBinance, api_error, klines_from, order_dict, trade_dict

D = Decimal


def make(fake=None, *, dry_run=False, creds=True, prefix="bot") -> tuple[ExchangeClient, FakeBinance]:
    fake = fake or FakeBinance()
    return ExchangeClient(fake, dry_run=dry_run, order_id_prefix=prefix, has_credentials=creds), fake


# --- market data --------------------------------------------------------------------------


async def test_symbol_filters_are_parsed_and_cached():
    ex, fake = make()
    f1 = await ex.symbol_filters("SOLUSDT")
    f2 = await ex.symbol_filters("SOLUSDT")
    assert f1 is f2 and f1.tick_size == D("0.01")
    assert len(fake.called("get_symbol_info")) == 1


async def test_unknown_or_halted_symbol_is_refused():
    ex, fake = make()
    fake.symbol_info = None
    with pytest.raises(ExchangeError, match="unknown symbol"):
        await ex.symbol_filters("NOPEUSDT")
    ex, fake = make()
    fake.symbol_info = {**fake.symbol_info, "status": "BREAK"}
    with pytest.raises(ExchangeError, match="not trading"):
        await ex.symbol_filters("SOLUSDT")


async def test_book_ticker_spread_is_a_fraction_of_mid():
    ex, _ = make()
    t = await ex.book_ticker("SOLUSDT")
    assert t.bid == D("120.41") and t.ask == D("120.42")
    assert t.spread_pct == D("0.01") / D("120.415")
    assert BookTicker("X", D("100"), D("100.2")).spread_pct > D("0.0019")  # about 0.2%
    assert BookTicker("X", D("0"), D("0")).spread_pct == D("1")  # no market: treated as wide


async def test_balance_and_fee():
    ex, fake = make()
    assert await ex.free_balance("USDT") == D("16")
    assert await ex.free_balance("DOGE") == D("0")
    fee = await ex.account_fees()
    assert fee.maker == fee.taker == D("0.001")
    assert fake.called("get_account") and not fake.called("get_trade_fee")  # /sapi is never used


async def test_candle_closes_drop_the_candle_still_forming():
    ex, fake = make()
    fake.klines["SOLUSDT"] = klines_from(["100", "101", "102", "103"], still_open=True)
    assert await ex.closed_candle_closes("SOLUSDT", "1h", 4) == [D("100"), D("101"), D("102")]
    fake.klines["SOLUSDT"] = klines_from(["100", "101"], still_open=False)
    assert await ex.closed_candle_closes("SOLUSDT", "1h", 2) == [D("100"), D("101")]
    assert fake.called("get_klines")[0] == {"symbol": "SOLUSDT", "interval": "1h", "limit": 4}


async def test_total_balance_counts_locked_funds_too():
    ex, fake = make()

    async def with_locked(**kw):
        fake.calls.append(("get_asset_balance", kw))
        return {"asset": "USDT", "free": "10.5", "locked": "5.5"}

    fake.get_asset_balance = with_locked
    assert await ex.total_balance("USDT") == D("16")


async def test_recent_trades_are_filtered_by_time_and_say_who_bought():
    from datetime import UTC, datetime

    ex, fake = make()
    fake.orders[5] = order_dict(5, "x", side="SELL", qty="0.13")
    fake.fill(5, price="125", qty="0.13")  # recorded at the fake's fixed trade time
    trade_time = (await ex.recent_trades("SOLUSDT", datetime(2000, 1, 1, tzinfo=UTC)))[0].time
    assert [t.is_buyer for t in await ex.recent_trades("SOLUSDT", trade_time)] == [False]
    assert await ex.recent_trades("SOLUSDT", datetime(2100, 1, 1, tzinfo=UTC)) == []
    assert fake.called("get_my_trades")[0] == {"symbol": "SOLUSDT", "limit": 500}  # no 24h time-range query


async def test_signed_calls_need_credentials():
    ex, fake = make(creds=False)
    for call in (ex.free_balance("USDT"), ex.account_fees(), ex.open_orders()):
        with pytest.raises(ExchangeError, match="BINANCE_API_KEY"):
            await call
    assert fake.calls == []


# --- limit buy ----------------------------------------------------------------------------


async def test_limit_buy_sends_exact_plain_strings():
    ex, fake = make()
    order = await ex.place_limit_buy("SOLUSDT", D("120.41"), D("0.130"))
    (sent,) = fake.called("create_order")
    assert sent["price"] == "120.41" and sent["quantity"] == "0.13"
    assert sent["side"] == "BUY" and sent["type"] == "LIMIT" and sent["timeInForce"] == "GTC"
    assert sent["newClientOrderId"].startswith("bot-") and len(sent["newClientOrderId"]) <= 36
    assert order.order_id == 1001 and order.status == "NEW"


async def test_dry_run_buy_never_reaches_the_order_endpoint():
    ex, fake = make(dry_run=True, creds=False)
    order = await ex.place_limit_buy("SOLUSDT", D("120.41"), D("0.130"))
    assert fake.called("create_order") == []
    assert order.order_id is None and order.status == "NEW" and order.orig_qty == D("0.130")


async def test_invalid_buy_is_blocked_before_sending():
    ex, fake = make()
    for price, qty in [(D("120.415"), D("0.130")), (D("120.41"), D("0.040")), (D("50"), D("0.130"))]:
        with pytest.raises(FilterViolation):
            await ex.place_limit_buy("SOLUSDT", price, qty)
    assert fake.called("create_order") == []


async def test_invalid_buy_is_blocked_in_dry_run_too():
    ex, _ = make(dry_run=True)
    with pytest.raises(FilterViolation):
        await ex.place_limit_buy("SOLUSDT", D("120.41"), D("0.040"))


# --- OCO ----------------------------------------------------------------------------------

OCO = {
    "quantity": D("0.129"),
    "take_profit_price": D("125.22"),
    "stop_price": D("118.00"),
    "stop_limit_price": D("117.76"),
}


async def test_oco_uses_the_new_endpoint_parameters():
    ex, fake = make()
    res = await ex.place_oco_sell("SOLUSDT", **OCO)
    (sent,) = fake.called("create_oco_order")
    assert sent["side"] == "SELL" and sent["quantity"] == "0.129"
    assert (sent["aboveType"], sent["abovePrice"]) == ("LIMIT_MAKER", "125.22")
    assert (sent["belowType"], sent["belowStopPrice"], sent["belowPrice"]) == ("STOP_LOSS_LIMIT", "118", "117.76")
    assert sent["belowTimeInForce"] == "GTC"
    assert res.order_list_id == 55
    assert res.take_profit.order_type == "LIMIT_MAKER" and res.stop_loss.order_type == "STOP_LOSS_LIMIT"
    assert res.take_profit.order_list_id == res.stop_loss.order_list_id == 55


async def test_oco_leg_ids_are_distinct_and_within_36_chars():
    ex, fake = make(prefix="abcdefghijkl")  # longest allowed prefix
    await ex.place_oco_sell("SOLUSDT", **OCO)
    (sent,) = fake.called("create_oco_order")
    ids = {sent["listClientOrderId"], sent["aboveClientOrderId"], sent["belowClientOrderId"]}
    assert len(ids) == 3
    assert all(len(i) <= 36 for i in ids)
    assert sent["aboveClientOrderId"].endswith("-tp") and sent["belowClientOrderId"].endswith("-sl")


async def test_oco_rejects_a_list_id_with_no_room_for_suffixes():
    ex, fake = make()
    with pytest.raises(ExchangeError, match="too long"):
        await ex.place_oco_sell("SOLUSDT", **OCO, list_client_order_id="x" * 36)
    assert fake.called("create_oco_order") == []


async def test_dry_run_oco_is_not_sent_and_has_two_legs():
    ex, fake = make(dry_run=True, creds=False)
    res = await ex.place_oco_sell("SOLUSDT", **OCO)
    assert fake.called("create_oco_order") == []
    assert res.order_list_id is None
    assert res.stop_loss.stop_price == D("118.00") and res.stop_loss.price == D("117.76")


@pytest.mark.parametrize(
    "override",
    [
        {"stop_price": D("126")},  # stop above the take-profit
        {"stop_limit_price": D("119")},  # limit above its own trigger
        {"quantity": D("0.040")},  # under the minimum order value
        {"stop_price": D("118.005")},  # off the tick grid
    ],
)
async def test_bad_oco_is_blocked_before_sending(override):
    ex, fake = make()
    with pytest.raises((FilterViolation, ExchangeError)):
        await ex.place_oco_sell("SOLUSDT", **{**OCO, **override})
    assert fake.called("create_oco_order") == []


# --- queries / cancel ---------------------------------------------------------------------


async def test_get_order_maps_missing_to_order_not_found():
    ex, _ = make()
    with pytest.raises(OrderNotFound):
        await ex.get_order("SOLUSDT", order_id=999)


async def test_get_order_other_errors_become_exchange_error():
    ex, fake = make()
    fake.raise_on["get_order"] = api_error(-1021, "timestamp outside recvWindow")
    with pytest.raises(ExchangeError) as err:
        await ex.get_order("SOLUSDT", order_id=1)
    assert not isinstance(err.value, OrderNotFound)


async def test_get_order_by_either_id():
    ex, fake = make()
    fake.orders[7] = order_dict(7, "bot-abc", status="FILLED", executed="0.130", quote="15.6533")
    assert (await ex.get_order("SOLUSDT", order_id=7)).status == "FILLED"
    assert (await ex.get_order("SOLUSDT", client_order_id="bot-abc")).order_id == 7
    with pytest.raises(ValueError):
        await ex.get_order("SOLUSDT")


async def test_cancel_returns_none_when_already_gone():
    ex, fake = make()
    fake.raise_on["cancel_order"] = api_error(-2011, "Unknown order sent.")
    assert await ex.cancel_order("SOLUSDT", order_id=1) is None


async def test_cancelling_one_oco_leg_returns_that_leg_not_a_crash():
    # Real testnet behaviour (2026-10-06): the reply describes the whole list, and both legs cancel.
    ex, fake = make()
    oco = await ex.place_oco_sell("SOLUSDT", **OCO)
    leg = await ex.cancel_order("SOLUSDT", order_id=oco.stop_loss.order_id)
    assert leg.order_id == oco.stop_loss.order_id and leg.status == "CANCELED"
    assert leg.order_list_id == oco.order_list_id
    assert fake.orders[oco.take_profit.order_id]["status"] == "CANCELED"  # the other leg went too


async def test_a_refused_order_is_order_rejected_with_the_code():
    ex, fake = make()
    fake.raise_on["create_oco_order"] = api_error(-2010, "The relationship of the prices for the orders is not correct.")
    with pytest.raises(OrderRejected) as err:
        await ex.place_oco_sell("SOLUSDT", **OCO)
    assert err.value.code == -2010 and "relationship of the prices" in str(err.value)


async def test_an_unclear_failure_is_not_reported_as_a_rejection():
    # A timeout or clock error does not tell us whether the order exists: the caller must reconcile.
    ex, fake = make()
    fake.raise_on["create_order"] = api_error(-1021, "Timestamp outside of recvWindow")
    with pytest.raises(ExchangeError) as err:
        await ex.place_limit_buy("SOLUSDT", D("120.41"), D("0.130"))
    assert not isinstance(err.value, OrderRejected) and err.value.code == -1021


async def test_cancel_is_not_sent_in_dry_run():
    ex, fake = make(dry_run=True, creds=False)
    assert await ex.cancel_order("SOLUSDT", order_id=1) is None
    assert fake.calls == []


async def test_order_fills_parse_commission():
    ex, fake = make()
    fake.trades[7] = [trade_dict(1, 7, price="120.41", qty="0.13", commission="0.00013", asset="SOL")]
    (fill,) = await ex.order_fills("SOLUSDT", 7)
    assert isinstance(fill, Fill)
    assert fill.commission == D("0.00013") and fill.commission_asset == "SOL" and fill.is_maker


# --- parsing / ids ------------------------------------------------------------------------


def test_exchange_order_parsing_handles_no_list_and_no_stop():
    o = ExchangeOrder.from_binance(order_dict(1, "c", list_id=-1))
    assert o.order_list_id is None and o.stop_price is None
    o = ExchangeOrder.from_binance(order_dict(2, "c", list_id=9, stop_price="118.00", executed="0.1", quote="12"))
    assert o.order_list_id == 9 and o.stop_price == D("118")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("NEW", OrderStatus.NEW),
        ("PENDING_NEW", OrderStatus.NEW),
        ("PARTIALLY_FILLED", OrderStatus.PARTIALLY_FILLED),
        ("FILLED", OrderStatus.FILLED),
        ("CANCELED", OrderStatus.CANCELED),
        ("EXPIRED", OrderStatus.EXPIRED),
        ("EXPIRED_IN_MATCH", OrderStatus.EXPIRED),
        ("REJECTED", OrderStatus.REJECTED),
        ("PENDING_CANCEL", None),
    ],
)
def test_status_mapping(raw, expected):
    assert to_order_status(raw) is expected


def test_unknown_status_is_an_error_not_a_guess():
    with pytest.raises(ExchangeError):
        to_order_status("SOMETHING_NEW")


def test_client_order_ids():
    ex, _ = make(prefix="tb")
    a, b = ex.new_client_order_id(), ex.new_client_order_id()
    assert a != b and a.startswith("tb-") and len(a) <= 36
    assert ex.owns(a) and not ex.owns("other-123") and not ex.owns("tbx-123")
