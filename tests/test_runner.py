import asyncio

from bot.config.settings import Settings
from bot.engine.runner import EngineRunner
from bot.services.binance_client import ExchangeClient
from tests.fakes import FakeBinance


class StubEngine:
    def __init__(self, fail_first_tick=False):
        self.ticks = 0
        self.connected = 0
        self.fail_first_tick = fail_first_tick

    async def tick(self):
        self.ticks += 1
        if self.fail_first_tick and self.ticks == 1:
            raise RuntimeError("tick blew up")

    async def on_connected(self):
        self.connected += 1

    async def on_stream_event(self, event):
        pass


class StubStream:
    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = False

    async def run(self, stop):
        self.started.set()
        await stop.wait()
        self.stopped = True


def runner(engine, *, stream=None, **env):
    settings = Settings(_env_file=None, ENGINE_TICK_SECONDS="1", **env)
    exchange = ExchangeClient(FakeBinance(), dry_run=settings.dry_run)
    return EngineRunner(engine, exchange, settings, stream=stream)


async def run_for(r, seconds):
    stop = asyncio.Event()
    task = asyncio.ensure_future(r.run(stop))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, timeout=3)  # a clean, prompt shutdown


async def test_with_a_stream_the_stream_owns_reconciliation_and_ticks_keep_running():
    engine, stream = StubEngine(), StubStream()
    await run_for(runner(engine, stream=stream), 0.2)
    assert stream.started.is_set() and stream.stopped
    assert engine.ticks >= 1
    assert engine.connected == 0  # the stream calls on_connected itself, on every (re)connect


async def test_without_a_stream_it_reconciles_once_at_start():
    engine = StubEngine()
    await run_for(runner(engine), 0.2)  # dry-run by default: no stream
    assert engine.connected == 1 and engine.ticks >= 1


async def test_a_failing_tick_does_not_end_the_loop():
    engine = StubEngine(fail_first_tick=True)
    await run_for(runner(engine, stream=StubStream()), 1.3)
    assert engine.ticks >= 2  # the loop survived the failure and ticked again


async def test_live_mode_with_keys_builds_the_real_stream():
    r = runner(
        StubEngine(), DRY_RUN="false", BINANCE_API_KEY="k", BINANCE_API_SECRET="s"
    )
    assert r._stream is not None


async def test_dry_run_and_missing_keys_both_skip_the_stream():
    assert runner(StubEngine())._stream is None
    assert runner(StubEngine(), DRY_RUN="false")._stream is None  # no keys
