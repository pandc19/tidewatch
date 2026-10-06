# Code Flow

How one trade moves through the system, and why each step is shaped the way it is. Parts marked **(planned)** are not built yet; the rest exists today.

## 0. Startup (built)

`python -m bot.main` → settings → database ping → `ExchangeClient.create()` → every pair in `SYMBOLS` is checked against Binance's live filters (tradable, OCO allowed) → with API keys: fee and balance lookup, then `reconcile()`. A bad pair or a reconciliation problem stops startup with exit code 1.

- **Settings** ([bot/config/settings.py](../../bot/config/settings.py)) are read once into an immutable object. Invalid values (allocation over 100%, a symbol that doesn't end in the quote asset, a zero stop-loss) stop the process at startup instead of mid-trade.
- **Database** ([bot/database/connection.py](../../bot/database/connection.py)) hands out sessions that commit on success and roll back on any error. `pool_pre_ping` lets it survive a Postgres restart.
- **Migrations** run before the app (`alembic upgrade head`), so code never meets an old schema.

## 1. Exchange layer (built, phase 2)

[binance_client.py](../../bot/services/binance_client.py) is the only module that talks to Binance REST. It returns small `Decimal` dataclasses (`ExchangeOrder`, `Fill`, `BookTicker`, ...), never raw dicts. Two rules live here so nothing can bypass them: every order is validated against the symbol's filters first, and `dry_run` returns a synthetic order instead of calling an order endpoint. Public market data needs no keys; signed calls raise a clear error without them. A refused order raises `OrderRejected` (Binance said no; nothing exists); any other failure leaves the outcome unknown and must be reconciled by client order id.

[user_stream.py](../../bot/services/user_stream.py) listens to the account stream, and [reconciliation.py](../../bot/services/reconciliation.py) brings the `orders` table in line with Binance. The stream is only a *notification* ("order X changed"); the order and its fills are re-read over REST, so a lost or odd event cannot corrupt state. After every (re)connect, and at startup, a full `reconcile()` closes any gap. It records status, fills, fees and the cancel reason, and reports failures and unknown orders; it never decides anything. Why: [ADR-004](../adr/004-stream-is-notification-rest-is-truth.md).

**Cancel reasons.** An order found cancelled with no reason is labelled `EXTERNAL`; the engine passes its own reason (`ENTRY_TIMEOUT`, `KILL_SWITCH`) when it cancels on purpose; the losing leg of an OCO pair becomes `OCO_COUNTERPART_FILLED`, whichever leg syncs first.

## 2. Scan and size an entry **(planned, phase 4)**

For each pair in `SYMBOLS`: RSI oversold plus EMA rebound. If several signal, the lowest RSI that also passes the spread filter (≤ `MAX_SPREAD_PCT`) wins. Size = `ALLOCATION_PCT` of the free quote balance, then rounded and checked with [precision.py](../../bot/config/precision.py).

*Why a fixed order of checks:* rounding comes first because Binance validates the rounded values, and a startup check refuses configurations whose orders would fall under Binance's minimum order value (about $5 on SOL/USDT, checked 2026-10-06).

## 3. Place the entry **(planned, phase 3)**

A `positions` row (`PENDING_ENTRY`) and an `orders` row (`BUY`, `LIMIT`, `expires_at = now + ENTRY_TIMEOUT_DAYS`) are written with our own `client_order_id` **before** the request is sent. *Why:* if the response is lost, restart can find the order on Binance by that id instead of buying twice. A partial unique index allows only one live position per symbol.

## 4. Fill → OCO exit **(planned, phase 3)**

The user-data stream reports the fill. The position becomes `OPEN`, and the bot immediately sends an OCO sell for the quantity actually held (after the entry fee, rounded down to the step; any sellable leftover of that coin is swept in). Take-profit is `entry × (1 + TAKE_PROFIT_PCT)`; the stop trigger is `entry × (1 − STOP_LOSS_PCT)` with a stop-limit slightly below it (`STOP_LIMIT_OFFSET_PCT`).

## 5. Timeout rotation **(planned, phase 3)**

A buy not filled by `expires_at` is cancelled (the engine passes `ENTRY_TIMEOUT` to `sync_order`), the position becomes `CANCELLED`, and the scan runs again. A *partly* filled buy keeps what filled: the remainder is cancelled and the filled part gets its OCO.

## 6. Close and record **(planned, phase 3 and 5)**

When one OCO leg fills, Binance cancels the other (stored as `OCO_COUNTERPART_FILLED`). The position closes, one `tax_ledger` row is written (quantities, prices, fees, net profit; USDT treated as USD), `daily_pnl` is updated, and Telegram sends the result.

## 7. Kill-switch **(planned, phase 4)**

If realised losses over a rolling 24 hours reach `DAILY_LOSS_LIMIT_PCT` of the account: cancel unfilled buys (`KILL_SWITCH`), write a `kill_switch_events` row (loss, limit, window), set `bot_state.trading_paused_until`, alert. It never sells an open position, which keeps its own OCO. The pause lives in the database so a restart can't forget it.

## Data model (built)

```
positions 1──* orders            orders: entry BUY + OCO legs (shared order_list_id)
positions 1──1 tax_ledger        one ledger row per closed position
kill_switch_events 1──* orders   which orders a trip cancelled
daily_pnl, bot_state             summary per UTC day / single-row pause state
```

See [ADR-002](../adr/002-positions-and-order-schema.md) for why it is shaped this way.
