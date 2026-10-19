"""The /status report: what the bot is doing right now. Read-only (ADR-008).

`gather_status` reads the database and, for an open position and the balance, the exchange
(public price, account balance). Any exchange read that fails leaves its field empty instead of
failing the report. `format_status` turns the result into Telegram HTML and touches nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from bot.config.settings import APP_NAME, Settings
from bot.database.models import (
    BotState,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    TaxLedger,
)
from bot.services.notifications import _e, _num, _signed_usd, _table, _when, mode_label

log = logging.getLogger(__name__)

LIVE = (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)
MAX_POSITIONS_SHOWN = 5
MAX_REASON_CHARS = 150
STALE_TICK_SECONDS = 180


@dataclass(frozen=True)
class PositionLine:
    symbol: str
    status: PositionStatus
    quantity: Decimal | None
    entry_price: Decimal | None  # the fill price once open
    buy_limit: Decimal | None  # the resting entry order, while waiting
    take_profit: Decimal | None
    stop_trigger: Decimal | None
    bid: Decimal | None  # current best bid, for an open position
    since: datetime | None


@dataclass(frozen=True)
class StatusData:
    mode: str
    quote_asset: str
    uptime_seconds: float
    tick_age_seconds: float | None
    stream_on: bool
    trading_blocked: str | None
    paused_until: datetime | None
    pause_reason: str | None
    free_quote: Decimal | None
    closed_24h: int
    net_24h: Decimal
    closed_total: int
    net_total: Decimal
    positions: list[PositionLine] = field(default_factory=list)


async def gather_status(
    *,
    settings: Settings,
    sessions,
    exchange,
    trading_blocked: str | None,
    tick_age_seconds: float | None,
    uptime_seconds: float,
    stream_on: bool,
    now: datetime | None = None,
) -> StatusData:
    now = now or datetime.now(UTC)
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(Position)
                .where(Position.status.in_([PositionStatus.PENDING_ENTRY, PositionStatus.OPEN]))
                .order_by(Position.created_at)
            )
        ).all()
        orders = []
        if rows:
            orders = (
                await session.scalars(
                    select(Order).where(Order.position_id.in_([p.id for p in rows]), Order.status.in_(LIVE))
                )
            ).all()
        state = await session.get(BotState, 1)
        since = now - timedelta(hours=24)
        n24, net24 = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(TaxLedger.net_profit_usd), 0)).where(
                    TaxLedger.sell_timestamp >= since
                )
            )
        ).one()
        n_all, net_all = (
            await session.execute(select(func.count(), func.coalesce(func.sum(TaxLedger.net_profit_usd), 0)))
        ).one()
        lines = []
        for p in rows[:MAX_POSITIONS_SHOWN]:
            mine = [o for o in orders if o.position_id == p.id]
            lines.append(
                PositionLine(
                    symbol=p.symbol,
                    status=p.status,
                    quantity=p.quantity,
                    entry_price=p.entry_price,
                    buy_limit=next((o.target_price for o in mine if o.side == OrderSide.BUY), None),
                    take_profit=next((o.target_price for o in mine if o.order_type == OrderType.LIMIT_MAKER), None),
                    stop_trigger=next((o.stop_price for o in mine if o.order_type == OrderType.STOP_LOSS_LIMIT), None),
                    bid=None,
                    since=p.opened_at or p.created_at,
                )
            )
        paused_until = state.trading_paused_until if state else None
        pause_reason = state.pause_reason if state else None

    lines = [await _with_bid(line, exchange) for line in lines]
    return StatusData(
        mode=mode_label(settings),
        quote_asset=settings.quote_asset,
        uptime_seconds=uptime_seconds,
        tick_age_seconds=tick_age_seconds,
        stream_on=stream_on,
        trading_blocked=trading_blocked,
        paused_until=paused_until if paused_until and paused_until > now else None,
        pause_reason=pause_reason,
        free_quote=await _free_quote(settings, exchange),
        closed_24h=int(n24),
        net_24h=Decimal(net24),
        closed_total=int(n_all),
        net_total=Decimal(net_all),
        positions=lines,
    )


async def _with_bid(line: PositionLine, exchange) -> PositionLine:
    if line.status != PositionStatus.OPEN:
        return line
    try:
        bid = (await exchange.book_ticker(line.symbol)).bid
    except Exception as exc:  # noqa: BLE001  a price is nice to have, never required
        log.debug("status: no price for %s (%s)", line.symbol, type(exc).__name__)
        return line
    return PositionLine(**{**line.__dict__, "bid": bid})


async def _free_quote(settings: Settings, exchange) -> Decimal | None:
    if not settings.has_binance_credentials:
        return None
    try:
        return await exchange.free_balance(settings.quote_asset)
    except Exception as exc:  # noqa: BLE001
        log.debug("status: no balance (%s)", type(exc).__name__)
        return None


# --- text -----------------------------------------------------------------------------------


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds % 60:02d}s"


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 1] + "…"


def format_status(data: StatusData) -> str:
    head = f"<b>[{_e(data.mode)}]</b> {_e(APP_NAME)}\n<b>Status</b>\n"
    quote = data.quote_asset

    if data.tick_age_seconds is None:
        tick = "no heartbeat yet"
    elif data.tick_age_seconds > STALE_TICK_SECONDS:
        tick = f"STALE: {_duration(data.tick_age_seconds)} ago"
    else:
        tick = f"{int(data.tick_age_seconds)}s ago"

    if data.trading_blocked:
        trading = "BLOCKED: " + _short(data.trading_blocked)
    elif data.paused_until:
        trading = "paused (kill-switch)"
    else:
        trading = "active"
    rows = [
        ("Uptime", _duration(data.uptime_seconds)),
        ("Last engine tick", tick),
        ("Account stream", "on" if data.stream_on else "off"),
        ("New entries", trading),
    ]
    if data.paused_until:
        rows.append(("Resumes", _when(data.paused_until)))
        if data.pause_reason:
            rows.append(("Pause reason", _short(data.pause_reason)))
    if data.free_quote is not None:
        rows.append((f"Free {quote}", f"{data.free_quote:,.2f}"))
    rows.append(("Closed, last 24h", f"{data.closed_24h}, net {_signed_usd(data.net_24h)} USD"))
    rows.append(("Closed, all time", f"{data.closed_total}, net {_signed_usd(data.net_total)} USD"))
    text = head + _table(rows)

    if not data.positions:
        return text + "\nNo open position."
    for p in data.positions:
        asset = p.symbol.removesuffix(quote)
        if p.status == PositionStatus.PENDING_ENTRY:
            prow = [
                (asset, "buy waiting"),
                ("Buy limit", f"{_num(p.buy_limit)} {quote}"),
                ("Quantity", _num(p.quantity)),
            ]
        else:
            prow = [
                (asset, "open"),
                ("Entry price", f"{_num(p.entry_price)} {quote}"),
                ("Quantity", _num(p.quantity)),
                ("Take-profit", f"{_num(p.take_profit)} {quote}" if p.take_profit else "NONE"),
                ("Stop-loss trigger", f"{_num(p.stop_trigger)} {quote}" if p.stop_trigger else "NONE"),
            ]
            if p.bid is not None and p.entry_price:
                change = (p.bid - p.entry_price) / p.entry_price
                prow.append(("Best bid now", f"{_num(p.bid)} ({change:+.2%})"))
            if not p.take_profit or not p.stop_trigger:
                prow.append(("Warning", "exit orders not live"))
        if p.since:
            prow.append(("Since", _when(p.since)))
        text += "\n" + _table(prow)
    return text
