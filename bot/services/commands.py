"""Read-only Telegram commands (ADR-008): `/status` and `/help`.

A long-polling task, separate from the engine: it never takes the engine's lock and never
changes anything. Rules that matter:

* Only messages from the configured chat are answered. Anything else is ignored, and logged by
  chat id only (never the text).
* The backlog is skipped at start, so a command sent before a restart never runs afterwards.
* Replies go through the alert queue, so they obey its pacing and size rules (ADR-007).
* Only one process can poll a token. The caller starts this after the instance lock is held.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from bot.config.settings import APP_NAME, Settings
from bot.services.notifications import NotifierError, TelegramNotifier, mode_label

log = logging.getLogger(__name__)

LONG_POLL_SECONDS = 25
MAX_BACKOFF_SECONDS = 60.0

Handler = Callable[[], Awaitable[str]]


class Poller(Protocol):
    async def poll_updates(self, offset: int | None, timeout: int) -> list[dict]: ...
    async def close(self) -> None: ...


class CommandListener:
    def __init__(
        self,
        *,
        poller: Poller,
        chat_id: str,
        reply: Callable[[str], None],
        handlers: dict[str, Handler],
        mode: str,
        long_poll_seconds: int = LONG_POLL_SECONDS,
        backoff_step: float = 5.0,
    ):
        self._poller = poller
        self._chat_id = str(chat_id)
        self._reply = reply
        self._handlers = dict(handlers)
        self._mode = mode
        self._long_poll = long_poll_seconds
        self._backoff_step = backoff_step
        self._warned: set[str] = set()

    # -- the loop

    async def run(self, stop: asyncio.Event) -> None:
        try:
            offset = await self._skip_backlog(stop)
            if stop.is_set():
                return
            failures = 0
            while not stop.is_set():
                try:
                    updates = await self._poll(offset, stop)
                except _Stopped:
                    return
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    await self._back_off(exc, failures, stop)
                    continue
                failures = 0
                for update in updates:
                    offset = max(offset or 0, int(update.get("update_id", 0)) + 1)
                    await self._handle(update)
        finally:
            await self._poller.close()

    async def _poll(self, offset: int | None, stop: asyncio.Event) -> list[dict]:
        """One long poll, abandoned at once if the bot is stopping."""
        poll = asyncio.ensure_future(self._poller.poll_updates(offset, self._long_poll))
        stopper = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({poll, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
        if not poll.done():
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
            raise _Stopped
        return poll.result()

    async def _skip_backlog(self, stop: asyncio.Event) -> int | None:
        """Confirm everything already waiting, without acting on it. Retries until it works."""
        failures = 0
        while not stop.is_set():
            try:
                updates = await self._poller.poll_updates(-1, 0)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                await self._back_off(exc, failures, stop)
                continue
            return int(updates[-1]["update_id"]) + 1 if updates else None
        return None

    async def _back_off(self, exc: Exception, failures: int, stop: asyncio.Event) -> None:
        # NotifierError text is already token-free; for anything else log the class name only.
        detail = str(exc) if isinstance(exc, NotifierError) else type(exc).__name__
        hint = ""
        if "409" in detail:
            hint = " (another process is polling this bot token: only one may)"
        delay = min(MAX_BACKOFF_SECONDS, self._backoff_step * failures)
        log.warning("telegram commands: polling failed: %s%s; retrying in %.0fs", detail, hint, delay)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    # -- one message

    async def _handle(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return
        sender = message.get("from") or {}
        chat_id = str(chat.get("id"))
        if chat_id != self._chat_id or sender.get("is_bot"):
            if chat_id not in self._warned:  # once per chat: a stranger cannot flood the log
                self._warned.add(chat_id)
                log.warning("telegram commands: ignored a command from chat %s (not the owner's)", chat_id)
            return
        name = text.split()[0][1:].split("@")[0].lower()
        if name == "start":
            name = "help"
        if name == "help":
            self._reply(self.help_text())
            return
        handler = self._handlers.get(name)
        if handler is None:
            self._reply(self._plain("Unknown command. Try /help."))
            return
        try:
            self._reply(await handler())
        except Exception as exc:  # noqa: BLE001  a failing report must not stop the listener
            log.warning("telegram commands: /%s failed (%s)", name, type(exc).__name__)
            self._reply(self._plain(f"/{name} could not be built; the details are in the log."))

    def _plain(self, text: str) -> str:
        return f"<b>[{html.escape(self._mode)}]</b> {html.escape(APP_NAME)}\n{html.escape(text)}"

    def help_text(self) -> str:
        lines = [f"/{name}: {doc}" for name, doc in COMMAND_HELP.items() if name in self._handlers or name == "help"]
        return self._plain("Commands (read-only):\n" + "\n".join(lines))


COMMAND_HELP = {
    "status": "what the bot is doing: mode, engine health, open position, results",
    "help": "this list",
}


def build_command_listener(
    settings: Settings, *, reply: Callable[[str], None], status: Handler
) -> CommandListener:
    """The listener for the configured bot and chat. Its own HTTP session: a long poll must not
    hold up the alert sender (each TelegramNotifier serialises its calls)."""
    poller = TelegramNotifier(
        settings.telegram_bot_token.get_secret_value(),
        settings.telegram_chat_id,
        timeout=LONG_POLL_SECONDS + 15,
        attempts=1,  # the listener backs off itself
        min_interval=0.0,
    )
    return CommandListener(
        poller=poller,
        chat_id=settings.telegram_chat_id,
        reply=reply,
        handlers={"status": status},
        mode=mode_label(settings),
    )


class _Stopped(Exception):
    """The bot is stopping: leave the loop."""
