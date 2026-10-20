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

Either way, run tools as `.venv/bin/pytest`, `.venv/bin/alembic`, and so on. Or activate the virtualenv once per terminal and drop the `.venv/bin/` prefix:

```bash
source .venv/bin/activate    # prompt shows (tidewatch) or (.venv); `deactivate` leaves it
```

Inside the venv `python` and `python3` are the same interpreter. The commands below keep the `.venv/bin/` prefix so they work either way.

## 2. Configure

```bash
cp .env.example .env
```

The defaults are safe: testnet and dry-run are on, and no Binance keys are needed yet. Percentages are fractions (`0.04` = 4%).

## 3. Create the databases

Connect as a superuser to your existing Postgres container (here named `postgres18`; use yours) and create a role and three databases:

```bash
docker exec postgres18 psql -U postgres \
  -c "CREATE ROLE tidewatch_app LOGIN PASSWORD 'changeme';" \
  -c "CREATE DATABASE tidewatch_local OWNER tidewatch_app;" \
  -c "CREATE DATABASE tidewatch_testnet OWNER tidewatch_app;" \
  -c "CREATE DATABASE tidewatch_test OWNER tidewatch_app;"
```

| Database | Used for | Setting |
|---|---|---|
| `tidewatch_local` | running the bot on this machine, demo mode | `DATABASE_URL` |
| `tidewatch_testnet` | the long testnet rehearsal (section 9), kept apart from demo data | `DATABASE_URL` when you rehearse |
| `tidewatch_test` | `pytest` only: throwaway, and what CI will use | `TEST_DATABASE_URL` |

`tidewatch_local` and `tidewatch_test` match `.env.example`. Change the names and `.env` together if you pick others. `pytest` refuses to run if `TEST_DATABASE_URL` is the same as `DATABASE_URL`.

## 4. Migrate

```bash
.venv/bin/alembic upgrade head                                             # tidewatch_local
DATABASE_URL="<your TEST_DATABASE_URL>" .venv/bin/alembic upgrade head     # tidewatch_test
```

Do the same for `tidewatch_testnet` before the rehearsal. Run it again for each database after a new migration.

To start a database over without dropping it, `.venv/bin/python scripts/reset_db.py local` (or `testnet`, `test`) deletes every row and keeps the schema. It is for this machine only: it refuses a non-local `DATABASE_URL` or a running bot, and asks you to type the database name. It does not touch the exchange: cancel any open testnet orders yourself first, or they are left orphaned.

## 5. Verify

```bash
.venv/bin/pytest                # 319 tests; the database tests use TEST_DATABASE_URL and skip without it
.venv/bin/python -m bot.main --check-only    # startup checks, then exits
.venv/bin/python -m bot.main                 # startup checks, then the engine runs until Ctrl+C
```

## 6. Binance testnet keys (optional until phase 3)

Public data (filters, prices) needs no keys, so steps 1-5 work without them. To exercise signed calls and the account stream, create testnet keys at https://testnet.binance.vision and put them in `.env` (`BINANCE_API_KEY`, `BINANCE_API_SECRET`). An ordinary HMAC key works for the account stream on the testnet (checked 2026-10-06), even though Binance's docs mention Ed25519. If your account ever refuses it, create an Ed25519 key and set `BINANCE_PRIVATE_KEY_PATH`. With keys set, `python -m bot.main` also logs your fee rate and balance and reconciles stored orders. Keep `DRY_RUN=true` until you want to test with fake money: no order is sent either way, and with keys set the scanner looks for entries; in dry-run it records the intended buy and times it out after `ENTRY_TIMEOUT_DAYS` without sending anything.

## 7. Telegram alerts (optional)

Without this the bot works and alerts are log lines only. To get them on your phone:

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name (**Tidewatch**) and a username (`tidewatch_japc_bot`; it must end in `bot` and be unique, so use your own if it is taken). BotFather replies with a **token** (looks like `123456789:AA...`). Treat it like a password: anyone with it can send messages as your bot. Never paste it into a chat, a ticket or a commit.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN=...`.
3. Open your new bot in Telegram, press **Start** and send it any message. A bot cannot message you first.
4. Run `.venv/bin/python -m bot.services.notifications --find-chat-id`. It prints the id and name of each chat that recently messaged the bot (never the token). Put your id in `.env` as `TELEGRAM_CHAT_ID=...`. If it finds nothing, send the bot another message and run it again.
5. Run `.venv/bin/python -m bot.services.notifications --test`. A test message should arrive. `python -m bot.main --check-only` sends one too and reports the result in the log.

Every alert starts with `[LIVE]`, `[TESTNET]` or `[DRY RUN]` so a practice alert cannot be mistaken for real money. If Telegram is down the bot keeps trading; failed alerts are logged.

**Commands (optional).** Set `TELEGRAM_COMMANDS=true` and restart; then `/status` in the bot's chat replies with the mode, engine health, the open position and results, and `/help` lists the commands. They are read-only and only the chat in `TELEGRAM_CHAT_ID` is answered. Only one running bot may have this on per token (a laptop and a server sharing one token would fight; Telegram answers 409), and `--find-chat-id` cannot run while a bot is polling. See ADR-008.

## 8. Run the whole bot on your machine, every feature on

Everything runs locally: the scanner, the trading engine, Telegram alerts, the kill-switch, the exports. You need sections 1 to 5 done (and section 7 for Telegram alerts). One script manages it:

```bash
./scripts/local.sh start demo    # TESTNET ONLY. See the whole cycle in minutes
./scripts/local.sh start         # use .env exactly as it is
./scripts/local.sh status        # running? mode? last log lines
./scripts/local.sh logs          # follow the log (Ctrl+C stops following, not the bot)
./scripts/local.sh stop          # clean shutdown
```

It runs the startup checks first and refuses to start if they fail. It keeps the Mac awake while the bot runs (a sleeping bot is a stopped bot); closing the lid still sleeps the Mac unless it is on power with an external display. Logs go to `logs/bot.log` (gitignored, rotated at 10 MB).

**Demo mode** forces the Binance **testnet** (fake money; it can never reach your real account) and changes only what is needed to make things happen fast: 1-minute candles, a loose RSI threshold so signals are frequent, a 0.3% take-profit and 0.25% stop-loss so trades close within minutes to hours, a tiny trade size (about $20 of fake money), and a 5-second tick. Each change is a normal setting, listed at the top of `scripts/local.sh`. You can add your own on the command line, for example `DAILY_LOSS_LIMIT_PCT=0.00001 ./scripts/local.sh start demo` makes the kill-switch trip on the first small loss so you can watch it.

What to look for, and where:

| Feature | What you see |
|---|---|
| Scanner | `entry signal: SOLUSDT RSI 31.2 ...` in the log |
| Entry, fill, protection | the buy, the fill, and the take-profit + stop-loss pair (log, and Telegram if configured) |
| Close and ledger | `trade closed ...`, then a row in the tax export |
| Alerts | Telegram messages tagged `[TESTNET]` (or `[DRY RUN]`) |
| Unprotected position | cancel the take-profit/stop-loss pair yourself (I tested this through the API) and you get "Needs your attention", then reminders |
| Manual sale | sell the coins yourself, outside the bot (tested through the API), and the position closes as `MANUAL` |
| Kill-switch | start with the tiny `DAILY_LOSS_LIMIT_PCT` above and wait for a losing trade |
| Tax export | `python -m bot.reports.tax_exporter --year 2026` |

**Keep test data and real data apart.** Testnet trades are stored in the same tables as real ones, and the tax export does not mark which is which. Use a **separate database** (a different `DATABASE_URL`) for anything real, so a CSV for your accountant can never contain fake-money trades.

## 9. Rehearse production on the testnet

Demo mode (section 8) is a quick tour, **not** a production run. To run the bot the way it would run for real, with only the money fake, use your real settings and the testnet, with `DRY_RUN=false` so real testnet orders are placed:

```bash
# .env (use a database of its own, see below)
BINANCE_TESTNET=true
DRY_RUN=false
BINANCE_API_KEY=...            # testnet keys
BINANCE_API_SECRET=...
DATABASE_URL=...               # a database used only for the rehearsal
TELEGRAM_BOT_TOKEN=...         # so you see the alerts you would see for real
TELEGRAM_CHAT_ID=...
ALLOCATION_PCT=0.0016          # the testnet wallet is 10,000 USDT: this makes each trade about $16
DAILY_LOSS_LIMIT_PCT=0.00005   # 0.005% of 10,000 is about $0.50, the same share as 3% of a $16 wallet
# leave everything else at its default: 4% take-profit, 2% stop-loss, 1-hour candles, RSI 30
```

then `./scripts/local.sh start`. The two scaling lines are what make it behave like your real $16 account: the same trade size, and a kill-switch that trips at the same *proportion* of the account.

Expect it to be **slow**. With the real settings the scanner signalled about once every day or two in a 250-day replay on real prices, and a trade then needs a 4% rise or a 2% fall to close, which takes days. Leave it running for a few weeks and read the alerts.

**What the testnet cannot tell you** (so a good rehearsal is still not proof of a good production run):

| Testnet | Real Binance |
|---|---|
| Trading fees were **0** until you set 0.1% maker and taker in the testnet settings (2026-10-19); a demo buy then paid 0.1% in the coin bought (ETH) | About 0.1% per side. Real results will be roughly 0.2% per trade worse, and the coin fee that shrinks the quantity you can sell is never exercised |
| Prices and liquidity roughly follow the real market but are not it | Real spreads, slippage and partial fills |
| An account of 10,000 fake USDT, scaled down by the two lines above | A real wallet of about $16 |
| A stop-loss or take-profit filling is rare to watch | The behaviour of the other OCO leg after a fill is unverified (see NEXT_STEPS.md) |
| Your own machine | A server: firewall, region, restarts, a managed database |

**Keep it apart from real data.** The rehearsal's trades go to the same tables as real ones, and the tax export does not mark which is which. Use a database of its own: `tidewatch_testnet`, created in section 3 (run `alembic upgrade head` against it, and point `DATABASE_URL` at it), and use a different one for real money later.

**For a closer match to a server**, run it in Docker instead: set `RESTART_POLICY=unless-stopped` and the database URL as the container sees it (`host.docker.internal` for Postgres on this Mac), then `docker compose up -d --build` and `docker compose logs -f app`. `./scripts/preflight.sh` checks that setup first. This is the same image and compose file a server would use.

## Running in Docker

```bash
docker compose build
docker compose run --rm \
  -e DATABASE_URL="postgresql+asyncpg://tidewatch_app:changeme@host.docker.internal:5432/tidewatch_local" \
  app
```

The container applies migrations, then starts the bot. Compose never starts a database: PostgreSQL is always external, and `DATABASE_URL` is all the app needs. For a server, see [docs/deployment.md](docs/deployment.md) and run `./scripts/preflight.sh` first.

## Export your trades

```bash
.venv/bin/python -m bot.reports.tax_exporter --year 2026     # writes exports/tax_ledger_2026.csv
.venv/bin/python -m bot.reports.tax_exporter                 # every trade
```

One row per closed trade: prices, totals, fees, net profit, timestamps (UTC) and how it ended.

**The sales sheet.** The same trades in your spreadsheet's layout, `Venta` rows only, ready to paste:

```bash
.venv/bin/python -m bot.reports.sales_sheet --year 2026 --month 10        # prints the rows
.venv/bin/python -m bot.reports.sales_sheet --year 2026 --out ventas.txt  # or a file; add --header for the title row
```

Tab-separated, Spanish format (`0,128`, `$118,2000`, `$15,55`), the date is the day in Ecuador (`REPORT_TIMEZONE`), and there are no fees: `Monto Directo` is quantity x sell price and `Monto` is the gross profit, quantity x (sell price - buy price). The CSV above keeps the fees and the net profit, so the two differ by the fees. From Telegram (with `TELEGRAM_COMMANDS=true`): `/csv [year|all]` and `/report [year|yyyy-mm]` send these as files. The tax year is the UTC year of the *sale*; USDT is treated as USD. This is a record of trades, not tax advice. Ask an accountant how your country wants it reported.

## Common problems

| Symptom | Cause |
|---|---|
| Database tests are skipped | `TEST_DATABASE_URL` is unset or unreachable. Do steps 3 and 4 |
| `role "tidewatch_app" does not exist` | Step 3 was run against a different Postgres than the one on port 5432 |
| `Telegram refused sendMessage: 401` | Wrong `TELEGRAM_BOT_TOKEN` |
| `Telegram refused sendMessage: 400 ... chat not found` | Wrong `TELEGRAM_CHAT_ID`, or you never sent the bot a message (step 3 of section 7) |
| `Bad CPU type in executable` from uv | uv picked an Intel-only Python. Run `uv sync --python 3.12` so uv manages its own |
