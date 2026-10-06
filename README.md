# trading-bot

Automated Binance spot trader. It scans a list of USDT pairs, places one limit buy, protects the fill with an OCO exit (take-profit + stop-loss), and records every closed trade in a capital-gains ledger. Built for a small account (about $16 to start), one position at a time.

> **Status: phase 2 of 6 (exchange layer).** Settings, database schema and migrations, the precision module, the Binance REST client, the account stream and order reconciliation exist and are tested against a fake exchange and live public endpoints. Not yet exercised with API keys (signed calls and the live account stream are unverified). The trading engine, scanner, risk manager, notifications and tax export are planned. `python -m bot.main` runs startup checks and exits.

The app name is a setting (`APP_NAME`), so the project can be renamed later without touching code.

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
│   └── scanner, risk_manager, notifications                       (planned)
├── engine/               state_machine                                          (planned)
├── reports/              tax_exporter                                           (planned)
└── main.py               Entrypoint
migrations/               Alembic migrations
tests/                    pytest (unit + schema tests against PostgreSQL)
docs/                     Guides, deployment, ADRs
```

## Documentation

- [GETTING_STARTED.md](GETTING_STARTED.md): run it locally in a few minutes
- [docs/README.md](docs/README.md): index of all docs
- [NEXT_STEPS.md](NEXT_STEPS.md): what is still pending

## License

Private project. No license granted.
