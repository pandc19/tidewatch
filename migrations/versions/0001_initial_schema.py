"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-06 08:18:50.685168
"""
import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('bot_state',
    sa.Column('id', sa.SmallInteger(), nullable=False),
    sa.Column('trading_paused_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('pause_reason', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('id = 1', name=op.f('ck_bot_state_single_row')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bot_state'))
    )
    op.execute("INSERT INTO bot_state (id) VALUES (1)")
    op.create_table('daily_pnl',
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('realized_pnl_usd', sa.Numeric(precision=38, scale=18), server_default='0', nullable=False),
    sa.Column('kill_switch_triggered', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.PrimaryKeyConstraint('date', name=op.f('pk_daily_pnl'))
    )
    op.create_table('kill_switch_events',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('triggered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('realized_loss_usd', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('account_balance_usd', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('loss_pct', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('limit_pct', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('resume_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_kill_switch_events'))
    )
    op.create_index('ix_kill_switch_events_triggered_at', 'kill_switch_events', ['triggered_at'], unique=False)
    op.create_table('positions',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('symbol', sa.String(length=20), nullable=False),
    sa.Column('status', sa.Enum('PENDING_ENTRY', 'OPEN', 'CLOSED', 'CANCELLED', name='position_status', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=38, scale=18), nullable=True),
    sa.Column('entry_price', sa.Numeric(precision=38, scale=18), nullable=True),
    sa.Column('exit_price', sa.Numeric(precision=38, scale=18), nullable=True),
    sa.Column('take_profit_pct', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('stop_loss_pct', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('exit_reason', sa.Enum('TAKE_PROFIT', 'STOP_LOSS', 'MANUAL', name='exit_reason', native_enum=False, create_constraint=False, length=32), nullable=True),
    sa.Column('realized_pnl_usd', sa.Numeric(precision=38, scale=18), nullable=True),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("exit_reason IN ('TAKE_PROFIT', 'STOP_LOSS', 'MANUAL')", name=op.f('ck_positions_exit_reason')),
    sa.CheckConstraint("status IN ('PENDING_ENTRY', 'OPEN', 'CLOSED', 'CANCELLED')", name=op.f('ck_positions_position_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_positions'))
    )
    op.create_index('ix_positions_closed_at', 'positions', ['closed_at'], unique=False)
    op.create_index('ix_positions_status', 'positions', ['status'], unique=False)
    op.create_index('uq_positions_symbol_active', 'positions', ['symbol'], unique=True, postgresql_where=sa.text("status IN ('PENDING_ENTRY', 'OPEN')"))
    op.create_table('orders',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('position_id', sa.Uuid(), nullable=False),
    sa.Column('symbol', sa.String(length=20), nullable=False),
    sa.Column('binance_order_id', sa.BigInteger(), nullable=True),
    sa.Column('client_order_id', sa.String(length=64), nullable=False),
    sa.Column('order_list_id', sa.BigInteger(), nullable=True),
    sa.Column('side', sa.Enum('BUY', 'SELL', name='order_side', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('order_type', sa.Enum('LIMIT', 'LIMIT_MAKER', 'STOP_LOSS_LIMIT', name='order_type', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('status', sa.Enum('NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', name='order_status', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('target_price', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('stop_price', sa.Numeric(precision=38, scale=18), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('filled_qty', sa.Numeric(precision=38, scale=18), server_default='0', nullable=False),
    sa.Column('avg_fill_price', sa.Numeric(precision=38, scale=18), nullable=True),
    sa.Column('commission', sa.Numeric(precision=38, scale=18), server_default='0', nullable=False),
    sa.Column('commission_asset', sa.String(length=20), nullable=True),
    sa.Column('cancel_reason', sa.Enum('ENTRY_TIMEOUT', 'KILL_SWITCH', 'OCO_COUNTERPART_FILLED', 'EXTERNAL', 'OTHER', name='cancel_reason', native_enum=False, create_constraint=False, length=32), nullable=True),
    sa.Column('cancel_detail', sa.Text(), nullable=True),
    sa.Column('kill_switch_event_id', sa.Uuid(), nullable=True),
    sa.Column('placed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("cancel_reason IN ('ENTRY_TIMEOUT', 'KILL_SWITCH', 'OCO_COUNTERPART_FILLED', 'EXTERNAL', 'OTHER')", name=op.f('ck_orders_cancel_reason')),
    sa.CheckConstraint("cancel_reason IS NULL OR status IN ('CANCELED', 'EXPIRED')", name=op.f('ck_orders_cancel_reason_only_when_cancelled')),
    sa.CheckConstraint("order_type IN ('LIMIT', 'LIMIT_MAKER', 'STOP_LOSS_LIMIT')", name=op.f('ck_orders_order_type')),
    sa.CheckConstraint("side IN ('BUY', 'SELL')", name=op.f('ck_orders_order_side')),
    sa.CheckConstraint("status IN ('NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED')", name=op.f('ck_orders_order_status')),
    sa.ForeignKeyConstraint(['kill_switch_event_id'], ['kill_switch_events.id'], name=op.f('fk_orders_kill_switch_event_id_kill_switch_events'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], name=op.f('fk_orders_position_id_positions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orders')),
    sa.UniqueConstraint('binance_order_id', name=op.f('uq_orders_binance_order_id')),
    sa.UniqueConstraint('client_order_id', name=op.f('uq_orders_client_order_id'))
    )
    op.create_index('ix_orders_expires_at_live', 'orders', ['expires_at'], unique=False, postgresql_where=sa.text("status IN ('NEW', 'PARTIALLY_FILLED')"))
    op.create_index('ix_orders_kill_switch_event_id', 'orders', ['kill_switch_event_id'], unique=False)
    op.create_index('ix_orders_order_list_id', 'orders', ['order_list_id'], unique=False)
    op.create_index('ix_orders_position_id', 'orders', ['position_id'], unique=False)
    op.create_index('ix_orders_symbol_status', 'orders', ['symbol', 'status'], unique=False)
    op.create_table('tax_ledger',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('position_id', sa.Uuid(), nullable=False),
    sa.Column('symbol', sa.String(length=20), nullable=False),
    sa.Column('amount_operated', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('buy_price_usd', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('sell_price_usd', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('total_buy_usd', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('total_sell_usd', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('fees_usd', sa.Numeric(precision=38, scale=18), server_default='0', nullable=False),
    sa.Column('net_profit_usd', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('buy_timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sell_timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], name=op.f('fk_tax_ledger_position_id_positions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tax_ledger')),
    sa.UniqueConstraint('position_id', name=op.f('uq_tax_ledger_position_id'))
    )
    op.create_index('ix_tax_ledger_sell_timestamp', 'tax_ledger', ['sell_timestamp'], unique=False)
    op.create_index('ix_tax_ledger_symbol', 'tax_ledger', ['symbol'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_tax_ledger_symbol', table_name='tax_ledger')
    op.drop_index('ix_tax_ledger_sell_timestamp', table_name='tax_ledger')
    op.drop_table('tax_ledger')
    op.drop_index('ix_orders_symbol_status', table_name='orders')
    op.drop_index('ix_orders_position_id', table_name='orders')
    op.drop_index('ix_orders_order_list_id', table_name='orders')
    op.drop_index('ix_orders_kill_switch_event_id', table_name='orders')
    op.drop_index('ix_orders_expires_at_live', table_name='orders', postgresql_where=sa.text("status IN ('NEW', 'PARTIALLY_FILLED')"))
    op.drop_table('orders')
    op.drop_index('uq_positions_symbol_active', table_name='positions', postgresql_where=sa.text("status IN ('PENDING_ENTRY', 'OPEN')"))
    op.drop_index('ix_positions_status', table_name='positions')
    op.drop_index('ix_positions_closed_at', table_name='positions')
    op.drop_table('positions')
    op.drop_index('ix_kill_switch_events_triggered_at', table_name='kill_switch_events')
    op.drop_table('kill_switch_events')
    op.drop_table('daily_pnl')
    op.drop_table('bot_state')
