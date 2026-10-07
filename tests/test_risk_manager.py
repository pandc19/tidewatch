from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from bot.config.precision import SymbolFilters
from bot.config.settings import Settings
from bot.database.models import (
    BotState,
    CancelReason,
    DailyPnl,
    KillSwitchEvent,
    Position,
    PositionStatus,
)
from bot.services.binance_client import ExchangeClient
from bot.services.risk_manager import KillSwitch, config_problems, spread_ok
from tests.fakes import SOL_INFO, FakeBinance

D = Decimal
NOW = datetime.now(UTC)


def cfg(**env) -> Settings:
    return Settings(_env_file=None, **env)


# --- spread filter ------------------------------------------------------------------------


def test_spread_filter_is_inclusive_at_the_limit():
    assert spread_ok(D("0.0002"), D("0.002"))
    assert spread_ok(D("0.002"), D("0.002"))
    assert not spread_ok(D("0.00201"), D("0.002"))


# --- startup config checks ----------------------------------------------------------------


@pytest.fixture
def market():
    f = SymbolFilters.from_symbol_info(SOL_INFO)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    return {s: f for s in symbols}, {s: D("120") for s in symbols}


def test_the_default_config_with_the_owners_wallet_is_fine(market):
    errors, warnings = config_problems(cfg(), *market, free_quote=D("16"))
    assert errors == [] and warnings == []


def test_a_profit_target_below_the_round_trip_fee_is_an_error(market):
    errors, _ = config_problems(cfg(TAKE_PROFIT_PCT="0.002"), *market, free_quote=D("16"))
    assert any("round-trip fee" in e for e in errors)
    assert config_problems(cfg(TAKE_PROFIT_PCT="0.0021"), *market, free_quote=D("16"))[0] == []


def test_a_stop_not_smaller_than_the_target_is_only_a_warning(market):
    errors, warnings = config_problems(cfg(TAKE_PROFIT_PCT="0.02", STOP_LOSS_PCT="0.03"), *market, free_quote=D("16"))
    assert errors == [] and any("not smaller" in w for w in warnings)


def test_one_stop_out_that_reaches_the_daily_limit_is_warned(market):
    _, warnings = config_problems(cfg(STOP_LOSS_PCT="0.04", TAKE_PROFIT_PCT="0.05"), *market, free_quote=D("16"))
    assert any("already reaches" in w for w in warnings)


def test_a_wallet_too_small_for_any_pair_is_an_error(market):
    errors, warnings = config_problems(cfg(), *market, free_quote=D("5"))
    assert any("none of the configured pairs" in e for e in errors)
    assert len(warnings) == 3  # and each pair is named


def test_without_keys_the_balance_checks_are_skipped(market):
    errors, warnings = config_problems(cfg(), *market, free_quote=None)
    assert errors == [] and warnings == []


# --- kill-switch --------------------------------------------------------------------------


class Harness:
    def __init__(self, session, **env):
        self.session = session
        self.fake = FakeBinance()
        self.fake.balances["USDT"] = "16"
        self.now = NOW
        self.cancelled: list[tuple] = []
        self.alerts: list[KillSwitchEvent] = []
        self.settings = cfg(**env)

        @asynccontextmanager
        async def sessions():
            async with session.begin_nested():
                yield session

        async def cancel(reason, detail, event_id):
            self.cancelled.append((reason, detail, event_id))

        async def notify(event):
            self.alerts.append(event)

        self.switch = KillSwitch(
            settings=self.settings, exchange=ExchangeClient(self.fake, dry_run=False), session_factory=sessions,
            cancel_entries=cancel, notify=notify, clock=lambda: self.now,
        )

    async def closed_trade(self, pnl: str, hours_ago: float = 1) -> None:
        self.session.add(Position(
            symbol="SOLUSDT", status=PositionStatus.CLOSED, take_profit_pct=D("0.04"), stop_loss_pct=D("0.02"),
            realized_pnl_usd=D(pnl), closed_at=self.now - timedelta(hours=hours_ago),
        ))
        await self.session.flush()


@pytest.fixture
def ks(session):
    return Harness(session)


async def test_nothing_happens_without_losses(ks):
    assert await ks.switch.check() is None
    await ks.closed_trade("0.60")
    assert await ks.switch.check() is None and ks.cancelled == []


async def test_a_loss_under_the_limit_does_not_trip(ks):
    await ks.closed_trade("-0.40")  # 2.5% of 16 USDT; the limit is 3% = 0.48
    assert await ks.switch.check() is None


async def test_a_loss_at_the_limit_trips_everything_at_once(ks):
    await ks.closed_trade("-0.50")  # 3.1% of 16

    event = await ks.switch.check()

    assert event is not None
    assert (event.realized_loss_usd, event.account_balance_usd) == (D("0.50"), D("16"))
    assert event.loss_pct == D("0.50") / D("16") and event.limit_pct == D("0.03")
    assert event.resume_at == ks.now + timedelta(hours=24)
    state = await ks.session.get(BotState, 1)
    assert state.trading_paused_until == event.resume_at and "daily loss limit" in state.pause_reason
    assert (await ks.session.get(DailyPnl, ks.now.date())).kill_switch_triggered is True
    assert ks.cancelled == [(CancelReason.KILL_SWITCH, state.pause_reason, event.id)]
    assert ks.alerts == [event]


async def test_it_does_not_trip_twice_while_paused(ks):
    await ks.closed_trade("-0.50")
    assert await ks.switch.check() is not None
    assert await ks.switch.check() is None
    assert len(ks.alerts) == 1 and len((await ks.session.scalars(select(KillSwitchEvent))).all()) == 1


async def test_the_same_losses_cannot_trip_it_again_after_the_pause(ks):
    await ks.closed_trade("-0.50", hours_ago=2)
    assert await ks.switch.check() is not None
    ks.now += timedelta(hours=25)
    await ks.closed_trade("-0.10", hours_ago=0.5)  # new, small
    assert await ks.switch.check() is None  # the old 0.50 is not counted again
    await ks.closed_trade("-0.45", hours_ago=0.4)  # new loss pushes the new total over the limit
    assert await ks.switch.check() is not None


async def test_with_a_short_pause_the_same_losses_still_cannot_trip_it_twice(session):
    ks = Harness(session, KILL_SWITCH_COOLDOWN_HOURS="6")
    await ks.closed_trade("-0.50", hours_ago=2)
    assert await ks.switch.check() is not None

    ks.now += timedelta(hours=7)  # the pause is over, but that loss is only 9 hours old: inside the 24h window
    assert await ks.switch.check() is None

    await ks.closed_trade("-0.50", hours_ago=0.5)  # a genuinely new loss does trip it again
    assert await ks.switch.check() is not None


async def test_gains_offset_losses_within_the_window(ks):
    await ks.closed_trade("-0.50")
    await ks.closed_trade("0.40")
    assert await ks.switch.check() is None  # net -0.10


async def test_losses_older_than_24_hours_are_ignored(ks):
    await ks.closed_trade("-0.90", hours_ago=25)
    assert await ks.switch.check() is None


async def test_the_account_includes_open_positions_at_entry_cost(session):
    ks = Harness(session)
    ks.fake.balances["USDT"] = "1"  # almost everything is in a position
    session.add(Position(
        symbol="BTCUSDT", status=PositionStatus.OPEN, take_profit_pct=D("0.04"), stop_loss_pct=D("0.02"),
        quantity=D("0.125"), entry_price=D("120"),  # 15 USDT of coins
    ))
    await session.flush()
    await ks.closed_trade("-0.40")  # 0.40 / 16 = 2.5%: fine. Against the bare 1 USDT it would be 40%
    assert await ks.switch.check() is None


async def test_a_custom_limit_and_cooldown_are_respected(session):
    ks = Harness(session, DAILY_LOSS_LIMIT_PCT="0.01", KILL_SWITCH_COOLDOWN_HOURS="6")
    await ks.closed_trade("-0.20")  # 1.25% of 16
    event = await ks.switch.check()
    assert event is not None and event.resume_at == ks.now + timedelta(hours=6)


async def test_an_empty_account_does_not_divide_by_zero(session):
    ks = Harness(session)
    ks.fake.balances["USDT"] = "0"
    await ks.closed_trade("-1")
    assert await ks.switch.check() is None
