# ADR-003: Safe defaults and one position at a time

## Status
Accepted

## Date
2026-10-06

## Context
The account holds about $16. Binance's minimum order value is about $5 on the candidate pairs, so the original 5–10% per-trade cap would give $0.80–$1.60 orders, all rejected. Splitting $16 across positions would also shrink each profit. This will eventually trade real money.

## Decision
- One open position, using `ALLOCATION_PCT` (default 98%) of the free quote balance. `MAX_OPEN_POSITIONS` stays a setting for later.
- Take-profit 4% and stop-loss 2% by default, both env vars. A win nets about +$0.61 and a loss about −$0.35 on $16 at 0.1% fees per side (fee rate assumed, not verified).
- `BINANCE_TESTNET=true` and `DRY_RUN=true` are the defaults.
- The daily-loss limit (default 3%) is validated but not hard-capped, because on a one-position wallet a single 2% stop is most of it.

## Consequences
- One stop-out plus one more loss in 24 hours pauses the bot. An unfilled buy locks the whole wallet until its timeout.
- Percentage sizing and several positions only become practical from roughly $60 (see NEXT_STEPS).
