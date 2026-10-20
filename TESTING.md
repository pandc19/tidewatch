# Testing guide

A manual test plan, in order. Everything up to stage 6 uses fake money (the Binance testnet) or sends no orders at all. **Stop at the end of stage 7**: real money is gated by [SETUP_CHECKLIST.md, section 8](SETUP_CHECKLIST.md), not by this file.

Tick a box only when you saw the expected result yourself. If something differs, write it down and fix the doc or the code; do not skip it.

> The entry signal has **no demonstrated edge** ([NEXT_STEPS.md](NEXT_STEPS.md)). These tests check that the *machinery* works (orders, protection, alerts, records), not that the bot makes money.

## Before you start

Read, in this order (about 30 minutes):

1. [README.md](README.md): what the bot is and its status.
2. [docs/guides/code-flow.md](docs/guides/code-flow.md): how one trade moves through the system. It tells you what each log line and alert in these tests means.
3. [GETTING_STARTED.md](GETTING_STARTED.md), sections 1 to 9: setup, Telegram, the demo mode and the testnet rehearsal.
4. [SETUP_CHECKLIST.md](SETUP_CHECKLIST.md): the stages A to E and the gate before real money.
5. [NEXT_STEPS.md](NEXT_STEPS.md), the "Still unverified" section: the things these tests cannot prove.
6. [docs/adr/005](docs/adr/005-engine-evaluates-state.md), [006](docs/adr/006-risk-controls-and-the-entry-signal.md) and [007](docs/adr/007-telegram-alerts-are-queued.md): why the engine, the kill-switch and the alerts behave as they do.
7. For stage 6 only: [docs/deployment.md](docs/deployment.md).

Reference while testing: [docs/guides/developer-guide.md](docs/guides/developer-guide.md) (commands, tests) and the table at the top of `.env.example`.

**Use a database of its own for each purpose** (GETTING_STARTED section 3): `tidewatch_test` for `pytest` (`TEST_DATABASE_URL`), `tidewatch_local` for demo runs and `tidewatch_testnet` for the long rehearsal. Testnet trades land in the same tables as real ones and the tax export does not mark which is which. Run `alembic upgrade head` against each one.

---

## 1. Automated checks (no network orders)

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/alembic upgrade head
.venv/bin/alembic check
```

- [ ] `pytest` passes with **no skipped database tests** (skips mean `TEST_DATABASE_URL` is unset or the database is unreachable).
- [ ] `ruff check .` is clean.
- [ ] `alembic upgrade head` succeeds on each empty database (`tidewatch_test`, `tidewatch_local`, `tidewatch_testnet`). `alembic check` reports no pending changes.

## 2. Startup checks

```bash
.venv/bin/python -m bot.main --check-only
```

- [ ] The log says the database is reachable, the exchange filters loaded, and (with testnet keys) your balance and "reconciled".
- [ ] The first startup line shows the mode you intended: testnet and dry-run on by default.
- [ ] With no keys in `.env`, it still passes and says what is missing, without a stack trace.
- [ ] No secret (key, secret, Telegram token, password) appears anywhere in the output.

## 3. Telegram (the biggest unverified item)

Steps are in GETTING_STARTED section 7.

```bash
.venv/bin/python -m bot.services.notifications --find-chat-id
.venv/bin/python -m bot.services.notifications --test
```

- [ ] `--find-chat-id` lists your chat and prints no token.
- [ ] `--test` puts a message on your phone, tagged `[TESTNET]` or `[DRY RUN]`.
- [ ] `--check-only` also sends one and reports the result in the log.
- [ ] Wrong token: log shows `Telegram refused sendMessage: 401` and the bot keeps going. Wrong chat id: `400 ... chat not found`. Neither prints the token or the request URL.
- [ ] Later, in stage 4: the `<pre>` tables in the messages line up on your phone.

## 4. Demo run on the testnet (minutes, fake money)

Needs testnet keys in `.env` (SETUP_CHECKLIST section 2). Demo mode forces the testnet.

```bash
./scripts/local.sh start demo
./scripts/local.sh status
./scripts/local.sh logs
```

Watch the log and your phone. Expected, in order:

- [ ] **Start:** checks pass, "Started (PID ...)", and a Telegram "started" message.
- [ ] **Scanner:** `entry signal: ...` lines (RSI value, pair).
- [ ] **Buy placed:** a log line and an alert; the order exists on the testnet.
- [ ] **Fill, then protection:** a take-profit and stop-loss pair (OCO) is placed and alerted.
- [ ] **Close:** `trade closed ...` with a PnL, and an alert. The close reason is take-profit or stop-loss.
- [ ] **Ledger:** `python -m bot.reports.tax_exporter` (stage 5) lists that trade.
- [ ] **Stop:** `./scripts/local.sh stop` says "Stopped cleanly", and you get a "stopped" alert.
- [ ] **Restart with an open position:** stop while a trade is open, start again. The position is picked up (reconciled), not duplicated, and its OCO is still on the exchange.

### Failure scenarios (do these while a position is open)

How to cancel or sell by hand is not scripted. NEXT_STEPS says these were tested "through the API"; the testnet website may also offer it. Use whichever works and note it here: ______

- [ ] **Cancel the OCO yourself.** Expect a "Needs your attention" alert about an unprotected position, then a reminder every 15 minutes (`UNPROTECTED_REMINDER_MINUTES`). The bot must **not** sell on its own.
- [ ] **Sell the coins by hand.** Expect the position to close as `MANUAL` within seconds, with the right PnL.
- [ ] **Cancel the open buy** before it fills. Expect the position to end cleanly with no leftover state.
- [ ] **Let a buy time out** (an unfilled entry). Expect it to be cancelled and the wallet freed.
- [ ] **Kill-switch.** `DAILY_LOSS_LIMIT_PCT=0.00001 ./scripts/local.sh start demo` and wait for a losing trade. Expect the kill-switch alert, no new entries afterwards, and the pause to survive a restart. (Known gap: it has only been tested with fabricated losses.)
- [ ] **Internet loss.** Disconnect the network for a minute while a position is open, then reconnect. The stream should reconnect, the engine should recover through polling, and nothing should be duplicated.

## 5. Tax export

```bash
.venv/bin/python -m bot.reports.tax_exporter --year 2026
```

- [ ] `exports/tax_ledger_2026.csv` exists with one row per closed trade.
- [ ] Prices, quantities, fees and net profit match the trades you saw, to the cent.
- [ ] The year is the calendar year of the sale by Ecuador days (`REPORT_TIMEZONE`); `sell_date_local` and `sell_date_utc` can differ in the evening. A `MANUAL` close is included.
- [ ] Rows come from the testing database only (no real trades mixed in).

## 6. Production rehearsal on the testnet (weeks)

GETTING_STARTED section 9: real settings, fake money, `DRY_RUN=false`, `ALLOCATION_PCT=0.0016`, `DAILY_LOSS_LIMIT_PCT=0.00005`.

```bash
./scripts/local.sh start
```

- [ ] It runs for days without a crash; check `./scripts/local.sh status` daily.
- [ ] Every alert you receive makes sense; read each "Needs your attention" the same day.
- [ ] How often does the account stream fail to subscribe? (NEXT_STEPS: once about 30 s in testing.) Count them.
- [ ] No leaked sockets or duplicate subscriptions after repeated failures.
- [ ] At least one trade closes by take-profit and one by stop-loss, and each OCO's other leg ends `CANCELED` or `EXPIRED`.

## 7. Docker and server rehearsal (still no real money)

On your Mac first, then on a droplet. See [docs/deployment.md](docs/deployment.md).

```bash
./scripts/preflight.sh
docker compose up -d --build
docker compose logs -f app
```

- [ ] `preflight.sh` has no FAIL lines. Warnings are understood.
- [ ] Try the branches that were written but never run: `DATABASE_URL` pointing at `localhost` (FAIL), `DRY_RUN=false` without keys (FAIL), a Binance URL that returns 451 (use `PREFLIGHT_PING_URL`).
- [ ] The container applies migrations, then starts. Logs appear in `docker compose logs`.
- [ ] `docker stop` shuts the bot down cleanly (a "stopped" alert, no SIGKILL) within the 40 s grace period.
- [ ] With `RESTART_POLICY=unless-stopped`, killing the container brings it back and it reconciles.
- [ ] On the droplet: Binance accepts the server's IP, the managed database connects over SSL, the DB user can create tables.
- [ ] A crashed bot is silent (no heartbeat yet). Decide how you will notice.

## 8. Dry-run on real data (no orders)

SETUP_CHECKLIST section 5: real read-only key, `BINANCE_TESTNET=false`, `DRY_RUN=true`.

- [ ] `--check-only` shows your **real** USDT balance and **real fee rate** (this is where the 0.1% assumption is finally checked).
- [ ] The bot logs the mode as dry-run, alerts say `[DRY RUN]`, and Binance shows **no orders** on your account.
- [ ] Dry-run entries time out after `ENTRY_TIMEOUT_DAYS` (no fills are simulated).

---

## What no test here can prove

From NEXT_STEPS.md, only real trades show these:

- The kill-switch with real losses, and the emergency stop on the live exchange.
- An OCO leg filling live, and partial fills.
- Real fees, including the coin fee that shrinks the sellable quantity.
- Whether the entry signal has any edge.

Real money starts only when the gate in [SETUP_CHECKLIST.md, section 8](SETUP_CHECKLIST.md) is fully ticked, including the signal decision and an accountant's answer.
