# Next Steps

Pending items only. When something is built or resolved, delete it from this file.

Last updated 2026-10-06, after phase 1 (foundation).

---

## Behaviour still to build (decided)

| Topic | Decision |
|---|---|
| Kill-switch | 3% realised loss in a rolling 24 hours: cancel unfilled buys, pause new buys for 24 hours, alert. It never sells an open position. Each trip writes a `kill_switch_events` row and tags the orders it cancelled |
| Pairs | Choose from `SYMBOLS`; when several signal, the lowest RSI that passes the spread check wins |
| Leftover coin after a sell | Sweep: each sell includes any sellable leftover of that coin, so leftovers stay under one quantity step per coin |
| Cancel reasons | Store the reason on every cancelled order (timeout, kill-switch, other OCO leg filled, cancelled outside the bot). The column and enum exist; the engine must fill them |
| Notifications | Telegram only (the Bot API is free), behind an interface |
| Startup check | Refuse to run if the configured size, take-profit or stop-loss would produce orders under Binance's minimum order value |
| Missed fills | REST reconciliation on startup and on every WebSocket reconnect |
| Deployment | docker-compose on a DigitalOcean droplet, with an external managed PostgreSQL (connection string only; compose creates no database) |

---

## Later

- **Pay fees in BNB.** Removes leftovers entirely and gives a fee discount. Needs about $5 of BNB, so wait until the wallet is bigger. Turn on "Use BNB to pay fees" in Binance; the bot needs no change.
- **Percentage-based trade size and more than one position.** Only workable from roughly $60 upward, because Binance's minimum order is $5 (checked for SOL/USDT on 2026-10-06).
- **Widen or tune the 2% stop** once there is real trade history.
- **Shorter buy timeout.** With one position, an unfilled buy locks the whole wallet for up to 3 days.
- **WhatsApp or another alert channel.**
- **Telegram commands.** Two-way control from the chat, for example `/status`, `/pause`, `/resume`, cancel the open buy, sell the open position now. Must only obey the owner's chat ID. The first version sends notifications only.

---

## To check

- Actual fee rate on the account (0.1% per side is assumed, not verified).
- Whether Binance's "convert small balances to BNB" can be triggered through the API.
- Which user-data stream method Binance currently supports for Spot, and what `python-binance` 1.0.37 uses (phase 2).
- Ask an accountant whether treating USDT as USD is acceptable for tax reporting, and how the swept leftover coin should be costed in `tax_ledger`.
- Whether GTC/USDT and the other candidate pairs pass the 0.2% spread filter often enough to be worth listing.
- Pick the managed database provider, then test the real connection: SSL (`sslmode` conversion is tested, a real server is not), a pooler endpoint vs the direct one (insight-engine found pooled Neon breaks asyncpg; the direct endpoint is the working choice), and that the DB user can create tables. The droplet guide has not been run anywhere.
- Compose `restart` policy is `"no"` because the phase 1 app exits after its checks. Change to `unless-stopped` when the trading loop exists.
- Add `ruff` and `alembic check` to CI once there is a repository.

---

## Build order

2. Exchange layer: Binance client, user-data stream, reconciliation on restart, on testnet.
3. Engine: position state machine, timeout rotation, OCO placement, leftover sweep.
4. Risk and scanner: spread filter, kill-switch, RSI/EMA scan, startup checks against Binance minimums.
5. Notifications and tax export.
6. Deployment: verify the Dockerfile, compose and droplet guide on a real droplet.
