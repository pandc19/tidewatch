from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bot.config.precision import SymbolFilters
from bot.database.models import OrderSide
from bot.engine.ledger import compute_trade_result, fee_value, is_cash_fee
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


def test_only_the_coin_fee_on_a_buy_is_not_a_cash_cost():
    assert not is_cash_fee(OrderSide.BUY, "SOL", "SOL")
    assert is_cash_fee(OrderSide.BUY, "BNB", "SOL")
    assert is_cash_fee(OrderSide.SELL, "USDT", "SOL")


# --- trade result -------------------------------------------------------------------------


def result(**over):
    args = {
        "symbol": "SOLUSDT", "buy_qty": D("0.13"), "buy_price": D("120.41"),
        "sell_qty": D("0.129"), "sell_price": D("125.22"),
        "buy_fee_value": D("0.0156533"), "sell_fee_value": D("0.0161534"),
        "buy_fee_is_cash": False, "sell_fee_is_cash": True,
        "buy_time": T1, "sell_time": T2,
    }
    return compute_trade_result(**{**args, **over})


def test_take_profit_with_standard_fees():
    r = result()
    assert r.total_buy == D("15.6533")
    assert r.total_sell == D("16.15338")
    # profit = sell - buy - quote-asset fee on the sell. The SOL fee on the buy is already
    # reflected in selling 0.129 instead of 0.130, so it is not subtracted a second time.
    assert r.net_profit == D("16.15338") - D("15.6533") - D("0.0161534")
    assert r.fees_usd == D("0.0156533") + D("0.0161534")  # reported in full
    assert r.amount_operated == D("0.129") and r.sell_time > r.buy_time


def test_stop_loss_is_a_negative_result_with_a_negative_return():
    r = result(sell_price=D("118.00"), sell_fee_value=D("0.0152"))
    assert r.net_profit < 0 and r.return_pct < 0


def test_return_pct_is_net_over_cash_spent():
    r = result(sell_qty=D("0.13"), sell_price=D("125.2264"), buy_fee_value=D("0"), sell_fee_value=D("0"))
    assert r.net_profit == D("0.13") * D("125.2264") - D("15.6533")
    assert r.return_pct == r.net_profit / D("15.6533")


def test_bnb_fees_on_both_sides_are_cash_costs():
    r = result(buy_fee_is_cash=True, buy_qty=D("0.129"), buy_fee_value=D("0.01"), sell_fee_value=D("0.01"))
    assert r.net_profit == r.total_sell - r.total_buy - D("0.02")


def test_zero_cost_trade_does_not_divide_by_zero():
    assert result(buy_qty=D("0"), sell_qty=D("0")).return_pct == 0
