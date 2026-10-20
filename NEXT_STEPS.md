# Next Steps

Pending items only. When something is built or resolved, delete it from this file. What *you* must set up (keys, Telegram, database, server) is in [SETUP_CHECKLIST.md](SETUP_CHECKLIST.md).

Last updated 2026-10-19, during phase 5 (CSV export and Telegram alerts built; Telegram delivery verified, trade-alert layout not yet seen).

---

## Telegram: trade alerts on a real run

The bot, token and chat ID work against real Telegram (`--test` arrived on the phone, 2026-10-19; wrong token and wrong chat ID fail cleanly without leaking the token). Still to check: `python -m bot.main --check-only` sending its message, and on a testnet run that the HTML renders as intended (tables aligned in the `<pre>` blocks).

---

## Decision needed from the owner: the entry signal has no demonstrated edge

The RSI-oversold + EMA cross-up rule is a placeholder I chose, not a validated strategy. Replayed on about 250 days of hourly mainnet candles (BTC, ETH, SOL, BNB, XRP; 190 signals, one position at a time, a candle touching both levels counted as a loss), the 4% take-profit came before the 2% stop **33%** of the time. After ~0.2% fees you need **36.7%** to break even, so the expected result was about **−0.24% per trade before slippage**. At this sample size that is not conclusive either way (the gap is about one standard error), but it is certainly not evidence of an edge. Per pair: ETH 43%, BNB 38%, SOL 33%, BTC 27%, XRP 22%.

Options (they combine):
1. **Run it on the testnet and in dry-run for a few weeks** and look at real behaviour before any real money. Costs nothing.
2. **A careful parameter study** (RSI threshold, candle size, lookback, EMA period, TP/SL ratio) with an out-of-sample split so it is not just curve-fitted. I can build a small replay tool for this.
3. **Accept it as a plumbing test** and treat all real-money use as not ready.

My recommendation: 1 now, 2 before real money. Nothing in the code should be described as profitable until then.

---

## Still unverified (testnet limits)

Verified live on the testnet on 2026-10-06: the full engine run (entry, fill, OCO, owner cancel, entry timeout), and an owner cancelling the OCO and selling by hand (closed as `MANUAL` within a second, exact PnL). Not yet seen, and hard to force on a testnet:

- **The kill-switch with real losing trades.** Tested against the real database with fabricated losses only.
- **The re-based emergency stop** on the live exchange (needs a price that fell through the stop). Tested against a fake that raises Binance's refusal.
- **An OCO leg actually filling** live: the counterpart's real status (`CANCELED` or `EXPIRED`) is tested against a fake only. Both are handled.
- **Fee math.** The testnet's commission rates read 0 until you set them to 0.1% maker and taker on 2026-10-19 (the startup check now logs 0.001; whether fills actually charge it, and in which asset, is not yet seen). The real rate (0.1% per side is assumed) and the coin fee that shrinks the sellable quantity show up only with real trades.
- **Partial fills** live, and the stream dropping mid-trade. The account stream failed to subscribe for about 30 seconds once during testing ("Request timed out"); the engine kept working through its polling safety net and the stream reconnected with backoff. Worth watching how often it happens.
- **Failed-subscription cleanup.** After a failed subscribe the library kept trying to reconnect in the background, and `close_connection()` takes about 10 seconds. Check that repeated failures don't leak sockets or duplicate subscriptions.
- **Dry run simulates no fills**: a dry-run entry only records the intent and times out.

---

## Behaviour still to build (decided)

| Topic | Decision |
|---|---|
| Deployment | Coolify on the shared DigitalOcean droplet, built from the Dockerfile on every merge to `main` ([docs/coolify.md](docs/coolify.md)); PostgreSQL stays external (`shared-postgres`, connection string only). The manual docker compose route remains in docs/deployment.md |

---

## Later

- **A replay/backtest tool** for the entry signal (see the decision above). Also useful: a re-entry cooldown, so a position that just closed cannot be reopened on the same signal candle.
- **`order_fills` table.** An order that paid commission in two assets (for example BNB for part of it) can't be represented by one commission column; the code keeps the larger one and logs an error. Fills would also be the cleanest source for the tax ledger.
- **Pay fees in BNB.** Removes leftovers entirely and gives a fee discount. Needs about $5 of BNB, so wait until the wallet is bigger. Turn on "Use BNB to pay fees" in Binance; the engine already values BNB fees.
- **Percentage-based trade size and more than one position.** Only workable from roughly $60 upward, because Binance's minimum order is $5 (checked for SOL/USDT on 2026-10-06). `MAX_OPEN_POSITIONS` already exists.
- **Widen or tune the 2% stop** once there is real trade history.
- **Shorter buy timeout.** With one position, an unfilled buy locks the whole wallet for up to 3 days.
- **WhatsApp or another alert channel.**
- **More Telegram commands.** `/status`, `/csv`, `/report` and `/help` exist (read-only, ADR-008; set `TELEGRAM_COMMANDS=true`; none seen against real Telegram yet, and the file upload not at all). Still to decide: state-changing commands such as `/pause`, `/resume`, cancel the open buy, sell the open position now. Each needs its own ADR and a safe path into the engine. `/status` could also show the last scan and why no entry has fired.
- **Partial manual sales and partial fills of an exit leg** are flagged or waited out; no special handling.

---

## To check

- **Email delivery of the reports** (Mailgun, three variables `MAILGUN_API_KEY`, `MAILGUN_DOMAIN`, `MAILGUN_FROM_EMAIL`, as in the owner's insight-engine). Not built: today `/csv` and `/report` arrive as Telegram files. Decide whether email should replace or add to that, and whether it should also send a monthly report on a schedule.

- Whether Binance's "convert small balances to BNB" can be triggered through the API.
- Ask an accountant whether treating USDT as USD is acceptable for tax reporting, and how the swept leftover coin (under one quantity step, from an earlier trade) should be costed in `tax_ledger`.
- Whether GTC/USDT keeps passing the 0.2% spread filter. One snapshot on 2026-10-06 showed 0.04% (BTC, ETH, SOL, BNB, XRP were all under 0.01%); sample it over several days before relying on it.
- Pick the managed database provider, then test the real connection: SSL (`sslmode` conversion is tested, a real server is not), a pooler endpoint vs the direct one (insight-engine found pooled Neon breaks asyncpg; the direct endpoint is the working choice), and that the DB user can create tables. The droplet guide has not been run anywhere.
- The restart policy is now `RESTART_POLICY` in `.env` (default `no`). Set `unless-stopped` when the bot should run unattended, after the signal decision above.
- **Optional Postgres service in compose** for the free-server route (a separate opt-in file, so the default stays "no database in compose"). Needs your decision; until then `docs/deployment.md` explains running Postgres yourself.
- **A heartbeat alert.** The engine now writes a heartbeat file and `python -m bot.instance` can check it (used by the Docker health check), so a hung or crashed bot shows as unhealthy in the platform. Nothing alerts you yet: decide between a periodic "still running" Telegram message and a platform notification.
- **Run the real deployment once** (any server) and note what needed adjusting; also exercise the pre-flight script's "Binance refuses this server" and "live mode without keys" branches, which were written but not run.
- Add `ruff` and `alembic check` to CI once there is a repository.

---

## Build order

5. Notifications: done; delivery verified, trade-alert layout to be seen on a testnet run.
6. Deployment: Dockerfile, CI and git flow built; the Coolify application, its database and the first deploy (checklist at the bottom of docs/coolify.md) are still to do.
