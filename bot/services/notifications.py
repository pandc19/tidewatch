"""Telegram alerts.

`NotificationService` is an `EngineObserver`: the engine calls its hooks while holding its lock,
so a hook only formats a message and puts it on a bounded queue. A background worker delivers
the queue through a `Notifier`. A slow or failing Telegram can therefore never delay or break
trading (ADR-007).

Setup helpers: `python -m bot.services.notifications --find-chat-id` and `--test`.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import re
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

import aiohttp

from bot.config.settings import APP_NAME, Settings
from bot.database.models import KillSwitchEvent, Order, Position
from bot.engine.ledger import TradeResult
from bot.engine.state_machine import EngineObserver

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000  # Telegram's hard limit is 4096; leave room for the cut-off notice


class NotifierError(Exception):
    """A message could not be delivered. The text never contains the bot token."""


class Notifier:
    """Anything that can deliver a text message."""

    async def send(self, text: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class NullNotifier(Notifier):
    """Used when Telegram is not configured: messages go nowhere."""

    async def send(self, text: str) -> None:
        return None


# --- Telegram over HTTP ---------------------------------------------------------------------

_TOKEN_SHAPE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")  # a bot token, in case a library echoes one


class TelegramNotifier(Notifier):
    """Calls the Bot API `sendMessage` over HTTP with aiohttp.

    Retries network errors and 5xx with backoff, waits out a 429 (capped), never retries other
    client errors (bad token, bad chat id), and spaces sends `min_interval` seconds apart.
    Failures raise `NotifierError` with the token scrubbed out; the token is part of the request
    URL, so no raw aiohttp error text is ever passed on.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        base_url: str = TELEGRAM_API,
        timeout: float = 10.0,
        attempts: int = 4,
        backoff: float = 1.0,
        max_retry_after: float = 30.0,
        min_interval: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._token = token
        self._chat_id = chat_id
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._attempts = max(1, attempts)
        self._backoff = backoff
        self._max_retry_after = max_retry_after
        self._min_interval = min_interval
        self._sleep = sleep
        self._http: aiohttp.ClientSession | None = None
        self._last_sent = float("-inf")
        self._pace = asyncio.Lock()

    def scrub(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "<token>")
        return _TOKEN_SHAPE.sub("<token>", text)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def send(self, text: str) -> None:
        await self._call(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    async def get_updates(self) -> list[dict]:
        result = await self._call("getUpdates", {"timeout": 0, "limit": 100})
        return result if isinstance(result, list) else []

    async def _call(self, method: str, payload: dict):
        async with self._pace:  # one call at a time, about one per `min_interval`
            wait = self._min_interval - (time.monotonic() - self._last_sent)
            if wait > 0:
                await self._sleep(wait)
            try:
                return await self._attempt_loop(method, payload)
            finally:
                self._last_sent = time.monotonic()

    async def _attempt_loop(self, method: str, payload: dict):
        last = "no attempt made"
        for attempt in range(1, self._attempts + 1):
            delay = self._backoff * 2 ** (attempt - 1)
            try:
                status, body = await self._post(method, payload)
            except (aiohttp.ClientError, TimeoutError) as exc:
                # Only the class name: aiohttp error text can contain the request URL.
                last = f"network error ({type(exc).__name__})"
            else:
                description = self.scrub(str(body.get("description", ""))) if body else ""
                if status == 200 and body.get("ok"):
                    return body.get("result")
                if status == 429:
                    retry_after = (body.get("parameters") or {}).get("retry_after", delay)
                    try:
                        delay = min(float(retry_after), self._max_retry_after)
                    except (TypeError, ValueError):
                        pass
                    last = f"rate limited (429), retry after {delay:g}s"
                elif status >= 500:
                    last = f"Telegram server error ({status})"
                else:
                    hint = " (check TELEGRAM_BOT_TOKEN)" if status in (401, 404) else ""
                    raise NotifierError(f"Telegram refused {method}: {status} {description}{hint}")
            if attempt < self._attempts:
                log.debug("telegram %s attempt %d failed: %s", method, attempt, last)
                await self._sleep(delay)
        raise NotifierError(f"Telegram {method} failed after {self._attempts} attempts: {last}")

    async def _post(self, method: str, payload: dict) -> tuple[int, dict]:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=self._timeout)
        url = f"{self._base}/bot{self._token}/{method}"
        async with self._http.post(url, json=payload) as response:
            try:
                body = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                body = {}
            return response.status, body if isinstance(body, dict) else {}


# --- message text ---------------------------------------------------------------------------


def mode_label(settings: Settings) -> str:
    """LIVE, TESTNET or DRY RUN. Dry run wins, so a practice message cannot look like real money."""
    if settings.dry_run:
        return "DRY RUN"
    return "TESTNET" if settings.binance_testnet else "LIVE"


def _e(value: object) -> str:
    return html.escape(str(value), quote=False)


def _num(value: Decimal | None) -> str:
    """A price or quantity without trailing zeros or an exponent."""
    if value is None:
        return "?"
    return format(value.normalize(), "f")


def _signed_usd(value: Decimal) -> str:
    return f"{value:+,.4f}"


def _when(moment: datetime | None) -> str:
    if moment is None:
        return "?"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _table(rows: list[tuple[str, str]]) -> str:
    """Telegram does not render Markdown tables: align the columns inside a <pre> block."""
    width = max(len(label) for label, _ in rows)
    return "<pre>" + _e("\n".join(f"{label.ljust(width)}  {value}" for label, value in rows)) + "</pre>"


def _fit(raw: str, budget: int) -> str:
    """Escape free text, cutting the *raw* text so the escaped result fits (never mid-entity)."""
    budget = max(budget, 20)
    text = _e(raw)
    while len(text) > budget:
        raw = raw[: max(1, int(len(raw) * 0.9) - 1)]
        text = _e(raw.rstrip()) + "…"
    return text


class MessageFormatter:
    """Turns engine events into Telegram HTML. Pure: no network, no database."""

    def __init__(self, settings: Settings):
        self._s = settings
        self.mode = mode_label(settings)

    def _head(self, title: str) -> str:
        return f"<b>[{_e(self.mode)}]</b> {_e(APP_NAME)}\n<b>{_e(title)}</b>\n"

    def _asset(self, symbol: str) -> str:
        quote = self._s.quote_asset
        return symbol[: -len(quote)] if symbol.endswith(quote) and symbol != quote else symbol

    def _prose(self, title: str, message: str) -> str:
        head = self._head(title)
        return head + _fit(message, MAX_MESSAGE_CHARS - len(head))

    def entry_placed(self, position: Position, order: Order) -> str:
        return self._head("Buy placed") + _table(
            [
                ("Asset", self._asset(position.symbol)),
                ("Limit price", f"{_num(order.target_price)} {self._s.quote_asset}"),
                ("Quantity", _num(order.quantity)),
                ("Expires", _when(order.expires_at)),
            ]
        )

    def entry_cancelled(self, position: Position, order: Order) -> str:
        reason = order.cancel_reason.value if order.cancel_reason else "unknown"
        rows = [
            ("Asset", self._asset(position.symbol)),
            ("Limit price", f"{_num(order.target_price)} {self._s.quote_asset}"),
            ("Reason", reason),
        ]
        return self._head("Buy cancelled, nothing was bought") + _table(rows)

    def exit_placed(self, position: Position, take_profit: Order, stop_loss: Order) -> str:
        quote = self._s.quote_asset
        return self._head("Protection placed") + _table(
            [
                ("Asset", self._asset(position.symbol)),
                ("Entry fill price", f"{_num(position.entry_price)} {quote}"),
                ("Take-profit", f"{_num(take_profit.target_price)} {quote}"),
                ("Stop-loss trigger", f"{_num(stop_loss.stop_price)} {quote}"),
            ]
        )

    def trade_closed(self, position: Position, result: TradeResult) -> str:
        outcome = "profit" if result.net_profit > 0 else "loss" if result.net_profit < 0 else "break-even"
        reason = position.exit_reason.value if position.exit_reason else "?"
        return self._head(f"Trade closed: {outcome}") + _table(
            [
                ("Asset", self._asset(result.symbol)),
                ("Net P/L (USD)", _signed_usd(result.net_profit)),
                ("Return", f"{result.return_pct:+.2%}"),
                ("Exit", reason),
                ("Closed", _when(result.sell_time)),
            ]
        )

    def kill_switch(self, event: KillSwitchEvent) -> str:
        quote = self._s.quote_asset
        body = (
            "Daily loss limit reached. New buys are cancelled and paused. "
            "Open positions stay protected by their own stop-loss; nothing is sold."
        )
        return (
            "🚨 " + self._head("KILL-SWITCH TRIPPED")
            + _table(
                [
                    ("Loss", f"{event.realized_loss_usd:,.4f} {quote}"),
                    ("Loss / account", f"{event.loss_pct:.2%} of {event.account_balance_usd:,.2f}"),
                    ("Limit", f"{event.limit_pct:.2%}"),
                    ("Trading resumes", _when(event.resume_at)),
                ]
            )
            + "\n" + _e(body)
        )

    def attention(self, message: str) -> str:
        return self._prose("⚠️ Needs your attention", message)

    def reminder(self, message: str) -> str:
        return self._prose("⏰ Reminder", message)

    def protected_again(self, position: Position) -> str:
        return self._head("Protected again") + _e(
            f"{self._asset(position.symbol)} has its stop-loss in place again."
        )

    def started(self) -> str:
        s = self._s
        return self._head("Bot started") + _table(
            [
                ("Symbols", ", ".join(s.symbols)),
                ("Take-profit", f"{s.take_profit_pct:.2%}"),
                ("Stop-loss", f"{s.stop_loss_pct:.2%}"),
            ]
        )

    def stopped(self) -> str:
        return self._head("Bot stopped") + _e("Shut down cleanly. Open positions keep their exits on Binance.")

    def test(self) -> str:
        return self._head("Test message") + _e("If you can read this, alerts reach this chat.")


# --- the queue ------------------------------------------------------------------------------


class NotificationService(EngineObserver):
    """Engine hooks in, Telegram messages out, with a bounded queue in between.

    The engine holds its lock while it calls a hook, so every hook only formats and enqueues,
    then returns. `_worker` does the network I/O. Nothing here raises into the engine.
    """

    def __init__(
        self,
        settings: Settings,
        notifier: Notifier,
        *,
        max_queue: int = 100,
        close_timeout: float = 10.0,
    ):
        self._notifier = notifier
        self._fmt = MessageFormatter(settings)
        self._max = max(1, max_queue)
        self._close_timeout = close_timeout
        self._pending: deque[str] = deque()
        self._wakeup = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task | None = None
        self.dropped = 0
        self.failed = 0

    # -- lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._worker(), name="notifications")

    async def close(self) -> None:
        """Deliver what is queued, for at most `close_timeout` seconds, then stop."""
        self._closing = True
        self._wakeup.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), self._close_timeout)
            except TimeoutError:
                log.warning("notifications: gave up flushing, %d message(s) not sent", len(self._pending))
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._notifier.close()

    # -- queue

    def notify(self, text: str) -> None:
        """Queue a ready-made message. Never blocks, never raises."""
        if self._closing:
            return
        if len(self._pending) >= self._max:
            self._pending.popleft()
            self.dropped += 1
            log.warning("notifications: queue full, dropped the oldest message (%d dropped)", self.dropped)
        self._pending.append(text)
        self._wakeup.set()

    def _emit(self, name: str, build: Callable[[], str]) -> None:
        try:
            text = build()
        except Exception:
            log.exception("notifications: could not format %s", name)
            text = f"[{self._fmt.mode}] {name}: an event happened but its message could not be built, see the log"
        try:
            self.notify(text)
        except Exception:
            log.exception("notifications: could not queue %s", name)

    async def _worker(self) -> None:
        while True:
            if not self._pending:
                if self._closing:
                    return
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            text = self._pending.popleft()
            try:
                await self._notifier.send(text)
            except asyncio.CancelledError:
                raise
            except NotifierError as exc:
                self.failed += 1
                log.warning("notifications: message not delivered: %s", exc)
            except Exception as exc:  # noqa: BLE001
                self.failed += 1  # class name only: an unknown error's text could echo a URL
                log.warning("notifications: message not delivered (%s)", type(exc).__name__)

    # -- EngineObserver hooks (each returns at once)

    async def entry_placed(self, position, order) -> None:
        self._emit("entry_placed", lambda: self._fmt.entry_placed(position, order))

    async def entry_cancelled(self, position, order) -> None:
        self._emit("entry_cancelled", lambda: self._fmt.entry_cancelled(position, order))

    async def exit_placed(self, position, take_profit, stop_loss) -> None:
        self._emit("exit_placed", lambda: self._fmt.exit_placed(position, take_profit, stop_loss))

    async def trade_closed(self, position, result) -> None:
        self._emit("trade_closed", lambda: self._fmt.trade_closed(position, result))

    async def attention(self, message: str) -> None:
        self._emit("attention", lambda: self._fmt.attention(message))

    async def kill_switch(self, event) -> None:
        self._emit("kill_switch", lambda: self._fmt.kill_switch(event))

    async def reminder(self, message: str) -> None:
        self._emit("reminder", lambda: self._fmt.reminder(message))

    async def protected_again(self, position) -> None:
        self._emit("protected_again", lambda: self._fmt.protected_again(position))

    # -- lifecycle messages

    def announce_started(self) -> None:
        self._emit("started", self._fmt.started)

    def announce_stopped(self) -> None:
        self._emit("stopped", self._fmt.stopped)


def build_notifier(settings: Settings) -> Notifier:
    if not settings.telegram_enabled:
        return NullNotifier()
    return TelegramNotifier(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id)


async def send_test_message(settings: Settings) -> str | None:
    """Sends one test message. Returns None on success, else the (token-free) reason."""
    if not settings.telegram_enabled:
        return "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not both set"
    notifier = build_notifier(settings)
    try:
        await notifier.send(MessageFormatter(settings).test())
        return None
    except NotifierError as exc:
        return str(exc)
    finally:
        await notifier.close()


# --- setup helpers --------------------------------------------------------------------------


def chats_from_updates(updates: list[dict]) -> dict[int, str]:
    """{chat id: a readable name} for everyone who recently messaged the bot."""
    found: dict[int, str] = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or update.get("my_chat_member") or {}
        chat = message.get("chat") or {}
        if "id" not in chat:
            continue
        name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("title")
        if chat.get("username"):
            name = f"{name} (@{chat['username']})" if name else f"@{chat['username']}"
        found.setdefault(chat["id"], name or "(no name)")
    return found


async def _find_chat_id(settings: Settings) -> int:
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather).")
        return 1
    notifier = TelegramNotifier(token, settings.telegram_chat_id)
    try:
        chats = chats_from_updates(await notifier.get_updates())
    except NotifierError as exc:
        print(f"Could not read updates: {exc}")
        return 1
    finally:
        await notifier.close()
    if not chats:
        print("No messages found. Open your bot in Telegram, press Start or send it any message, run this again.")
        return 1
    print("Chats that recently messaged the bot (put the id in TELEGRAM_CHAT_ID):")
    for chat_id, name in chats.items():
        print(f"  {chat_id}  {name}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bot.services.notifications")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--find-chat-id", action="store_true", help="list chat ids that messaged the bot")
    group.add_argument("--test", action="store_true", help="send a test message")
    args = parser.parse_args()
    settings = Settings()
    if args.find_chat_id:
        sys.exit(asyncio.run(_find_chat_id(settings)))
    problem = asyncio.run(send_test_message(settings))
    print("Test message sent." if problem is None else f"Test message failed: {problem}")
    sys.exit(0 if problem is None else 1)


if __name__ == "__main__":
    main()
