"""Schema rules, checked against the real (migrated) PostgreSQL.

Every test runs inside a transaction that is rolled back, so the database stays
empty. Skipped when no database is reachable at DATABASE_URL.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.settings import get_settings
from bot.database.connection import Database
from bot.database.models import (
    BotState,
    CancelReason,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
)

D = Decimal


@pytest.fixture
async def session():
    db = Database.from_settings(get_settings())
    try:
        await db.ping()
    except Exception as exc:  # noqa: BLE001  any failure to connect means skip, not fail
        await db.dispose()
        pytest.skip(f"PostgreSQL not reachable: {exc!r}")
    async with db.engine.connect() as conn:
        outer = await conn.begin()
        s = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        yield s
        await s.close()
        await outer.rollback()
    await db.dispose()


def position(symbol="SOLUSDT", status=PositionStatus.PENDING_ENTRY) -> Position:
    return Position(
        symbol=symbol, status=status, take_profit_pct=D("0.04"), stop_loss_pct=D("0.02")
    )


def order(pos: Position, **kw) -> Order:
    defaults = {
        "position": pos,
        "symbol": pos.symbol,
        "client_order_id": uuid.uuid4().hex,
        "side": OrderSide.BUY,
        "order_type": OrderType.LIMIT,
        "status": OrderStatus.NEW,
        "target_price": D("120.41"),
        "quantity": D("0.130"),
    }
    return Order(**{**defaults, **kw})


async def test_migration_seeded_the_single_bot_state_row(session):
    rows = (await session.execute(text("SELECT id FROM bot_state"))).scalars().all()
    assert rows == [1]


async def test_bot_state_rejects_a_second_row(session):
    session.add(BotState(id=2))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_only_one_live_position_per_symbol(session):
    session.add(position())
    await session.flush()
    session.add(position())
    with pytest.raises(IntegrityError, match="uq_positions_symbol_active"):
        await session.flush()


async def test_closed_position_frees_the_symbol(session):
    session.add_all([position(status=PositionStatus.CLOSED), position(status=PositionStatus.CLOSED)])
    session.add(position())
    await session.flush()  # two closed + one live on the same symbol is fine


async def test_different_symbols_can_be_live_together(session):
    session.add_all([position("SOLUSDT"), position("BTCUSDT")])
    await session.flush()


async def test_dry_run_orders_without_binance_id_do_not_collide(session):
    pos = position()
    session.add_all([order(pos), order(pos)])
    await session.flush()


async def test_binance_order_id_is_unique(session):
    pos = position()
    session.add_all([order(pos, binance_order_id=42), order(pos, binance_order_id=42)])
    with pytest.raises(IntegrityError, match="uq_orders_binance_order_id"):
        await session.flush()


async def test_client_order_id_is_unique(session):
    pos = position()
    session.add_all([order(pos, client_order_id="same"), order(pos, client_order_id="same")])
    with pytest.raises(IntegrityError, match="uq_orders_client_order_id"):
        await session.flush()


async def test_cancel_reason_is_stored_on_a_cancelled_order(session):
    pos = position()
    o = order(
        pos,
        status=OrderStatus.CANCELED,
        cancel_reason=CancelReason.ENTRY_TIMEOUT,
        cancel_detail="not filled within 3 days",
        expires_at=datetime.now(UTC),
    )
    session.add(o)
    await session.flush()
    await session.refresh(o)
    assert o.cancel_reason is CancelReason.ENTRY_TIMEOUT


async def test_cancel_reason_rejected_on_a_live_order(session):
    pos = position()
    session.add(order(pos, status=OrderStatus.NEW, cancel_reason=CancelReason.KILL_SWITCH))
    with pytest.raises(IntegrityError, match="cancel_reason_only_when_cancelled"):
        await session.flush()


async def test_unknown_status_rejected_by_the_database(session):
    pos = position()
    session.add(pos)
    await session.flush()
    with pytest.raises(DBAPIError, match="ck_orders_order_status"):
        await session.execute(
            text(
                "INSERT INTO orders (position_id, symbol, client_order_id, side, order_type,"
                " status, target_price, quantity)"
                " VALUES (:p, 'SOLUSDT', 'x', 'BUY', 'LIMIT', 'BOGUS', 1, 1)"
            ),
            {"p": pos.id},
        )


async def test_numbers_round_trip_as_exact_decimals(session):
    pos = position()
    o = order(pos, target_price=D("0.00001234"), quantity=D("26.9"))
    session.add(o)
    await session.flush()
    await session.refresh(o)
    assert o.target_price == D("0.00001234")
    assert o.quantity == D("26.9")
    assert isinstance(o.target_price, Decimal)
