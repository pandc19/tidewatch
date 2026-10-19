# Deployment (manual: docker compose on a server)

> The planned route is Coolify, like poketrain: see [coolify.md](coolify.md) and [git-flow.md](git-flow.md). This page is the manual alternative, and its host comparison still applies.

A runbook for running the bot on a server, unattended. **You do not have to deploy yet**: [SETUP_CHECKLIST.md](../SETUP_CHECKLIST.md) says when it makes sense.

## What is verified, and what is not

| Verified (on a laptop, 2026-10-07) | **Not verified** |
|---|---|
| The image builds for ARM and for x86 (amd64), and runs the bot's startup checks on both | Anything on a real server: firewall, Docker install, the unit staying up for days |
| `docker stop` shuts the bot down cleanly (exit 0, about a second); before the fix it was killed after the grace period | A managed database or a Postgres on the server |
| The bot runs as a non-root user; logs go to stdout and are rotated; the restart policy comes from `.env` | Binance accepting your specific server's IP (check it with `scripts/preflight.sh`) |
| `scripts/preflight.sh` on the real project configuration | The pre-flight script's "Binance refuses this server" and "live mode without keys" branches (written and read-checked, not exercised) |
| The tax export streams out of a container with no volume | Telegram against the real API |

## What the bot needs from a host

1. **A process that never sleeps.** It holds an open Binance stream, ticks every 30 seconds, and runs entry timeouts and the kill-switch.
2. **An IP address Binance accepts.** Binance refuses requests from restricted locations, including **US IP addresses** ("Service unavailable from a restricted location"). Pick a server outside the United States.
3. **Ideally a fixed public IP,** so the Binance API key can be restricted to it. A key that works from anywhere is more dangerous if it leaks (keep withdrawals off either way).
4. **A PostgreSQL it can reach.** The bot only makes outbound connections (Binance, Telegram, the database); nothing needs to connect to it.
5. Very little else: one small container, a few hundred MB of RAM.

If the bot dies, the exit orders already on Binance keep protecting open positions. What stops is new entries, entry timeouts, alerts and the kill-switch.

## Choosing a host (as of 2026-10-07)

This comes from web searches, not from testing; provider terms change, so check their pages before signing up.

| Option | Fit | Notes |
|---|---|---|
| Render, Koyeb, Fly.io, Railway free tiers | **No** | Free services sleep (Render after ~15 min, Koyeb after an hour), free background workers do not exist (Render), or there is no real free tier any more (Fly.io, Railway). |
| Google Cloud free e2-micro VM | **Probably no** | Always-on, but free only in three **US** regions, which Binance likely blocks. |
| **Oracle Cloud Always Free VM** | Best free option | Always-on, and you choose a non-US home region. Oracle stops free VMs it considers idle (CPU, network and, on ARM shapes, memory all under 20% for 7 days), which a mostly idle bot is. Upgrading the account to Pay As You Go prevents that (it stays free within the Always Free limits, but needs a card). The free ARM allowance was reduced in 2026. Signup sometimes fails. |
| A small paid VPS (about $4 to $6 a month) | Reliable | DigitalOcean, Hetzner (EU) and others. Large next to a $16 wallet. |
| A computer at home that stays on | Free | The IP changes and the machine must not sleep. |

**Database.** Neon's free plan is a poor match: its compute suspends only after 5 idle minutes, but the bot queries every 30 seconds, so it never suspends and burns about 182 compute-hours a month against a 100-hour allowance. The database then stops answering mid-month (the data stays). Supabase's free plan pauses projects after a week of inactivity and is meant for hobby use. The free option that fits is **Postgres on the same server** (see below). A paid managed database is the low-effort option.

## First-time setup on an Ubuntu 22.04 server

Written from the design, never run on a real server. Expect to adjust.

1. Create the server in a region where Binance is available. Use an SSH key and disable password login.
2. Firewall: allow SSH and nothing else inbound (`ufw allow OpenSSH`, then `ufw enable`). The bot needs no inbound ports.
3. Install Docker Engine and the compose plugin by following Docker's own instructions for Ubuntu: https://docs.docker.com/engine/install/ubuntu/
4. Get the code (`git clone <your repo>`), then `cd` into it.
5. `cp .env.example .env` and `chmod 600 .env`. Fill in at least `DATABASE_URL`, the Binance keys, `BINANCE_TESTNET`, `DRY_RUN`, and the Telegram values. Start with `DRY_RUN=true`.
6. **`./scripts/preflight.sh`**. It checks docker, `.env`, that `DATABASE_URL` does not point at `localhost` (inside Docker that is the container itself), that Binance accepts this server, and your public IP (for the key's allowlist), then builds the image and runs the bot's own `--check-only`. It never sends an order. Fix every FAIL line.
7. Allow the server's IP on the database (trusted sources) and on the Binance API key.
8. Start: `docker compose up -d --build`, then `docker compose logs -f app`. You should see the scan line and, with Telegram configured, a "Bot started" message.
9. When you want it to survive crashes and reboots, set `RESTART_POLICY=unless-stopped` in `.env` and run `docker compose up -d` again. (Default is `no`, so a crash during testing stays visible. A misconfigured bot under `unless-stopped` restarts in a loop with growing delays: read the logs.)

## Day to day

| Task | Command |
|---|---|
| Follow the logs | `docker compose logs -f --tail 200 app` |
| Is it running? | `docker compose ps` |
| Stop (clean shutdown, up to 40 s) | `docker compose stop app` |
| Check the setup again | `./scripts/preflight.sh` |
| Export your trades | `docker compose run --rm -T app python -m bot.reports.tax_exporter --year 2026 --out /dev/stdout > tax_2026.csv` |

The export goes to stdout so no folder needs mounting (a mounted folder usually has the wrong owner for the container's non-root user).

## Upgrading

1. Back up the database first (below).
2. `git pull && docker compose up -d --build`. Pending migrations run automatically when the container starts, before the bot does. Run one instance only: two starting at once would race on the migration.
3. Read the logs. To go back: `git checkout <earlier commit>` and rebuild. A migration that added a column is harmless to leave; if you must undo one, restore the backup (the bot has `alembic downgrade`, but a restore is safer).

## Backups

The `tax_ledger` is your capital-gains record.

- Use the provider's automatic backups if there are any, **and** take your own dump before every upgrade and on a schedule: `pg_dump "<DATABASE_URL without the +asyncpg part>" > backup.sql`. Copy it off the server.
- Restore into an empty database: `psql "<url>" < backup.sql`, then start the bot (migrations bring it up to date).
- Regularly export the ledger as a CSV as well. It is readable without the database.

## Postgres on the same server (the free route)

The compose file deliberately has no database service. To run your own, install Postgres on the server (or run it as a separate container), create a database and a dedicated user, and set `DATABASE_URL` with the **host's address as the container sees it** (`host.docker.internal` reaches the host; compose already maps it). Keep Postgres listening only on the local network, never open to the internet. Memory on a 1 GB VM is tight; a bigger free shape is better. Back it up yourself (above). Whether to ship an optional database service in compose is an open decision in [NEXT_STEPS.md](../NEXT_STEPS.md).

## Security

- `.env` is `chmod 600` and never committed. Anyone who reads it can use your keys.
- The Binance key: **spot trading only, withdrawals off, restricted to the server's IP.**
- SSH by key only, a firewall with SSH as the only inbound rule, and security updates on (`unattended-upgrades`).
- The Telegram token is a password. If it leaks, revoke it with @BotFather and create a new one.
- The database user is dedicated to the bot, and the database only accepts the server's IP.

## When something is wrong

| You see | Likely cause |
|---|---|
| `Service unavailable from a restricted location` or HTTP 451/403 from Binance | The server's IP is in a region Binance blocks (US addresses in particular). Use another region. |
| `DATABASE_URL points at localhost` (pre-flight) or `database unreachable` | Inside Docker `localhost` is the container. Use the database's real host. |
| `Invalid API-key, IP, or permissions` (code -2015) | The key's IP allowlist does not include this server, or the key lacks spot-trading permission. |
| Timestamp errors (code -1021) | The server's clock is wrong. Check `timedatectl` and keep time sync on. |
| `Telegram refused sendMessage: 401` / `400 ... chat not found` | Wrong token / wrong chat id, or you never messaged the bot ([GETTING_STARTED.md](../GETTING_STARTED.md), section 7). |
| The container keeps restarting | Read `docker compose logs app`: a startup check is failing and `RESTART_POLICY=unless-stopped` keeps retrying. |
| "user stream interrupted" lines now and then | Normal reconnects; the bot keeps working through its polling fallback. Many in a row is worth a look. |
| Disk filling up | Logs are capped at 5 files of 10 MB; check the database and your own dumps. |

## Sources for the host comparison (checked 2026-10-07)

[Oracle idle reclamation](https://news.ycombinator.com/item?id=34680826), [Oracle free tier changes in 2026](https://braindetox.kr/en/posts/oracle_always_free_tier_reduced_2026.html), [Google Compute Engine free tier](https://agentdeals.dev/vendor/google-compute-engine), [Neon free plan limits](https://neon.com/faqs/free-plan-limits-and-quotas), [Render free tier](https://justinmckelvey.com/blog/is-render-free), [Koyeb free tier](https://www.srvrlss.io/provider/koyeb/), [Fly.io free tier](https://www.saaspricepulse.com/blog/flyio-free-tier-2026), [Binance restricted locations](https://trading-strategies.academy/archives/47269), [Supabase free tier](https://agentdeals.dev/vendor/supabase).
