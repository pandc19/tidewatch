"""tax_ledger: one row per sale, not per position

A stop that sells part of a position and expires is booked when it expires, so a position can
have several rows. `position_id` stops being unique; `sell_order_id` (unique) ties a row to the
order that made the sale, so a sale is never booked twice. Rows written before this migration
keep it null.

Downgrading fails while any position has more than one row.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("uq_tax_ledger_position_id"), "tax_ledger", type_="unique")
    op.create_index("ix_tax_ledger_position_id", "tax_ledger", ["position_id"], unique=False)
    op.add_column("tax_ledger", sa.Column("sell_order_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_tax_ledger_sell_order_id_orders"), "tax_ledger", "orders", ["sell_order_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(op.f("uq_tax_ledger_sell_order_id"), "tax_ledger", ["sell_order_id"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_tax_ledger_sell_order_id"), "tax_ledger", type_="unique")
    op.drop_constraint(op.f("fk_tax_ledger_sell_order_id_orders"), "tax_ledger", type_="foreignkey")
    op.drop_column("tax_ledger", "sell_order_id")
    op.drop_index("ix_tax_ledger_position_id", table_name="tax_ledger")
    op.create_unique_constraint(op.f("uq_tax_ledger_position_id"), "tax_ledger", ["position_id"])
