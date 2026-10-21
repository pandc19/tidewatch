-- FAKE closed trades for trying /report, /csv and the sales sheet on your machine.
-- They reproduce two rows of the owner's spreadsheet (SOL and GTC, October 2026).
--
--   docker exec -i postgres18 psql -U tidewatch_app -d tidewatch_local < scripts/seed_demo_sales.sql
--
-- Local databases only, never the rehearsal or a real one. Remove them afterwards with
--   .venv/bin/python scripts/reset_db.py local
-- (that empties the whole database). Fees 0.03 USD per trade (0.015 buy, 0.015 sell); the net profit is after fees.

WITH closed AS (
  INSERT INTO positions (symbol, status, quantity, entry_price, exit_price, take_profit_pct, stop_loss_pct,
                         exit_reason, realized_pnl_usd, opened_at, closed_at)
  VALUES
    ('SOLUSDT', 'CLOSED', 0.128, 118.2, 121.5, 0.04, 0.02, 'TAKE_PROFIT', 0.39,
     '2026-10-03 14:00+00', '2026-10-05 03:30+00'),
    ('GTCUSDT', 'CLOSED', 71.8, 0.2180, 0.2223, 0.04, 0.02, 'TAKE_PROFIT', 0.28,
     '2026-10-04 10:00+00', '2026-10-05 14:00+00')
  RETURNING id, symbol
)
INSERT INTO tax_ledger (position_id, symbol, amount_operated, buy_price_usd, sell_price_usd,
                        total_buy_usd, total_sell_usd, buy_fee_usd, sell_fee_usd, fees_usd, net_profit_usd,
                        buy_timestamp, sell_timestamp)
SELECT closed.id, v.symbol, v.qty, v.buy, v.sell, v.qty * v.buy, v.qty * v.sell, 0.015, 0.015, 0.03, v.net,
       v.bought::timestamptz, v.sold::timestamptz
FROM closed
JOIN (VALUES
  ('SOLUSDT', 0.128::numeric, 118.2::numeric, 121.5::numeric, 0.39::numeric, '2026-10-03 14:00+00', '2026-10-05 03:30+00'),
  ('GTCUSDT', 71.8::numeric, 0.2180::numeric, 0.2223::numeric, 0.28::numeric, '2026-10-04 10:00+00', '2026-10-05 14:00+00')
) AS v(symbol, qty, buy, sell, net, bought, sold) ON v.symbol = closed.symbol;
