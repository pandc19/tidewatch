"""Risk rules: entry sizing, the spread filter, startup config checks and the kill-switch."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.precision import SymbolFilters
from bot.config.settings import Settings
from bot.database.models import (
    BotState,
    CancelReason,
    DailyPnl,
    KillSwitchEvent,
    Position,
    PositionStatus,
)
from bot.services.binance_client import ExchangeClient

log = logging.getLogger(__name__)

# Assumed worst-case fee taken in the coin on a buy (0.1% is Binance's standard rate;
# doubled for margin). The sell must still clear the minimum order value after it.
FEE_BUFFER = Decimal("0.002")


def size_entry(
    free_quote: Decimal,
    allocation_pct: Decimal,
    price: Decimal,
    filters: SymbolFilters,
    *,
    stop_loss_pct: Decimal,
    stop_limit_offset_pct: Decimal,
) -> Decimal | None:
    """Quantity to buy with `allocation_pct` of the free quote balance, or None.

    None means no valid trade fits: the quantity rounds below Binance's minimum, or the
    position could not be *protected*. The stop-loss leg sells a little less (after the
    fee) at a lower price, so its value must still reach the minimum order value;
    otherwise the OCO would be refused and the coins left unprotected.
    """
    if free_quote <= 0 or price <= 0:
        return None
    quantity = filters.round_quantity(free_quote * allocation_pct / price)
    if quantity < filters.min_qty or quantity * price < filters.min_notional:
        return None

    worst_case_sellable = filters.round_quantity(quantity * (1 - FEE_BUFFER))
    stop_limit_price = filters.round_price(
        price * (1 - stop_loss_pct) * (1 - stop_limit_offset_pct)
    )
    if worst_case_sellable < filters.min_qty or worst_case_sellable * stop_limit_price < filters.min_notional:
        return None
    return quantity


# --------------------------------------------------------------------------- spread filter

def spread_ok(spread_pct: Decimal, max_spread_pct: Decimal) -> bool:
    """True if the bid-ask spread (a fraction of the mid price) is tight enough to trade."""
    return spread_pct <= max_spread_pct


# ------------------------------------------------------------------------- startup checks

# Standard Binance spot fee (0.1%) paid on both legs.
ROUND_TRIP_FEE = Decimal("0.002")


def database_mismatch(settings: Settings) -> list[str]:
    """Errors when the database name and the exchange disagree (testnet trades must not land in
    a real-money database or the reverse; the tax export cannot tell them apart).

    Only a guard on the names: it cannot know what an unnamed database holds.
    """
    name = (make_url(settings.database_url.get_secret_value()).database or "").lower()
    if not settings.binance_testnet and "testnet" in name:
        return [f"BINANCE_TESTNET=false (real exchange) but the database '{name}' is a testnet one"]
    if settings.binance_testnet and any(word in name for word in ("prod", "live")):
        return [f"BINANCE_TESTNET=true (fake money) but the database '{name}' looks like a real one"]
    return []


def config_problems(
    settings: Settings,
    filters: dict[str, SymbolFilters],
    prices: dict[str, Decimal],
    free_quote: Decimal | None,
) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the configured strategy. Errors should stop the bot starting.

    `free_quote` is None when there are no API keys, which skips the balance checks.
    """
    errors: list[str] = []
    warnings: list[str] = []

    errors.extend(database_mismatch(settings))
    if settings.take_profit_pct <= ROUND_TRIP_FEE:
        errors.append(
            f"TAKE_PROFIT_PCT {settings.take_profit_pct} does not cover the ~{ROUND_TRIP_FEE} "
            "round-trip fee: every win would be a loss"
        )
    if settings.stop_loss_pct >= settings.take_profit_pct:
        warnings.append(
            f"STOP_LOSS_PCT {settings.stop_loss_pct} is not smaller than TAKE_PROFIT_PCT "
            f"{settings.take_profit_pct}: a loss costs at least as much as a win earns"
        )
    one_loss = settings.stop_loss_pct * settings.allocation_pct
    if one_loss >= settings.daily_loss_limit_pct:
        warnings.append(
            f"one stopped-out trade (~{one_loss:.4f} of the account) already reaches "
            f"DAILY_LOSS_LIMIT_PCT {settings.daily_loss_limit_pct}"
        )

    if free_quote is not None:
        viable = [
            s for s in settings.symbols
            if size_entry(
                free_quote, settings.allocation_pct, prices[s], filters[s],
                stop_loss_pct=settings.stop_loss_pct, stop_limit_offset_pct=settings.stop_limit_offset_pct,
            ) is not None
        ]
        for symbol in settings.symbols:
            if symbol not in viable:
                warnings.append(f"{symbol}: {free_quote} {settings.quote_asset} cannot fund a protectable trade")
        if not viable:
            errors.append(
                f"none of the configured pairs can be traded with {free_quote} {settings.quote_asset}: "
                "Binance's minimum order value would reject the entry or its stop-loss"
            )
    return errors, warnings


# ------------------------------------------------------------------------------ kill-switch

class KillSwitch:
    """Stops new trading after a bad rolling 24 hours. It never sells anything.

    Realised PnL of positions closed in the last 24 hours (gains offset losses). If the net
    result is a loss of at least `daily_loss_limit_pct` of the account, it cancels unfilled
    entry buys, pauses new entries for `kill_switch_cooldown_hours` (stored in `bot_state`,
    so a restart cannot forget it), records why in `kill_switch_events`, and alerts.

    After a trip only *new* losses count, so the same ones cannot trip it twice.
    The account is valued as quote balance (free + locked) plus open positions at entry cost.
    """

    WINDOW = timedelta(hours=24)

    def __init__(
        self,
        *,
        settings: Settings,
        exchange: ExchangeClient,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        cancel_entries: Callable[[CancelReason, str, uuid.UUID | None], Awaitable[None]],
        notify: Callable[[KillSwitchEvent], Awaitable[None]],
        clock: Callable[[], datetime],
    ):
        self._s = settings
        self._exchange = exchange
        self._sessions = session_factory
        self._cancel_entries = cancel_entries
        self._notify = notify
        self._clock = clock

    async def check(self) -> KillSwitchEvent | None:
        now = self._clock()
        async with self._sessions() as session:
            state = await session.get(BotState, 1)
            if state and state.trading_paused_until and state.trading_paused_until > now:
                return None  # already paused
            last = (
                await session.scalars(
                    select(KillSwitchEvent).order_by(KillSwitchEvent.triggered_at.desc()).limit(1)
                )
            ).first()
            window_start = now - self.WINDOW
            if last is not None:
                window_start = max(window_start, last.triggered_at)
            closed = (
                await session.scalars(
                    select(Position).where(
                        Position.status == PositionStatus.CLOSED,
                        Position.closed_at >= window_start,
                        Position.closed_at <= now,
                    )
                )
            ).all()
            net = sum((p.realized_pnl_usd or Decimal(0) for p in closed), Decimal(0))
            if net >= 0:
                return None
            held = (
                await session.scalars(select(Position).where(Position.status == PositionStatus.OPEN))
            ).all()
            held_value = sum((p.quantity * p.entry_price for p in held), Decimal(0))

        equity = await self._exchange.total_balance(self._s.quote_asset) + held_value
        if equity <= 0:
            return None
        loss, loss_pct = -net, -net / equity
        if loss_pct < self._s.daily_loss_limit_pct:
            return None

        resume_at = now + timedelta(hours=self._s.kill_switch_cooldown_hours)
        reason = (
            f"daily loss limit: lost {loss:.4f} ({loss_pct:.2%} of {equity:.2f}) "
            f"against a {self._s.daily_loss_limit_pct:.2%} limit"
        )
        async with self._sessions() as session:
            event = KillSwitchEvent(
                triggered_at=now, window_start=window_start, realized_loss_usd=loss,
                account_balance_usd=equity, loss_pct=loss_pct,
                limit_pct=self._s.daily_loss_limit_pct, resume_at=resume_at,
            )
            session.add(event)
            await session.flush()
            state = await session.get(BotState, 1)
            if state is None:
                state = BotState(id=1)
                session.add(state)
            state.trading_paused_until, state.pause_reason = resume_at, reason
            today = now.astimezone(UTC).date()
            day = await session.get(DailyPnl, today)
            if day is None:
                day = DailyPnl(date=today, realized_pnl_usd=Decimal(0), kill_switch_triggered=True)
                session.add(day)
            day.kill_switch_triggered = True
            event_id = event.id

        log.critical("KILL-SWITCH: %s. No new entries until %s", reason, resume_at.isoformat())
        await self._cancel_entries(CancelReason.KILL_SWITCH, reason, event_id)
        await self._notify(event)
        return event
