# Next Steps

Pending items only. When something is built or resolved, delete it from this file.

Last updated 2026-10-06, after phase 3 (engine).

---

## Still unverified (testnet limits)

Verified on the testnet on 2026-10-06 with HMAC keys, including a full engine run (entry, fill, OCO, owner cancel, entry timeout, clean shutdown): see the developer guide. Not yet seen, and hard to force on a testnet:

- **An OCO leg actually filling** on the live exchange: the counterpart's real status (`CANCELED` or `EXPIRED`) and the relabelling are tested against a fake only. Both statuses are handled.
- **Fee math.** The testnet's commission rates read 0. The real rate (0.1% per side is assumed) and the coin fee that shrinks the sellable quantity show up only with real trades; startup logs the account rate. The ledger rules (coin fee on a buy not subtracted twice) are unit-tested, not checked against a real statement.
- **Partial fills** live, and the stream dropping mid-trade.
- **Failed-subscription cleanup.** After a failed subscribe the library kept trying to reconnect in the background, and `close_connection()` takes about 10 seconds. Check that repeated failures don't leak sockets or duplicate subscriptions (try it by pointing the stream at a bad key while the engine runs).
- **Dry run simulates no fills**: a dry-run entry only records the intent and times out. A simulated fill would be needed to rehearse the whole cycle without testnet.

---

## Behaviour still to build (decided)

| Topic | Decision |
|---|---|
| Unprotected position | Decided, never sell on its own. **(1) Exits cancelled outside the bot** (for example in the Binance app): one Telegram alert (plus a reminder every 15 min, proposed), and the bot does not re-place them. **(2) Stop-loss refused because the price already fell past it:** place the OCO anyway with the stop re-based to `stop_loss_pct` below the *current* bid (the take-profit stays as planned), and alert, e.g. "stop moved from 117.60 to 113.68". That bounds the loss instead of leaving the coins unprotected; the owner can still sell or cancel by hand. If it is still refused, keep retrying quietly. No market-sell fallback |
| Manual sale detection (gap found 2026-10-06) | If the owner sells by hand, the position stays OPEN forever and blocks new entries (max 1). Detect it (no live exit and the free balance can't cover the position) and close the position as `MANUAL`, taking the sale from the exchange's trade history so the ledger is right |
| Failed-exit retries (bug found 2026-10-06) | Each retry of a refused exit adds two new `REJECTED` order rows (every tick, about 5,700 a day). Throttle the retries (for example every few minutes) and reuse the rows |
| Kill-switch | 3% realised loss in a rolling 24 hours: `engine.cancel_pending_entries(KILL_SWITCH, ...)` (built), write a `kill_switch_events` row, set `bot_state.trading_paused_until` (the engine already respects it), alert. It never sells an open position |
| Entry source | `EntrySource` implementation: RSI-oversold plus EMA-rebound scan over `SYMBOLS`; when several signal, the lowest RSI that passes the spread check wins. The runner currently uses `NoEntries` |
| Spread filter | Skip an entry if the bid-ask spread is over `MAX_SPREAD_PCT` (`ExchangeClient.book_ticker().spread_pct` exists) |
| Startup check | Refuse to run if the configured size, take-profit or stop-loss would produce orders under Binance's minimum order value (`size_entry` already refuses unprotectable trades at entry time) |
| Notifications | Telegram only (the Bot API is free), implemented as an `EngineObserver` (hooks exist: entry placed/cancelled, exit placed, trade closed, needs attention) |
| CSV export | `reports/tax_exporter.py`: export `tax_ledger` to CSV |
| Deployment | docker-compose on a DigitalOcean droplet, with an external managed PostgreSQL (connection string only; compose creates no database) |

---

## Later

- **`order_fills` table.** An order that paid commission in two assets (for example BNB for part of it) can't be represented by one commission column; the code keeps the larger one and logs an error. Fills would also be the cleanest source for the tax ledger.
- **Pay fees in BNB.** Removes leftovers entirely and gives a fee discount. Needs about $5 of BNB, so wait until the wallet is bigger. Turn on "Use BNB to pay fees" in Binance; the engine already values BNB fees.
- **Percentage-based trade size and more than one position.** Only workable from roughly $60 upward, because Binance's minimum order is $5 (checked for SOL/USDT on 2026-10-06). `MAX_OPEN_POSITIONS` already exists.
- **Widen or tune the 2% stop** once there is real trade history.
- **Shorter buy timeout.** With one position, an unfilled buy locks the whole wallet for up to 3 days.
- **WhatsApp or another alert channel.**
- **Telegram commands.** Two-way control from the chat, for example `/status`, `/pause`, `/resume`, cancel the open buy, sell the open position now. Must only obey the owner's chat ID. The first version sends notifications only.
- **Partial fills of an exit leg** are waited out until the leg is fully filled; no special handling yet.

---

## To check

- Whether Binance's "convert small balances to BNB" can be triggered through the API.
- Ask an accountant whether treating USDT as USD is acceptable for tax reporting, and how the swept leftover coin (under one quantity step, from an earlier trade) should be costed in `tax_ledger`.
- Whether GTC/USDT keeps passing the 0.2% spread filter. One snapshot on 2026-10-06 showed 0.04% (BTC, ETH, SOL, BNB, XRP were all under 0.01%); sample it over several days before relying on it.
- Pick the managed database provider, then test the real connection: SSL (`sslmode` conversion is tested, a real server is not), a pooler endpoint vs the direct one (insight-engine found pooled Neon breaks asyncpg; the direct endpoint is the working choice), and that the DB user can create tables. The droplet guide has not been run anywhere.
- Compose `restart` policy is `"no"`. The loop now runs, but with no scanner there is nothing to keep alive; set `unless-stopped` once entries exist (phase 4 or 6).
- Add `ruff` and `alembic check` to CI once there is a repository.

---

## Build order

4. Risk and scanner: spread filter, kill-switch, RSI/EMA entry source, startup checks against Binance minimums.
5. Notifications (Telegram observer) and the CSV tax export.
6. Deployment: verify the Dockerfile, compose and droplet guide on a real droplet.
