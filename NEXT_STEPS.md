# Next Steps

Pending items only. When something is built or resolved, delete it from this file. What *you* must set up (keys, Telegram, database, server) is in [SETUP_CHECKLIST.md](SETUP_CHECKLIST.md).

Last updated 2026-10-20, during phase 6: the bot runs on the Binance testnet on Coolify (since 2026-10-19) and is being monitored for a few days. Telegram delivery is verified; trade-alert layout is not yet seen.

---

## Telegram: trade alerts on a real run

The bot, token and chat ID work against real Telegram (`--test` arrived on the phone, 2026-10-19; wrong token and wrong chat ID fail cleanly without leaking the token). Still to check: `python -m bot.main --check-only` sending its message, and on a testnet run that the HTML renders as intended (tables aligned in the `<pre>` blocks).

---

## Monitoring the testnet rehearsal (running since 2026-10-19, a few days)

The bot runs on Coolify against the testnet with `DRY_RUN=false`, `ALLOCATION_PCT=0.0016` and `DAILY_LOSS_LIMIT_PCT=0.00005`. **The code that ran was older than its tag:** the container tagged e4756c3 (#17) held a cached build from between #14 and #17 (seen 2026-10-26: the TRX close on 10-25 was booked with the old ledger rule). The Dockerfile fix forces the rebuild; count the rehearsal from the first deploy after it, and confirm the running code as in [docs/coolify.md](docs/coolify.md). Nothing is wrong if it stays quiet: the signal fired about once every one to three days in the replay. While it runs, look for:

- **The alerts' look on the phone:** the `<pre>` tables of the buy, protection and close alerts (the first trade, 2026-10-21, sent all three).
- **The take-profit way out:** a close by take-profit and its other leg ending `CANCELED` or `EXPIRED`. A stop-loss close was seen on 2026-10-21 (the take-profit leg ended `EXPIRED`), and a deploy with that position open reconciled cleanly.
- **The account stream:** how often it fails to subscribe, and any "unprotected" or "needs your attention" alert.
- **Coolify:** the application stays healthy; the stop timeout is 40 s or more.

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
- **Fee math.** The testnet's commission rates read 0 until you set them to 0.1% maker and taker on 2026-10-19 and the fills charge it: a 2026-10-21 trade paid 0.1% in the coin bought on the buy (0.0000114 of 0.0114 SNDKB) and 0.1% in USDT on the sell. Still unseen: the real mainnet rate (0.1% per side is assumed).
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
- Ask an accountant whether treating USDT as USD is acceptable for tax reporting, and how a leftover coin (the coin fee and the rounding of a sale down to a quantity step stay in the account, are not booked as a loss, and under one step are swept into the next sale of that coin, costed at that trade's buy price) should be treated in `tax_ledger`.
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
6. Deployment: built and running on the testnet via Coolify since 2026-10-19; now monitoring for a few days (see the section above). Left: the rest of the Coolify checklist (stop timeout, a hand-over with an open position, backups) and the gate to real money.
