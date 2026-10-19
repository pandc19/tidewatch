# ADR-008: Telegram commands are read-only, owner-only, and opt-in

## Status
Accepted

## Date
2026-10-20

## Context
The owner wants to ask the bot what it is doing from the phone (`/status`), and later perhaps to control it. ADR-007 made Telegram one-way: the engine only sends. Receiving adds an inbound path into a process that holds money logic, behind a bot token that anyone can message. Telegram allows only one `getUpdates` poller per token, and a deploy briefly runs two containers.

## Decision
- **Read-only first.** `/status` and `/help` only. A handler reads the database and public or account data and returns text; it never writes, never places or cancels an order, and never takes the engine's lock. Any command that changes state (`/pause`, cancel, sell) needs its own ADR first.
- **Owner only.** A message is answered only if its chat id equals `TELEGRAM_CHAT_ID` and the sender is not a bot. Everything else is ignored silently and logged once per chat id, without its text.
- **No stale commands.** At start the listener confirms the backlog (`getUpdates` with `offset=-1`) without acting on it, and retries until that works. A command sent before a restart never runs after it. This matters most for the state-changing commands a later ADR may add.
- **Separate from the engine.** A long-polling task (25 s) with its own HTTP session, so it cannot delay alerts or trades. It stops at once when the bot stops.
- **Started after the instance lock.** Two processes polling one token get `409 Conflict`; during a deploy the new container waits for the lock, so only the holder polls. A 409 is logged with a hint and retried with backoff.
- **Opt-in.** `TELEGRAM_COMMANDS=false` by default. A laptop and a server sharing a token must not both poll; only the one with it set answers.
- **Replies use the alert queue** (`NotificationService.notify`), so they obey ADR-007's pacing, size and token rules. Errors carry no token and no raw aiohttp text; unknown errors log their class only.

## Consequences
- `/status` shows only what the process can know: mode, uptime, heartbeat age, stream on/off, whether entries are blocked or paused, free balance, the open position with its exits and the best bid, and results for 24 hours and all time. It does not show the last scan or why no entry has fired.
- `python -m bot.services.notifications --find-chat-id` also uses `getUpdates` and conflicts with a running poller: run it while commands are off.
- A reply can be delayed behind queued alerts.
- Anyone who gets into the owner's Telegram account can read the status. Nothing sensitive is in it (no keys, no URLs), but positions and balances are.
- Not verified against real Telegram when this was written: the tests use a local server that imitates the Bot API.
