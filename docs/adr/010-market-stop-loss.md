# ADR-010: The stop-loss leg is a market stop

## Status
Accepted

## Date
2026-11-03

## Context
The OCO's lower leg was a `STOP_LOSS_LIMIT`: at the trigger it placed a limit sell `STOP_LIMIT_OFFSET_PCT` (0.2%) lower. A limit only bounds the loss if it fills. On the testnet, 2026-10-29 20:00 UTC, BNB fell from 743 to 613 within an hour. The stop (trigger 725.55, limit 724.09, entry 740.36) triggered, Binance expired the take-profit, and the limit sell rested above the market for days while the price went as low as 597 (−19% from the entry). The engine has flagged that case since #22, but the owner then has to act by hand.

Options considered:
1. **Market stop (`STOP_LOSS`)**: once triggered it sells at whatever the market pays.
2. **A wider offset** (1–2%): fills through small gaps, still skipped by a large one, and gives up most of the price control a limit was for.
3. **Keep the stop-limit** and rely on the alert: works only while the owner is awake and watching.

## Decision
- New exits use `belowType=STOP_LOSS` with `belowStopPrice` only (no limit price, no time in force); the take-profit stays `LIMIT_MAKER`. Checked 2026-11-03: Binance's `POST /api/v3/orderList/oco` docs list `STOP_LOSS` for `belowType`, and live `exchangeInfo` lists `STOP_LOSS` and `ocoAllowed` for BNB, SOL and BTC on both the testnet and mainnet. Not yet seen: Binance accepting and filling one (it happens on the first testnet exit after the deploy).
- `OrderType` gains `STOP_LOSS`. Its row stores the trigger in both `stop_price` and `target_price` (a market order has no limit; `target_price` is not null).
- `STOP_LIMIT_OFFSET_PCT` is removed. The filter checks and the "can this trade be protected" sizing use the stop price.
- Exits already on the exchange as `STOP_LOSS_LIMIT` are left as they are; the code still handles them, including the triggered-but-not-filling alert. Nothing replaces an order the owner can see without being asked.
- The emergency re-based stop (ADR-006) is unchanged: Binance still refuses a stop at or above the market, and the re-based stop is now a market stop too.

## Consequences
- A triggered stop always sells, so the loss stops growing. It is not bounded at 2%: in a gap it sells at the gap price, and on a thin book it can slip further. On the liquid pairs the 0.2% spread filter admits, normal slippage is small.
- Fees are unchanged: a triggered stop-limit is a taker order too.
- The replay tool already modelled the stop as a market-like sale (filling at the stop, or at the open after a gap), so the parameter study's numbers describe this behaviour better than the old one.
- The bot still never sells except through the OCO it placed (the market stop is part of that OCO).
- A Binance refusal of the new leg type would go through the existing refused-exit path: the position is flagged as unprotected and the exit retried every `EXIT_RETRY_SECONDS`.
