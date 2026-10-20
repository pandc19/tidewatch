# ADR-007: Telegram alerts are queued, sent as HTML, and never carry the token

## Status
Accepted

## Date
2026-10-06

## Context
The owner needs to hear about a fill, a protected position, a closed trade, a kill-switch trip and an unprotected position without watching logs. The engine calls its observer hooks while holding its lock (ADR-005), so anything slow in a hook stalls trade handling. Telegram can be slow, rate-limit (429), or be down, and the bot token is part of every request URL.

## Decision
- **Queue, then deliver.** `NotificationService` (an `EngineObserver`) formats the message inside the hook and puts it on a bounded queue (100) without awaiting anything; a background worker sends from the queue. If the queue is full the oldest message is dropped and a warning is logged. A hook never raises: a message that cannot be formatted is replaced by a short fallback, a failed delivery is logged. `close()` flushes for at most 10 seconds, then gives up and says how many were not sent.
- **Delivery rules** (`TelegramNotifier`): 10 s timeout; network errors and 5xx are retried with backoff (4 attempts); a 429 waits Telegram's `retry_after`, capped at 30 s; other client errors (bad token, bad chat id) are not retried and say why; sends are spaced about one second apart.
- **HTML, not Markdown.** Telegram does not render Markdown tables. Messages use `parse_mode=HTML`, tables go in `<pre>` blocks with aligned columns, and every dynamic value is HTML-escaped. Free text (attention and reminder messages) is cut before escaping so a message stays under about 4000 characters without splitting a tag or entity.
- **Every message names the mode**, `[LIVE]`, `[TESTNET]` or `[DRY RUN]` (dry run wins), so practice alerts cannot be mistaken for real money.
- **Token hygiene.** The token is a `SecretStr`, read only where the URL is built. Errors raised by the notifier carry the exception class and Telegram's own description with the token scrubbed, never aiohttp's text (which can contain the URL), and are raised without the chained exception. Unknown errors in the worker log only their class name.
- **Telegram is optional.** Without a token and chat id the engine keeps the default no-op observer and the bot logs one warning that alerts go to the log only.
- **Notifications only.** The alert path sends and never reads. Read-only commands are a separate task with their own rules: ADR-008.

## Consequences
- Alerts are best effort. A message can be dropped (full queue), lost on shutdown (flush limit), or fail after retries; the log still has the CRITICAL lines for the serious cases.
- Order is preserved, but a long outage delays later messages behind the retries of an earlier one.
- Chat ids are found with `python -m bot.services.notifications --find-chat-id`; it uses `getUpdates`, which only shows people who messaged the bot recently.
- Not verified against real Telegram when this was written: the tests use a local server that imitates the Bot API.
