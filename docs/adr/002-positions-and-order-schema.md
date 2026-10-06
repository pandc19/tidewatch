# ADR-002: Positions table and order schema

## Status
Accepted

## Date
2026-10-06

## Context
The original spec had `orders`, `tax_ledger` and `daily_pnl`. That cannot represent a trade: nothing links a buy to the OCO pair that closes it, an OCO is two orders sharing one list id (so a single unique order id doesn't fit), fills and fees aren't stored, and a kill-switch pause would be forgotten on restart.

## Decision
- `positions`: one row per trade (`PENDING_ENTRY → OPEN → CLOSED`, or `CANCELLED`). A partial unique index allows one live position per symbol.
- `orders`: every order, with `position_id`, our `client_order_id` (unique), nullable unique `binance_order_id` (null in dry-run), `order_list_id` for OCO legs, fill price/quantity, commission, and a stored `cancel_reason` (`ENTRY_TIMEOUT`, `KILL_SWITCH`, `OCO_COUNTERPART_FILLED`, `EXTERNAL`, `OTHER`). A CHECK allows a reason only on cancelled or expired orders.
- OCO legs use order types `LIMIT_MAKER` (take-profit) and `STOP_LOSS_LIMIT`, the types Binance reports, instead of a single `OCO` value.
- `kill_switch_events`: why a trip happened; orders it cancelled reference it.
- `bot_state`: a single row (CHECK `id = 1`) holding the pause, seeded by the migration.
- `tax_ledger` gains `position_id` (unique) and `fees_usd`. `daily_pnl` stays a per-UTC-day summary; the kill-switch itself uses a rolling 24-hour sum of closed positions.
- Enums are strings with CHECK constraints, not native PostgreSQL enums.

## Consequences
- Every close produces exactly one ledger row, traceable to its orders.
- Adding an enum value is a plain migration.
- The unique index on live positions is a database-level guard in addition to the engine's own rule.
