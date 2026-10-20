from decimal import Decimal

import pytest

from bot.config.settings import Settings
from bot.services.binance_client import ExchangeClient, ExchangeError
from bot.services.scanner import Scanner
from bot.services.universe import Universe, candidates, select, swing_of, tier_of
from tests.fakes import FakeBinance, klines_from

D = Decimal


def row(symbol, volume="50000000", bid="100.00", ask="100.01"):
    return {"symbol": symbol, "quoteVolume": volume, "bidPrice": bid, "askPrice": ask}


def hourly(swing: str, n: int = 24) -> list:
    """`n` finished hourly candles whose highest close is `swing` above the lowest, plus the forming one."""
    closes = [D(100)] * (n - 1) + [D(100) * (1 + D(swing))]
    return klines_from([str(c) for c in [*closes, closes[-1]]], still_open=True)


def settings(**env):
    return Settings(_env_file=None, SYMBOLS_AUTO="true", **env)


def test_tiers_follow_the_24h_swing():
    assert tier_of(D("0.003")) is None  # flat: stablecoin-like
    assert tier_of(D("0.014")) == "low"
    assert tier_of(D("0.04")) == "medium"
    assert tier_of(D("0.09")) == "high"


def test_swing_needs_a_full_day_of_candles():
    assert swing_of([D(100)] * 10) is None
    assert swing_of([D(100)] * 23 + [D(105)]) == D("0.05")


def test_candidates_drop_stablecoins_leveraged_tokens_thin_and_wide_pairs_and_other_quotes():
    rows = [
        row("BTCUSDT", "900000000"),
        row("USDCUSDT", "800000000"),  # stablecoin
        row("BTCUPUSDT", "700000000"),  # leveraged token
        row("ETHBTC", "600000000"),  # not the quote asset
        row("SOLUSDT", "100000"),  # too little volume
        row("XRPUSDT", "50000000", bid="1.00", ask="1.01"),  # about 1% spread
        row("ETHUSDT", "400000000"),
        {"symbol": "BADUSDT", "quoteVolume": "oops"},
    ]
    assert [c[0] for c in candidates(rows, settings())] == ["BTCUSDT", "ETHUSDT"]


async def test_select_keeps_pairs_up_to_the_allowed_risk_and_drops_flat_ones():
    fake = FakeBinance()
    fake.ticker_rows = [row("AAAUSDT", "9" * 9), row("BBBUSDT", "8" * 9), row("CCCUSDT", "7" * 9), row("DDDUSDT", "6" * 9)]
    fake.klines = {
        "AAAUSDT": hourly("0.015"),  # low
        "BBBUSDT": hourly("0.04"),  # medium
        "CCCUSDT": hourly("0.12"),  # high
        "DDDUSDT": hourly("0.001"),  # flat
    }
    exchange = ExchangeClient(fake, dry_run=True)

    medium = await select(exchange, settings())
    assert [(p.symbol, p.tier) for p in medium] == [("AAAUSDT", "low"), ("BBBUSDT", "medium")]
    low = await select(exchange, settings(SYMBOLS_AUTO_MAX_RISK="low"))
    assert [p.symbol for p in low] == ["AAAUSDT"]
    high = await select(exchange, settings(SYMBOLS_AUTO_MAX_RISK="high", SYMBOLS_AUTO_COUNT="2"))
    assert [p.symbol for p in high] == ["AAAUSDT", "BBBUSDT"]  # capped at the count


async def test_a_pair_that_cannot_be_checked_is_skipped_not_fatal():
    fake = FakeBinance()
    fake.ticker_rows = [row("AAAUSDT", "9" * 9), row("BBBUSDT", "8" * 9)]
    fake.klines = {"BBBUSDT": hourly("0.015")}
    fake.raise_once["get_klines"] = [ExchangeError("boom")]  # the first lookup, AAAUSDT's, fails
    exchange = ExchangeClient(fake, dry_run=True)
    assert [p.symbol for p in await select(exchange, settings())] == ["BBBUSDT"]


@pytest.fixture
def clock():
    return {"t": 0.0}


async def test_universe_is_the_fixed_list_when_off(clock):
    fake = FakeBinance()
    u = Universe(Settings(_env_file=None, SYMBOLS="BTCUSDT,ETHUSDT"), ExchangeClient(fake, dry_run=True))
    await u.refresh()
    assert u.symbols == ["BTCUSDT", "ETHUSDT"]
    assert fake.called("get_ticker") == []  # off means no extra calls at all


async def test_universe_refreshes_hourly_and_falls_back_on_failure(clock):
    fake = FakeBinance()
    fake.ticker_rows = [row("AAAUSDT")]
    fake.klines = {"AAAUSDT": hourly("0.015")}
    u = Universe(settings(SYMBOLS="BTCUSDT"), ExchangeClient(fake, dry_run=True), clock=lambda: clock["t"])
    assert u.symbols == ["BTCUSDT"]  # before the first refresh
    await u.refresh()
    assert u.symbols == ["AAAUSDT"]

    fake.ticker_rows = [row("BBBUSDT")]
    fake.klines["BBBUSDT"] = hourly("0.02")
    clock["t"] = 59 * 60
    await u.refresh()
    assert u.symbols == ["AAAUSDT"]  # not stale yet
    clock["t"] = 61 * 60
    await u.refresh()
    assert u.symbols == ["BBBUSDT"]

    fake.raise_on["get_ticker"] = RuntimeError("network")
    clock["t"] = 200 * 60
    await u.refresh()
    assert u.symbols == ["BBBUSDT"]  # a failed refresh keeps the last good list


async def test_universe_with_nothing_passing_keeps_the_fixed_list(clock):
    fake = FakeBinance()
    fake.ticker_rows = [row("USDCUSDT")]
    u = Universe(settings(SYMBOLS="SOLUSDT"), ExchangeClient(fake, dry_run=True))
    await u.refresh()
    assert u.symbols == ["SOLUSDT"]


async def test_the_scanner_scans_the_dynamic_list(clock):
    fake = FakeBinance()
    fake.ticker_rows = [row("AAAUSDT")]
    fake.klines = {"AAAUSDT": hourly("0.015")}
    s = settings(SYMBOLS="BTCUSDT")
    scanner = Scanner(s, ExchangeClient(fake, dry_run=True), clock=lambda: clock["t"])
    await scanner.scan()
    scanned = {c["symbol"] for c in fake.called("get_klines")}
    assert "BTCUSDT" not in scanned and "AAAUSDT" in scanned
