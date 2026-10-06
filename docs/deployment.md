# Deployment

Target: a DigitalOcean droplet (Ubuntu 22.04 LTS) running the app with docker-compose, against an **external PostgreSQL** (a managed database or any server you can reach). Compose runs only the app; it never creates a database.

> Phase 1 only ships the container and compose file, and they only run startup checks. Treat this guide as the plan for the droplet and re-check it in phase 6, when the trading loop exists. Nothing here has been run on a droplet or against a managed database yet.

## Database

1. Create a PostgreSQL database and a dedicated user for the bot (not the admin user). PostgreSQL 13 or newer is required (`gen_random_uuid()`).
2. Allow the droplet's IP in the database's trusted sources / firewall.
3. Copy the connection string the provider gives you into `DATABASE_URL`. Provider-style URLs work as pasted:

   ```
   DATABASE_URL=postgres://user:password@host:port/dbname?sslmode=require
   ```

   At startup the app converts it for asyncpg (`postgresql+asyncpg://`, and `sslmode=` becomes `ssl=`, because asyncpg rejects `sslmode`). URL-encode special characters in the password (`@` → `%40`).
4. Use the **direct** connection, not a pooled endpoint. A pooler (PgBouncer) in transaction mode breaks asyncpg's prepared statements. The sibling project insight-engine hit exactly this on Neon: its deploy notes say to use the host *without* `-pooler`. That finding is from Neon; not yet tested here with the chosen provider.

Migrations run automatically each time the container starts. The database user needs permission to create tables in its schema.

## Prepare the droplet

1. Create the droplet, add your SSH key, disable password login.
2. Install Docker Engine and the compose plugin.
3. Firewall: allow SSH only. The bot makes outbound connections only.

## Binance API key

- Enable **Spot trading only**. Leave withdrawals **off**.
- Restrict the key to the droplet's public IP.
- Start with the testnet (`BINANCE_TESTNET=true`, separate testnet keys).

## Configure

```bash
git clone <repo> && cd <repo>
cp .env.example .env
```

Set `DATABASE_URL`, the Binance keys and the Telegram values. Keep `DRY_RUN=true` until the testnet run looks right. Going live is two deliberate edits: `BINANCE_TESTNET=false`, `DRY_RUN=false`.

## Start

```bash
docker compose up -d --build
docker compose logs -f app
```

## Operate

| Task | Command |
|---|---|
| Logs | `docker compose logs -f app` |
| Stop | `docker compose stop app` |
| Update | `git pull && docker compose up -d --build` |

The `tax_ledger` is your capital-gains record. Keep the provider's automatic backups on, and take your own dump before every upgrade:

```bash
pg_dump "<connection string without +asyncpg>" > backup.sql
```
