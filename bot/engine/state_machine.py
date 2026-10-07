"""The trading engine: one position from entry order to closed trade.

    PENDING_ENTRY --buy fills (or part-fills then times out)--> OPEN --exit leg fills--> CLOSED
          |
          +--buy cancelled or rejected with nothing filled--> CANCELLED

**State is evaluated, not event-driven.** `_evaluate` looks at a position's orders and
decides what should happen next. Stream events, timeouts, reconnects and the periodic
tick all just call it. It is idempotent and crash-safe: after a restart the same call
picks up exactly where things stood, with no memory of which event was missed.

**Persist before send.** An order row (with our own client order id) is committed before
the request goes to Binance. If the answer is lost, the order is found again by that id
instead of being placed twice.

Everything that changes state runs under one lock, so a stream event, the timeout check
and the tick can never interleave.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config.precision import FilterViolation, SymbolFilters
from bot.config.settings import Settings
from bot.database.models import (
    BotState,
    CancelReason,
    DailyPnl,
    ExitReason,
    KillSwitchEvent,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    TaxLedger,
)
from bot.engine.ledger import TradeResult, compute_trade_result, fee_value, is_cash_fee
from bot.services.binance_client import (
    ExchangeClient,
    ExchangeError,
    OrderRejected,
)
from bot.services.reconciliation import LIVE, reconcile, sync_order
from bot.services.risk_manager import KillSwitch, size_entry
from bot.services.user_stream import OrderEvent

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

ACTIVE = (PositionStatus.PENDING_ENTRY, PositionStatus.OPEN)
DEAD = (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED)

# Client-order-id prefix of the order rows we create for sales the owner made by hand.
MANUAL_PREFIX = "manual-"
# Binance's "price relationship" refusal (e.g. take-profit already below the market).
PRICE_RELATIONSHIP_ERROR = -2010
# How far above the best ask a bumped take-profit is placed so it rests as a maker order.
TP_BUMP_PCT = Decimal("0.0005")
# An order with no Binance id after this long is looked up by client id (lost response).
UNKNOWN_ORDER_GRACE = timedelta(seconds=30)

ALLOWED_TRANSITIONS: dict[PositionStatus, set[PositionStatus]] = {
    PositionStatus.PENDING_ENTRY: {PositionStatus.OPEN, PositionStatus.CANCELLED},
    PositionStatus.OPEN: {PositionStatus.CLOSED},
    PositionStatus.CLOSED: set(),
    PositionStatus.CANCELLED: set(),
}


class IllegalTransition(RuntimeError):
    pass


def transition(position: Position, new_status: PositionStatus) -> None:
    if new_status not in ALLOWED_TRANSITIONS[position.status]:
        raise IllegalTransition(f"{position.symbol}: {position.status.value} -> {new_status.value}")
    position.status = new_status


@dataclass(frozen=True)
class EntrySignal:
    symbol: str
    price: Decimal  # limit price for the buy


class EntrySource(Protocol):
    """Where entries come from (the scanner and risk gates, phase 4)."""

    async def next_entry(self) -> EntrySignal | None: ...


class NoEntries:
    """Phase 3 default: manage existing positions, open none."""

    async def next_entry(self) -> EntrySignal | None:
        return None


class EngineObserver:
    """Hooks for notifications (phase 5). Every method is a no-op by default."""

    async def entry_placed(self, position: Position, order: Order) -> None: ...
    async def entry_cancelled(self, position: Position, order: Order) -> None: ...
    async def exit_placed(self, position: Position, take_profit: Order, stop_loss: Order) -> None: ...
    async def trade_closed(self, position: Position, result: TradeResult) -> None: ...
    async def attention(self, message: str) -> None: ...
    async def kill_switch(self, event: KillSwitchEvent) -> None: ...
    async def reminder(self, message: str) -> None: ...
    async def protected_again(self, position: Position) -> None: ...


@dataclass
class _Unprotected:
    """A position that is holding coins with no stop-loss, and when we last told the owner."""

    since: datetime
    last_notice: datetime
    message: str
    kinds: set


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TradingEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        exchange: ExchangeClient,
        session_factory: SessionFactory,
        entry_source: EntrySource | None = None,
        observer: EngineObserver | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self._settings = settings
        self._exchange = exchange
        self._sessions = session_factory
        self._source = entry_source or NoEntries()
        self._observer = observer or EngineObserver()
        self._clock = clock
        self._lock = asyncio.Lock()
        self._attention_keys: set[str] = set()
        self._last_exit_attempt: dict = {}
        self._unprotected: dict = {}
        # The hard circuit breaker. It runs inside the tick, which already holds the lock, so it
        # is given the lock-free cancel.
        self._kill_switch = KillSwitch(
            settings=settings,
            exchange=exchange,
            session_factory=session_factory,
            cancel_entries=self._cancel_pending_entries,
            notify=self._observer.kill_switch,
            clock=clock,
        )
        # Set when reconciliation finds something it cannot explain; blocks new entries.
        self.trading_blocked: str | None = None

    # ------------------------------------------------------------------ public entry points

    async def open_position(self, signal: EntrySignal) -> Position | None:
        async with self._lock:
            return await self._open_position(signal)

    async def tick(self) -> None:
        """Periodic work: re-read live orders, entry timeouts, safety net, then a new entry if allowed."""
        async with self._lock:
            await self._safe(self._kill_switch.check(), "checking the kill-switch")
            await self._safe(self._sync_live_orders(), "re-reading live orders")
            await self._safe(self._check_timeouts(), "checking entry timeouts")
            for position_id in await self._active_position_ids():
                await self._safe(self._evaluate(position_id), f"evaluating position {position_id}")
            if not isinstance(self._source, NoEntries):
                await self._safe(self._maybe_enter(), "looking for an entry")

    async def on_connected(self) -> None:
        """After (re)connecting the stream, and at startup: reconcile with the exchange."""
        async with self._lock:
            async with self._sessions() as session:
                report = await reconcile(session, self._exchange)
            if report.errors or report.orphans:
                self.trading_blocked = (
                    f"reconciliation: {len(report.errors)} unreadable orders, "
                    f"{len(report.orphans)} unknown orders on the exchange"
                )
                await self._notify_attention(self.trading_blocked)
            else:
                self.trading_blocked = None
            for position_id in await self._active_position_ids():
                await self._safe(self._evaluate(position_id), f"evaluating position {position_id}")

    async def on_stream_event(self, event: OrderEvent) -> None:
        async with self._lock:
            async with self._sessions() as session:
                order = await self._find_order(session, event)
                if order is None:
                    if self._exchange.owns(event.client_order_id):
                        log.error("event for our order %s but no row exists", event.client_order_id)
                    return
                await sync_order(session, self._exchange, order)
                position_id = order.position_id
            await self._evaluate(position_id)

    async def cancel_pending_entries(
        self,
        reason: CancelReason,
        detail: str,
        kill_switch_event_id=None,
    ) -> None:
        """Cancel every unfilled entry buy. Never sells anything."""
        async with self._lock:
            await self._cancel_pending_entries(reason, detail, kill_switch_event_id)

    async def _cancel_pending_entries(
        self, reason: CancelReason, detail: str, kill_switch_event_id=None
    ) -> None:
        """The same, for callers that already hold the lock (the tick, so the kill-switch)."""
        async with self._sessions() as session:
            ids = (
                await session.scalars(
                    select(Order.id)
                    .join(Position)
                    .where(
                        Order.side == OrderSide.BUY,
                        Order.status.in_(LIVE),
                        Position.status == PositionStatus.PENDING_ENTRY,
                    )
                )
            ).all()
        for order_id in ids:
            await self._safe(
                self._cancel_entry(order_id, reason, detail, kill_switch_event_id),
                f"cancelling entry {order_id}",
            )

    # ------------------------------------------------------------------------------- entry

    async def _cannot_open(self, session: AsyncSession, symbol: str | None) -> str | None:
        if self.trading_blocked:
            return f"trading blocked: {self.trading_blocked}"
        state = await session.get(BotState, 1)
        if state and state.trading_paused_until and state.trading_paused_until > self._clock():
            return f"paused until {state.trading_paused_until.isoformat()} ({state.pause_reason})"
        live = (
            await session.scalars(select(Position).where(Position.status.in_(ACTIVE)))
        ).all()
        if len(live) >= self._settings.max_open_positions:
            return f"already {len(live)} live positions (max {self._settings.max_open_positions})"
        if symbol and any(p.symbol == symbol for p in live):
            return f"{symbol} already has a live position"
        return None

    async def _maybe_enter(self) -> None:
        async with self._sessions() as session:
            if await self._cannot_open(session, None):
                return
        signal = await self._source.next_entry()
        if signal is not None:
            await self._open_position(signal)

    async def _open_position(self, signal: EntrySignal) -> Position | None:
        s = self._settings
        async with self._sessions() as session:
            reason = await self._cannot_open(session, signal.symbol)
        if reason:
            log.info("not opening %s: %s", signal.symbol, reason)
            return None

        filters = await self._exchange.symbol_filters(signal.symbol)
        price = filters.round_price(signal.price)
        free = await self._exchange.free_balance(filters.quote_asset)
        quantity = size_entry(
            free, s.allocation_pct, price, filters,
            stop_loss_pct=s.stop_loss_pct, stop_limit_offset_pct=s.stop_limit_offset_pct,
        )
        if quantity is None:
            log.info("not opening %s: %s free %s cannot fund a protectable trade", signal.symbol, filters.quote_asset, free)
            return None

        client_id = self._exchange.new_client_order_id()
        # Persist BEFORE sending, so a lost answer can be matched by client order id.
        async with self._sessions() as session:
            position = Position(
                symbol=signal.symbol,
                status=PositionStatus.PENDING_ENTRY,
                take_profit_pct=s.take_profit_pct,
                stop_loss_pct=s.stop_loss_pct,
            )
            session.add(position)
            await session.flush()
            order = Order(
                position_id=position.id,
                symbol=signal.symbol,
                client_order_id=client_id,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.NEW,
                target_price=price,
                quantity=quantity,
                expires_at=self._clock() + timedelta(days=s.entry_timeout_days),
            )
            session.add(order)
            await session.flush()
            position_id, order_id = position.id, order.id

        try:
            placed = await self._exchange.place_limit_buy(
                signal.symbol, price, quantity, client_order_id=client_id
            )
        except OrderRejected as exc:
            log.warning("entry for %s refused by Binance: %s", signal.symbol, exc)
            async with self._sessions() as session:
                order = await session.get(Order, order_id)
                order.status = OrderStatus.REJECTED
                order.cancel_detail = str(exc)
                transition(await session.get(Position, position_id), PositionStatus.CANCELLED)
            return None
        except ExchangeError:
            # Outcome unknown: the order may exist. Leave it pending; it is looked up by
            # client order id (see _resolve_unknown_orders) before anything else happens.
            log.exception("entry for %s: unknown outcome, will look it up by client id", signal.symbol)
            return await self._get_position(position_id)

        async with self._sessions() as session:
            order = await session.get(Order, order_id)
            order.binance_order_id = placed.order_id
            if not self._exchange.dry_run:
                await sync_order(session, self._exchange, order)  # an instant fill shows up here
        log.info("entry placed: %s %s @ %s (%s)%s", signal.symbol, quantity, price, client_id,
                 " [DRY RUN]" if self._exchange.dry_run else "")
        position = await self._get_position(position_id)
        await self._observer.entry_placed(position, order)
        await self._evaluate(position_id)
        return await self._get_position(position_id)

    # ------------------------------------------------------------------------- evaluation

    async def _evaluate(self, position_id) -> None:
        position = await self._get_position(position_id)
        if position.status == PositionStatus.PENDING_ENTRY:
            await self._evaluate_pending(position_id)
        elif position.status == PositionStatus.OPEN:
            await self._evaluate_open(position_id)

    async def _evaluate_pending(self, position_id) -> None:
        opened = cancelled = False
        async with self._sessions() as session:
            position = await self._load(session, position_id)
            buy = self._buy_order(position)
            if buy.status == OrderStatus.FILLED or (buy.status in DEAD and buy.filled_qty > 0):
                filters = await self._exchange.symbol_filters(position.symbol)
                fee = buy.commission if buy.commission_asset == filters.base_asset else Decimal(0)
                transition(position, PositionStatus.OPEN)
                position.quantity = buy.filled_qty - fee  # what we actually hold, after the coin fee
                position.entry_price = buy.avg_fill_price
                position.opened_at = buy.filled_at or self._clock()
                opened = True
            elif buy.status in DEAD:  # nothing filled
                transition(position, PositionStatus.CANCELLED)
                cancelled = True
        if opened:
            log.info("entry filled: %s", position_id)
            await self._place_exit(position_id)
        elif cancelled:
            await self._observer.entry_cancelled(
                await self._get_position(position_id), await self._get_order(buy.id)
            )

    async def _evaluate_open(self, position_id) -> None:
        async with self._sessions() as session:
            position = await self._load(session, position_id)
            symbol, held = position.symbol, position.quantity
            sells = [o for o in position.orders if o.side == OrderSide.SELL]
            filled = next((o for o in sells if o.status == OrderStatus.FILLED), None)
            live = [o for o in sells if o.status in LIVE]
            all_rejected = bool(sells) and all(o.status == OrderStatus.REJECTED for o in sells)
        if filled is not None:
            self._unprotected.pop(position_id, None)
            await self._close(position_id, filled.id)
            return
        if live:
            await self._clear_unprotected(position_id)  # protected (again): say so if we had warned
            return
        if await self._detect_manual_sale(position_id):
            self._unprotected.pop(position_id, None)
            return  # the owner sold it by hand: the position is now closed
        if not sells or all_rejected:
            await self._place_exit(position_id)  # never placed, or refused last time: try again
        else:
            # Exits exist but none is live or filled: cancelled outside the bot. Do not fight a
            # deliberate manual cancel by re-placing it; tell the owner, and keep reminding them.
            await self._flag_unprotected(
                position_id,
                "exits-cancelled",
                f"{symbol}: your sell orders were cancelled outside the bot. "
                f"You still hold {held:f} {symbol.removesuffix(self._settings.quote_asset)} with no stop-loss.",
            )
        await self._remind_unprotected(position_id)

    async def _detect_manual_sale(self, position_id) -> bool:
        """True if the owner sold this position by hand, in which case it has been closed.

        Looks for sells in the account's trade history that none of our orders explain. A
        partial manual sale is only flagged, never guessed at.
        """
        if self._exchange.dry_run:
            return False
        async with self._sessions() as session:
            position = await self._load(session, position_id)
            symbol, held = position.symbol, position.quantity
            since = position.opened_at or position.created_at
            ours = {o.binance_order_id for o in position.orders if o.binance_order_id}
        try:
            filters = await self._exchange.symbol_filters(symbol)
            trades = await self._exchange.recent_trades(symbol, since)
        except ExchangeError:
            log.exception("could not look for a manual sale of %s", symbol)
            return False
        sells = [t for t in trades if not t.is_buyer and t.order_id not in ours]
        sold = sum((t.quantity for t in sells), Decimal(0))
        if sold <= 0:
            return False
        if sold < held - filters.step_size:
            await self._flag_unprotected(
                position_id,
                "partial-manual-sale",
                f"{symbol}: you sold {sold} of the {held} the bot holds. Partial manual sales are not "
                "handled automatically: the rest has no stop-loss, so sell or protect it yourself.",
            )
            return False

        # Book exactly the position's quantity, even if the owner also sold coins of their own.
        quantity = min(sold, held)
        share = quantity / sold
        fees: Counter[str] = Counter()
        for t in sells:
            fees[t.commission_asset] += t.commission * share
        fee_asset, fee_amount = fees.most_common(1)[0] if fees else (None, Decimal(0))
        order_ids = sorted({t.order_id for t in sells})
        average = sum((t.price * t.quantity for t in sells), Decimal(0)) / sold
        async with self._sessions() as session:
            order = Order(
                position_id=position_id,
                symbol=symbol,
                binance_order_id=order_ids[0],
                client_order_id=f"{MANUAL_PREFIX}{order_ids[0]}",
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,  # the real type is unknown; not used for manual sales
                status=OrderStatus.FILLED,
                target_price=average,
                quantity=quantity,
                filled_qty=quantity,
                avg_fill_price=average,
                commission=fee_amount,
                commission_asset=fee_asset,
                filled_at=max(t.time for t in sells),
                cancel_detail=f"sold outside the bot (exchange orders {', '.join(map(str, order_ids))})",
            )
            session.add(order)
            await session.flush()
            order_id = order.id
        log.warning("%s: sold by hand (%s at %s average). Closing the position.", symbol, quantity, average)
        await self._close(position_id, order_id)
        return True

    # ------------------------------------------------------------------------------- exit

    async def _place_exit(self, position_id) -> None:
        async with self._sessions() as session:
            position = await self._load(session, position_id)
            if position.status != PositionStatus.OPEN:
                return
            symbol, entry, held = position.symbol, position.entry_price, position.quantity
            tp_pct, sl_pct = position.take_profit_pct, position.stop_loss_pct
            was_refused = any(
                o.side == OrderSide.SELL and o.status == OrderStatus.REJECTED for o in position.orders
            )

        now = self._clock()
        last = self._last_exit_attempt.get(position_id)
        retry_gap = timedelta(seconds=self._settings.exit_retry_seconds)
        if was_refused and last is not None and now - last < retry_gap:
            return  # refused a moment ago: not again on every tick
        self._last_exit_attempt[position_id] = now

        filters = await self._exchange.symbol_filters(symbol)
        quantity = await self._exit_quantity(held, filters)
        if quantity < filters.min_qty:
            await self._flag_unprotected(
                position_id,
                "nothing-to-sell",
                f"{symbol}: the bot holds a position but the free balance covers only {quantity}, "
                "so no stop-loss can be placed. Check your balance.",
            )
            return
        take_profit = filters.round_price(entry * (1 + tp_pct))
        stop = filters.round_price(entry * (1 - sl_pct))
        stop_limit = filters.round_price(stop * (1 - self._settings.stop_limit_offset_pct))
        planned_stop, rebased_stop = stop, None

        # Persist before sending. The pair refused last time is reused, so retrying never piles
        # up rows.
        async with self._sessions() as session:
            position = await self._load(session, position_id)
            refused = sorted(
                (o for o in position.orders if o.side == OrderSide.SELL and o.status == OrderStatus.REJECTED),
                key=lambda o: o.placed_at,
            )
            tp_row = next((o for o in reversed(refused) if o.order_type == OrderType.LIMIT_MAKER), None)
            sl_row = next((o for o in reversed(refused) if o.order_type == OrderType.STOP_LOSS_LIMIT), None)
            if tp_row is not None and sl_row is not None:
                base_id = tp_row.client_order_id.removesuffix("-tp")
                for row in (tp_row, sl_row):
                    row.status, row.cancel_detail, row.quantity = OrderStatus.NEW, None, quantity
                tp_row.target_price = take_profit
                sl_row.target_price, sl_row.stop_price = stop_limit, stop
            else:
                base_id = self._exchange.new_client_order_id(reserve=3)
                tp_row = Order(
                    position_id=position_id, symbol=symbol, client_order_id=f"{base_id}-tp",
                    side=OrderSide.SELL, order_type=OrderType.LIMIT_MAKER, status=OrderStatus.NEW,
                    target_price=take_profit, quantity=quantity,
                )
                sl_row = Order(
                    position_id=position_id, symbol=symbol, client_order_id=f"{base_id}-sl",
                    side=OrderSide.SELL, order_type=OrderType.STOP_LOSS_LIMIT, status=OrderStatus.NEW,
                    target_price=stop_limit, stop_price=stop, quantity=quantity,
                )
                session.add_all([tp_row, sl_row])
            await session.flush()
            tp_id, sl_id = tp_row.id, sl_row.id

        placed = None
        for attempt in (1, 2):
            try:
                placed = await self._exchange.place_oco_sell(
                    symbol, quantity, take_profit, stop, stop_limit, list_client_order_id=base_id
                )
                break
            except OrderRejected as exc:
                if attempt == 1 and exc.code == PRICE_RELATIONSHIP_ERROR:
                    # The market moved past one of the two prices while the buy was filling.
                    ticker = await self._exchange.book_ticker(symbol)
                    floor = filters.round_price(ticker.ask * (1 + TP_BUMP_PCT), ROUND_UP)
                    if floor > take_profit:
                        # Above the target: rest the take-profit just above the market.
                        log.warning("%s: take-profit %s is below the market, moving it to %s", symbol, take_profit, floor)
                        take_profit = floor
                        await self._update_exit_prices(tp_id, sl_id, take_profit, stop, stop_limit)
                        continue
                    if stop >= ticker.bid:
                        # Below the stop: Binance refuses a stop behind the market, which would
                        # leave the coins unprotected. Put an emergency stop the usual distance
                        # below the CURRENT price instead; it bounds the loss and the owner is told.
                        stop = filters.round_price(ticker.bid * (1 - sl_pct))
                        stop_limit = filters.round_price(stop * (1 - self._settings.stop_limit_offset_pct))
                        rebased_stop = (stop, ticker.bid)
                        log.warning("%s: price %s is past the stop %s, using an emergency stop at %s", symbol, ticker.bid, planned_stop, stop)
                        await self._update_exit_prices(tp_id, sl_id, take_profit, stop, stop_limit)
                        continue
                await self._mark_exit_failed(tp_id, sl_id, position_id, str(exc))
                return
            except FilterViolation as exc:
                await self._mark_exit_failed(tp_id, sl_id, position_id, str(exc))
                return
            except ExchangeError:
                # Unknown outcome: leave the rows pending; they are looked up by client id.
                log.exception("exit for %s: unknown outcome, will look it up by client id", symbol)
                return

        async with self._sessions() as session:
            tp_row, sl_row = await session.get(Order, tp_id), await session.get(Order, sl_id)
            tp_row.binance_order_id, tp_row.order_list_id = placed.take_profit.order_id, placed.order_list_id
            sl_row.binance_order_id, sl_row.order_list_id = placed.stop_loss.order_id, placed.order_list_id
        log.info("exit placed: %s %s tp=%s stop=%s/%s", symbol, quantity, take_profit, stop, stop_limit)
        if rebased_stop is not None:
            new_stop, market = rebased_stop
            await self._attention_once(
                f"{position_id}:stop-rebased",
                f"{symbol}: the price fell to {market}, already past your stop at {planned_stop}. "
                f"A protective stop was placed at {new_stop} instead, so the loss from your entry at {entry} "
                f"is now capped near {(entry - new_stop) / entry:.1%}. Sell or cancel it yourself if you prefer",
            )
        await self._observer.exit_placed(
            await self._get_position(position_id), await self._get_order(tp_id), await self._get_order(sl_id)
        )
        await self._clear_unprotected(position_id)

    async def _update_exit_prices(self, tp_id, sl_id, take_profit, stop, stop_limit) -> None:
        async with self._sessions() as session:
            tp_row, sl_row = await session.get(Order, tp_id), await session.get(Order, sl_id)
            tp_row.target_price = take_profit
            sl_row.stop_price, sl_row.target_price = stop, stop_limit

    async def _exit_quantity(self, held: Decimal, filters: SymbolFilters) -> Decimal:
        """How much to sell: the position, plus any crumb of the coin lying around.

        If the free balance is the position plus less than one quantity step, the extra is
        leftover from earlier sales and is swept into this one. A bigger surplus is someone's
        own holding and is left alone. Never more than the free balance.
        """
        free = await self._exchange.free_balance(filters.base_asset)
        if held <= free < held + filters.step_size:
            wanted = free
        else:
            wanted = min(held, free)
        return filters.round_quantity(wanted)

    async def _mark_exit_failed(self, tp_id, sl_id, position_id, detail: str) -> None:
        async with self._sessions() as session:
            for oid in (tp_id, sl_id):
                order = await session.get(Order, oid)
                order.status = OrderStatus.REJECTED
                order.cancel_detail = detail
            symbol, quantity = order.symbol, order.quantity
        await self._flag_unprotected(
            position_id,
            "exit-refused",
            f"{symbol}: Binance refused the sell orders ({detail}). "
            f"You hold {quantity:f} {symbol.removesuffix(self._settings.quote_asset)} with no stop-loss. "
            f"Retrying every {self._settings.exit_retry_seconds}s.",
        )

    # ------------------------------------------------------------------------------ close

    async def _close(self, position_id, sell_id) -> None:
        async with self._sessions() as session:
            position = await self._load(session, position_id)
            if position.status != PositionStatus.OPEN:
                return
            buy, sell = self._buy_order(position), await session.get(Order, sell_id)
            symbol = position.symbol
            others = [o.id for o in position.orders if o.side == OrderSide.SELL and o.id != sell_id and o.status in LIVE]

        filters = await self._exchange.symbol_filters(symbol)
        buy_fee = await self._fee_value(buy, filters)
        sell_fee = await self._fee_value(sell, filters)
        now = self._clock()
        result = compute_trade_result(
            symbol=symbol,
            buy_qty=buy.filled_qty,
            buy_price=buy.avg_fill_price,
            sell_qty=sell.filled_qty,
            sell_price=sell.avg_fill_price,
            buy_fee_value=buy_fee,
            sell_fee_value=sell_fee,
            buy_fee_is_cash=is_cash_fee(OrderSide.BUY, buy.commission_asset, filters.base_asset),
            sell_fee_is_cash=is_cash_fee(OrderSide.SELL, sell.commission_asset, filters.base_asset),
            buy_time=buy.filled_at or position.opened_at or now,
            sell_time=sell.filled_at or now,
        )

        async with self._sessions() as session:
            position = await self._load(session, position_id)
            if position.status != PositionStatus.OPEN:
                return
            transition(position, PositionStatus.CLOSED)
            position.exit_price = result.sell_price
            if sell.client_order_id.startswith(MANUAL_PREFIX):
                position.exit_reason = ExitReason.MANUAL
            elif sell.order_type == OrderType.LIMIT_MAKER:
                position.exit_reason = ExitReason.TAKE_PROFIT
            else:
                position.exit_reason = ExitReason.STOP_LOSS
            position.realized_pnl_usd = result.net_profit
            position.closed_at = result.sell_time
            session.add(
                TaxLedger(
                    position_id=position_id, symbol=symbol,
                    amount_operated=result.amount_operated,
                    buy_price_usd=result.buy_price, sell_price_usd=result.sell_price,
                    total_buy_usd=result.total_buy, total_sell_usd=result.total_sell,
                    fees_usd=result.fees_usd, net_profit_usd=result.net_profit,
                    buy_timestamp=result.buy_time, sell_timestamp=result.sell_time,
                )
            )
            day = result.sell_time.astimezone(UTC).date()
            pnl = await session.get(DailyPnl, day)
            if pnl is None:
                pnl = DailyPnl(date=day, realized_pnl_usd=Decimal(0), kill_switch_triggered=False)
                session.add(pnl)
            pnl.realized_pnl_usd += result.net_profit

        log.info("trade closed: %s %s net %s (%.2f%%)", symbol, position.exit_reason.value,
                 result.net_profit, result.return_pct * 100)
        # The exchange cancelled the other leg; read it so it ends up terminal and labelled.
        if not self._exchange.dry_run:
            for order_id in others:
                async with self._sessions() as session:
                    await sync_order(session, self._exchange, await session.get(Order, order_id))
        await self._observer.trade_closed(await self._get_position(position_id), result)

    async def _fee_value(self, order: Order, filters: SymbolFilters) -> Decimal:
        asset_price = None
        asset = order.commission_asset
        if order.commission and asset not in (None, filters.base_asset, filters.quote_asset):
            asset_price = await self._exchange.avg_price(f"{asset}{filters.quote_asset}")
        return fee_value(
            order.commission, asset, base=filters.base_asset, quote=filters.quote_asset,
            price=order.avg_fill_price or order.target_price, asset_price=asset_price,
        )

    # ------------------------------------------------------------ timeouts / lost orders

    async def _check_timeouts(self) -> None:
        async with self._sessions() as session:
            expired = (
                await session.scalars(
                    select(Order.id).where(
                        Order.side == OrderSide.BUY,
                        Order.status.in_(LIVE),
                        Order.expires_at <= self._clock(),
                    )
                )
            ).all()
        for order_id in expired:
            await self._safe(
                self._cancel_entry(order_id, CancelReason.ENTRY_TIMEOUT, f"not filled within {self._settings.entry_timeout_days} days"),
                f"cancelling expired entry {order_id}",
            )

    async def _cancel_entry(self, order_id, reason: CancelReason, detail: str, event_id=None) -> None:
        async with self._sessions() as session:
            order = await session.get(Order, order_id)
            if order is None or order.status not in LIVE:
                return
            symbol, binance_id, client_id, position_id = (
                order.symbol, order.binance_order_id, order.client_order_id, order.position_id,
            )
            if self._exchange.dry_run:  # nothing exists on the exchange: just record it
                order.status = OrderStatus.CANCELED
                order.cancel_reason, order.cancel_detail = reason, detail
                order.kill_switch_event_id = event_id
        if not self._exchange.dry_run:
            await self._exchange.cancel_order(symbol, order_id=binance_id, client_order_id=None if binance_id else client_id)
            async with self._sessions() as session:
                order = await session.get(Order, order_id)
                await sync_order(
                    session, self._exchange, order,
                    cancel_reason=reason, cancel_detail=detail, kill_switch_event_id=event_id,
                )
        log.info("entry %s cancelled: %s", order_id, reason.value)
        await self._evaluate(position_id)

    async def _sync_live_orders(self) -> None:
        """Re-read every live order over REST: a polling safety net behind the stream.

        A missed or out-of-order stream event (for example only one leg of a cancelled OCO
        pair being reported) is corrected within one tick instead of waiting for a reconnect.
        Orders sent but never confirmed (lost response) are asked for by client order id,
        but only after a grace period, so one still in flight is not declared missing.
        """
        if self._exchange.dry_run:
            return
        async with self._sessions() as session:
            live = (
                await session.scalars(
                    select(Order.id).where(
                        Order.status.in_(LIVE),
                        or_(
                            Order.binance_order_id.is_not(None),
                            Order.placed_at <= self._clock() - UNKNOWN_ORDER_GRACE,
                        ),
                    )
                )
            ).all()
        for order_id in live:
            async with self._sessions() as session:
                order = await session.get(Order, order_id)
                await self._safe(sync_order(session, self._exchange, order), f"re-reading order {order_id}")

    # --------------------------------------------------------------------------- helpers

    async def _load(self, session: AsyncSession, position_id) -> Position:
        return (
            await session.scalars(
                select(Position)
                .where(Position.id == position_id)
                .options(selectinload(Position.orders))
                .execution_options(populate_existing=True)
            )
        ).one()

    async def _get_position(self, position_id) -> Position:
        async with self._sessions() as session:
            return await self._load(session, position_id)

    async def _get_order(self, order_id) -> Order:
        async with self._sessions() as session:
            return await session.get(Order, order_id)

    @staticmethod
    def _buy_order(position: Position) -> Order:
        return next(o for o in position.orders if o.side == OrderSide.BUY)

    async def _active_position_ids(self):
        async with self._sessions() as session:
            return (
                await session.scalars(select(Position.id).where(Position.status.in_(ACTIVE)))
            ).all()

    async def _find_order(self, session: AsyncSession, event: OrderEvent) -> Order | None:
        order = (
            await session.scalars(select(Order).where(Order.binance_order_id == event.order_id))
        ).first()
        if order is None:
            order = (
                await session.scalars(select(Order).where(Order.client_order_id == event.client_order_id))
            ).first()
        return order

    async def _flag_unprotected(self, position_id, kind: str, message: str) -> None:
        """Tell the owner the first time each *kind* of problem appears; later ticks only remind.

        A position can be unprotected for several reasons in turn (exits cancelled, then a
        partial manual sale); each new reason is announced once, and reminders carry the latest.
        """
        now = self._clock()
        state = self._unprotected.get(position_id)
        if state is None:
            state = self._unprotected[position_id] = _Unprotected(now, now, message, set())
        state.message = message
        if kind not in state.kinds:
            state.kinds.add(kind)
            state.last_notice = now
            await self._notify_attention(message)

    async def _remind_unprotected(self, position_id) -> None:
        state = self._unprotected.get(position_id)
        minutes = self._settings.unprotected_reminder_minutes
        now = self._clock()
        if state is None or minutes == 0 or now - state.last_notice < timedelta(minutes=minutes):
            return
        state.last_notice = now
        waited = int((now - state.since).total_seconds() // 60)
        log.critical("STILL UNPROTECTED after %d min: %s", waited, state.message)
        await self._observer.reminder(f"Still unprotected after {waited} min. {state.message}")

    async def _clear_unprotected(self, position_id) -> None:
        if self._unprotected.pop(position_id, None) is not None:
            await self._observer.protected_again(await self._get_position(position_id))

    async def _attention_once(self, key: str, message: str) -> None:
        if key in self._attention_keys:
            return
        self._attention_keys.add(key)
        await self._notify_attention(message)

    async def _notify_attention(self, message: str) -> None:
        log.critical("NEEDS ATTENTION: %s", message)
        await self._observer.attention(message)

    @staticmethod
    async def _safe(coro, what: str) -> None:
        """One failing step must not stop the others (or the loop)."""
        try:
            await coro
        except Exception:
            log.exception("error while %s", what)
