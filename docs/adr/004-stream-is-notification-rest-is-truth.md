# ADR-004: The account stream is a notification, REST is the truth

## Status
Accepted

## Date
2026-10-06

## Context
The spec asks for real-time fills over the user-data WebSocket instead of polling. Binance removed the old listenKey stream on 2026-02-20; the replacement is a WebSocket API subscription (`userDataStream.subscribe.signature`) that Binance documents as needing Ed25519 keys. python-binance 1.0.37 already uses it. A WebSocket only reports events while connected, and the exact event shapes could not be verified without API keys.

## Decision
- An `executionReport` only tells the bot *which* order changed (`parse_event` reads symbol, order id, client id, status). The handler then re-reads that order and its fills over REST (`reconciliation.sync_order`) and writes the result. A lost, duplicated or odd event cannot corrupt state.
- On every (re)connect, after subscribing, the stream runs a full `reconcile()`, so a gap is always closed. The same function runs at startup.
- Reconnects use exponential backoff that resets only after the connection stayed healthy for 30 seconds, so a flapping socket cannot cause a reconcile every second.
- Reconciliation records facts only and returns `OrderChange`s; the engine decides what to do. It reports failures and unknown orders instead of guessing, and labels cancellations: `EXTERNAL` by default, the engine's reason when the bot cancelled, `OCO_COUNTERPART_FILLED` when the other OCO leg filled.
- Every order is validated against the symbol filters, and `dry_run` is enforced, inside `ExchangeClient`, the one module that talks to Binance.

## Consequences
- A few extra REST calls per order event. Fine for one position at a time.
- Correctness does not depend on the event payload; only on the REST order and trade endpoints.
- Commission is stored per order in one asset. If an order paid fees in two assets, one is kept and an error is logged; an `order_fills` table is the proper fix (NEXT_STEPS).
- Checked on the testnet (2026-10-06): an HMAC key opens the stream, and real order events match what `parse_event` reads. Not yet seen live: an OCO leg filling, fee amounts, partial fills.
