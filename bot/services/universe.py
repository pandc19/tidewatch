"""The dynamic symbol list (ADR-009): the most liquid pairs, each tagged with a risk tier.

Off unless `SYMBOLS_AUTO=true`. The tier describes how violently a pair moves and how costly it
is to trade, which decides how often a 2% stop-loss gets hit and how much it slips. It is *not*
a forecast of which pair will gain: the entry signal has no demonstrated edge (ADR-006), and
ranking by recent gains would pick the pairs that already ran up, the opposite of a dip buy.

Selection, in order:
1. Pairs of the quote asset whose base is not a stablecoin, fiat or leveraged token.
2. 24h traded value at least `SYMBOLS_AUTO_MIN_VOLUME`, and a spread within `MAX_SPREAD_PCT`.
3. The most traded first; for the top candidates the last 24 hourly candles give the tier.
4. Pairs above `SYMBOLS_AUTO_MAX_RISK`, flat pairs (stablecoin-like) and pairs that are not
   tradable with OCO are dropped; the first `SYMBOLS_AUTO_COUNT` remain.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from bot.config.settings import Settings
from bot.services.binance_client import ExchangeClient

log = logging.getLogger(__name__)

TIERS = ("low", "medium", "high")
# 24h swing, (highest close - lowest close) / lowest close over the last hourly candles.
FLAT_BELOW = Decimal("0.005")  # a pair that barely moves is a stablecoin or pegged: nothing to buy
LOW_UP_TO = Decimal("0.025")
MEDIUM_UP_TO = Decimal("0.06")
SWING_CANDLES = 24
# Candle lookups per refresh: more than the list needs, since some candidates are dropped.
CANDIDATE_FACTOR = 3

NOT_TRADED_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "USD1", "USDE", "BFUSD", "XUSD", "UST", "USTC", "AEUR",
    "EUR", "EURI", "GBP", "TRY", "BRL", "ARS", "UAH", "PLN", "RON", "CZK", "JPY", "MXN", "COP", "U",
    "PAXG", "XAUT",  # gold-backed
})
_LEVERAGED = re.compile(r"(UP|DOWN|BULL|BEAR)$")


@dataclass(frozen=True)
class Pick:
    symbol: str
    quote_volume: Decimal
    spread_pct: Decimal
    swing_pct: Decimal
    tier: str


def tier_of(swing: Decimal) -> str | None:
    """`None` for a flat pair."""
    if swing < FLAT_BELOW:
        return None
    if swing <= LOW_UP_TO:
        return "low"
    return "medium" if swing <= MEDIUM_UP_TO else "high"


def swing_of(closes: list[Decimal]) -> Decimal | None:
    recent = closes[-SWING_CANDLES:]
    if len(recent) < SWING_CANDLES or min(recent) <= 0:
        return None
    return (max(recent) - min(recent)) / min(recent)


def candidates(rows: list[dict], settings: Settings) -> list[tuple[str, Decimal, Decimal]]:
    """(symbol, 24h quote volume, spread) of the pairs passing the cheap filters, most traded first."""
    quote = settings.quote_asset
    out = []
    for row in rows:
        symbol = row.get("symbol", "")
        if not symbol.endswith(quote):
            continue
        base = symbol[: -len(quote)]
        if not base or base in NOT_TRADED_BASES or _LEVERAGED.search(base):
            continue
        try:
            volume = Decimal(row["quoteVolume"])
            bid, ask = Decimal(row["bidPrice"]), Decimal(row["askPrice"])
        except (KeyError, ArithmeticError):
            continue
        if bid <= 0 or ask <= 0 or volume < settings.symbols_auto_min_volume:
            continue
        spread = (ask - bid) / ((ask + bid) / 2)
        if spread > settings.max_spread_pct:
            continue
        out.append((symbol, volume, spread))
    return sorted(out, key=lambda c: c[1], reverse=True)


async def select(exchange: ExchangeClient, settings: Settings) -> list[Pick]:
    allowed = TIERS[: TIERS.index(settings.symbols_auto_max_risk) + 1]
    ranked = candidates(await exchange.tickers_24h(), settings)
    picks: list[Pick] = []
    for symbol, volume, spread in ranked[: settings.symbols_auto_count * CANDIDATE_FACTOR]:
        if len(picks) >= settings.symbols_auto_count:
            break
        try:
            filters = await exchange.symbol_filters(symbol)  # raises if halted or unknown
            if not filters.oco_allowed:
                continue
            swing = swing_of(await exchange.closed_candle_closes(symbol, "1h", SWING_CANDLES + 1))
        except Exception:
            log.exception("universe: %s skipped", symbol)
            continue
        tier = None if swing is None else tier_of(swing)
        if tier in allowed:
            picks.append(Pick(symbol, volume, spread, swing, tier))
    return picks


class Universe:
    """The symbols the scanner looks at: `SYMBOLS` when the dynamic list is off or fails."""

    def __init__(
        self, settings: Settings, exchange: ExchangeClient, *, clock: Callable[[], float] = time.monotonic
    ):
        self._s = settings
        self._exchange = exchange
        self._clock = clock
        self._picks: list[Pick] = []
        self._refreshed: float | None = None

    @property
    def symbols(self) -> list[str]:
        if not self._s.symbols_auto or not self._picks:
            return list(self._s.symbols)
        return [p.symbol for p in self._picks]

    @property
    def picks(self) -> list[Pick]:
        return list(self._picks)

    async def refresh(self) -> None:
        """Re-select if the list is stale. A failure keeps the previous list (or `SYMBOLS`)."""
        if not self._s.symbols_auto:
            return
        now = self._clock()
        if self._refreshed is not None and now - self._refreshed < self._s.symbols_auto_refresh_minutes * 60:
            return
        self._refreshed = now
        try:
            picks = await select(self._exchange, self._s)
        except Exception:
            log.exception("universe: refresh failed, keeping %s", ",".join(self.symbols))
            return
        if not picks:
            log.warning("universe: nothing passed the filters, keeping %s", ",".join(self.symbols))
            return
        self._picks = picks
        log.info(
            "universe: %d pairs (max risk %s): %s", len(picks), self._s.symbols_auto_max_risk,
            ", ".join(f"{p.symbol} {p.tier} swing {p.swing_pct:.1%}" for p in picks),
        )
