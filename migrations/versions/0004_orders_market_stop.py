"""orders: allow the STOP_LOSS order type (the market stop-loss leg, ADR-010)

Existing STOP_LOSS_LIMIT rows stay valid. Downgrading fails while any STOP_LOSS row exists.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

OLD = ("LIMIT", "LIMIT_MAKER", "STOP_LOSS_LIMIT")
NEW = (*OLD, "STOP_LOSS")


def _constraint(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_orders_order_type"), "orders", type_="check")
    listed = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(op.f("ck_orders_order_type"), "orders", f"order_type IN ({listed})")


def upgrade() -> None:
    _constraint(NEW)


def downgrade() -> None:
    _constraint(OLD)
