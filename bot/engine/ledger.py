"""Profit and loss of one closed trade. Pure maths: no database, no exchange.

USDT is treated as USD. Rules:

* ``total_buy`` is what the buy cost, ``total_sell`` what the sell returned.
* A fee taken *in the coin on a buy* (Binance's default) is **not** subtracted again:
  those coins are simply missing from what was sold, so ``total_sell`` already
  reflects it.
* Every other fee is a real outflow and is subtracted: the quote-asset fee on a sell,
  and any BNB fee, valued in the quote asset.
* ``fees_usd`` records the value of *all* fees (for reporting), so it can be larger
  than what was subtracted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bot.database.models import OrderSide

ZERO = Decimal(0)


def fee_value(
    commission: Decimal,
    asset: str | None,
    *,
    base: str,
    quote: str,
    price: Decimal,
    asset_price: Decimal | None = None,
) -> Decimal:
    """Value of a commission in the quote asset.

    `asset_price` is the quote price of `asset` for fees paid in a third coin (BNB).
    """
    if not commission or asset is None:
        return ZERO
    if asset == quote:
        return commission
    if asset == base:
        return commission * price
    if asset_price is None:
        raise ValueError(f"need a price for the {asset} fee")
    return commission * asset_price


def is_cash_fee(side: OrderSide, asset: str | None, base: str) -> bool:
    """False only for the coin fee on a buy, which the lower sold quantity already reflects."""
    return not (side == OrderSide.BUY and asset == base)


@dataclass(frozen=True)
class TradeResult:
    symbol: str
    amount_operated: Decimal  # quantity sold
    buy_price: Decimal
    sell_price: Decimal
    total_buy: Decimal
    total_sell: Decimal
    fees_usd: Decimal
    net_profit: Decimal
    buy_time: datetime
    sell_time: datetime

    @property
    def return_pct(self) -> Decimal:
        """Net return on the cash spent, as a fraction (0.04 = 4%)."""
        return self.net_profit / self.total_buy if self.total_buy else ZERO


def compute_trade_result(
    *,
    symbol: str,
    buy_qty: Decimal,
    buy_price: Decimal,
    sell_qty: Decimal,
    sell_price: Decimal,
    buy_fee_value: Decimal,
    sell_fee_value: Decimal,
    buy_fee_is_cash: bool,
    sell_fee_is_cash: bool,
    buy_time: datetime,
    sell_time: datetime,
) -> TradeResult:
    total_buy = buy_qty * buy_price
    total_sell = sell_qty * sell_price
    cash_fees = (buy_fee_value if buy_fee_is_cash else ZERO) + (
        sell_fee_value if sell_fee_is_cash else ZERO
    )
    return TradeResult(
        symbol=symbol,
        amount_operated=sell_qty,
        buy_price=buy_price,
        sell_price=sell_price,
        total_buy=total_buy,
        total_sell=total_sell,
        fees_usd=buy_fee_value + sell_fee_value,
        net_profit=total_sell - total_buy - cash_fees,
        buy_time=buy_time,
        sell_time=sell_time,
    )
