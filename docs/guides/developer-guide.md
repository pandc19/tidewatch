# Developer Guide

## Commands

| Task | Command |
|---|---|
| Install | `uv sync --python 3.12` |
| Tests | `.venv/bin/pytest` |
| Lint | `.venv/bin/ruff check .` |
| Apply migrations | `.venv/bin/alembic upgrade head` |
| Run | `.venv/bin/python -m bot.main` |

## Rules that matter

1. **Money is `Decimal`.** Never `float`, in code, tests or the database. Pass Binance values as strings. `to_decimal` refuses floats on purpose.
2. **Every price and quantity goes through [precision.py](../../bot/config/precision.py)** before it reaches Binance: round, then `validate_order`. Quantities round down, never up.
3. **Percentages are fractions** (`0.04` = 4%) everywhere: env vars, settings, columns.
4. **Nothing is hardcoded that the owner may tune**: it is a setting in [settings.py](../../bot/config/settings.py) and a line in `.env.example`.
5. **The app name only lives in `APP_NAME`, `pyproject.toml` and the compose project name.** Don't write it into code, queries or messages; read `settings.app_name`.
6. **Dry-run must never reach Binance's order endpoints.** Check `settings.dry_run` at the lowest layer that sends an order, not in callers.
7. **Never log or print secrets.** Keys are `SecretStr`.

## Changing the schema

1. Edit [models.py](../../bot/database/models.py).
2. `alembic revision --autogenerate -m "what changed"` and **read the result**. Autogenerate duplicates enum CHECK constraints (it emits the `Enum`'s own and the named one); keep the `op.f(...)` ones and set `create_constraint=False` on the `sa.Enum` columns, as in `0001`.
3. `alembic upgrade head`, then `alembic check` (must say "No new upgrade operations detected") and `alembic downgrade -1 && alembic upgrade head`.
4. Add a test in [tests/test_schema.py](../../tests/test_schema.py) for any new constraint.

## Tests

- Unit tests (`test_precision.py`, `test_settings.py`) need nothing.
- Schema tests run against the real migrated PostgreSQL inside a transaction that is rolled back, and skip if no database is reachable.
- Fixtures for Binance filters mirror live `exchangeInfo` responses. When adding one, copy a real response instead of inventing values.

## Adding things

- **A setting:** add the field with validation to `Settings`, add it to `.env.example`, add a case to `tests/test_settings.py`.
- **A new alert channel (planned):** implement the notification interface in `services/notifications.py`. The engine must only talk to the interface.
- **A new strategy (planned):** the scanner returns ranked candidates; keep entry sizing and risk checks outside it.

## Facts checked vs assumed

Checked against Binance's live public API (2026-10-06): filter names and shapes (`PRICE_FILTER`, `LOT_SIZE`, `NOTIONAL`, `PERCENT_PRICE_BY_SIDE`) on mainnet and testnet, OCO allowed on the candidate pairs, $5 minimum notional. **Assumed, not verified:** the account's fee rate (0.1% per side), the user-data stream method. See [NEXT_STEPS.md](../../NEXT_STEPS.md).
