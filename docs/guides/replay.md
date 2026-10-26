# The replay tool

Answers one question: **if the bot had followed this entry rule and this exit rule over past market data, what would it have made after fees?** It reads public candles and simulates trades in memory. It uses no database, no API key and sends no order, so it cannot touch the bot, the testnet account or real money. It is research, not advice.

```bash
.venv/bin/python -m bot.replay                       # 250 days, 5 pairs, the 9 built-in exit rules
.venv/bin/python -m bot.replay --days 120 --per-pair
.venv/bin/python -m bot.replay --variant fixed:0.04:0.02 --variant trailing:0.02:0.03
.venv/bin/python -m bot.replay --rsi-oversold 40 --trades-csv exports/replay_trades.csv
```

The first run downloads the candles from Binance's **mainnet** public market data (the testnet's prices are synthetic, so replaying on them would say nothing) into `data/candles/` (git-ignored). Later runs read the cache and download only the newest candles; `--refresh` downloads everything again. A run takes under a minute.

## What it simulates

- **Entry:** the bot's own `evaluate_signal` on the last 200 closed candles of each pair, with your `.env` entry settings as defaults (`RSI_OVERSOLD`, `RSI_PERIOD`, `RSI_LOOKBACK`, `EMA_PERIOD`, `SCAN_INTERVAL`) or the `--rsi-*` / `--ema-period` / `--interval` flags. The entry is assumed filled at the signal candle's close.
- **One position at a time**, like the bot. While a trade is open signals are ignored; when several pairs signal on the same candle the lowest RSI wins.
- **Fees** on both sides (`--fee`, default 0.1%) and extra **slippage** on stop fills (`--slippage`, default 0.05%).
- **Exit rules** (`--variant`, repeatable):

| Spec | Rule |
|---|---|
| `fixed:TP:SL` | Take-profit and stop-loss at fixed fractions of the entry. `fixed:0.04:0.02` is what the bot does now. |
| `atr:TP_MULT:SL_MULT` | The same, sized from the pair's average candle range over the last 14 candles (so a calm pair gets tighter levels). |
| `breakeven:TP:SL:TRIGGER` | Fixed levels, but once the price is up `TRIGGER` the stop moves to the entry plus the round-trip fee. |
| `trailing:SL:TRAIL` | A stop that follows the highest price by `TRAIL`, starting `SL` below the entry. No target. |
| `partial:TP:SL:TRAIL` | Half is sold at the target; the rest is protected at the entry plus fees and then trailed. |

The bot itself only has the fixed rule. The others are for comparison; running one live would need a new ADR (see [CLAUDE.md](../../CLAUDE.md)).

## Conservative choices

A candle's high and low do not say which came first, so: a candle touching both the stop and the target counts as a **stop**; a stop that moves only applies from the **next** candle; a stop fills at the stop (or at the open if the candle gapped through it) and a bit worse; a position still open when the data ends is sold at the last close (`OPEN_AT_END`) and ends the replay.

It is still optimistic in places: the real entry is a limit order at the bid that may never fill, the spread filter is not simulated, and real fills can be worse than the candle suggests.

## Reading the output

Two tables per run, the same variants in each:

- **Before the cut-off** (the first 70% of the period, `--holdout 0.3`): where you would look for something to tune.
- **From the cut-off on** (the last 30%): data not used for choosing, so **judge by this one**.

Columns: trades, win rate (net above zero), **mean net return per trade** with its **95% range**, the most losing trades in a row, and a verdict: `above zero` / `below zero` only when the whole range is on one side, `not distinguishable from zero` when it straddles zero, and `too few trades` under 30. With a few dozen trades most ranges straddle zero. That is the honest result, not a failure of the tool.

Rules of thumb:

- A variant matters only if it is **above zero in both periods**. One good table is luck until proven otherwise.
- The footer prints how many variants were tried. Each extra one raises the chance that one looks good by accident.
- `--per-pair` splits the trades by pair; each pair has few, so it is colour, not proof.
- `--trades-csv` writes every simulated trade so any one can be checked by hand against the candles.

Nothing here makes the entry signal profitable (ADR-006): the replay can only show how it would have behaved, and the past is not the future.

## The 2026-10 parameter study

A one-off script around this package (not committed), run on 2026-10-26. The rules were set before any result was seen:

- **Data:** 1h and 4h mainnet candles, 1,100 days (2023-10 to 2026-10), 10 pairs: BTC, ETH, SOL, BNB, XRP, DOGE, ADA, LINK, AVAX, LTC.
- **Grid:** RSI oversold 20/25/30/35, lookback 3/6/12, EMA 9/21, and 13 exit rules (fixed, ATR, trailing, break-even): 598 settings with any signal. Fees 0.1% per side, slippage 0.05% on stops.
- **Choosing and judging:** settings ranked on the first 70% only (to 2025-11-30), by the low end of the 95% range, needing at least 60 trades. The top 5 were judged once on the last 30%. To pass, a setting needed its range above zero there **and** to beat random entries with the same exit.
- **Baseline:** random entry candles at a similar rate, 20 seeds, same exits and one-position loop.
- **Fidelity:** signals came from the bot's own `evaluate_signal` on 200-candle windows. A fast prefilter only skipped candles that could not signal; on three pair/interval samples it matched `find_signals` exactly.

Result: **nothing passed.** No setting's range was above zero even on the first 70%. Ranking on it did not predict the last 30% (Spearman −0.20). The signal averaged 0.05–0.07% per trade worse than random entries, which lose about the fees. The bot's own settings lost −0.59% per trade on the last 30% (124 trades, range −1.07% to −0.11%). The last 30% was a falling market, but the signal did not beat random entries in the rising part either.

To test a new entry idea, repeat the study the same way: set the rules before the first run, choose on one period, judge once on a later one, and compare with random entries.

## Code

`bot/replay/`: `candles.py` (download and cache), `exits.py` (the rules), `simulate.py` (signals and the one-position loop), `stats.py`, `report.py`, `__main__.py`. The candle download is `ExchangeClient.candles` in `bot/services/binance_client.py`, the only module that talks to Binance. Tests: `tests/test_replay.py`, no network.
