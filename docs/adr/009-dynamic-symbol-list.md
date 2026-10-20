# ADR-009: An optional dynamic symbol list, tiered by risk, off by default

## Status
Accepted

## Date
2026-10-20

## Context
`SYMBOLS` is fixed (BTC, ETH, SOL). With one position at a time, more pairs mean more chances for a signal, and the owner wants the list to follow the market. Picking pairs by recent gains would be wrong for this strategy: the entry is a dip buy (ADR-006), and the pairs with the biggest gains are the ones that already ran up, with the widest spreads and sharpest reversals. Which pair will make money cannot be known; how violently it moves and how costly it is to trade can be measured.

## Decision
- **Off by default.** `SYMBOLS_AUTO=false`. Turning it on (and restarting) makes the scanner use the dynamic list; `SYMBOLS` stays the startup check and the fallback.
- **Rank by liquidity, not gains.** Pairs of `QUOTE_ASSET` with at least `SYMBOLS_AUTO_MIN_VOLUME` of 24h traded value and a spread within `MAX_SPREAD_PCT`, most traded first. Stablecoins, fiat, gold-backed and leveraged tokens are excluded by base name.
- **A risk tier from the last 24 hourly candles**: swing = (highest close − lowest close) / lowest close. Under 0.5% is flat (stablecoin-like: dropped), up to 2.5% `low`, up to 6% `medium`, above that `high`. `SYMBOLS_AUTO_MAX_RISK` (default `medium`) is the highest tier kept. The swing comes from candles, not the 24h ticker's high/low: on the testnet the ticker range is nonsense (BTC 25%, checked 2026-10-20) while its candles match mainnet.
- **Only tradable pairs**: each pick passes `symbol_filters` (status TRADING) and must allow OCO.
- **Refreshed every `SYMBOLS_AUTO_REFRESH_MINUTES`** (default 60) by the scanner, using public endpoints only. A failed or empty refresh keeps the previous list, or `SYMBOLS` before the first success. Positions already open are not affected: they live in the database, not in the list.

## Consequences
- The tier is a risk label, not a forecast. A `low` pair can still lose; a `high` one can win. The signal still has no demonstrated edge, so more pairs mean more trades, not better ones.
- With a 2% stop-loss and a 4% take-profit, `high` pairs (over 6% swing a day) get stopped out by noise; that is why they are off by default.
- The list changes over time, so results are harder to compare run to run. The log line `universe: ...` records each selection with its tiers.
- Thresholds (0.5%, 2.5%, 6%) are chosen by judgement, not fitted to data.
- The tickers call returns every pair (about 3,700 rows) once per refresh, plus up to three candle lookups per wanted pair.
