import io
from decimal import Decimal

import pytest

from bot.replay import candles as store
from bot.replay.exits import (
    DEFAULT_VARIANTS,
    OPEN_AT_END,
    Atr,
    BreakEven,
    Costs,
    Fixed,
    Partial,
    Trailing,
    parse_variant,
)
from bot.replay.report import full_report, write_trades
from bot.replay.simulate import EntryParams, Signal, find_signals, net_return, run
from bot.replay.stats import split_at, summarize
from bot.services.binance_client import Candle, ExchangeClient
from tests.fakes import FakeBinance
from tests.test_scanner import dip_then

D = Decimal
H = 3_600_000
COSTS = Costs(fee=D("0.001"), slippage=D("0.0005"))
ENTRY = D(100)


def C(open_, high, low, close, i=0) -> Candle:
    return Candle(i * H, D(open_), D(high), D(low), D(close), i * H + H - 1)


def flat(i, price="100") -> Candle:
    return C(price, price, price, price, i)


# --- exit rules -----------------------------------------------------------------------------


def test_fixed_take_profit_fills_at_the_target():
    out = Fixed(D("0.04"), D("0.02")).run(ENTRY, [], [C(100, 105, 99, 104)], COSTS)
    assert (out.price, out.bars, out.reason) == (D("104.00"), 1, "TAKE_PROFIT")


def test_fixed_stop_loss_fills_a_little_below_the_stop():
    out = Fixed(D("0.04"), D("0.02")).run(ENTRY, [], [flat(1), C(100, 101, 97, 98)], COSTS)
    assert (out.price, out.bars, out.reason) == (D("98.00") * D("0.9995"), 2, "STOP_LOSS")


def test_a_candle_touching_both_levels_counts_as_a_stop():
    out = Fixed(D("0.04"), D("0.02")).run(ENTRY, [], [C(100, 105, 97, 100)], COSTS)
    assert out.reason == "STOP_LOSS"


def test_a_gap_down_through_the_stop_fills_at_the_open():
    out = Fixed(D("0.04"), D("0.02")).run(ENTRY, [], [C(95, 96, 94, 95)], COSTS)
    assert out.price == D("95") * D("0.9995")


def test_a_trade_still_open_when_the_data_ends_is_sold_at_the_last_close():
    out = Fixed(D("0.04"), D("0.02")).run(ENTRY, [], [flat(1), C(100, 101, 99, 100.5)], COSTS)
    assert (out.price, out.bars, out.reason) == (D("100.5"), 2, OPEN_AT_END)
    assert Fixed(D("0.04"), D("0.02")).run(ENTRY, [], [], COSTS).bars == 0


def test_trailing_stop_follows_the_high_and_only_applies_from_the_next_candle():
    # candle 1 rises to 110 and its low (99) is above the 98 stop: no exit. The stop becomes 107.8.
    out = Trailing(D("0.02"), D("0.02")).run(ENTRY, [], [C(100, 110, 99, 109), C(109, 109, 107, 108)], COSTS)
    assert (out.price, out.bars, out.reason) == (D("107.8") * D("0.9995"), 2, "TRAILING_STOP")


def test_break_even_moves_the_stop_to_the_entry_plus_fees_after_the_trigger():
    out = BreakEven(D("0.10"), D("0.02"), D("0.03")).run(
        ENTRY, [], [C(100, 103.5, 99, 103), C(103, 103, 100, 101)], COSTS
    )
    assert (out.price, out.bars, out.reason) == (D("100.2") * D("0.9995"), 2, "BREAKEVEN_STOP")


def test_partial_sells_half_at_the_target_and_trails_the_rest():
    out = Partial(D("0.04"), D("0.02"), D("0.02")).run(
        ENTRY, [], [C(100, 105, 99, 104), C(104, 104, 102, 103)], COSTS
    )
    rest = D("105") * D("0.98") * D("0.9995")  # the trail from the 105 high
    assert (out.price, out.bars, out.reason) == (D("0.5") * D("104") + D("0.5") * rest, 2, "PARTIAL_THEN_STOP")


def test_atr_levels_come_from_the_recent_candle_range():
    history = [C(100, 101, 99, 100, i) for i in range(16)]  # every candle ranges 2: ATR is 2%
    future = [C(100, 105, 99, 104)]
    atr = Atr(D("2"), D("1")).run(ENTRY, history, future, COSTS)
    assert atr == Fixed(D("0.04"), D("0.02")).run(ENTRY, [], future, COSTS)


def test_variant_specs_parse_and_bad_ones_are_refused():
    assert parse_variant("fixed:0.04:0.02") == Fixed(D("0.04"), D("0.02"))
    assert parse_variant("partial:0.04:0.02:0.02") == Partial(D("0.04"), D("0.02"), D("0.02"))
    assert all(parse_variant(spec) for spec in DEFAULT_VARIANTS)
    for bad in ("fixed:0.04", "nope:1:2", "fixed:a:b", "fixed:0:0.02"):
        with pytest.raises(ValueError):
            parse_variant(bad)


def test_net_return_charges_the_fee_on_both_sides():
    assert net_return(D(100), D(104), D("0.001")) == D(104) * D("0.999") / (D(100) * D("1.001")) - 1
    assert net_return(D(100), D(100), D("0")) == 0


# --- the simulation ------------------------------------------------------------------------


def series(*prices_by_symbol):
    return {name: candles for name, candles in prices_by_symbol}


def test_one_position_at_a_time_and_the_lowest_rsi_wins():
    a = [flat(0), flat(1), C(100, 105, 100, 100, 2), C(100, 101, 100, 100, 3), C(100, 105, 100, 100, 4)]
    b = [flat(0), flat(1), C(100, 100, 97, 98, 2), flat(3), flat(4)]
    data = {"AAA": a, "BBB": b}
    signals = [
        Signal("AAA", 1, 1 * H, D(25), D(100)),
        Signal("BBB", 1, 1 * H, D(20), D(100)),  # lowest RSI: BBB is bought, and stopped out on candle 2
        Signal("AAA", 2, 2 * H, D(10), D(100)),  # allowed: the stop happened during candle 2
        Signal("AAA", 3, 3 * H, D(10), D(100)),  # ignored: that trade is still open
    ]
    trades = run(signals, data, Fixed(D("0.04"), D("0.02")), COSTS)
    assert [(t.symbol, t.reason, t.bars) for t in trades] == [("BBB", "STOP_LOSS", 1), ("AAA", "TAKE_PROFIT", 2)]
    assert trades[0].net_return < 0 < trades[1].net_return
    assert trades[1].entry_time == 2 * H and trades[1].exit_time == 4 * H


def test_a_trade_open_at_the_end_stops_the_replay():
    data = {"AAA": [flat(0), flat(1), flat(2), flat(3)]}
    signals = [Signal("AAA", 1, 1 * H, D(20), D(100)), Signal("AAA", 2, 2 * H, D(20), D(100))]
    (trade,) = run(signals, data, Fixed(D("0.04"), D("0.02")), COSTS)
    assert trade.reason == OPEN_AT_END and trade.exit_time == 3 * H


def test_signals_come_from_the_bots_own_entry_function():
    closes = dip_then("1.10")
    candles = [Candle(i * H, c, c, c, c, i * H + H - 1) for i, c in enumerate(closes)]
    params = EntryParams(14, D(30), 6, 9)
    found = find_signals({"AAA": candles}, params, window=len(closes))
    assert [s.index for s in found] == [len(closes) - 1] and found[0].price == closes[-1]
    assert find_signals({"AAA": candles[:-1]}, params, window=len(closes) - 1) == []


# --- statistics -----------------------------------------------------------------------------


def trade(net, entry=0):
    from bot.replay.simulate import Trade

    return Trade("AAA", entry, D(100), entry + H, D(100), "TAKE_PROFIT", 1, D(25), D(net))


def test_summary_counts_wins_and_the_worst_losing_streak():
    s = summarize([trade(x) for x in ("0.1", "-0.05", "-0.02", "-0.01", "0.03")])
    assert (s.trades, s.wins, s.mean, s.worst_streak) == (5, 2, D("0.01"), 3)
    assert s.verdict == "too few trades" and s.low < s.mean < s.high
    assert summarize([]).trades == 0


def test_the_verdict_needs_enough_trades_and_a_range_clear_of_zero():
    wins = summarize([trade("0.010" if i % 2 else "0.011") for i in range(40)])
    losses = summarize([trade("-0.010" if i % 2 else "-0.011") for i in range(40)])
    noisy = summarize([trade("0.05" if i % 2 else "-0.05") for i in range(40)])
    assert (wins.verdict, losses.verdict, noisy.verdict) == ("above zero", "below zero", "not distinguishable from zero")


def test_the_split_is_by_entry_time():
    before, after = split_at([trade("0.1", 5), trade("0.1", 10), trade("0.1", 15)], 10)
    assert (len(before), len(after)) == (1, 2)


def test_the_report_has_both_periods_and_the_count_of_variants_tried():
    results = {"fixed": [trade("0.01", 1 * H)], "trailing": []}
    text = full_report(results, 5 * H, pairs=True, tried=2)
    assert "Variants tried: 2" in text and "held out" in text and "Per pair" in text
    out = io.StringIO()
    write_trades(out, results)
    assert out.getvalue().splitlines()[1].startswith("fixed,AAA,1970-01-01T01:00:00Z,100")


# --- candle history -------------------------------------------------------------------------


def rows(first, last):
    """Binance-shaped klines for hourly candles opening at first*H .. last*H."""
    return [[i * H, "100", "101", "99", "100", "1", i * H + H - 1, "0", 1, "0", "0", "0"] for i in range(first, last + 1)]


async def test_the_exchange_reads_public_candles():
    fake = FakeBinance()
    fake.klines["AAA"] = rows(0, 2)
    exchange = ExchangeClient(fake, dry_run=True, has_credentials=False)
    got = await exchange.candles("AAA", "1h", start_ms=0, end_ms=10 * H)
    assert [c.open_time for c in got] == [0, H, 2 * H] and got[0].high == D(101)
    assert fake.called("get_klines")[0] == {"symbol": "AAA", "interval": "1h", "startTime": 0, "endTime": 10 * H, "limit": 1000}


async def test_history_drops_the_forming_candle_caches_and_downloads_only_the_tail(tmp_path):
    fake = FakeBinance()
    exchange = ExchangeClient(fake, dry_run=True, has_credentials=False)
    fake.klines["AAA"] = rows(76, 100)  # the last one is still forming at now = 100 h
    first = await store.history(exchange, "AAA", "1h", 1, tmp_path, now_ms=100 * H)
    assert [c.open_time // H for c in first] == list(range(76, 100))
    assert (tmp_path / "AAA_1h.csv").exists()

    fake.klines["AAA"] = rows(99, 101)  # an hour later: only the tail comes back
    second = await store.history(exchange, "AAA", "1h", 1, tmp_path, now_ms=101 * H)
    assert fake.called("get_klines")[-1]["startTime"] == 99 * H + 1  # not the whole history again
    assert [c.open_time // H for c in second] == list(range(77, 101))

    fake.klines["AAA"] = rows(76, 100)
    again = await store.history(exchange, "AAA", "1h", 1, tmp_path, now_ms=100 * H, refresh=True)
    assert fake.called("get_klines")[-1]["startTime"] == 76 * H and len(again) == 24


def test_the_cache_round_trips_exactly(tmp_path):
    candles = [Candle(0, D("0.00001234"), D("1E+2"), D("0.5"), D("1"), H - 1)]
    store.save(tmp_path / "x.csv", candles)
    (got,) = store.load(tmp_path / "x.csv")
    assert got == candles[0] and store.load(tmp_path / "missing.csv") == []
