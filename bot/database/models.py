"""SQLAlchemy ORM schema.

Tables:
  positions          one row per trade, from entry order to closed exit
  orders             every order sent to Binance (entry buy, and both OCO sell legs)
  tax_ledger         one row per closed position, for capital-gains reporting
  daily_pnl          realised PnL per UTC day
  bot_state          single row; the kill-switch pause survives restarts
  kill_switch_events one row per kill-switch trip, with why and what it cancelled

Prices, quantities and USD amounts are NUMERIC(38,18), read back as `Decimal`.
Enums are stored as plain strings with a CHECK constraint (no native PG enums,
so adding a value is a simple migration).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NUMERIC = Numeric(38, 18)

# Stable constraint names, so Alembic can drop/alter them later.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class OrderSide(enum.StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(enum.StrEnum):
    LIMIT = "LIMIT"  # entry buy
    LIMIT_MAKER = "LIMIT_MAKER"  # OCO take-profit leg
    STOP_LOSS_LIMIT = "STOP_LOSS_LIMIT"  # OCO stop-loss leg


class OrderStatus(enum.StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class CancelReason(enum.StrEnum):
    ENTRY_TIMEOUT = "ENTRY_TIMEOUT"  # buy did not fill before expires_at
    KILL_SWITCH = "KILL_SWITCH"  # daily loss limit tripped
    OCO_COUNTERPART_FILLED = "OCO_COUNTERPART_FILLED"  # other OCO leg filled
    EXTERNAL = "EXTERNAL"  # cancelled outside the bot (e.g. Binance app)
    OTHER = "OTHER"  # see cancel_detail


class PositionStatus(enum.StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"  # buy placed, nothing filled yet
    OPEN = "OPEN"  # holding coins, OCO exit active
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"  # entry cancelled with nothing filled


class ExitReason(enum.StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    MANUAL = "MANUAL"


def _enum(py_enum: type[enum.Enum], name: str) -> Enum:
    return Enum(py_enum, name=name, native_enum=False, create_constraint=True, length=32)


def _timestamptz(**kwargs) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kwargs)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_status", "status"),
        # At most one live position per symbol: stops stacking buys on a falling coin.
        Index(
            "uq_positions_symbol_active",
            "symbol",
            unique=True,
            postgresql_where=text("status IN ('PENDING_ENTRY', 'OPEN')"),
        ),
        Index("ix_positions_closed_at", "closed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    symbol: Mapped[str] = mapped_column(String(20))
    status: Mapped[PositionStatus] = mapped_column(_enum(PositionStatus, "position_status"))
    # Quantity actually held after the entry fee, and the average prices.
    quantity: Mapped[Decimal | None] = mapped_column(NUMERIC)
    entry_price: Mapped[Decimal | None] = mapped_column(NUMERIC)
    exit_price: Mapped[Decimal | None] = mapped_column(NUMERIC)
    # Snapshot of the strategy settings this trade was opened with.
    take_profit_pct: Mapped[Decimal] = mapped_column(NUMERIC)
    stop_loss_pct: Mapped[Decimal] = mapped_column(NUMERIC)
    exit_reason: Mapped[ExitReason | None] = mapped_column(_enum(ExitReason, "exit_reason"))
    realized_pnl_usd: Mapped[Decimal | None] = mapped_column(NUMERIC)
    opened_at: Mapped[datetime | None] = _timestamptz()
    closed_at: Mapped[datetime | None] = _timestamptz()
    created_at: Mapped[datetime] = _timestamptz(server_default=func.now())
    updated_at: Mapped[datetime] = _timestamptz(server_default=func.now(), onupdate=func.now())

    orders: Mapped[list[Order]] = relationship(back_populates="position")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_position_id", "position_id"),
        Index("ix_orders_symbol_status", "symbol", "status"),
        Index("ix_orders_order_list_id", "order_list_id"),
        # Timeout sweep only ever looks at orders that are still live.
        Index(
            "ix_orders_expires_at_live",
            "expires_at",
            postgresql_where=text("status IN ('NEW', 'PARTIALLY_FILLED')"),
        ),
        Index("ix_orders_kill_switch_event_id", "kill_switch_event_id"),
        CheckConstraint(
            "cancel_reason IS NULL OR status IN ('CANCELED', 'EXPIRED')",
            name="cancel_reason_only_when_cancelled",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("positions.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(20))
    # Null until Binance answers, and always null in dry-run. Postgres allows many NULLs
    # in a unique column, so dry-run orders never collide.
    binance_order_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    # Our own id, sent to Binance so a lost response can be matched on restart.
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    # Shared by the two legs of an OCO pair.
    order_list_id: Mapped[int | None] = mapped_column(BigInteger)
    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide, "order_side"))
    order_type: Mapped[OrderType] = mapped_column(_enum(OrderType, "order_type"))
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus, "order_status"))
    target_price: Mapped[Decimal] = mapped_column(NUMERIC)
    stop_price: Mapped[Decimal | None] = mapped_column(NUMERIC)  # stop-loss trigger
    quantity: Mapped[Decimal] = mapped_column(NUMERIC)
    filled_qty: Mapped[Decimal] = mapped_column(NUMERIC, default=Decimal(0), server_default="0")
    avg_fill_price: Mapped[Decimal | None] = mapped_column(NUMERIC)
    commission: Mapped[Decimal] = mapped_column(NUMERIC, default=Decimal(0), server_default="0")
    commission_asset: Mapped[str | None] = mapped_column(String(20))
    cancel_reason: Mapped[CancelReason | None] = mapped_column(_enum(CancelReason, "cancel_reason"))
    cancel_detail: Mapped[str | None] = mapped_column(Text)
    kill_switch_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kill_switch_events.id", ondelete="SET NULL")
    )
    placed_at: Mapped[datetime] = _timestamptz(server_default=func.now())
    expires_at: Mapped[datetime | None] = _timestamptz()
    filled_at: Mapped[datetime | None] = _timestamptz()  # exchange time of the last fill
    updated_at: Mapped[datetime] = _timestamptz(server_default=func.now(), onupdate=func.now())

    position: Mapped[Position] = relationship(back_populates="orders")


class TaxLedger(Base):
    """One row per closed position. USDT is treated as USD."""

    __tablename__ = "tax_ledger"
    __table_args__ = (
        Index("ix_tax_ledger_sell_timestamp", "sell_timestamp"),
        Index("ix_tax_ledger_symbol", "symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    position_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("positions.id", ondelete="RESTRICT"), unique=True
    )
    symbol: Mapped[str] = mapped_column(String(20))
    amount_operated: Mapped[Decimal] = mapped_column(NUMERIC)
    buy_price_usd: Mapped[Decimal] = mapped_column(NUMERIC)
    sell_price_usd: Mapped[Decimal] = mapped_column(NUMERIC)
    total_buy_usd: Mapped[Decimal] = mapped_column(NUMERIC)
    total_sell_usd: Mapped[Decimal] = mapped_column(NUMERIC)
    # net_profit_usd = total_sell_usd - total_buy_usd - buy_fee_usd - sell_fee_usd
    buy_fee_usd: Mapped[Decimal] = mapped_column(NUMERIC, default=Decimal(0), server_default="0")
    sell_fee_usd: Mapped[Decimal] = mapped_column(NUMERIC, default=Decimal(0), server_default="0")
    fees_usd: Mapped[Decimal] = mapped_column(NUMERIC, default=Decimal(0), server_default="0")  # buy + sell
    net_profit_usd: Mapped[Decimal] = mapped_column(NUMERIC)
    buy_timestamp: Mapped[datetime] = _timestamptz()
    sell_timestamp: Mapped[datetime] = _timestamptz()


class DailyPnl(Base):
    """Summary per UTC day. The kill-switch itself uses a rolling 24h sum of closed positions."""

    __tablename__ = "daily_pnl"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    realized_pnl_usd: Mapped[Decimal] = mapped_column(
        NUMERIC, default=Decimal(0), server_default="0"
    )
    kill_switch_triggered: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )


class BotState(Base):
    """Exactly one row (id = 1)."""

    __tablename__ = "bot_state"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    trading_paused_until: Mapped[datetime | None] = _timestamptz()
    pause_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _timestamptz(server_default=func.now(), onupdate=func.now())


class KillSwitchEvent(Base):
    """Why trading stopped. The orders it cancelled point back here via orders.kill_switch_event_id."""

    __tablename__ = "kill_switch_events"
    __table_args__ = (Index("ix_kill_switch_events_triggered_at", "triggered_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    triggered_at: Mapped[datetime] = _timestamptz(server_default=func.now())
    window_start: Mapped[datetime] = _timestamptz()
    realized_loss_usd: Mapped[Decimal] = mapped_column(NUMERIC)
    account_balance_usd: Mapped[Decimal] = mapped_column(NUMERIC)
    loss_pct: Mapped[Decimal] = mapped_column(NUMERIC)
    limit_pct: Mapped[Decimal] = mapped_column(NUMERIC)
    resume_at: Mapped[datetime] = _timestamptz()
