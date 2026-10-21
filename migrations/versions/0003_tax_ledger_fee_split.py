"""tax_ledger: buy and sell fee as separate columns

Rows written before this migration keep 0 in both new columns; their fees_usd and
net_profit_usd were computed with the old rule (cost of every coin bought, including
leftovers).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("buy_fee_usd", "sell_fee_usd"):
        op.add_column(
            "tax_ledger",
            sa.Column(name, sa.Numeric(precision=38, scale=18), server_default="0", nullable=False),
        )


def downgrade() -> None:
    op.drop_column("tax_ledger", "sell_fee_usd")
    op.drop_column("tax_ledger", "buy_fee_usd")
