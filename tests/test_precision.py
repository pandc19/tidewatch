from decimal import ROUND_UP, Decimal

import pytest

from bot.config.precision import (
    FilterViolation,
    SymbolFilters,
    format_decimal,
    round_price,
    round_quantity,
    to_decimal,
)

D = Decimal


def _info(symbol, tick, min_price, max_price, step, min_qty, max_qty, **extra):
    """Mirrors the shape of one symbol in /api/v3/exchangeInfo (checked live 2026-10-06)."""
    return {
        "symbol": symbol,
        "baseAsset": symbol.removesuffix("USDT"),
        "quoteAsset": "USDT",
        "ocoAllowed": True,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": min_price, "maxPrice": max_price, "tickSize": tick},
            {"filterType": "LOT_SIZE", "minQty": min_qty, "maxQty": max_qty, "stepSize": step},
            {
                "filterType": "NOTIONAL",
                "minNotional": "5.00000000",
                "applyMinToMarket": True,
                "maxNotional": "9000000.00000000",
                "applyMaxToMarket": False,
                "avgPriceMins": 5,
            },
            {
                "filterType": "PERCENT_PRICE_BY_SIDE",
                "bidMultiplierUp": "1.2",
                "bidMultiplierDown": "0.5",
                "askMultiplierUp": "2",
                "askMultiplierDown": "0.8",
                "avgPriceMins": 5,
            },
        ],
        **extra,
    }


@pytest.fixture
def sol() -> SymbolFilters:
    return SymbolFilters.from_symbol_info(
        _info("SOLUSDT", "0.01000000", "0.01000000", "10000.00000000",
              "0.00100000", "0.00100000", "90000.00000000")
    )


@pytest.fixture
def gtc() -> SymbolFilters:
    return SymbolFilters.from_symbol_info(
        _info("GTCUSDT", "0.00001000", "0.00001000", "10000.00000000",
              "0.10000000", "0.10000000", "9222449.00000000")
    )


# --- rounding -----------------------------------------------------------------------------


def test_quantity_rounds_down_to_step():
    # 0.130 SOL bought, 0.1% fee taken in SOL: 0.12987 held, only 0.129 can be sold.
    assert round_quantity(D("0.12987"), D("0.001")) == D("0.129")


def test_quantity_never_rounds_up():
    assert round_quantity(D("0.1299999"), D("0.001")) == D("0.129")
    assert round_quantity(D("0.0009"), D("0.001")) == D("0")


def test_price_rounds_down_by_default_and_up_on_request():
    assert round_price(D("120.419"), D("0.01")) == D("120.41")
    assert round_price(D("120.411"), D("0.01"), ROUND_UP) == D("120.42")


def test_coarse_step_on_cheap_coin(gtc):
    assert gtc.round_quantity(D("26.99")) == D("26.9")
    assert gtc.round_price(D("0.2180049")) == D("0.21800")


def test_decimal_has_no_float_error():
    assert round_quantity(D("0.3"), D("0.1")) == D("0.3")
    assert round_price(D("1.1") * D("3"), D("0.01")) == D("3.30")


def test_zero_step_means_filter_disabled():
    assert round_quantity(D("1.23456"), D("0")) == D("1.23456")


def test_format_decimal_is_plain_text():
    assert format_decimal(D("0.00100000")) == "0.001"
    assert format_decimal(D("1E+2")) == "100"
    assert format_decimal(D("120.410")) == "120.41"
    assert format_decimal(D("0.000")) == "0"
    assert format_decimal(D("0.00001000")) == "0.00001"


def test_floats_are_refused():
    with pytest.raises(TypeError):
        to_decimal(0.1)
    assert to_decimal("0.1") == D("0.1")


# --- filter parsing -----------------------------------------------------------------------


def test_from_symbol_info_reads_live_shape(sol):
    assert sol.symbol == "SOLUSDT"
    assert sol.tick_size == D("0.01")
    assert sol.step_size == D("0.001")
    assert sol.min_notional == D("5")
    assert sol.oco_allowed is True
    assert sol.price_band is not None
    assert sol.price_band.bid_down == D("0.5")
    assert sol.price_band.ask_up == D("2")


def test_legacy_min_notional_and_percent_price_names():
    info = _info("OLDUSDT", "0.01", "0.01", "1000", "0.01", "0.01", "1000")
    info["filters"] = [
        f for f in info["filters"] if f["filterType"] in ("PRICE_FILTER", "LOT_SIZE")
    ] + [
        {"filterType": "MIN_NOTIONAL", "minNotional": "10.0", "applyToMarket": True, "avgPriceMins": 5},
        {"filterType": "PERCENT_PRICE", "multiplierUp": "5", "multiplierDown": "0.2", "avgPriceMins": 5},
    ]
    f = SymbolFilters.from_symbol_info(info)
    assert f.min_notional == D("10")
    assert f.price_band is not None
    assert f.price_band.bid_up == f.price_band.ask_up == D("5")


def test_missing_notional_and_band_are_tolerated():
    info = _info("BAREUSDT", "0.01", "0.01", "1000", "0.01", "0.01", "1000")
    info["filters"] = info["filters"][:2]
    f = SymbolFilters.from_symbol_info(info)
    assert f.min_notional == D("0")
    assert f.price_band is None
    assert f.violations("BUY", D("10"), D("1")) == []


# --- order checks -------------------------------------------------------------------------


def test_owner_16_dollar_trade_is_valid(sol):
    # $16 wallet * 0.98 at 120.41: 15.68 / 120.41 = 0.1302 SOL -> 0.130
    price = D("120.41")
    qty = sol.round_quantity(D("16") * D("0.98") / price)
    assert qty == D("0.130")
    assert sol.violations("BUY", price, qty, avg_price=D("120.4")) == []


def test_below_min_notional_is_rejected(sol):
    problems = sol.violations("BUY", D("120.41"), D("0.040"))  # $4.82
    assert any(p.startswith("NOTIONAL") for p in problems)


def test_stop_leg_can_fall_under_min_notional(sol):
    # A $5.06 entry is fine, but its 2.2%-below stop-limit sell is under $5: the OCO is refused.
    entry_qty = D("0.042")
    assert sol.violations("BUY", D("120.41"), entry_qty) == []
    stop_limit = sol.round_price(D("120.41") * (1 - D("0.02") - D("0.002")))
    assert any(p.startswith("NOTIONAL") for p in sol.violations("SELL", stop_limit, entry_qty))


def test_price_and_quantity_must_sit_on_the_grid(sol):
    problems = sol.violations("BUY", D("120.415"), D("0.1305"))
    assert any("not a multiple of tick" in p for p in problems)
    assert any("not a multiple of step" in p for p in problems)


def test_quantity_bounds(sol):
    assert any("below minQty" in p for p in sol.violations("BUY", D("120.41"), D("0.0005")))
    assert any("above maxQty" in p for p in sol.violations("BUY", D("120.41"), D("90001")))


def test_price_bounds(sol):
    assert any("below minPrice" in p for p in sol.violations("BUY", D("0.001"), D("10")))
    assert any("above maxPrice" in p for p in sol.violations("SELL", D("10001"), D("1")))


def test_percent_price_band_is_side_specific(sol):
    avg = D("120")
    assert sol.violations("BUY", D("119"), D("0.5"), avg_price=avg) == []
    assert any("PERCENT_PRICE" in p for p in sol.violations("BUY", D("50"), D("0.5"), avg_price=avg))
    # A bid may go up to 1.2x the average, an ask up to 2x.
    assert any("PERCENT_PRICE" in p for p in sol.violations("BUY", D("150"), D("0.5"), avg_price=avg))
    assert sol.violations("SELL", D("150"), D("0.5"), avg_price=avg) == []
    assert any("PERCENT_PRICE" in p for p in sol.violations("SELL", D("300"), D("0.5"), avg_price=avg))


def test_price_band_skipped_without_average(sol):
    assert sol.violations("BUY", D("50"), D("0.5")) == []


def test_validate_order_reports_every_problem(sol):
    with pytest.raises(FilterViolation) as err:
        sol.validate_order("BUY", D("120.415"), D("0.0005"))
    assert len(err.value.violations) >= 3
    assert "PRICE_FILTER" in str(err.value) and "LOT_SIZE" in str(err.value)


def test_validate_order_passes_silently_when_valid(sol):
    sol.validate_order("SELL", D("125.22"), D("0.129"), avg_price=D("120"))


def test_tick_filter_disabled_when_zero():
    info = _info("ZUSDT", "0.00000000", "0.00000000", "0.00000000", "0.001", "0.001", "100")
    f = SymbolFilters.from_symbol_info(info)
    assert f.violations("BUY", D("12.3456789"), D("1")) == []
