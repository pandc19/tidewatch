"""Bring the `orders` table in line with what Binance says.

Used on startup, after every stream reconnect, and for each stream event
(`sync_order`). It records facts only: status, fills, fees, and *why* an order
was cancelled. It never decides anything (placing an OCO, closing a position):
it returns `OrderChange`s and the engine acts on them.

The cancel reason is set by whoever cancels on purpose (timeout, kill-switch).
An order found cancelled with no reason is labelled EXTERNAL, then relabelled
OCO_COUNTERPART_FILLED if the other leg of its OCO pair filled.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import CancelReason, Order, OrderSide, OrderStatus, OrderType
from bot.services.binance_client import (
    ExchangeClient,
    ExchangeError,
    ExchangeOrder,
    OrderNotFound,
    to_order_status,
)

log = logging.getLogger(__name__)

LIVE = (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)
CANCELLED = (OrderStatus.CANCELED, OrderStatus.EXPIRED)


@dataclass(frozen=True)
class OrderChange:
    order_id: uuid.UUID
    position_id: uuid.UUID
    symbol: str
    side: OrderSide
    order_type: OrderType
    old_status: OrderStatus
    new_status: OrderStatus
    filled_qty: Decimal  # cumulative
    newly_filled: Decimal
    avg_fill_price: Decimal | None
    commission: Decimal
    commission_asset: str | None


@dataclass
class ReconcileReport:
    changes: list[OrderChange] = field(default_factory=list)
    # Open on Binance with our prefix but unknown to the database (e.g. a crash between
    # sending and saving). Reported, never adopted automatically.
    orphans: list[ExchangeOrder] = field(default_factory=list)
    # (order id, message) for orders that could not be checked. The engine must not assume
    # the database is consistent while this is non-empty.
    errors: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    skipped_dry_run: bool = False


async def sync_order(
    session: AsyncSession,
    exchange: ExchangeClient,
    order: Order,
    *,
    cancel_reason: CancelReason | None = None,
    cancel_detail: str | None = None,
    kill_switch_event_id: uuid.UUID | None = None,
) -> OrderChange | None:
    """Re-read one order from Binance and update its row. None if nothing changed.

    When the bot has just cancelled this order on purpose, the engine passes the
    reason here, so a stream event racing ahead cannot label it EXTERNAL.
    """
    old_status, old_filled = order.status, order.filled_qty
    try:
        if order.binance_order_id is not None:
            ex = await exchange.get_order(order.symbol, order_id=order.binance_order_id)
        else:
            ex = await exchange.get_order(order.symbol, client_order_id=order.client_order_id)
    except OrderNotFound:
        if order.binance_order_id is not None:
            raise  # an order Binance gave us an id for vanishing is not something to guess about
        # Never got an id: the request was lost before Binance accepted it.
        order.status = OrderStatus.REJECTED
        order.cancel_detail = "not found on the exchange by client order id"
        await session.flush()
        return _change(order, old_status, old_filled)

    new_status = to_order_status(ex.status) or order.status  # PENDING_CANCEL: still live
    order.binance_order_id = ex.order_id
    order.order_list_id = ex.order_list_id
    order.status = new_status
    order.filled_qty = ex.executed_qty

    if ex.executed_qty > 0:
        order.avg_fill_price = ex.cumulative_quote_qty / ex.executed_qty
        fills = await exchange.order_fills(order.symbol, ex.order_id)
        if sum((f.quantity for f in fills), Decimal(0)) != ex.executed_qty:
            log.warning("fills for %s do not add up to executed qty yet; fee may be incomplete", ex.order_id)
        _apply_commission(order, fills)
        if fills:
            order.filled_at = max(f.time for f in fills)

    if new_status in CANCELLED and order.cancel_reason is None:
        if cancel_reason is not None:
            order.cancel_reason = cancel_reason
            order.cancel_detail = cancel_detail
            order.kill_switch_event_id = kill_switch_event_id
        else:
            order.cancel_reason = CancelReason.EXTERNAL
            order.cancel_detail = "cancelled or expired on the exchange, not by this bot"

    await session.flush()
    if order.order_list_id is not None:
        await _relabel_oco_cancellations(session, order.order_list_id)
    return _change(order, old_status, old_filled)


async def reconcile(session: AsyncSession, exchange: ExchangeClient) -> ReconcileReport:
    """Sync every live order and look for orders the database does not know."""
    report = ReconcileReport()
    if exchange.dry_run:
        report.skipped_dry_run = True  # dry-run orders were never sent: nothing to compare
        return report

    live = (
        await session.scalars(
            select(Order).where(Order.status.in_(LIVE)).order_by(Order.placed_at)
        )
    ).all()
    for order in live:
        try:
            change = await sync_order(session, exchange, order)
        except ExchangeError as exc:
            log.error("cannot reconcile order %s (%s): %s", order.id, order.symbol, exc)
            report.errors.append((order.id, str(exc)))
            continue
        if change is not None:
            report.changes.append(change)

    open_orders = await exchange.open_orders()
    ours = [o for o in open_orders if exchange.owns(o.client_order_id)]
    if ours:
        known = set(
            (
                await session.scalars(
                    select(Order.client_order_id).where(
                        Order.client_order_id.in_([o.client_order_id for o in ours])
                    )
                )
            ).all()
        )
        report.orphans = [o for o in ours if o.client_order_id not in known]
        for o in report.orphans:
            log.error("orphan order on Binance, not in database: %s %s", o.symbol, o.client_order_id)
    return report


def _apply_commission(order: Order, fills: list) -> None:
    totals: Counter[str] = Counter()
    for f in fills:
        totals[f.commission_asset] += f.commission
    if not totals:
        return
    if len(totals) > 1:
        # One commission column cannot hold two assets. Keep the largest and say so loudly;
        # an `order_fills` table is the proper fix (NEXT_STEPS.md).
        log.error("order %s paid commission in several assets %s; storing only one", order.symbol, dict(totals))
    asset, amount = totals.most_common(1)[0]
    order.commission_asset = asset
    order.commission = amount


async def _relabel_oco_cancellations(session: AsyncSession, order_list_id: int) -> None:
    legs = (await session.scalars(select(Order).where(Order.order_list_id == order_list_id))).all()
    if not any(leg.status == OrderStatus.FILLED for leg in legs):
        return
    for leg in legs:
        if leg.status in CANCELLED and leg.cancel_reason == CancelReason.EXTERNAL:
            leg.cancel_reason = CancelReason.OCO_COUNTERPART_FILLED
            leg.cancel_detail = "the other leg of the OCO pair filled"
    await session.flush()


def _change(order: Order, old_status: OrderStatus, old_filled: Decimal) -> OrderChange | None:
    if order.status == old_status and order.filled_qty == old_filled:
        return None
    return OrderChange(
        order_id=order.id,
        position_id=order.position_id,
        symbol=order.symbol,
        side=order.side,
        order_type=order.order_type,
        old_status=old_status,
        new_status=order.status,
        filled_qty=order.filled_qty,
        newly_filled=order.filled_qty - old_filled,
        avg_fill_price=order.avg_fill_price,
        commission=order.commission,
        commission_asset=order.commission_asset,
    )
