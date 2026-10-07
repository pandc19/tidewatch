# ADR-005: The engine evaluates state, persists before sending, and does not repair what the owner cancels

## Status
Accepted

## Date
2026-10-06

## Context
A trade spans several asynchronous steps (buy, fill, OCO, fill), each of which can be reported late, twice, out of order, or not at all, and the process can restart between any two. Binance can also lose an answer after accepting an order. The owner trades about $16, so a double buy or an unprotected position matters.

## Decision
- **Evaluate, don't react.** `TradingEngine._evaluate(position)` reads a position's orders and decides the next step (open and protect, cancel, close, retry the exit). Stream events, timeouts, reconnects and the periodic tick only call it. It is idempotent and crash-safe: a restart needs no memory of missed events.
- **Persist before send.** The order row, with our own client order id, is committed before the request. A lost answer is resolved by asking Binance for that client id (after a grace period), so nothing is bought twice. A refusal (`OrderRejected`) means nothing exists; any other failure means unknown, and is resolved by lookup.
- **One lock** serialises every state change, so a stream event, the timeout check and the tick never interleave.
- **A polling safety net.** Every tick re-reads each live order over REST, so a missed stream event (for example only one leg of a cancelled OCO being reported) is corrected within a tick.
- **State graph** `PENDING_ENTRY → OPEN → CLOSED`, `PENDING_ENTRY → CANCELLED`, enforced in code (`transition`). A partly filled buy that times out keeps what filled and becomes `OPEN`.
- **Exit sizing.** Sell the position (net of the coin fee) and, only if the free balance is the position plus less than one quantity step, sweep that crumb too. A bigger surplus is the owner's own holding and is never sold.
- **Take-profit already passed.** If Binance refuses the OCO with -2010, the take-profit is moved just above the best ask and retried once. If it is still refused, the position is flagged and retried every tick.
- **The owner's cancel is respected.** If an OPEN position's exits were cancelled outside the bot, the engine flags it once and does **not** re-place them: it cannot tell a mistake from a deliberate decision, and fighting the owner is worse than asking.
- **Dry run** records the intended entry and times it out; it simulates no fills and never calls an order endpoint.
- **Notifications are hooks** (`EngineObserver`, no-op by default), so phase 5 plugs in Telegram without touching the engine.

## Consequences
- More REST calls than a purely event-driven design (a few per 30 seconds); negligible at one position.
- A position can sit OPEN and unprotected until the owner reacts to the alert (or, for a refused exit, until a retry succeeds). Alerts are phase 5; until then it is only logged at CRITICAL.
- If the market has already fallen below the stop when the OCO is placed, Binance refuses it and the take-profit bump cannot help. A market-sell fallback is still to be designed (NEXT_STEPS).
