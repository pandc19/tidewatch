"""Account (user-data) stream: order-update notifications with reconnect.

Design: the stream is a *notification*, REST is the *truth*. An event only says
"order X changed"; the handler re-reads the order over REST (see
`reconciliation.sync_order`). So a lost, duplicated or oddly-shaped event can
never corrupt state, and every (re)connect runs a full reconciliation to close
the gap while the socket was down.

Binance removed the old listenKey stream on 2026-02-20; python-binance 1.0.37's
`user_socket()` subscribes with `userDataStream.subscribe.signature` on the
WebSocket API instead. Checked on the testnet (2026-10-06): an HMAC key opens it and
the events match `parse_event` (see NEXT_STEPS.md for what is still unseen).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from binance import AsyncClient, BinanceSocketManager

log = logging.getLogger(__name__)


class Socket(Protocol):
    async def recv(self) -> dict[str, Any]: ...


SocketFactory = Callable[[], AbstractAsyncContextManager[Socket]]


@dataclass(frozen=True)
class OrderEvent:
    """The few fields needed to know *which* order to re-read."""

    symbol: str
    order_id: int
    client_order_id: str
    order_list_id: int | None
    status: str  # raw Binance status (X)
    execution_type: str  # raw execution type (x)


def parse_event(message: dict[str, Any]) -> OrderEvent | None:
    """Return an OrderEvent for an `executionReport`, None for anything else.

    Accepts both the bare event and the WebSocket-API wrapper
    `{"subscriptionId": n, "event": {...}}`. python-binance delivers bare events
    (seen live). Field letters follow Binance's executionReport (s symbol,
    i orderId, c/C client id, g orderListId, X status, x execution type).
    """
    event = message.get("event")
    if isinstance(event, dict):
        message = event
    if message.get("e") != "executionReport":
        return None
    try:
        # On a cancel, `c` is the cancel request's id and `C` the original order's.
        original = message.get("C") or ""
        client_id = original if message.get("x") == "CANCELED" and original else message["c"]
        list_id = message.get("g", -1)
        return OrderEvent(
            symbol=message["s"],
            order_id=int(message["i"]),
            client_order_id=client_id,
            order_list_id=None if list_id in (None, -1) else int(list_id),
            status=message["X"],
            execution_type=message.get("x", ""),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("unparseable executionReport: %r", message)
        return None


class StreamGap(Exception):
    """The socket reported an error or closed: events may have been missed."""


class UserStream:
    def __init__(
        self,
        socket_factory: SocketFactory,
        on_order_event: Callable[[OrderEvent], Awaitable[None]],
        on_connected: Callable[[], Awaitable[None]],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_backoff: float = 60.0,
        healthy_after: float = 30.0,
    ):
        self._socket_factory = socket_factory
        self._on_order_event = on_order_event
        self._on_connected = on_connected
        self._sleep = sleep
        self._clock = clock
        self._max_backoff = max_backoff
        self._healthy_after = healthy_after

    @classmethod
    def for_client(
        cls,
        client: AsyncClient,
        on_order_event: Callable[[OrderEvent], Awaitable[None]],
        on_connected: Callable[[], Awaitable[None]],
    ) -> UserStream:
        return cls(lambda: BinanceSocketManager(client).user_socket(), on_order_event, on_connected)

    async def run(self, stop: asyncio.Event) -> None:
        """Run until `stop` is set. Reconnects with backoff; never raises on a bad socket.

        Backoff resets only after a connection stayed up for `healthy_after` seconds, so a
        socket that connects and drops at once cannot trigger a reconcile every second.
        """
        backoff = 1.0
        stopper = asyncio.ensure_future(stop.wait())
        try:
            while not stop.is_set():
                connected_at: float | None = None
                try:
                    async with self._socket_factory() as sock:
                        # Reconcile *after* subscribing so no event falls between the two.
                        await self._on_connected()
                        connected_at = self._clock()
                        await self._pump(sock, stop, stopper)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if connected_at is not None and self._clock() - connected_at >= self._healthy_after:
                        backoff = 1.0
                    log.warning("user stream interrupted; reconnecting in %.0fs", backoff, exc_info=True)
                    if not stop.is_set():
                        await self._sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff)
        finally:
            stopper.cancel()

    async def _pump(self, sock: Socket, stop: asyncio.Event, stopper: asyncio.Future) -> None:
        while not stop.is_set():
            recv = asyncio.ensure_future(sock.recv())
            done, _ = await asyncio.wait({recv, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if recv not in done:
                recv.cancel()
                return
            message = recv.result()
            if message.get("e") == "error":
                raise StreamGap(f"socket error: {message!r}")
            event = parse_event(message)
            if event is None:
                continue
            try:
                await self._on_order_event(event)
            except Exception:
                log.exception("handler failed for %s; the next reconciliation will catch up", event)
