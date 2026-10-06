# Getting Started

Local setup from a fresh clone. About five minutes.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for you)
- A PostgreSQL database you can reach with a connection string (here: a container already running on `localhost:5432`; in production, a managed database)

## 1. Install

```bash
uv sync --python 3.12
```

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
.venv/bin/pytest                # 59 tests; the schema tests need the database
.venv/bin/python -m bot.main    # logs "database reachable", then exits (phase 1)
```

## Running in Docker

```bash
docker compose build
docker compose run --rm \
  -e DATABASE_URL="postgresql+asyncpg://trading_bot_app:changeme@host.docker.internal:5432/trading_bot_local" \
  app
```

The container applies migrations, then starts the bot. Compose never starts a database: PostgreSQL is always external, and `DATABASE_URL` is all the app needs. For production see [docs/deployment.md](docs/deployment.md).

## Common problems

| Symptom | Cause |
|---|---|
| Schema tests are skipped | No database at `DATABASE_URL`. Do step 3 and 4 |
| `role "trading_bot_app" does not exist` | Step 3 was run against a different Postgres than the one on port 5432 |
| `Bad CPU type in executable` from uv | uv picked an Intel-only Python. Run `uv sync --python 3.12` so uv manages its own |
