"""Message text, the queue, and the engine's behaviour with a notifier attached."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot.config.settings import Settings
from bot.database.models import (
    CancelReason,
    ExitReason,
    KillSwitchEvent,
    Order,
    OrderType,
    Position,
)
from bot.engine.ledger import TradeResult
from bot.engine.state_machine import TradingEngine
from bot.services.binance_client import ExchangeClient
from bot.services.notifications import (
    MAX_MESSAGE_CHARS,
    MessageFormatter,
    NotificationService,
    Notifier,
    NotifierError,
    mode_label,
)
from tests.fakes import FakeBinance
from tests.test_engine import Clock, H, Recorder

D = Decimal
WHEN = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)


def cfg(**env) -> Settings:
    return Settings(_env_file=None, **env)


def position(**kw) -> Position:
    defaults = {"symbol": "SOLUSDT", "entry_price": D("120.41000"), "exit_reason": ExitReason.TAKE_PROFIT}
    return Position(**{**defaults, **kw})


def order(**kw) -> Order:
    defaults = {"symbol": "SOLUSDT", "target_price": D("120.4100"), "quantity": D("0.130"), "expires_at": WHEN}
    return Order(**{**defaults, **kw})


def result(net=None, total_buy=None) -> TradeResult:
    net = D("0.4500") if net is None else net
    total_buy = D("15.6533") if total_buy is None else total_buy
    return TradeResult(
        symbol="SOLUSDT", amount_operated=D("0.129"), buy_price=D("120.41"), sell_price=D("125.22"),
        total_buy=total_buy, total_sell=D("16.15"), fees_usd=D("0.03"), net_profit=net,
        buy_time=WHEN - timedelta(hours=2), sell_time=WHEN,
    )


# --- the mode label ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dry, testnet, label",
    [("true", "true", "DRY RUN"), ("true", "false", "DRY RUN"), ("false", "true", "TESTNET"),
     ("false", "false", "LIVE")],
)
def test_mode_label_dry_run_wins(dry, testnet, label):
    assert mode_label(cfg(DRY_RUN=dry, BINANCE_TESTNET=testnet)) == label


# --- formatting ----------------------------------------------------------------------------


def fmt(**env) -> MessageFormatter:
    return MessageFormatter(cfg(**env))


def test_every_message_carries_the_mode_and_the_app_name():
    f = fmt(DRY_RUN="false", BINANCE_TESTNET="false")
    texts = [
        f.entry_placed(position(), order()),
        f.entry_cancelled(position(), order(cancel_reason=CancelReason.ENTRY_TIMEOUT)),
        f.exit_placed(position(), order(target_price=D("125.22")), order(stop_price=D("118.00"))),
        f.trade_closed(position(), result()),
        f.kill_switch(_event()),
        f.attention("look"), f.reminder("still"), f.protected_again(position()),
        f.started(), f.stopped(), f.test(),
    ]
    for text in texts:
        assert "<b>[LIVE]</b> Tidewatch" in text, text
    assert "[LIVE]" not in fmt(DRY_RUN="true").started() and "[DRY RUN]" in fmt(DRY_RUN="true").started()


def test_buy_placed_shows_asset_limit_price_and_expiry():
    text = fmt().entry_placed(position(), order())
    assert "Buy placed" in text
    assert "<pre>" in text and "Asset" in text and "SOL" in text
    assert "120.41 USDT" in text  # trailing zeros trimmed
    assert "2026-10-06 15:30 UTC" in text


def test_protection_placed_shows_fill_take_profit_and_stop_trigger():
    text = fmt().exit_placed(position(), order(target_price=D("125.22")), order(stop_price=D("118.00"),
                             target_price=D("117.76"), order_type=OrderType.STOP_LOSS_LIMIT))
    assert "Protection placed" in text
    assert "120.41 USDT" in text and "125.22 USDT" in text and "118 USDT" in text
    assert "117.76" not in text  # the trigger is shown, not the stop-limit price


def test_trade_closed_is_an_aligned_table_with_profit_return_and_time():
    text = fmt().trade_closed(position(), result())
    assert "Trade closed: profit" in text
    rows = text.split("<pre>")[1].split("</pre>")[0].splitlines()
    assert len({row.index("  ", 0) for row in rows if row}) >= 1
    values_start = {len(r) - len(r.split("  ", 1)[1].lstrip()) for r in rows}
    assert len(values_start) == 1  # every value starts in the same column
    assert "+0.4500" in text and "+2.87%" in text and "TAKE_PROFIT" in text
    assert "2026-10-06 15:30 UTC" in text


def test_a_losing_trade_is_labelled_and_signed():
    text = fmt().trade_closed(position(exit_reason=ExitReason.STOP_LOSS), result(net=D("-0.3100")))
    assert "Trade closed: loss" in text and "-0.3100" in text and "-1.98%" in text


def _event() -> KillSwitchEvent:
    return KillSwitchEvent(
        realized_loss_usd=D("1.2000"), account_balance_usd=D("16.00"), loss_pct=D("0.075"),
        limit_pct=D("0.05"), resume_at=WHEN + timedelta(hours=24),
    )


def test_kill_switch_message_says_everything_the_owner_needs():
    text = fmt().kill_switch(_event())
    assert "KILL-SWITCH TRIPPED" in text and "🚨" in text
    assert "1.2000 USDT" in text and "7.50%" in text and "5.00%" in text
    assert "cancelled and paused" in text
    assert "2026-10-07 15:30 UTC" in text
    assert "stay protected" in text


def test_attention_reminder_and_protected_again():
    f = fmt()
    assert "Needs your attention" in f.attention("SOLUSDT has no stop")
    assert "Reminder" in f.reminder("Still unprotected after 15 min.")
    assert "SOL has its stop-loss in place again" in f.protected_again(position())


def test_entry_cancelled_names_the_reason():
    text = fmt().entry_cancelled(position(), order(cancel_reason=CancelReason.KILL_SWITCH))
    assert "nothing was bought" in text and "KILL_SWITCH" in text


def test_started_lists_mode_symbols_and_targets():
    assert "dynamic, top 12 (up to medium risk)" in fmt(SYMBOLS_AUTO="true").started()
    text = fmt(SYMBOLS="SOLUSDT,ETHUSDT").started()
    assert "SOLUSDT, ETHUSDT" in text and "4.00%" in text and "2.00%" in text


def test_dynamic_values_are_html_escaped():
    evil = "<script>&\"x\"</script>"
    f = fmt()
    for text in (f.attention(evil), f.reminder(evil), f.entry_placed(position(symbol="A<B>USDT"), order())):
        assert "<script>" not in text and "A<B>" not in text
    assert "&lt;script&gt;&amp;" in f.attention(evil)


def test_long_text_is_truncated_without_breaking_the_markup():
    text = fmt().attention("<&>" * 5000)
    assert len(text) <= MAX_MESSAGE_CHARS + 50
    assert text.endswith("…")
    assert "&lt;&amp;&gt;" in text
    assert not text.rstrip("…").endswith(("&", "&l", "&lt", "&am", "&amp", "&g", "&gt"))  # no cut entity


# --- the queue -----------------------------------------------------------------------------


class Recording(Notifier):
    def __init__(self, delay=0.0, fail=None):
        self.sent: list[str] = []
        self.delay = delay
        self.fail = fail
        self.closed = False

    async def send(self, text):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        self.sent.append(text)

    async def close(self):
        self.closed = True


def service(notifier, **kw) -> NotificationService:
    return NotificationService(cfg(), notifier, **kw)


async def test_hooks_return_at_once_even_when_the_sender_hangs():
    stuck = asyncio.Event()

    class Hangs(Notifier):
        async def send(self, text):
            await stuck.wait()

    svc = service(Hangs())
    svc.start()
    await asyncio.wait_for(svc.attention("one"), 0.5)
    await asyncio.wait_for(svc.attention("two"), 0.5)
    await asyncio.wait_for(svc.reminder("three"), 0.5)
    svc._closing = True  # let the test end without waiting for the hung send
    svc._task.cancel()


async def test_hooks_do_not_await_the_network():
    """No awaiting happens inside a hook: it finishes before the event loop runs anything else."""
    notifier = Recording(delay=0.05)
    svc = service(notifier)
    svc.start()
    await svc.attention("a")
    assert notifier.sent == []  # still queued: the worker has not even been scheduled
    await svc.close()
    assert len(notifier.sent) == 1


async def test_a_failing_sender_is_logged_and_the_next_message_still_goes(caplog):
    class FlakySender(Recording):
        async def send(self, text):
            if "first" in text:
                raise NotifierError("Telegram refused sendMessage: 400 chat not found")
            await super().send(text)

    notifier = FlakySender()
    svc = service(notifier)
    svc.start()
    await svc.attention("first")
    await svc.attention("second")
    with caplog.at_level(logging.WARNING):
        await svc.close()

    assert len(notifier.sent) == 1 and "second" in notifier.sent[0]
    assert "chat not found" in caplog.text and svc.failed == 1


async def test_an_unexpected_sender_error_logs_only_its_class(caplog):
    svc = service(Recording(fail=RuntimeError("https://api.telegram.org/bot123:SECRET/sendMessage")))
    svc.start()
    await svc.attention("x")
    with caplog.at_level(logging.WARNING):
        await svc.close()

    assert "RuntimeError" in caplog.text and "SECRET" not in caplog.text


async def test_order_is_preserved():
    notifier = Recording()
    svc = service(notifier)
    for i in range(10):
        await svc.reminder(f"msg {i}")
    svc.start()
    await svc.close()

    assert [t.split("\n")[-1] for t in notifier.sent] == [f"msg {i}" for i in range(10)]


async def test_overflow_drops_the_oldest_and_warns(caplog):
    notifier = Recording()
    svc = service(notifier, max_queue=3)
    with caplog.at_level(logging.WARNING):
        for i in range(5):
            await svc.reminder(f"msg {i}")
    svc.start()
    await svc.close()

    assert [t.split("\n")[-1] for t in notifier.sent] == ["msg 2", "msg 3", "msg 4"]
    assert svc.dropped == 2 and "dropped the oldest" in caplog.text


async def test_close_flushes_what_is_queued_and_closes_the_notifier():
    notifier = Recording()
    svc = service(notifier)
    svc.start()
    svc.announce_started()
    await svc.attention("x")
    svc.announce_stopped()
    await svc.close()

    assert len(notifier.sent) == 3 and notifier.closed
    assert "Bot stopped" in notifier.sent[-1]


async def test_close_gives_up_after_its_time_limit(caplog):
    notifier = Recording(delay=10)
    svc = service(notifier, close_timeout=0.2)
    svc.start()
    await svc.attention("a")
    await svc.attention("b")
    started = asyncio.get_running_loop().time()
    with caplog.at_level(logging.WARNING):
        await svc.close()

    assert asyncio.get_running_loop().time() - started < 2
    assert notifier.sent == [] and notifier.closed
    assert "gave up flushing" in caplog.text


async def test_messages_after_close_are_ignored():
    notifier = Recording()
    svc = service(notifier)
    svc.start()
    await svc.close()
    await svc.attention("late")

    assert notifier.sent == []


async def test_a_hook_never_raises_even_with_bad_input(caplog):
    notifier = Recording()
    svc = service(notifier)
    svc.start()
    with caplog.at_level(logging.ERROR):
        await svc.trade_closed(None, None)  # cannot be formatted
        await svc.entry_placed(object(), object())
    await svc.close()

    assert len(notifier.sent) == 2
    assert all("could not be built" in t for t in notifier.sent)


# --- with the real engine ------------------------------------------------------------------


def engine_with(session, observer) -> H:
    fake = FakeBinance()
    exchange = ExchangeClient(fake, dry_run=False, order_id_prefix="bot")
    clock = Clock()

    @asynccontextmanager
    async def sessions():
        async with session.begin_nested():
            yield session

    settings = cfg(DRY_RUN="false", BINANCE_TESTNET="true")
    engine = TradingEngine(
        settings=settings, exchange=exchange, session_factory=sessions, observer=observer, clock=clock,
    )
    return H(engine, fake, exchange, clock, Recorder(), session)


async def test_a_whole_trade_produces_the_alerts_in_order(session):
    notifier = Recording()
    svc = NotificationService(cfg(DRY_RUN="false", BINANCE_TESTNET="true"), notifier)
    svc.start()
    h = engine_with(session, svc)

    await h.open_and_fill()
    tp = await h.leg(OrderType.LIMIT_MAKER)
    h.fake.fill(tp.binance_order_id, price="125.22")
    await h.engine.on_stream_event(h.event(tp, "FILLED"))
    await svc.close()

    titles = [t.split("\n")[1] for t in notifier.sent]
    assert titles == ["<b>Buy placed</b>", "<b>Protection placed</b>", "<b>Trade closed: profit</b>"]
    assert all("[TESTNET]" in t for t in notifier.sent)
    assert "SOL" in notifier.sent[0] and "120.41 USDT" in notifier.sent[0]
    assert "125.22 USDT" in notifier.sent[1] and "118 USDT" in notifier.sent[1]


async def test_a_sender_that_hangs_does_not_stall_trading(session):
    class Hangs(Notifier):
        async def send(self, text):
            await asyncio.Event().wait()

    svc = NotificationService(cfg(), Hangs(), close_timeout=0.1)
    svc.start()
    h = engine_with(session, svc)

    async def trade():
        await h.open_and_fill()
        tp = await h.leg(OrderType.LIMIT_MAKER)
        h.fake.fill(tp.binance_order_id, price="125.22")
        await h.engine.on_stream_event(h.event(tp, "FILLED"))

    await asyncio.wait_for(trade(), 5)
    assert (await h.position()).status.value == "CLOSED"
    await svc.close()


async def test_a_sender_that_raises_does_not_affect_the_engine(session):
    notifier = Recording(fail=RuntimeError("telegram exploded"))
    svc = NotificationService(cfg(), notifier)
    svc.start()
    h = engine_with(session, svc)

    await h.open_and_fill()
    tp = await h.leg(OrderType.LIMIT_MAKER)
    h.fake.fill(tp.binance_order_id, price="125.22")
    await h.engine.on_stream_event(h.event(tp, "FILLED"))
    await svc.close()

    pos = await h.position()
    assert pos.status.value == "CLOSED" and pos.exit_reason == ExitReason.TAKE_PROFIT
    assert svc.failed == 3
