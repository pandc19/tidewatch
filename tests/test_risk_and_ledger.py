from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bot.config.precision import SymbolFilters
from bot.engine.ledger import compute_trade_result, fee_value
from bot.services.risk_manager import size_entry
from tests.fakes import SOL_INFO

D = Decimal
T1, T2 = datetime(2026, 10, 6, 12, tzinfo=UTC), datetime(2026, 10, 7, 9, tzinfo=UTC)


@pytest.fixture
def sol() -> SymbolFilters:
    return SymbolFilters.from_symbol_info(SOL_INFO)


KW = {"stop_loss_pct": D("0.02"), "stop_limit_offset_pct": D("0.002")}


# --- sizing -------------------------------------------------------------------------------


def test_the_owners_16_dollar_wallet_sizes_to_a_valid_trade(sol):
    assert size_entry(D("16"), D("0.98"), D("120.84"), sol, **KW) == D("0.129")


def test_a_wallet_too_small_for_the_minimum_order_gets_no_trade(sol):
    assert size_entry(D("5"), D("0.98"), D("120.84"), sol, **KW) is None  # $4.90 < $5


def test_a_trade_whose_stop_leg_could_not_be_placed_is_refused(sol):
    # $5.17 buys 0.043 SOL: a valid $5.16 buy. After a worst-case fee only 0.042 SOL can be
    # sold, and at the stop-limit price (117.37) that is $4.93, under the $5 minimum: the
    # OCO would be refused and the coins left unprotected.
    assert size_entry(D("5.17"), D("1"), D("120.00"), sol, **KW) is None


def test_a_trade_just_big_enough_to_be_protected_is_allowed(sol):
    assert size_entry(D("5.60"), D("1"), D("120.00"), sol, **KW) is not None


@pytest.mark.parametrize("free,price", [(D("0"), D("100")), (D("-1"), D("100")), (D("16"), D("0"))])
def test_nonsense_inputs_give_no_trade(sol, free, price):
    assert size_entry(free, D("0.98"), price, sol, **KW) is None


def test_quantity_always_rounds_down_on_the_step(sol):
    qty = size_entry(D("1000"), D("0.5"), D("123.456"), sol, **KW)
    assert qty % sol.step_size == 0 and qty * D("123.456") <= D("500")


# --- fees ---------------------------------------------------------------------------------


def test_fee_values_in_each_asset():
    kw = {"base": "SOL", "quote": "USDT", "price": D("120")}
    assert fee_value(D("0.00013"), "SOL", **kw) == D("0.0156")
    assert fee_value(D("0.05"), "USDT", **kw) == D("0.05")
    assert fee_value(D("0.001"), "BNB", **kw, asset_price=D("780")) == D("0.78")
    assert fee_value(D("0"), "SOL", **kw) == 0 and fee_value(D("1"), None, **kw) == 0


def test_a_fee_in_a_third_coin_needs_its_price():
    with pytest.raises(ValueError, match="BNB"):
        fee_value(D("0.001"), "BNB", base="SOL", quote="USDT", price=D("120"))


# --- trade result -------------------------------------------------------------------------


def result(**over):
    args = {
        "symbol": "SOLUSDT", "buy_qty": D("0.13"), "buy_price": D("120.41"), "buy_coin_fee": D("0.00013"),
        "sell_qty": D("0.129"), "sell_price": D("125.22"),
        "buy_fee_value": D("0.0156533"), "sell_fee_value": D("0.0161534"),
        "buy_time": T1, "sell_time": T2,
    }
    return compute_trade_result(**{**args, **over})


def test_take_profit_with_standard_fees():
    r = result()
    share = D("0.129") / D("0.12987")  # of the coins received, 0.129 were sold: the rest is a leftover
    assert r.total_buy == D("0.129") * D("120.41")  # the coins sold, not the 0.130 ordered
    assert r.total_sell == D("16.15338")
    assert r.buy_fee_usd == (D("0.0156533") * share).quantize(D("1e-8")) and r.sell_fee_usd == D("0.0161534")
    assert r.fees_usd == r.buy_fee_usd + r.sell_fee_usd
    assert r.net_profit == r.total_sell - r.total_buy - r.fees_usd
    assert r.amount_operated == D("0.129") and r.sell_time > r.buy_time


def test_the_unsold_leftover_is_not_booked_as_a_loss():
    """The SNDKB stop-loss of 2026-10-21: bought 0.0114, 0.0000114 taken as fee, sold 0.0113."""
    r = result(
        symbol="SNDKBUSDT", buy_qty=D("0.0114"), buy_price=D("1392.91"), buy_coin_fee=D("0.0000114"),
        sell_qty=D("0.0113"), sell_price=D("1364.91"),
        buy_fee_value=D("0.0000114") * D("1392.91"), sell_fee_value=D("0.01542348"),
    )
    assert r.total_buy == D("15.739883") and r.total_sell == D("15.423483")
    assert round(r.net_profit, 4) == D("-0.3476")  # was -0.4711 when the leftover counted as spent
    assert r.net_profit == r.total_sell - r.total_buy - r.buy_fee_usd - r.sell_fee_usd


def test_stop_loss_is_a_negative_result_with_a_negative_return():
    r = result(sell_price=D("118.00"), sell_fee_value=D("0.0152"))
    assert r.net_profit < 0 and r.return_pct < 0


def test_selling_every_coin_received_counts_the_whole_buy_fee():
    r = result(buy_qty=D("0.13"), buy_coin_fee=D("0.00013"), sell_qty=D("0.12987"))
    assert r.buy_fee_usd == D("0.0156533")


def test_return_pct_is_net_over_the_cost_of_the_coins_sold():
    r = result(sell_qty=D("0.13"), sell_price=D("125.2264"), buy_fee_value=D("0"), sell_fee_value=D("0"))
    assert r.net_profit == D("0.13") * D("125.2264") - D("0.13") * D("120.41")
    assert r.return_pct == r.net_profit / (D("0.13") * D("120.41"))


def test_bnb_fees_on_both_sides_are_costs_and_nothing_was_left_over():
    r = result(buy_qty=D("0.129"), buy_coin_fee=D("0"), buy_fee_value=D("0.01"), sell_fee_value=D("0.01"))
    assert r.buy_fee_usd == D("0.01")
    assert r.net_profit == r.total_sell - r.total_buy - D("0.02")


def test_a_swept_leftover_is_costed_at_this_buy_price_and_the_buy_fee_is_not_doubled():
    r = result(buy_qty=D("0.129"), buy_coin_fee=D("0"), sell_qty=D("0.1295"), buy_fee_value=D("0.01"))
    assert r.buy_fee_usd == D("0.01") and r.total_buy == D("0.1295") * D("120.41")


def test_zero_cost_trade_does_not_divide_by_zero():
    r = result(buy_qty=D("0"), buy_coin_fee=D("0"), sell_qty=D("0"))
    assert r.return_pct == 0 and r.buy_fee_usd == 0
