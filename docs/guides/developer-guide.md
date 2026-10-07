# Developer Guide

## Commands

| Task | Command |
|---|---|
| Install | `uv sync --python 3.12` |
| Tests | `.venv/bin/pytest` |
| Lint | `.venv/bin/ruff check .` |
| Apply migrations | `.venv/bin/alembic upgrade head` |
| Run | `.venv/bin/python -m bot.main` |
| Find your Telegram chat id | `.venv/bin/python -m bot.services.notifications --find-chat-id` |
| Send a Telegram test message | `.venv/bin/python -m bot.services.notifications --test` |

## Rules that matter

1. **Money is `Decimal`.** Never `float`, in code, tests or the database. Pass Binance values as strings. `to_decimal` refuses floats on purpose.
2. **Every price and quantity goes through [precision.py](../../bot/config/precision.py)** before it reaches Binance: round, then `validate_order`. Quantities round down, never up.
3. **Percentages are fractions** (`0.04` = 4%) everywhere: env vars, settings, columns.
4. **Nothing is hardcoded that the owner may tune**: it is a setting in [settings.py](../../bot/config/settings.py) and a line in `.env.example`.
5. **The app name only lives in `APP_NAME`, `pyproject.toml` and the compose project name.** Don't write it into code, queries or messages; read `settings.app_name`.
6. **Dry-run must never reach Binance's order endpoints.** It is enforced inside `ExchangeClient`, the only module that talks to Binance. Don't call the python-binance client from anywhere else.
   Likewise every order is filter-validated there; don't add a second path that skips it.
7. **Never log or print secrets.** Keys are `SecretStr`.

## Changing the schema

1. Edit [models.py](../../bot/database/models.py).
2. `alembic revision --autogenerate -m "what changed"` and **read the result**. Autogenerate duplicates enum CHECK constraints (it emits the `Enum`'s own and the named one); keep the `op.f(...)` ones and set `create_constraint=False` on the `sa.Enum` columns, as in `0001`.
3. `alembic upgrade head`, then `alembic check` (must say "No new upgrade operations detected") and `alembic downgrade -1 && alembic upgrade head`.
4. Add a test in [tests/test_schema.py](../../tests/test_schema.py) for any new constraint.

## Tests

- Unit tests (`test_precision.py`, `test_settings.py`, `test_binance_client.py`, `test_user_stream.py`) need nothing.
- `tests/fakes.py` has `FakeBinance`, a stateful stand-in for python-binance's client: orders it accepts persist, `fill()`/`cancel_externally()` move balances and cancel the other OCO leg, and every call is recorded so tests can assert what was (or was not) sent. Add new endpoints there, copying real response shapes.
- `tests/test_engine.py` drives whole trades against the real database and that fake. The engine takes a `session_factory`, so tests hand it the shared rolled-back session. When you change engine behaviour, break it on purpose once and check a test fails.
- `tests/test_telegram_notifier.py` runs `TelegramNotifier` against a local aiohttp server that imitates the Bot API (real HTTP): requests, 429, 4xx, 5xx, timeouts and token hygiene. `tests/test_notifications.py` covers message text, the queue and the engine with a notifier attached. Nothing in the suite talks to real Telegram.
- Schema and reconciliation tests run against the real migrated PostgreSQL inside a transaction that is rolled back (`tests/conftest.py`), and skip if no database is reachable.
- Fixtures for Binance filters mirror live `exchangeInfo` responses. When adding one, copy a real response instead of inventing values.

## Adding things

- **A different entry signal:** change `evaluate_signal` in `scanner.py` (a pure function of closing prices, easy to test with hand-built series; start from `tests/test_scanner.py`). The scanner already ranks, spread-filters and picks. Replay it on history before trusting it (the replay in ADR-006 is a few lines against public candles).
- **An entry strategy:** implement `EntrySource.next_entry()` (returns an `EntrySignal` with the symbol and limit price, or None). The engine does the gating, sizing, persisting and protecting; the source only decides *what* to buy.
- **A notification:** subclass `EngineObserver` (hooks: entry placed/cancelled, exit placed, trade closed, needs attention, kill-switch tripped, reminder, protected again). A notification must only enqueue: the engine holds its lock while calling a hook, so a slow network call there would stall trade handling. `NotificationService` in [notifications.py](../../bot/services/notifications.py) is the working example (queue, background worker, hooks that never raise); pass it to `TradingEngine(observer=...)`.

- **A setting:** add the field with validation to `Settings`, add it to `.env.example`, add a case to `tests/test_settings.py`.
- **A new alert channel:** subclass `Notifier` (an async `send(text)` and `close()`) in `services/notifications.py` and hand it to `NotificationService`; the queue, formatting and mode label come for free. The engine only ever talks to `EngineObserver`. Messages are Telegram HTML, so another channel may need its own formatter.
- **Never put the Telegram token in a log, an error or a message.** It is part of the request URL, so `TelegramNotifier` raises `NotifierError` with scrubbed text and no chained aiohttp error; keep it that way.
- **A new strategy (planned):** the scanner returns ranked candidates; keep entry sizing and risk checks outside it.

## Facts checked vs assumed

**Checked live (2026-10-06, public endpoints, no keys):** filter names and shapes on mainnet and testnet, OCO allowed and $5 minimum notional on the candidate pairs, order book spreads, and the full dry-run path (size, round, validate) against live filters and average price. **Checked in Binance's docs and python-binance's source:** the listenKey stream was removed on 2026-02-20; python-binance 1.0.37 subscribes with `userDataStream.subscribe.signature`; the `orderList/oco` parameter names.

**Checked on the testnet with HMAC keys (2026-10-06):** balance, open orders, fee rates from `/api/v3/account`; the account stream opens with an HMAC key (a wrong secret gets no response); placing, cancelling and filling a limit buy with the real `executionReport` events (bare events with `s`, `i`, `c`, `C`, `x`, `X`, `g`; a cancel carries the original id in `C`; `listStatus` and `outboundAccountPosition` also arrive); fills and fees are visible immediately after a fill; OCO placement; cancelling one OCO leg cancels both and Binance replies with the whole list; a take-profit already below the market is refused with code -2010. **Not verified** (see [NEXT_STEPS.md](../../NEXT_STEPS.md)): an OCO leg filling, real fee amounts (testnet rates read 0), partial fills, the stream dropping mid-trade.

**Order placement errors:** `OrderRejected` (with `.code`) means Binance refused the order and nothing exists. Any other `ExchangeError` (timeout, clock error) leaves the outcome unknown: reconcile by client order id before retrying, or you may buy twice.

**Don't use python-binance's `/sapi` helpers** (`get_trade_fee`, margin, wallet, ...): the library has no testnet routing for them, so with `testnet=True` they send your key to the real exchange. `ExchangeClient.account_fees()` uses `/api/v3/account` instead, and a test fails if `get_trade_fee` is ever called.
