from decimal import Decimal

import pytest

from bot.config.settings import Settings
from bot.services.binance_client import ExchangeClient
from bot.services.indicators import ema_series, rsi_series
from bot.services.scanner import Scanner, evaluate_signal
from tests.fakes import FakeBinance, klines_from

D = Decimal
KW = {"rsi_period": 14, "rsi_oversold": D("30"), "rsi_lookback": 6, "ema_period": 9}


def decline(start: Decimal, candles: int, step: str = "0.985") -> list[Decimal]:
    out, price = [], start
    for _ in range(candles):
        price *= D(step)
        out.append(price)
    return out


def dip_then(last: str) -> list[Decimal]:
    """Flat, a steep decline (RSI collapses), then one candle that closes at `last` x the low."""
    flat = [D(100)] * 40
    down = decline(D(100), 25)
    return [*flat, *down, down[-1] * D(last)]


def series(closes):
    return rsi_series(closes, 14), ema_series(closes, 9)


def test_a_dip_followed_by_a_cross_up_is_a_signal():
    closes = dip_then("1.10")
    rsis, emas = series(closes)
    assert min(rsis[-6:]) <= 30  # the dip really was oversold
    assert closes[-1] > emas[-1] and closes[-2] <= emas[-2]  # and the last candle crossed up
    assert evaluate_signal(closes, **KW) == rsis[-1]


def test_a_dip_that_does_not_recover_through_the_ema_is_not_a_signal():
    closes = dip_then("1.005")  # a tiny bounce, still far under the EMA
    assert closes[-1] < series(closes)[1][-1]
    assert evaluate_signal(closes, **KW) is None


def wobble(candles: int, start: Decimal | None = None) -> list[Decimal]:
    """Small ups and downs, slightly rising: RSI hovers mid-range instead of collapsing."""
    out, price = [], start if start is not None else D(100)
    for i in range(candles):
        price *= D("1.004") if i % 2 == 0 else D("0.997")
        out.append(price)
    return out


def test_a_cross_up_without_an_oversold_dip_is_not_a_signal():
    base = wobble(60)
    mild = [*base, base[-1] * D("0.988"), base[-1] * D("0.988") ** 2]  # two small down candles
    closes = [*mild, mild[-1] * D("1.04")]
    rsis, emas = series(closes)
    assert min(rsis[-6:]) > 30  # RSI never got oversold
    assert closes[-1] > emas[-1] and closes[-2] <= emas[-2]  # yet the close did cross up
    assert evaluate_signal(closes, **KW) is None


def test_an_oversold_reading_older_than_the_lookback_does_not_count():
    old = dip_then("1.10")  # a deep, genuinely oversold dip, already rebounded
    old = [*old, *wobble(12, old[-1])]  # many calm candles later
    old = [*old, old[-1] * D("0.985"), old[-1] * D("0.985") ** 2]  # a mild new dip
    closes = [*old, old[-1] * D("1.05")]
    rsis, emas = series(closes)
    assert min(rsis) <= 30 and min(rsis[-6:]) > 30  # oversold once, but not within the lookback
    assert closes[-1] > emas[-1] and closes[-2] <= emas[-2]
    assert evaluate_signal(closes, **KW) is None


def test_the_signal_fires_on_one_candle_only():
    closes = dip_then("1.10")
    assert evaluate_signal(closes, **KW) is not None
    closes.append(closes[-1] * D("1.01"))  # the next candle is already above the EMA: no longer a cross
    assert evaluate_signal(closes, **KW) is None


def test_too_little_history_is_no_signal():
    assert evaluate_signal([D(100)] * 10, **KW) is None


# --- the scanner --------------------------------------------------------------------------


def candles(closes):
    """Kline rows for these closes plus the still-forming candle the client drops."""
    return klines_from([str(c) for c in [*closes, closes[-1]]], still_open=True)


@pytest.fixture
def world():
    fake = FakeBinance()
    exchange = ExchangeClient(fake, dry_run=True)
    clock = {"t": 1000.0}

    def make(**env):
        return Scanner(Settings(_env_file=None, **env), exchange, clock=lambda: clock["t"])

    return fake, make, clock


async def test_the_lowest_rsi_among_signalling_pairs_wins(world):
    fake, make, _ = world
    deep = dip_then("1.10")
    shallow = [D(100)] * 40 + decline(D(100), 14, "0.975")
    shallow = [*shallow, shallow[-1] * D("1.12")]
    fake.klines["BTCUSDT"], fake.klines["ETHUSDT"] = candles(shallow), candles(deep)
    fake.klines["SOLUSDT"] = candles([D(100)] * 60)  # nothing happening
    assert evaluate_signal(shallow, **KW) is not None  # both really signal

    found = await make(SYMBOLS="BTCUSDT,ETHUSDT,SOLUSDT").scan()

    assert [c.symbol for c in found] == sorted([c.symbol for c in found], key=lambda s: {c.symbol: c.rsi for c in found}[s])
    assert {c.symbol for c in found} == {"BTCUSDT", "ETHUSDT"}
    best = await make(SYMBOLS="BTCUSDT,ETHUSDT,SOLUSDT").next_entry()
    assert best.symbol == found[0].symbol


async def test_a_wide_spread_removes_a_signalling_pair(world):
    fake, make, _ = world
    fake.klines["BTCUSDT"] = fake.klines["ETHUSDT"] = candles(dip_then("1.10"))
    fake.tickers["BTCUSDT"] = ("100.00", "100.50")  # 0.5% wide: over the 0.2% limit
    fake.tickers["ETHUSDT"] = ("100.00", "100.01")
    found = await make(SYMBOLS="BTCUSDT,ETHUSDT").scan()
    assert [c.symbol for c in found] == ["ETHUSDT"]


async def test_the_entry_is_a_limit_at_the_bid_or_a_set_discount_below_it(world):
    fake, make, _ = world
    fake.klines["SOLUSDT"] = candles(dip_then("1.10"))
    fake.tickers["SOLUSDT"] = ("120.41", "120.42")
    assert (await make(SYMBOLS="SOLUSDT").scan())[0].price == D("120.41")
    discounted = (await make(SYMBOLS="SOLUSDT", ENTRY_DISCOUNT_PCT="0.01").scan())[0].price
    assert discounted == D("119.20")  # 120.41 * 0.99 = 119.2059, rounded down to the tick


async def test_it_scans_at_most_once_per_interval(world):
    fake, make, clock = world
    fake.klines["SOLUSDT"] = candles(dip_then("1.10"))
    scanner = make(SYMBOLS="SOLUSDT", SCAN_EVERY_SECONDS="60")
    assert await scanner.next_entry() is not None
    calls = len(fake.called("get_klines"))
    clock["t"] += 30
    assert await scanner.next_entry() is None and len(fake.called("get_klines")) == calls  # too soon
    clock["t"] += 31
    assert await scanner.next_entry() is not None


async def test_one_failing_pair_does_not_hide_the_others(world):
    fake, make, _ = world
    fake.klines["ETHUSDT"] = candles(dip_then("1.10"))

    original = fake.get_klines

    async def flaky(**kw):
        if kw["symbol"] == "BTCUSDT":
            raise RuntimeError("exchange hiccup")
        return await original(**kw)

    fake.get_klines = flaky
    assert [c.symbol for c in await make(SYMBOLS="BTCUSDT,ETHUSDT").scan()] == ["ETHUSDT"]


async def test_no_signal_means_no_entry(world):
    fake, make, _ = world
    fake.klines["SOLUSDT"] = candles([D(100)] * 60)
    assert await make(SYMBOLS="SOLUSDT").next_entry() is None


async def test_the_unfinished_candle_is_never_used(world):
    fake, make, _ = world
    closes = dip_then("1.10")
    # the signal exists only in the candle that is still forming: it must be ignored
    forming = klines_from([str(c) for c in closes[:-1]] + [str(closes[-1])], still_open=True)
    fake.klines["SOLUSDT"] = forming
    assert await make(SYMBOLS="SOLUSDT").scan() == []


async def test_it_reports_what_it_saw_once_an_hour_even_without_a_signal(world, caplog):
    fake, make, clock = world
    fake.klines["SOLUSDT"] = candles([D(100)] * 60)
    scanner = make(SYMBOLS="SOLUSDT", SCAN_EVERY_SECONDS="60")
    with caplog.at_level("INFO", logger="bot.services.scanner"):
        await scanner.next_entry()
        clock["t"] += 61
        await scanner.next_entry()  # within the hour: silent
        assert [r.message for r in caplog.records if r.message.startswith("scan:")] == [(
            "scan: 1 pair(s) checked, 0 unreadable, 0 signal(s); closest to oversold: SOLUSDT lowest RSI 50.0 "
            "of the last 6 candles (signal needs <= 30, then a cross up through the EMA)"
        )]
        clock["t"] += 3600
        await scanner.next_entry()
        assert len([r for r in caplog.records if r.message.startswith("scan:")]) == 2
