# Glossary

The trading and operations words used in this project, with the meaning they have **here**. Setting names are the `.env` variables; column and status names are in the database (`docs/guides/queries.sql`). Values are the defaults.

## The market

| Term | Meaning |
|---|---|
| **Spot** | Buying and selling the actual coin, immediately. No borrowing, no leverage, no futures. This bot only trades spot. |
| **Symbol / pair** | A market such as `BTCUSDT`. `SYMBOLS` lists the ones the bot scans (BTC, ETH, SOL). |
| **Base asset / quote asset** | In `BTCUSDT`, BTC is the base (what you buy) and USDT is the quote (what you pay with). `QUOTE_ASSET=USDT`. |
| **USDT** | A coin pegged to the US dollar. The tax ledger treats it as USD (an accountant should confirm that is acceptable). |
| **Wallet: free / locked** | Free balance is what the bot can spend. Balance reserved by an open order is locked. The bot sizes trades on the free quote balance. |
| **Candle (kline)** | One time slice of price: open, high, low, close. `SCAN_INTERVAL=1h` means one candle per hour. The bot only reads **closed** candles. |
| **Bid / ask / spread** | The best price someone will pay (bid) and the best price someone will sell at (ask). The spread is their gap as a share of the mid price. `MAX_SPREAD_PCT=0.2%`: a wider spread means the pair is skipped. |
| **Slippage** | The difference between the price you expected and the price you got. Not measured yet (a testnet fills at friendly prices). |
| **Liquidity** | How easily you can trade without moving the price. The big pairs have tight spreads. |

## Orders

| Term | Meaning |
|---|---|
| **Limit order** | An order to buy or sell at a given price or better. The entry buy is a limit order placed at the best bid, so it waits in the book. |
| **Maker / taker** | A maker adds a resting order to the book. A taker removes one by trading against it immediately. Both pay 0.1% on the testnet account here (the real rate is assumed to be the same). |
| **Entry** | The buy that opens a position (`LIMIT`, `BUY`). |
| **Exit** | The sells that close it: the take-profit and the stop-loss. |
| **Take-profit (TP)** | A sell placed above the entry to lock in a gain. `TAKE_PROFIT_PCT=4%`. In the database it is the `LIMIT_MAKER` order. |
| **Stop-loss (SL)** | A sell that triggers if the price falls to a set level, to cap the loss. `STOP_LOSS_PCT=2%`. In the database it is the `STOP_LOSS_LIMIT` order. |
| **Stop price vs limit price** | The stop price is the trigger. When the market reaches it, a limit order is placed at the lower limit price. The gap is `STOP_LIMIT_OFFSET_PCT=0.2%`, so the sell can still fill if the price keeps dropping. |
| **OCO (one cancels the other)** | The take-profit and stop-loss placed as a pair on Binance. When one fills, Binance cancels the other. Cancelling either one cancels both. Shares an `order_list_id`. |
| **Protected / unprotected** | A position is protected while its OCO is live on the exchange. If the OCO is gone and the coins are still held, it is unprotected: the bot alerts every `UNPROTECTED_REMINDER_MINUTES=15` and never sells on its own. |
| **Client order ID** | The label the bot gives each order (`bot-…`, the prefix is `ORDER_ID_PREFIX`). It is how the bot recognises its own orders after a restart. `-tp` and `-sl` mark the two legs. |
| **Entry timeout** | An unfilled entry buy is cancelled after `ENTRY_TIMEOUT_DAYS=3`. |

### Order statuses (`orders.status`)

`NEW` open and waiting · `PARTIALLY_FILLED` part bought or sold · `FILLED` done · `CANCELED` cancelled · `EXPIRED` ended without filling (the other leg of an OCO after its pair filled) · `REJECTED` Binance refused it.

### Cancel reasons (`orders.cancel_reason`)

`ENTRY_TIMEOUT` the buy did not fill in time · `KILL_SWITCH` the daily loss limit tripped · `OCO_COUNTERPART_FILLED` the other leg filled · `EXTERNAL` cancelled outside the bot (for example in the Binance app) · `OTHER` see `cancel_detail`.

## Positions and results

| Term | Meaning |
|---|---|
| **Position** | One trade from entry to exit. Only `MAX_OPEN_POSITIONS=1` at a time. |
| **Position statuses** | `PENDING_ENTRY` the buy is placed, nothing filled · `OPEN` coins held, OCO active · `CLOSED` finished · `CANCELLED` the entry was cancelled with nothing filled. |
| **Exit reason** | `TAKE_PROFIT`, `STOP_LOSS`, or `MANUAL` (you sold the coins yourself). |
| **Allocation** | The share of the free quote balance each trade uses. `ALLOCATION_PCT=0.98` by default; the testnet rehearsal uses `0.0016` (about 16 USDT) to imitate a small wallet. |
| **Realised PnL** | Profit or loss of a closed trade, in USD. Open positions have none yet. |
| **Net profit / return %** | Realised PnL after fees; the same as a share of the cost. |
| **Break-even win rate** | The share of trades that must hit the take-profit to cover the stops and the fees. With a 4% target, a 2% stop and 0.2% fees it is **36.7%**. The replay won 33%. |
| **Edge** | A real, repeatable advantage. The entry signal has **no demonstrated edge** (ADR-006): do not call it profitable. |
| **Reporte de ventas (sales sheet)** | The ledger as `Venta` rows in the owner's spreadsheet columns: Fecha, Plataforma, Tipo, Motivo, Observaciones, Cantidad Operada, Precio Unitario de Compra / de Venta, Monto Directo, Monto. Tab-separated Spanish format, Ecuador dates, no fees. `Monto Directo` = quantity x sell price; `Monto` = gross profit. |
| **Gross vs net profit** | Gross is quantity x (sell price - buy price), what the sheet records. Net is what the bot books after fees are subtracted (never added), so it is a little lower. The CSV has both the fees and the net. |
| **Tax ledger** | One row per closed trade: prices, totals, fees, net profit, UTC timestamps. The record for capital-gains reporting (`tax_exporter`). |

## Fees and exchange limits

| Term | Meaning |
|---|---|
| **Commission (fee)** | What Binance charges per trade: 0.1% per side on this account. Paid in the coin received, so buying 0.0082 ETH leaves a little less than that to sell. |
| **Commission asset** | The coin the fee was taken in (`orders.commission_asset`). |
| **Round-trip fee** | A buy plus a sell: about **0.2%**. A take-profit must be larger than this (`TAKE_PROFIT_PCT` is checked at startup). |
| **Dust** | Leftover coin below the smallest sellable step, for example after rounding the sell quantity down. |
| **Filters** | Binance's rules for a pair, read at startup: **step size** (quantity increments), **tick size** (price increments), **min quantity**, and **min notional** (the smallest order value, about $5). Every price and quantity is rounded to them before it is sent. |
| **Protectable trade** | A trade big enough that its stop-loss sell still meets the minimum order value after the fee. Smaller trades are refused. |
| **Region block** | Binance refuses requests from some regions (US addresses). A blocked server fails the startup checks. |

## The entry signal

| Term | Meaning |
|---|---|
| **Scanner** | The part of the bot that looks for entries, every `SCAN_EVERY_SECONDS=60`, on closed candles only. |
| **RSI (relative strength index)** | A 0 to 100 score of recent buying versus selling strength, over 14 candles (`RSI_PERIOD`). |
| **Oversold** | RSI at or below `RSI_OVERSOLD=30`: the price fell fast. |
| **Lookback** | How many recent candles are checked for an oversold reading: `RSI_LOOKBACK=6`. |
| **EMA (exponential moving average)** | A smoothed average price that weights recent candles more: `EMA_PERIOD=9`. |
| **Cross-up** | The latest close is above the EMA and the one before was at or below it. |
| **Signal** | Oversold within the lookback **and** a cross-up on the latest closed candle, on a pair with a tight enough spread. If several pairs signal, the lowest RSI wins. |
| **Entry discount** | Optionally place the buy a little below the best bid: `ENTRY_DISCOUNT_PCT=0`. |

## Safety and recovery

| Term | Meaning |
|---|---|
| **Kill-switch** | If realised losses over the last 24 hours reach `DAILY_LOSS_LIMIT_PCT` of the account, the bot cancels unfilled buys and pauses **new entries** for `KILL_SWITCH_COOLDOWN_HOURS=24`. It never sells anything. The pause survives restarts (`bot_state`) and is logged in `kill_switch_events`. |
| **Reconciliation** | Comparing the bot's database with what Binance says, at startup and after every stream reconnect. It records facts (fills, cancels); the engine decides what to do. |
| **Orphan** | An order on Binance with the bot's prefix that the database does not know. The bot reports it, blocks new entries, and never adopts it. Two bots on one testnet account create orphans for each other. |
| **Account stream** | Binance's live feed of changes to your orders and balance (user data stream). Off in dry-run. |
| **Polling safety net** | Re-reading live orders over REST every tick, so a missed stream event is corrected. |
| **Engine tick** | The engine's periodic check (`ENGINE_TICK_SECONDS=30`): entry timeouts, unprotected positions, new entries. |
| **Command (Telegram)** | A message such as `/status` sent to the bot. Read-only, answered only for `TELEGRAM_CHAT_ID`, and only when `TELEGRAM_COMMANDS=true` (ADR-008). Stale ones from before a restart are skipped. |
| **Long polling** | How the bot listens: it asks Telegram "anything new?" and Telegram holds the request open up to 25 seconds. Only one process may do it per token (a second gets 409 Conflict). |
| **Observer hook / alert queue** | Alerts are queued by the engine and sent separately, so a slow Telegram can never delay a trade (ADR-007). |

## Modes

| Term | Meaning |
|---|---|
| **Testnet** | Binance's practice exchange with fake money (`BINANCE_TESTNET=true`). |
| **Dry run** | The bot decides but sends no order (`DRY_RUN=true`). No fills are simulated. Alerts say `[DRY RUN]`. |
| **Demo** | `./scripts/local.sh start demo`: forces the testnet, `DRY_RUN=false`, fast signals and tiny targets, so a whole trade happens within minutes. |
| **Rehearsal** | The long testnet run with the real settings and small trade size, in its own database. Alerts say `[TESTNET]`. |
| **Live** | Real exchange, real money (`BINANCE_TESTNET=false`, `DRY_RUN=false`). Alerts say `[LIVE]`. Blocked until the gate in `SETUP_CHECKLIST.md` section 8. |

## Running it

| Term | Meaning |
|---|---|
| **Instance lock** | A PostgreSQL advisory lock that allows one engine per database. A second instance waits, then takes over when the first stops. |
| **Heartbeat** | A file the engine touches every tick; the container health check reads its age. |
| **Health check** | Docker's `HEALTHCHECK`, run every 15 seconds. Unhealthy means no engine tick for `HEARTBEAT_MAX_AGE_SECONDS=180`, or the process is gone. |
| **Start period** | The time after container start during which health failures are ignored. Coolify waits all of it before its first check (45 s now). |
| **Hand-over** | A deploy replacing a running container: the new one waits on the lock, the old one is stopped, the new one takes over and reconciles. Each sends a "stopped" and a "started" alert. |
| **Migration** | A versioned change to the database schema (Alembic). Runs at every container start. |
| **Guard** | The startup check that refuses a database whose name disagrees with the exchange (`testnet` name on the real exchange, `prod`/`live` name on the testnet). |
