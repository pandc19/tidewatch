# Running modes: what to run where

A reminder for the owner. Three independent settings decide what a run does, and mixing them up is the main way to confuse yourself.

| Setting | Decides | Where it comes from |
|---|---|---|
| `BINANCE_TESTNET` | Which exchange: the testnet (fake money) or the real Binance | `.env` |
| `DRY_RUN` | Whether orders are sent at all | `.env`, or `start demo` (see below) |
| `DATABASE_URL` | Which database records the run | `.env` only |

`BINANCE_TESTNET` never chooses the database. Only `DATABASE_URL` does.

## The modes

| Mode | Command | Exchange | Orders sent | Typical database |
|---|---|---|---|---|
| Dry run | `./scripts/local.sh start` with `DRY_RUN=true` | reads only | none | `tidewatch_local` |
| Demo | `./scripts/local.sh start demo` | testnet, forced | yes (fake money) | `tidewatch_local` |
| Rehearsal | `./scripts/local.sh start` with `DRY_RUN=false`, `BINANCE_TESTNET=true`, `DATABASE_URL` set to `tidewatch_testnet` | testnet | yes (fake money) | `tidewatch_testnet` |
| Live | `BINANCE_TESTNET=false`, `DRY_RUN=false` | real | yes (real money) | a database of its own; not allowed until SETUP_CHECKLIST section 8 is ticked |

- **Demo overrides `.env`.** `start demo` passes `DRY_RUN=false` and `BINANCE_TESTNET=true` to that process, plus fast signals and tiny targets ([scripts/local.sh](../../scripts/local.sh)). `.env` can say `DRY_RUN=true` and a demo still places testnet orders. The first log line shows the real values (`testnet=True dry_run=False`).
- **A dry run does less.** It skips reconciliation, the account stream, live-order polling and manual-sale detection. It records the entries it would make and they time out. It tests the scanner, not fills, the OCO, fees or closing.
- **Orders on the testnet use fake funds.** `DRY_RUN=false` there spends the testnet's fake USDT. The real exchange is never contacted while `BINANCE_TESTNET=true`.

## The guard

Startup refuses a database that disagrees with the exchange, by name only ([bot/services/risk_manager.py](../../bot/services/risk_manager.py)):

- `BINANCE_TESTNET=false` with a database name containing `testnet`: refused.
- `BINANCE_TESTNET=true` with a name containing `prod` or `live`: refused.

It does not look at `DRY_RUN`, and a name like `tidewatch_main` is not caught. Name your real-money database with `prod` or `live`.

## One bot per testnet account

Every database and every machine using the same testnet keys shares one wallet and one set of orders.

- **Two bots that send orders** (both `DRY_RUN=false`) see each other's open orders as **orphans**: on the exchange with the bot's prefix, not in their own database. Reconciliation reports them, sets `trading_blocked`, and `--check-only` fails. The bot never adopts or cancels them.
- **Safe combinations:** one sending bot, plus any number of dry-run bots (they never read the exchange's orders); or two sending bots with different `ORDER_ID_PREFIX` values (not tested).
- **Never point two running bots at the same database.**
- A dry-run bot with an `OPEN` position in its database whose OCO is real on the exchange will never notice the fill: the record goes stale. Close such a position (or cancel its OCO and reset the database) before switching to dry run.

## Starting over

To empty a database and leave nothing behind on the exchange:

1. Stop the bot: `./scripts/local.sh stop`.
2. If a position is open, either let it close (start again and wait), or cancel its OCO on the exchange. Cancelling either leg of an OCO cancels both. Otherwise the orders become orphans the next time you start.
3. `.venv/bin/python scripts/reset_db.py local` (or `testnet`, `test`). It keeps the schema, re-seeds the single `bot_state` row, and refuses a non-local host or a running bot. It asks you to type the database name.
4. Start again.

Cancelling an OCO leaves the coins in the wallet unsold. The bot never sells on its own. The cancel is an exchange call that needs your say-so; there is no script for it in the repo. Cancel by hand, or ask for a one-off script.

## Looking at the data

[queries.sql](queries.sql) holds nine read-only queries (positions, orders, trades with their orders, live orders, tax ledger, daily PnL, kill-switch, pause state) to paste into DBeaver. Run it as a script (Alt+X) so each statement gets its own result tab.

Position statuses: `PENDING_ENTRY`, `OPEN`, `CLOSED`, `CANCELLED`. Exits: `TAKE_PROFIT`, `STOP_LOSS`, `MANUAL`. The full enums are in [bot/database/models.py](../../bot/database/models.py).

## Running on a server for days

Rehearsal mode, on a droplet, with a managed database named for the testnet (for example `tidewatch_testnet`). The server steps in [../deployment.md](../deployment.md) have never been run anywhere; expect to adjust. Before it starts:

- The server's database is clean, and no other bot with the same prefix is sending orders on that testnet account.
- A crash is silent (no heartbeat yet). Decide how you will notice.
- Set `RESTART_POLICY=unless-stopped` only once it runs well.
- The testnet may be reset by Binance from time to time (not checked how often); the bot would reconcile afterwards.
