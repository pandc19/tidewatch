# Getting Started

Local setup from a fresh clone. About five minutes.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for you)
- A PostgreSQL database you can reach with a connection string (here: a container already running on `localhost:5432`; in production, a managed database)

## 1. Install

Creates `.venv` and installs the dependencies, including the dev tools. Python 3.12 is used to match the Docker image (not the system 3.14, which is untested here).

**With uv** (recommended; install it with `brew install uv`):

```bash
uv sync --python 3.12
```

**Without uv** (needs Python 3.11+ and pip 25.1+):

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e . --group dev
```

Either way, run tools as `.venv/bin/pytest`, `.venv/bin/alembic`, and so on. You can also `source .venv/bin/activate` once per terminal and drop the `.venv/bin/` prefix; inside the venv `python` and `python3` are the same interpreter.

## 2. Configure

```bash
cp .env.example .env
```

The defaults are safe: testnet and dry-run are on, and no Binance keys are needed yet. Percentages are fractions (`0.04` = 4%).

## 3. Create the database

Connect as a superuser to your existing Postgres container (here named `postgres18`; use yours) and create a role and database for the bot:

```bash
docker exec postgres18 psql -U postgres \
  -c "CREATE ROLE trading_bot_app LOGIN PASSWORD 'changeme';" \
  -c "CREATE DATABASE trading_bot_local OWNER trading_bot_app;"
```

These match the `DATABASE_URL` in `.env.example`. Change both together if you pick other names.

## 4. Migrate

```bash
.venv/bin/alembic upgrade head
```

## 5. Verify

```bash
.venv/bin/pytest                # 319 tests; schema and reconciliation tests need the database
.venv/bin/python -m bot.main --check-only    # startup checks, then exits
.venv/bin/python -m bot.main                 # startup checks, then the engine runs until Ctrl+C
```

## 6. Binance testnet keys (optional until phase 3)

Public data (filters, prices) needs no keys, so steps 1-5 work without them. To exercise signed calls and the account stream, create testnet keys at https://testnet.binance.vision and put them in `.env` (`BINANCE_API_KEY`, `BINANCE_API_SECRET`). An ordinary HMAC key works for the account stream on the testnet (checked 2026-10-06), even though Binance's docs mention Ed25519. If your account ever refuses it, create an Ed25519 key and set `BINANCE_PRIVATE_KEY_PATH`. With keys set, `python -m bot.main` also logs your fee rate and balance and reconciles stored orders. Keep `DRY_RUN=true` until you want to test with fake money: no order is sent either way, and with keys set the scanner looks for entries; in dry-run it records the intended buy and times it out after `ENTRY_TIMEOUT_DAYS` without sending anything.

## 7. Telegram alerts (optional)

Without this the bot works and alerts are log lines only. To get them on your phone:

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username. BotFather replies with a **token** (looks like `123456789:AA...`). Treat it like a password: anyone with it can send messages as your bot. Never paste it into a chat, a ticket or a commit.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN=...`.
3. Open your new bot in Telegram, press **Start** and send it any message. A bot cannot message you first.
4. Run `.venv/bin/python -m bot.services.notifications --find-chat-id`. It prints the id and name of each chat that recently messaged the bot (never the token). Put your id in `.env` as `TELEGRAM_CHAT_ID=...`. If it finds nothing, send the bot another message and run it again.
5. Run `.venv/bin/python -m bot.services.notifications --test`. A test message should arrive. `python -m bot.main --check-only` sends one too and reports the result in the log.

Every alert starts with `[LIVE]`, `[TESTNET]` or `[DRY RUN]` so a practice alert cannot be mistaken for real money. If Telegram is down the bot keeps trading; failed alerts are logged.

## Running in Docker

```bash
docker compose build
docker compose run --rm \
  -e DATABASE_URL="postgresql+asyncpg://trading_bot_app:changeme@host.docker.internal:5432/trading_bot_local" \
  app
```

The container applies migrations, then starts the bot. Compose never starts a database: PostgreSQL is always external, and `DATABASE_URL` is all the app needs. For production see [docs/deployment.md](docs/deployment.md).

## Export your trades

```bash
.venv/bin/python -m bot.reports.tax_exporter --year 2026     # writes exports/tax_ledger_2026.csv
.venv/bin/python -m bot.reports.tax_exporter                 # every trade
```

One row per closed trade: prices, totals, fees, net profit, timestamps (UTC) and how it ended. The tax year is the UTC year of the *sale*; USDT is treated as USD. This is a record of trades, not tax advice. Ask an accountant how your country wants it reported.

## Common problems

| Symptom | Cause |
|---|---|
| Schema tests are skipped | No database at `DATABASE_URL`. Do step 3 and 4 |
| `role "trading_bot_app" does not exist` | Step 3 was run against a different Postgres than the one on port 5432 |
| `Telegram refused sendMessage: 401` | Wrong `TELEGRAM_BOT_TOKEN` |
| `Telegram refused sendMessage: 400 ... chat not found` | Wrong `TELEGRAM_CHAT_ID`, or you never sent the bot a message (step 3 of section 7) |
| `Bad CPU type in executable` from uv | uv picked an Intel-only Python. Run `uv sync --python 3.12` so uv manages its own |
