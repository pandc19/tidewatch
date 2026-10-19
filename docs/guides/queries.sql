-- 1. Health summary
select
  (select count(*) from positions where status in ('PENDING_ENTRY','OPEN')) as active_positions,
  (select count(*) from orders where status in ('NEW','PARTIALLY_FILLED')) as live_orders,
  (select count(*) from tax_ledger) as closed_trades,
  (select coalesce(sum(net_profit_usd), 0) from tax_ledger) as net_profit_usd,
  (select trading_paused_until from bot_state) as paused_until;

-- 2. Positions: one row per trade
select symbol, status, quantity, entry_price, exit_price, exit_reason,
       realized_pnl_usd, take_profit_pct, stop_loss_pct, opened_at, closed_at, created_at
from positions
order by created_at desc;

-- 3. Orders: every leg with fills and fees
select placed_at, symbol, side, order_type, status, target_price, stop_price,
       quantity, filled_qty, avg_fill_price, commission, commission_asset,
       cancel_reason, cancel_detail, order_list_id, client_order_id, filled_at
from orders
order by placed_at desc;

-- 4. Each trade with its orders
select p.created_at, p.symbol, p.status as position_status, p.exit_reason,
       o.side, o.order_type, o.status as order_status,
       o.target_price, o.avg_fill_price, o.commission, o.commission_asset, o.cancel_reason
from positions p
join orders o on o.position_id = p.id
order by p.created_at desc, o.placed_at;

-- 5. Orders still live in our database (should match Binance's open orders)
select symbol, side, order_type, status, target_price, stop_price, order_list_id, client_order_id
from orders
where status in ('NEW','PARTIALLY_FILLED');

-- 6. Tax ledger: one row per closed trade
select sell_timestamp, symbol, amount_operated, buy_price_usd, sell_price_usd,
       total_buy_usd, total_sell_usd, fees_usd, net_profit_usd, buy_timestamp
from tax_ledger
order by sell_timestamp desc;

-- 7. Daily PnL (what the kill-switch watches)
select date, realized_pnl_usd, kill_switch_triggered
from daily_pnl
order by date desc;

-- 8. Kill-switch history
select triggered_at, realized_loss_usd, account_balance_usd, loss_pct, limit_pct, window_start, resume_at
from kill_switch_events
order by triggered_at desc;

-- 9. Is trading paused? (always one row)
select trading_paused_until, pause_reason, updated_at
from bot_state;
