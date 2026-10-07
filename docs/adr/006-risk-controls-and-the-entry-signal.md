# ADR-006: Risk controls, the owner's manual actions, and an honest entry signal

## Status
Accepted

## Date
2026-10-06

## Context
Phase 4 adds the parts that decide when to trade and when to stop. The owner trades about $16 and also trades by hand, so the bot must tolerate their manual actions without fighting them, and must never be able to make a bad day worse.

## Decision
- **Kill-switch.** Realised PnL of positions closed in the last 24 hours (gains offset losses). A net loss of at least `DAILY_LOSS_LIMIT_PCT` of the account trips it: cancel unfilled entry buys (reason `KILL_SWITCH`, linked to a `kill_switch_events` row), pause new entries for `KILL_SWITCH_COOLDOWN_HOURS` in `bot_state` (so a restart cannot forget), alert. It never sells. After a trip only *newer* losses count, so a short pause cannot re-trip on the same ones. The account is the quote balance (free + locked) plus open positions at entry cost.
- **Unprotected positions: notify, never sell.** If exits are cancelled outside the bot, flag once and do not re-place them (it cannot tell a mistake from a decision). If Binance refuses the stop-loss because the price already fell past it, place the OCO anyway with the stop `STOP_LOSS_PCT` below the *current* bid (take-profit unchanged) and tell the owner; this bounds the loss instead of leaving the coins bare. No market-sell fallback.
- **Manual sales.** If an OPEN position has no live or filled exit, the account's trade history is checked for sells that none of our orders explain. A sale covering the position closes it as `MANUAL`, booking only the position's quantity (not coins the owner also sold) from the real trade prices and fees. A partial sale is flagged, not guessed at.
- **Refused-exit retries are throttled** (`EXIT_RETRY_SECONDS`) and reuse the same order rows, so a persistent refusal cannot pile up thousands of rows.
- **Entry signal** (`scanner.py`): on closed candles, RSI was at or below `RSI_OVERSOLD` within the last `RSI_LOOKBACK` candles and the latest close crossed up through the EMA. Of the pairs that signal and pass the spread filter, the lowest RSI wins. The entry is a limit buy at the best bid.
- **Startup checks refuse a strategy that cannot work** (a profit target at or under the ~0.2% round-trip fee, or a wallet too small for any pair to be protected) and warn about a stop larger than the target.

## Consequences
- **The signal is a placeholder, not a validated strategy.** Replayed on about 250 days of hourly mainnet candles for five pairs, 190 signals reached the 4% target before the 2% stop only 33% of the time; break-even after fees is 36.7%, for an expected −0.24% per trade before slippage (not statistically conclusive at this sample size, but no evidence of an edge). Do not trade real money on it until it has been examined (NEXT_STEPS).
- A re-based stop can lock in a larger loss than planned (about −5.6% in the worked example) in exchange for a bounded one.
- Reminders for an unprotected position and the Telegram messages themselves arrive with notifications (phase 5); until then the alerts are CRITICAL log lines.
