# Tidewatch

Automated Binance spot trader. It scans a list of USDT pairs, places one limit buy, protects the fill with an OCO exit (take-profit + stop-loss), and records every closed trade in a capital-gains ledger. Built for a small account (about $16 to start), one position at a time.

> **Status: phase 6 of 6 (deployment): deployed on the Binance testnet through Coolify since 2026-10-19 (fake money, `DRY_RUN=false`), monitored for a few days. No real money.** The Binance client, account stream, reconciliation, trading engine, entry scanner (RSI + EMA), spread filter and kill-switch exist and are tested against a fake exchange and the Binance testnet. **The entry signal is a placeholder: replayed on 250 days of history it did not beat the fees (ADR-006). Don't trade real money with it yet.** The CSV tax export is built (`python -m bot.reports.tax_exporter`); Telegram alerts are built (set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, see [GETTING_STARTED.md](GETTING_STARTED.md); without them alerts are log lines only), and a test message was verified on real Telegram (trade alerts and their layout still to be seen on a testnet run). Deployment: the Dockerfile, CI and git flow are built and the bot runs on Coolify ([docs/coolify.md](docs/coolify.md)); real money stays gated by [SETUP_CHECKLIST.md](SETUP_CHECKLIST.md) section 8. `python -m bot.main` runs the startup checks and then the engine until Ctrl+C; `--check-only` stops after the checks.

The app name (Tidewatch) lives in `APP_NAME` in `bot/config/settings.py`, `pyproject.toml` and the compose project name.

## How a trade works (target design)

```
scan SYMBOLS ──> limit BUY ──fills──> OCO SELL (take-profit + stop-loss) ──fills──> ledger row
                    │                                                                  │
                    └─ not filled in ENTRY_TIMEOUT_DAYS: cancel, record why, rescan    └─ Telegram alert
```

Guardrails: spread filter, one position at a time, and a kill-switch that pauses new buys for 24 hours after a 3% realised loss in a rolling 24 hours (it never sells an open position). Details in [docs/guides/code-flow.md](docs/guides/code-flow.md).

## Safety defaults

`BINANCE_TESTNET=true` and `DRY_RUN=true` out of the box. A fresh checkout cannot send a real order. Going live means changing both on purpose.

## Project structure

```
bot/
├── config/
│   ├── settings.py       Environment variables, validated, Decimal percentages
│   └── precision.py      Tick/step rounding, min-notional and price-band checks
├── database/
│   ├── connection.py     Async PostgreSQL pool
│   └── models.py         ORM schema (positions, orders, tax_ledger, daily_pnl, ...)
├── services/
│   ├── binance_client.py     REST wrapper: filters, orders, OCO, fills (dry-run safe)
│   ├── user_stream.py        Account stream with reconnect
│   ├── reconciliation.py     Sync stored orders with Binance, label cancel reasons
│   │   ├── risk_manager.py       Entry sizing, spread filter, startup checks, kill-switch
│   ├── scanner.py            RSI-oversold + EMA cross-up entry signal
│   ├── indicators.py         RSI and EMA on Decimal closes
│   └── notifications.py      Telegram alerts: queue, sender, message text
├── engine/
│   ├── state_machine.py      Position state machine and trading engine
│   ├── runner.py             Stream + tick loop, clean shutdown
│   └── ledger.py             Profit/loss of a closed trade
├── reports/
│   └── tax_exporter.py   Export the tax ledger to CSV (per year, by Ecuador days)
└── main.py               Entrypoint
migrations/               Alembic migrations
scripts/local.sh          Run the bot on this machine in the background (start, stop, status, logs; `start demo` for a fast testnet tour)
scripts/preflight.sh      Pre-flight checks to run on a server (never sends an order)
tests/                    pytest (unit + schema tests against PostgreSQL)
docs/                     Guides, deployment, ADRs
```

## Documentation

- [GETTING_STARTED.md](GETTING_STARTED.md): run it locally in a few minutes
- [SETUP_CHECKLIST.md](SETUP_CHECKLIST.md): everything only you can set up (keys, Telegram, database, server) and the gate before real money
- [docs/README.md](docs/README.md): index of all docs
- [NEXT_STEPS.md](NEXT_STEPS.md): what is still pending

## License

Private project. No license granted.
