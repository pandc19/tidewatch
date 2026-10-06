# ADR-001: Decimal money and exchange filter rounding

## Status
Accepted

## Date
2026-10-06

## Context
Binance rejects any order whose price or quantity breaks a symbol filter: tick size, step size, minimum order value, price band. The account is about $16, so rejections and rounding losses are a large share of every trade. Float arithmetic adds its own errors (`0.1 + 0.2`).

## Decision
- All prices, quantities, USD amounts and percentages are `Decimal` in code and `NUMERIC(38,18)` in PostgreSQL. `to_decimal` refuses floats.
- [precision.py](../../bot/config/precision.py) parses the symbol's filters once into `SymbolFilters`, rounds (quantities always down), and `validate_order` reports every filter an order would break before anything is sent.
- Both the current (`NOTIONAL`, `PERCENT_PRICE_BY_SIDE`) and legacy (`MIN_NOTIONAL`, `PERCENT_PRICE`) filter names are read. A zero-sized filter is treated as disabled, as on Binance.

## Consequences
- Slightly more verbose code, in return for exact money maths.
- Rounding quantities down means a small leftover of the coin after a sale; the planned sweep (see NEXT_STEPS) absorbs it.
- The price-band check needs Binance's 5-minute average price; without it that check is skipped, so real orders must pass it.
