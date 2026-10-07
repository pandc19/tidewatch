"""Runs the engine: the account stream plus a periodic tick, until asked to stop."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from bot.config.settings import Settings
from bot.engine.state_machine import TradingEngine
from bot.services.binance_client import ExchangeClient
from bot.services.user_stream import UserStream

log = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = 5.0


class Stream(Protocol):
    async def run(self, stop: asyncio.Event) -> None: ...


class EngineRunner:
    def __init__(
        self,
        engine: TradingEngine,
        exchange: ExchangeClient,
        settings: Settings,
        *,
        stream: Stream | None = None,
    ):
        self._engine = engine
        self._settings = settings
        # Dry-run sends no orders and has nothing to hear about; without keys the stream
        # cannot open. In both cases only the tick runs.
        use_stream = not settings.dry_run and settings.has_binance_credentials
        self._stream = stream or (
            UserStream.for_client(exchange.raw, engine.on_stream_event, engine.on_connected)
            if use_stream
            else None
        )

    async def run(self, stop: asyncio.Event) -> None:
        tasks: list[asyncio.Future] = []
        if self._stream is not None:
            tasks.append(asyncio.ensure_future(self._stream.run(stop)))  # reconciles on connect
        else:
            await self._engine.on_connected()
        tasks.append(asyncio.ensure_future(self._tick_loop(stop)))
        log.info(
            "engine running (tick every %ss, stream %s)",
            self._settings.engine_tick_seconds,
            "on" if self._stream is not None else "off",
        )
        await stop.wait()
        # Both tasks watch `stop`: give them a moment to finish cleanly (the stream closes
        # its socket in order), then cancel only what is still stuck.
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("engine stopped")

    async def _tick_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._engine.tick()
            except Exception:
                log.exception("engine tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._settings.engine_tick_seconds)
            except TimeoutError:
                pass
