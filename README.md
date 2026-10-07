# trading-bot

Automated Binance spot trader. It scans a list of USDT pairs, places one limit buy, protects the fill with an OCO exit (take-profit + stop-loss), and records every closed trade in a capital-gains ledger. Built for a small account (about $16 to start), one position at a time.

> **Status: phase 4 of 6 (risk and scanner).** The Binance client, account stream, reconciliation, trading engine, entry scanner (RSI + EMA), spread filter and kill-switch exist and are tested against a fake exchange and the Binance testnet. **The entry signal is a placeholder: replayed on 250 days of history it did not beat the fees (ADR-006). Don't trade real money with it yet.** Planned: Telegram alerts and CSV export (phase 5), droplet deployment (phase 6). `python -m bot.main` runs the startup checks and then the engine until Ctrl+C; `--check-only` stops after the checks.

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
│   │   ├── risk_manager.py       Entry sizing, spread filter, startup checks, kill-switch
│   ├── scanner.py            RSI-oversold + EMA cross-up entry signal
│   ├── indicators.py         RSI and EMA on Decimal closes
│   └── notifications                                              (planned)
├── engine/
│   ├── state_machine.py      Position state machine and trading engine
│   ├── runner.py             Stream + tick loop, clean shutdown
│   └── ledger.py             Profit/loss of a closed trade
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
