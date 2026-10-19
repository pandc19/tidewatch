# Deploying with Coolify

A push to `main` makes Coolify build the [Dockerfile](../Dockerfile) on the droplet and start the new container. The branching rules that feed it are in [git-flow.md](git-flow.md). The droplet (`shared-apps-1`), Traefik and `shared-postgres` are shared with other apps and are created by the `shared-infra` repo; this page covers only what is specific to the bot.

**Status:** the image is built and checked locally, including the container hand-over: two containers on one database, the second waits (healthy) and takes over after `docker stop` or `kill -9` of the first, and a waiter that times out exits 1 (2026-10-19, keyless dry-run, throwaway database). **First Coolify deploy, 2026-10-19:** Coolify accepted the application with no port, built the image from the Dockerfile (about a minute), found the image's health check, waited out the full 120 s start period, and the deployment finished; the bot sent its "started" Telegram message tagged `[TESTNET]` (so `DRY_RUN=false` on the testnet). **Second deploy, same day:** the hand-over worked on Coolify with no position open. The new container logged "another instance is running… waiting for it to stop", waited exactly the 120 s start period, Coolify then stopped the old one (a clean "bot stopped" alert arrived), and the new one took the lock within a second and started ("bot started" alert). The deploy took 3 m 34 s. Not yet seen: a hand-over with an open position, and the configured stop timeout (the clean shutdown only shows it was long enough this time). Items marked *verify* are things I could not check from the repo; confirm each once in the dashboard and tick the checklist at the bottom.

## How Tidewatch differs from a web app

| A typical web app | Tidewatch (bot) |
|---|---|
| Has a port, a domain, TLS | **No port, no domain.** It only makes outbound connections (Binance, Telegram, PostgreSQL) |
| Health check = HTTP `/api/health` | Health check = a heartbeat file the engine touches every tick (`python -m bot.instance`) |
| Two copies running for a moment is harmless | Two engines on one database could both place orders. The instance lock (`bot/instance.py`) makes the new container wait until the old one has stopped |
| Rolling update, then the old container stops | Same, but the new container sits waiting for the lock (still beating, so it counts as healthy) until Coolify stops the old one, then takes over and reconciles |

## Coolify: application settings

| Setting | Value |
|---|---|
| Source | GitHub App, repository `pandc19/tidewatch`, branch `main` |
| Build pack | Dockerfile (`/Dockerfile`) |
| Ports exposed | none. Coolify accepted this (first deploy). It may generate a domain for the app; remove it, nothing listens |
| Domain | none |
| Auto deploy | on (gated by the GitHub ruleset in [git-flow.md](git-flow.md)) |
| Health check | Coolify's own: **off**. The image's `HEALTHCHECK` is used: the deploy log says "Custom healthcheck found in Dockerfile" and the application's health check setting shows it. Coolify waits the whole start period (120 s) before the first check, so a deploy takes at least 2 minutes |
| Stop timeout | **40 s or more.** The bot flushes alerts and closes the Binance stream on SIGTERM, which takes up to about 20 s. Compose sets 40 s; the Dockerfile cannot. *Verify* where Coolify sets it (custom Docker options `--stop-timeout 40`, or its setting) |
| Restart | always/unless-stopped is fine once it runs well; a crash loop is visible in the logs |
| Server | `shared-apps-1` |

### Environment variables

All **runtime** variables. Untick "Available at Buildtime" on the secrets: build-time variables are passed to `docker build` and can be read from image layers. The build needs none of them.

| Variable | Value / notes |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://tidewatch_app:<password>@<shared-postgres internal host>:5432/<database>`. Percent-encode special characters. A provider-style `postgres://…?sslmode=require` is also accepted |
| `BINANCE_TESTNET` | `true` for the rehearsal. `false` only after the gate in [SETUP_CHECKLIST.md](../SETUP_CHECKLIST.md) section 8 |
| `DRY_RUN` | `false` to rehearse with orders on the testnet, `true` to send nothing |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | secrets. Testnet keys for the rehearsal |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | secrets. The alerts say `[TESTNET]`, `[DRY RUN]` or `[LIVE]` |
| `ORDER_ID_PREFIX` | leave the default (`bot`) unless two sending bots share one account (untested) |
| strategy settings | the rest of [.env.example](../.env.example): `SYMBOLS`, `TAKE_PROFIT_PCT`, `STOP_LOSS_PCT`, `ALLOCATION_PCT`, `DAILY_LOSS_LIMIT_PCT`, … |

Keep this table and `.env.example` in sync when a variable is added. The names `HEARTBEAT_*` and `INSTANCE_LOCK_WAIT_SECONDS` have good defaults and need not be set.

## The database

- One database per purpose on `shared-postgres`, role `tidewatch_app` (`NOSUPERUSER NOCREATEDB NOCREATEROLE`, owns only its databases, `CONNECT` revoked from `PUBLIC`, so it cannot open another app's database).
- **Rehearsal:** database `tidewatch_testnet`. **Real money, later:** a different database whose name contains `prod` or `live`. The startup guard refuses `BINANCE_TESTNET=false` with a `testnet` name and `BINANCE_TESTNET=true` with a `prod`/`live` name ([running-modes.md](guides/running-modes.md)).
- The migrations run at every container start (`alembic upgrade head`), and the role needs to create tables.
- **Migrations run while the old container may still be running.** Write them so the previous code can live with them (add columns nullable or with a default; drop or rename in a later PR). A failed migration stops the new container, and the old one keeps running.
- The `tax_ledger` is the capital-gains record. It needs a scheduled backup in Coolify, and a restore tested at least once.

## What a deploy does to a running bot

1. Merge to `main`; Coolify builds the image (about a minute; the image is small).
2. The new container runs `alembic upgrade head`, the startup checks, then **waits for the instance lock**, beating its heartbeat.
3. Coolify sees it healthy and stops the old container (SIGTERM, clean shutdown within the stop timeout). PostgreSQL frees the lock the moment that process ends, even if it is killed.
4. The new container takes the lock, reconciles with Binance, and carries on. Open positions keep their exit orders on the exchange throughout; the bot never sells by itself.
5. If the new container never gets the lock (10 minutes by default) or dies, the deployment fails and the old one keeps running.

Steps 2 to 4 are seen with plain containers and on Coolify (see Status). Each deploy sends a "stopped" and a "started" alert. Coolify waits the whole 120 s start period before its first health check, so the new container waits about two minutes for the lock. Not tested: a hand-over with an open position, and what Coolify does when the stop takes longer than its timeout.

## Binance and the server

- **Region.** Binance refuses US addresses ("Service unavailable from a restricted location"). *Verify* the droplet's region and that Binance answers from it before relying on it: a keyless request to `https://api.binance.com/api/v3/time` from the droplet must return JSON, not an HTTP 451.
- **API key IP restriction.** With real keys, restrict the key to the droplet's reserved IP. The testnet needs no restriction.
- Withdrawals stay off on every key.

## Day to day

- Logs: Coolify's log view for the application (the bot logs to stdout).
- Health: the application's status (healthy/unhealthy) is the heartbeat. Unhealthy means no engine tick for 3 minutes. Nothing alerts you yet ([NEXT_STEPS.md](../NEXT_STEPS.md)).
- Stop the bot cleanly from Coolify (Stop), not by deleting the container.
- Secrets live only in Coolify and the local `.env`.

## Checklist: what only exists in the dashboards

- [x] Coolify: application created from the GitHub App, Dockerfile build pack, no port accepted (2026-10-19)
- [ ] Coolify: all variables set as runtime-only, secrets unticked for build time
- [x] Coolify: the image's health check is read and the first deploy finished (2026-10-19)
- [ ] Coolify: stop timeout 40 s or more
- [ ] Postgres: database and role created, `CONNECT` revoked from `PUBLIC`, a backup schedule, one test restore
- [ ] Binance answers from the droplet (HTTP 200 on `/api/v3/time`)
- [x] A first deploy: it ran and sent its "started" alert (with `DRY_RUN=false` on the testnet, not the dry run first suggested)
- [x] A second deploy over the running container shows the new one waiting, then taking over (2026-10-19, no position open)
- [ ] GitHub: the `main` ruleset and settings from [git-flow.md](git-flow.md), with `check` required
