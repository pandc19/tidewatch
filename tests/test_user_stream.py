import asyncio
from contextlib import asynccontextmanager

import pytest

from bot.services.user_stream import OrderEvent, UserStream, parse_event

REPORT = {
    "e": "executionReport", "s": "SOLUSDT", "c": "bot-abc", "C": "", "S": "BUY", "o": "LIMIT",
    "x": "TRADE", "X": "FILLED", "i": 1001, "g": -1,
}


# --- parsing ------------------------------------------------------------------------------


def test_parses_a_bare_execution_report():
    assert parse_event(REPORT) == OrderEvent("SOLUSDT", 1001, "bot-abc", None, "FILLED", "TRADE")


def test_parses_the_websocket_api_wrapper():
    assert parse_event({"subscriptionId": 0, "event": REPORT}).order_id == 1001


def test_oco_events_carry_the_list_id():
    assert parse_event({**REPORT, "g": 55}).order_list_id == 55


def test_cancel_events_use_the_original_client_id():
    cancel = {**REPORT, "x": "CANCELED", "X": "CANCELED", "c": "cancel-req-id", "C": "bot-abc"}
    assert parse_event(cancel).client_order_id == "bot-abc"


@pytest.mark.parametrize(
    "message",
    [
        {"e": "outboundAccountPosition", "B": []},
        {"e": "balanceUpdate"},
        {"e": "listStatus", "s": "SOLUSDT", "g": 55},
        {"e": "executionReport", "s": "SOLUSDT"},  # missing fields
        {"e": "executionReport", "s": "SOLUSDT", "c": "x", "X": "NEW", "i": "not-a-number"},
        {},
    ],
)
def test_ignores_everything_else_without_raising(message):
    assert parse_event(message) is None


# --- run loop -----------------------------------------------------------------------------


class FakeSocket:
    """Yields scripted items; an Exception item is raised; once empty it blocks like a quiet socket."""

    def __init__(self, items):
        self.items = list(items)

    async def recv(self):
        if not self.items:
            await asyncio.Event().wait()
        item = self.items.pop(0)
        if callable(item):  # a scripted side effect, e.g. advancing the fake clock
            item()
            return await self.recv()
        if isinstance(item, Exception):
            raise item
        return item


def factory_for(*sockets):
    queue = list(sockets)

    @asynccontextmanager
    async def factory():
        yield queue.pop(0)

    return factory


class Harness:
    def __init__(self, *sockets, connect_errors=()):
        self.events, self.connects, self.sleeps = [], 0, []
        self.stop = asyncio.Event()
        self.now = 0.0
        self.connect_errors = list(connect_errors)
        self.handler_error = None
        self.stream = UserStream(
            factory_for(*sockets),
            self.on_event,
            self.on_connected,
            sleep=self.sleep,
            clock=lambda: self.now,
            max_backoff=4.0,
            healthy_after=30.0,
        )

    async def on_event(self, event):
        self.events.append(event)
        if self.handler_error and len(self.events) == 1:
            raise self.handler_error
        if len(self.events) >= self.stop_after:
            self.stop.set()

    async def on_connected(self):
        self.connects += 1
        if self.connect_errors:
            raise self.connect_errors.pop(0)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    stop_after = 99

    async def run(self):
        await asyncio.wait_for(self.stream.run(self.stop), timeout=3)


async def test_events_reach_the_handler_and_unrelated_ones_are_skipped():
    h = Harness(FakeSocket([{"e": "balanceUpdate"}, REPORT, {**REPORT, "i": 1002}]))
    h.stop_after = 2
    await h.run()
    assert [e.order_id for e in h.events] == [1001, 1002]
    assert h.connects == 1 and h.sleeps == []


async def test_reconciles_on_every_connect_and_reconnects_after_a_drop():
    h = Harness(FakeSocket([REPORT, RuntimeError("socket closed")]), FakeSocket([{**REPORT, "i": 1002}]))
    h.stop_after = 2
    await h.run()
    assert [e.order_id for e in h.events] == [1001, 1002]
    assert h.connects == 2  # once per (re)connect, so a gap is always followed by a reconcile
    assert h.sleeps == [1.0]


async def test_an_error_message_from_the_library_counts_as_a_gap():
    h = Harness(FakeSocket([{"e": "error", "type": "BinanceWebsocketClosed"}]), FakeSocket([REPORT]))
    h.stop_after = 1
    await h.run()
    assert h.connects == 2 and len(h.events) == 1


async def test_failing_reconciliation_backs_off_then_recovers():
    h = Harness(FakeSocket([]), FakeSocket([]), FakeSocket([REPORT]),
                connect_errors=[RuntimeError("db down"), RuntimeError("db down")])
    h.stop_after = 1
    await h.run()
    assert h.connects == 3
    assert h.sleeps == [1.0, 2.0]  # doubles; resets after a good connect


async def test_backoff_is_capped():
    h = Harness(*[FakeSocket([RuntimeError("x")]) for _ in range(5)] + [FakeSocket([REPORT])])
    h.stop_after = 1
    await h.run()
    assert h.sleeps == [1.0, 2.0, 4.0, 4.0, 4.0]


async def test_backoff_resets_only_after_a_healthy_connection():
    h = Harness(FakeSocket([RuntimeError("x")]), FakeSocket([RuntimeError("x")]), FakeSocket([]))
    # 1st drop is instant (backoff 1 -> next 2); the 2nd connection lives 100s, so it resets to 1.
    h.stream._socket_factory = factory_for(
        FakeSocket([RuntimeError("instant")]),
        FakeSocket([lambda: setattr(h, "now", 100.0), RuntimeError("after a long time")]),
        FakeSocket([REPORT]),
    )
    h.stop_after = 1
    await h.run()
    assert h.sleeps == [1.0, 1.0]


async def test_a_handler_bug_does_not_drop_the_stream():
    h = Harness(FakeSocket([REPORT, {**REPORT, "i": 1002}]))
    h.handler_error = ValueError("bug")
    h.stop_after = 2
    await h.run()
    assert len(h.events) == 2 and h.connects == 1


async def test_stop_ends_a_quiet_socket_promptly():
    h = Harness(FakeSocket([]))
    task = asyncio.ensure_future(h.run())
    await asyncio.sleep(0.05)
    h.stop.set()
    await task
    assert h.connects == 1


async def test_cancellation_propagates():
    h = Harness(FakeSocket([]))
    task = asyncio.ensure_future(h.stream.run(h.stop))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
