# Next Steps

Pending items only. When something is built or resolved, delete it from this file.

Last updated 2026-10-06, after phase 2 (exchange layer).

---

## Still unverified (testnet limits)

Verified on the testnet on 2026-10-06 with HMAC keys (see the developer guide): balance, open orders, fee rates, the account stream, placing and cancelling orders, a fill with immediate fill/fee data, OCO placement, cancelling one OCO leg (cancels both), the rejected-take-profit error (-2010), and the real `executionReport` shape. Not yet seen, and hard to force on a testnet:

- **An OCO leg actually filling.** The testnet price did not move, so the counterpart's real status (`CANCELED` or `EXPIRED`) and the `OCO_COUNTERPART_FILLED` relabelling are only tested against a fake. Both statuses are handled.
- **Fee math.** The testnet's commission rates read 0. The real rate (0.1% per side is assumed) and the base-asset fee that shrinks the quantity you can sell show up only with real trades; startup logs the account rate.
- **Partial fills** on the live exchange, and the stream dropping mid-trade.
- **Failed-subscription cleanup.** After a failed subscribe the library kept trying to reconnect in the background, and `close_connection()` takes about 10 seconds. When the engine wires the stream, check that repeated failures don't leak sockets or duplicate subscriptions.

---

## Behaviour still to build (decided)

| Topic | Decision |
|---|---|
| Kill-switch | 3% realised loss in a rolling 24 hours: cancel unfilled buys, pause new buys for 24 hours, alert. It never sells an open position. Each trip writes a `kill_switch_events` row and tags the orders it cancelled (`sync_order` already accepts the reason and event id) |
| Pairs | Choose from `SYMBOLS`; when several signal, the lowest RSI that passes the spread check wins |
| Leftover coin after a sell | Sweep: each sell includes any sellable leftover of that coin, so leftovers stay under one quantity step per coin |
| Cancel reasons | The engine must pass its reason (`ENTRY_TIMEOUT`, `KILL_SWITCH`) to `sync_order` whenever it cancels on purpose. Reconciliation already labels the rest (`EXTERNAL`, `OCO_COUNTERPART_FILLED`) |
| Notifications | Telegram only (the Bot API is free), behind an interface |
| Startup check | Refuse to run if the configured size, take-profit or stop-loss would produce orders under Binance's minimum order value |
| Stream wiring | The engine starts `UserStream.for_client(...)`: `on_connected` runs `reconcile()`, `on_order_event` calls `sync_order` for that order and acts on the returned `OrderChange` (fill → place the OCO) |
| Take-profit edge case | If the market is already past the target when the OCO is placed, Binance rejects it with code -2010 (confirmed on the testnet), which `place_oco_sell` raises as `OrderRejected`. The engine needs a fallback (for example, sell at market). Any other placement failure leaves the outcome unknown: reconcile by client order id first |
| Deployment | docker-compose on a DigitalOcean droplet, with an external managed PostgreSQL (connection string only; compose creates no database) |

---

## Later

- **`order_fills` table.** An order that paid commission in two assets (for example BNB for part of it) can't be represented by one commission column; the code keeps the larger one and logs an error. Fills would also be the cleanest source for the tax ledger.
- **Pay fees in BNB.** Removes leftovers entirely and gives a fee discount. Needs about $5 of BNB, so wait until the wallet is bigger. Turn on "Use BNB to pay fees" in Binance; the bot needs no change.
- **Percentage-based trade size and more than one position.** Only workable from roughly $60 upward, because Binance's minimum order is $5 (checked for SOL/USDT on 2026-10-06).
- **Widen or tune the 2% stop** once there is real trade history.
- **Shorter buy timeout.** With one position, an unfilled buy locks the whole wallet for up to 3 days.
- **WhatsApp or another alert channel.**
- **Telegram commands.** Two-way control from the chat, for example `/status`, `/pause`, `/resume`, cancel the open buy, sell the open position now. Must only obey the owner's chat ID. The first version sends notifications only.

---

## To check

- Whether Binance's "convert small balances to BNB" can be triggered through the API.
- Ask an accountant whether treating USDT as USD is acceptable for tax reporting, and how the swept leftover coin should be costed in `tax_ledger`.
- Whether GTC/USDT keeps passing the 0.2% spread filter. One snapshot on 2026-10-06 showed 0.04% (BTC, ETH, SOL, BNB, XRP were all under 0.01%); sample it over several days before relying on it.
- Pick the managed database provider, then test the real connection: SSL (`sslmode` conversion is tested, a real server is not), a pooler endpoint vs the direct one (insight-engine found pooled Neon breaks asyncpg; the direct endpoint is the working choice), and that the DB user can create tables. The droplet guide has not been run anywhere.
- Compose `restart` policy is `"no"` because the app still exits after its startup checks. Change to `unless-stopped` when the trading loop exists.
- Add `ruff` and `alembic check` to CI once there is a repository.

---

## Build order

3. Engine: position state machine, timeout rotation, OCO placement, leftover sweep, stream wiring.
4. Risk and scanner: spread filter, kill-switch, RSI/EMA scan, startup checks against Binance minimums.
5. Notifications and tax export.
6. Deployment: verify the Dockerfile, compose and droplet guide on a real droplet.
