# Code Flow

How one trade moves through the system, and why each step is shaped the way it is. Parts marked **(planned)** are not built yet; the rest exists today. The engine runs, but with no scanner it opens no positions on its own.

## 0. Startup (built)

`python -m bot.main` → settings → database ping → `ExchangeClient.create()` → every pair in `SYMBOLS` is checked against Binance's live filters (tradable, OCO allowed) → with API keys: fee and balance lookup, then `reconcile()`. A bad pair or a reconciliation problem stops startup with exit code 1.

- **Settings** ([bot/config/settings.py](../../bot/config/settings.py)) are read once into an immutable object. Invalid values (allocation over 100%, a symbol that doesn't end in the quote asset, a zero stop-loss) stop the process at startup instead of mid-trade.
- **Database** ([bot/database/connection.py](../../bot/database/connection.py)) hands out sessions that commit on success and roll back on any error. `pool_pre_ping` lets it survive a Postgres restart.
- **Migrations** run before the app (`alembic upgrade head`), so code never meets an old schema.

## 1. Exchange layer (built, phase 2)

[binance_client.py](../../bot/services/binance_client.py) is the only module that talks to Binance REST. It returns small `Decimal` dataclasses (`ExchangeOrder`, `Fill`, `BookTicker`, ...), never raw dicts. Two rules live here so nothing can bypass them: every order is validated against the symbol's filters first, and `dry_run` returns a synthetic order instead of calling an order endpoint. Public market data needs no keys; signed calls raise a clear error without them. A refused order raises `OrderRejected` (Binance said no; nothing exists); any other failure leaves the outcome unknown and must be reconciled by client order id.

[user_stream.py](../../bot/services/user_stream.py) listens to the account stream, and [reconciliation.py](../../bot/services/reconciliation.py) brings the `orders` table in line with Binance. The stream is only a *notification* ("order X changed"); the order and its fills are re-read over REST, so a lost or odd event cannot corrupt state. After every (re)connect, and at startup, a full `reconcile()` closes any gap. It records status, fills, fees and the cancel reason, and reports failures and unknown orders; it never decides anything. Why: [ADR-004](../adr/004-stream-is-notification-rest-is-truth.md).

**Cancel reasons.** An order found cancelled with no reason is labelled `EXTERNAL`; the engine passes its own reason (`ENTRY_TIMEOUT`, `KILL_SWITCH`) when it cancels on purpose; the losing leg of an OCO pair becomes `OCO_COUNTERPART_FILLED`, whichever leg syncs first.

## 2. Scan and size an entry (built, phase 4)

`Scanner` (an `EntrySource`) runs at most every `SCAN_EVERY_SECONDS`. For each pair in `SYMBOLS` it takes the closed candles (the one still forming is dropped), computes RSI and EMA ([indicators.py](../../bot/services/indicators.py), checked against StockCharts' published examples) and applies one rule: RSI was at or below `RSI_OVERSOLD` within the last `RSI_LOOKBACK` candles **and** the latest close crossed up through the EMA (so a dip signals on exactly one candle). Signalling pairs with a spread over `MAX_SPREAD_PCT` are dropped; the lowest RSI of the rest wins, and the entry is a limit buy at the best bid (optionally `ENTRY_DISCOUNT_PCT` below).

**Honest caveat:** this rule is a placeholder. Replayed on 250 days of hourly history it won 33% of the time against the 36.7% needed (ADR-006). The engine does the gating and sizing: `size_entry` rounds down to the step and refuses a trade whose stop-loss sell could not be placed after fees (Binance's minimum order value).

## 3. Place the entry (built, phase 3)

`TradingEngine.open_position` checks the gates (not blocked, not paused, fewer than `MAX_OPEN_POSITIONS` live, no live position on that symbol), sizes the buy with `size_entry` (98% of the free quote balance, refused if the stop-loss leg could not be placed after fees), then **commits** a `positions` row (`PENDING_ENTRY`) and an `orders` row (`BUY`, `LIMIT`, `expires_at = now + ENTRY_TIMEOUT_DAYS`) with our own `client_order_id` **before** the request is sent. *Why:* if the answer is lost, the order is found again by that id instead of being bought twice. A refusal (`OrderRejected`) cancels the position; any other failure leaves it pending and is resolved by lookup on a later tick. Entries come from an `EntrySource`; until the scanner exists (phase 4) the runner uses one that never signals.

## 4. Fill → OCO exit (built, phase 3)

`_evaluate` sees the buy `FILLED` (or cancelled with a partial fill) and opens the position: quantity is what was filled minus the coin fee, entry price is the average fill. `_place_exit` sells that quantity (plus a sub-step crumb, never the owner's own coins), with take-profit `entry × (1 + take_profit_pct)`, stop trigger `entry × (1 − stop_loss_pct)` and a stop-limit `STOP_LIMIT_OFFSET_PCT` below it. The two exit rows are committed before the OCO is sent. If Binance refuses with -2010 because the market already passed one of the two prices, it is corrected once and retried: past the **target**, the take-profit moves just above the best ask; past the **stop**, an emergency stop is placed `STOP_LOSS_PCT` below the *current* bid (take-profit unchanged) and the owner is told, so the loss is bounded instead of the coins left bare. If it is still refused, the same two `REJECTED` rows are reused, the position is flagged as unprotected, and the tick retries no more often than `EXIT_RETRY_SECONDS`. Nothing is ever sold on the bot's own initiative.

## 5. Timeout rotation (built, phase 3)

Each tick cancels buys past `expires_at` through `sync_order(..., cancel_reason=ENTRY_TIMEOUT)`, so the reason is stored with the cancellation. An unfilled buy becomes `CANCELLED` and frees the slot; a *partly* filled one keeps what filled and becomes `OPEN` with its OCO. If the buy filled in the instant before the cancel, Binance refuses the cancel ("unknown order"), the sync finds the fill, and the position opens normally.

## 6. Close and record (built, phase 3)

When an exit leg is `FILLED`, `_close` computes the result (`engine/ledger.py`), sets the position `CLOSED` with `exit_reason` (`TAKE_PROFIT` for the limit-maker leg, `STOP_LOSS` otherwise), writes one `tax_ledger` row using the exchange's fill times, adds to that UTC day's `daily_pnl`, and syncs the other leg so it ends terminal and labelled `OCO_COUNTERPART_FILLED`. Profit is `total_sell − total_buy − cash fees`; the coin fee on the buy is not subtracted twice (see `ledger.py`). If the owner sells by hand, the bot notices (an OPEN position with no live or filled exit, and a sell in the account's trade history that none of its orders explains) and closes the position as `MANUAL`, booking exactly the position's quantity from the real trade prices and fees; a partial sale is flagged, not guessed at. The CSV export and the Telegram message arrive in phase 5 (observer hooks are in place).

## 7. Kill-switch (built, phase 4)

Checked first on every tick. It adds up the realised PnL of positions closed in the last 24 hours (gains offset losses). If the net result is a loss of at least `DAILY_LOSS_LIMIT_PCT` of the account (quote balance plus open positions at entry cost), it cancels unfilled entry buys (reason `KILL_SWITCH`, linked to a new `kill_switch_events` row), sets `bot_state.trading_paused_until` for `KILL_SWITCH_COOLDOWN_HOURS` (stored, so a restart cannot forget it), marks the day in `daily_pnl`, and alerts. It never sells: an open position keeps its own OCO. After a trip only newer losses count, so the same ones cannot trip it twice. The engine refuses new entries while the pause lasts.

## How the pieces run

`python -m bot.main` → startup checks → `EngineRunner`: the account stream (live mode with keys) calls `engine.on_connected` on every (re)connect and `engine.on_stream_event` for each order update; a tick every `ENGINE_TICK_SECONDS` calls `engine.tick`. Every state change runs under one lock and ends in `_evaluate`, which looks at a position's orders and decides the next step, so the same code handles a live event, a missed one, and a restart. Why: [ADR-005](../adr/005-engine-evaluates-state.md).

```
PENDING_ENTRY ──buy filled (or part-filled then timed out)──▶ OPEN ──exit leg filled──▶ CLOSED
      │
      └──buy cancelled / refused, nothing filled──▶ CANCELLED
```

## Data model (built)

```
positions 1──* orders            orders: entry BUY + OCO legs (shared order_list_id)
positions 1──1 tax_ledger        one ledger row per closed position
kill_switch_events 1──* orders   which orders a trip cancelled
daily_pnl, bot_state             summary per UTC day / single-row pause state
```

See [ADR-002](../adr/002-positions-and-order-schema.md) for why it is shaped this way.
