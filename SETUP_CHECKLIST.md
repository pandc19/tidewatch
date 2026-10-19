# Setup checklist: what only you can set up

The code is built up to phase 5. What is left is mostly **accounts, keys and servers that only you can create**. This file lists each piece, in order: why it is needed, how to do it, how to check it worked, and what has **not** been verified yet.

Tick the boxes as you go. Work through the stages in order; do not skip ahead to real money.

> **Read this first.** Crypto trading can lose money, this software has not been audited, and its entry signal has **no demonstrated edge** (see [NEXT_STEPS.md](NEXT_STEPS.md)). Nothing here is financial or tax advice.

## The stages

| Stage | What it is | Money at risk | Status |
|---|---|---|---|
| **A** Local + Binance testnet | Develop and test with fake money | None | Done on your machine |
| **B** Telegram alerts | Alerts on your phone | None | Done; test message verified 2026-10-19 |
| **C** Dry-run on real market data | Real prices and balance, never sends an order | None | Ready to try |
| **D** Server + managed database | Run unattended on a DigitalOcean droplet | None (still dry-run) | **Not yet run anywhere** |
| **E** Small live trades | Real orders with a small wallet | **Yes** | Blocked until the gate below passes |

---

## 1. PostgreSQL (the bot's memory)

- [x] **Local development.** The Postgres container on port 5432 with the `tidewatch_local`, `tidewatch_testnet` and `tidewatch_test` databases. Done.
- [ ] **Production: a managed database.** Needed so trade history, the tax ledger and the kill-switch pause survive restarts and server rebuilds.
  1. Pick a provider (DigitalOcean Managed Databases, Neon, Supabase, ...). PostgreSQL 13 or newer.
  2. Create a database and a dedicated user for the bot (not the admin user). The user needs to create tables in its schema, because migrations run at every start.
  3. Allow the server's IP in the database's trusted sources.
  4. Copy the **direct** connection string, not a pooled one. A pooler (PgBouncer) breaks asyncpg's prepared statements; the sister project insight-engine hit this on Neon.
  5. Put it in `.env` as `DATABASE_URL=...`. Provider-style URLs (`postgres://...?sslmode=require`) work as pasted; special characters in the password must be URL-encoded (`@` becomes `%40`).
  6. Check: `python -m bot.main --check-only` says "database reachable".
- **Not verified:** a real managed database (SSL, user permissions). Only the local container has been used.

## 2. Binance testnet keys (fake money)

- [x] Done. Keys from https://testnet.binance.vision in `BINANCE_API_KEY` / `BINANCE_API_SECRET`, with `BINANCE_TESTNET=true`.
- Check: `python -m bot.main --check-only` logs your balance and "reconciled".
- Run everything on your own machine: `./scripts/local.sh start demo` (testnet only, fast) or `./scripts/local.sh start`. See [GETTING_STARTED.md, section 8](GETTING_STARTED.md) for the quick demo and [section 9](GETTING_STARTED.md) to rehearse production on the testnet (your real settings, fake money). Use a **separate database** for real money: testnet trades are stored in the same tables and the tax export does not mark which is which.

## 3. Telegram alerts

Full steps with troubleshooting: [GETTING_STARTED.md, section 7](GETTING_STARTED.md). In short:

- [x] In Telegram, open **@BotFather**, send `/newbot`, pick a name. Copy the **token** into `.env` as `TELEGRAM_BOT_TOKEN`. Treat it like a password and never paste it into a chat or commit.
- [x] Open your new bot, press **Start**, send it any message (a bot cannot message you first).
- [x] Run `python -m bot.services.notifications --find-chat-id`, and put the id it prints in `.env` as `TELEGRAM_CHAT_ID`.
- [x] Check: `python -m bot.services.notifications --test` (or `python -m bot.main --check-only`) should put a message on your phone.
- What you will get: buy placed, protection placed, trade closed, kill-switch, "needs attention", reminders every 15 minutes while a position has no stop-loss (`UNPROTECTED_REMINDER_MINUTES`), and started/stopped. Every message starts with `[TESTNET]`, `[DRY RUN]` or `[LIVE]`.
- **Not verified:** how the trade alerts look on the phone (the `<pre>` tables); only the test message has been seen.

## 4. Decide the entry signal (a blocker for real money)

- [ ] Read the decision at the top of [NEXT_STEPS.md](NEXT_STEPS.md). In a 250-day replay the current RSI + EMA rule won 33% of the time against the 36.7% needed to break even after fees.
- [ ] Choose: run it on the testnet and in dry-run for a few weeks first (my suggestion), and/or ask for a careful parameter study with an out-of-sample test.
- Without this decision, treat everything as a rehearsal.

## 5. Stage C: dry-run on real market data (still no orders)

Uses the real market and your real balance, but the bot refuses to send any order.

- [ ] Create a **real** Binance API key (section 7 below), with **trading permission left off for now** if you want to be extra safe: dry-run only reads.
- [ ] In `.env`: `BINANCE_TESTNET=false`, `DRY_RUN=true`, the real key and secret.
- [ ] Run `python -m bot.main`. It scans, records the entries it *would* make, and times them out after `ENTRY_TIMEOUT_DAYS`. Alerts say `[DRY RUN]`.
- Check: `python -m bot.main --check-only` shows your real USDT balance and your real fee rate (this is where the 0.1% assumption finally gets verified).
- Limit: dry-run simulates **no fills**, so it shows what the scanner would do, not what trades would have earned.

## 6. The server (Stage D): a DigitalOcean droplet

**Planned route: Coolify**, the shared DigitalOcean droplet and `shared-postgres`. Steps and the dashboard checklist: [docs/coolify.md](docs/coolify.md). The steps below are the manual docker compose alternative.

Full runbook, including how to choose a host and a free-tier comparison: [docs/deployment.md](docs/deployment.md). The image and compose file are verified on a laptop for ARM and x86; **the server steps have never been run anywhere**, so expect to adjust. Pick a server **outside the United States** (Binance blocks US IPs) that never sleeps; most free platforms fail one of those two tests.

- [ ] Create an Ubuntu 22.04 droplet, add your SSH key, disable password login, firewall allowing SSH only (the bot only makes outbound connections).
- [ ] Install Docker Engine and the compose plugin.
- [ ] `git clone` the repo, `cp .env.example .env`, `chmod 600 .env`, fill it in (the table in section 9).
- [ ] Run **`./scripts/preflight.sh`** and fix every FAIL line. It checks `.env`, that the database URL is not `localhost`, that Binance accepts this server, shows the IP to allow on your key, and runs the bot's own checks. It never sends an order.
- [ ] Add the droplet's IP to the managed database's trusted sources (section 1) and to the Binance key's IP restriction (section 7).
- [ ] `docker compose up -d --build`, then `docker compose logs -f app`.
- [ ] When you are happy it should run unattended, set `RESTART_POLICY=unless-stopped` in `.env` and run `docker compose up -d` again (no need to edit the compose file).
- [ ] Back up: the database (the provider's automatic backups, plus a `pg_dump` before every upgrade). The `tax_ledger` is your capital-gains record.
- **Not verified:** the Dockerfile and compose file on a real server; the bot running for days; Binance reachable from the droplet's region.

## 7. A real Binance API key (only for stages C to E)

- [ ] Turn on two-factor authentication on the Binance account.
- [ ] In Binance, open API Management and create a key (system-generated HMAC is fine; the testnet's HMAC key opened the account stream).
- [ ] Permissions: enable **spot trading** only. **Leave withdrawals OFF.** Do not enable margin, futures or anything else.
- [ ] Restrict the key to the **droplet's IP address** (and your own IP while testing from your laptop).
- [ ] Copy the secret once into `.env`; Binance never shows it again.
- If the live account stream refuses an HMAC key, create an Ed25519 key and set `BINANCE_PRIVATE_KEY_PATH` (a file outside the repo).
- **Not verified by me:** the exact menu wording on Binance's site, which changes. Follow Binance's own on-screen text for where each permission lives.

## 8. Stage E gate: do not trade real money until all of these are true

- [ ] The entry-signal decision (section 4) is made, and you accept what it says.
- [x] Telegram alerts verified on your phone (section 3): test message only; trade alerts still to be seen.
- [ ] The server ran in dry-run (and/or on the testnet) for a few weeks without surprises, and you read the alerts it sent.
- [ ] A real fee rate and balance appeared in the logs (section 5), and `--check-only` passes with no config warnings.
- [ ] You understand what the bot does when things go wrong: it **never sells on its own**; if your exit orders are cancelled it alerts you and reminds you every 15 minutes; if the price falls through the stop it places an emergency stop below the current price; if you sell by hand it closes the position as `MANUAL`.
- [ ] You know the **kill-switch has never fired on real losses** (only in tests) and the **emergency stop has never run on the real exchange**.
- [ ] You start with an amount you can afford to lose. With about $16, `MAX_OPEN_POSITIONS=1` and `ALLOCATION_PCT=0.98` are the defaults.
- [ ] You asked an accountant how your country wants these trades reported, and whether treating USDT as USD is acceptable.
- Then, and only then: `BINANCE_TESTNET=false`, `DRY_RUN=false`. The bot logs `LIVE MODE` at start and every alert says `[LIVE]`.

## 9. `.env` by stage

| Setting | A: testnet | C/D: dry-run, real data | E: live |
|---|---|---|---|
| `BINANCE_TESTNET` | `true` | `false` | `false` |
| `DRY_RUN` | `true` (or `false` to trade fake money) | `true` | `false` |
| `BINANCE_API_KEY` / `_SECRET` | testnet keys | real key | real key |
| `DATABASE_URL` | local container | managed database | managed database |
| `TELEGRAM_BOT_TOKEN` / `_CHAT_ID` | optional | yes | yes |
| `SYMBOLS` | defaults | defaults | start with the most liquid pairs |
| `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` | 0.04 / 0.02 | same | same, once you have chosen them |
| `DAILY_LOSS_LIMIT_PCT` | 0.03 | same | same |

All other settings and their meaning are in `.env.example`. Never commit `.env`.

## 10. Routine once it is running

- Export your trades: `python -m bot.reports.tax_exporter --year 2026` (see [GETTING_STARTED.md](GETTING_STARTED.md)).
- Read any alert that says "Needs your attention" the same day.
- Check the logs after every restart or upgrade: `docker compose logs --tail 200 app`.
- There is no heartbeat message yet: if the bot stops, you hear the "stopped" message only on a clean shutdown. A crash is silent until you look (a to-do in [NEXT_STEPS.md](NEXT_STEPS.md)).
