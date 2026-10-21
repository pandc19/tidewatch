"""Profit and loss of one closed trade. Pure maths: no database, no exchange.

USDT is treated as USD. One rule, so the numbers can be checked by hand:

    net profit = total_sell - total_buy - buy_fee - sell_fee

* ``total_sell`` is the coins sold times the sell price.
* ``total_buy`` is the *same* coins times the buy price. Coins bought but not sold (the coin
  fee on the buy, and what rounding the sell down to a quantity step leaves behind) are not
  part of this trade: they stay in the account and are not booked as a loss.
* ``buy_fee`` is the buy's fee in USD, in whatever asset it was paid (the coin bought, BNB,
  USDT), counted for the share of the bought coins that were sold. ``sell_fee`` is the sell's.
* ``fees_usd`` is ``buy_fee + sell_fee``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal(0)
ONE = Decimal(1)
EIGHT_PLACES = Decimal("1e-8")


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


@dataclass(frozen=True)
class TradeResult:
    symbol: str
    amount_operated: Decimal  # quantity sold
    buy_price: Decimal
    sell_price: Decimal
    total_buy: Decimal  # amount_operated x buy_price
    total_sell: Decimal  # amount_operated x sell_price
    buy_fee_usd: Decimal  # the buy's fee, for the part of the bought coins that was sold
    sell_fee_usd: Decimal
    net_profit: Decimal
    buy_time: datetime
    sell_time: datetime

    @property
    def fees_usd(self) -> Decimal:
        return self.buy_fee_usd + self.sell_fee_usd

    @property
    def return_pct(self) -> Decimal:
        """Net return on the cost of the coins sold, as a fraction (0.04 = 4%)."""
        return self.net_profit / self.total_buy if self.total_buy else ZERO


def compute_trade_result(
    *,
    symbol: str,
    buy_qty: Decimal,
    buy_price: Decimal,
    buy_coin_fee: Decimal,
    sell_qty: Decimal,
    sell_price: Decimal,
    buy_fee_value: Decimal,
    sell_fee_value: Decimal,
    buy_time: datetime,
    sell_time: datetime,
) -> TradeResult:
    """`buy_coin_fee` is the part of the buy's fee paid in the coin itself (0 for BNB or USDT)."""
    received = buy_qty - buy_coin_fee
    # The share of the bought coins that this sale covers. Over 100% happens when a leftover
    # from an earlier trade was swept in; the extra coins are costed at this buy price.
    sold_share = min(ONE, sell_qty / received) if received > 0 else ZERO
    buy_fee_usd = (buy_fee_value * sold_share).quantize(EIGHT_PLACES, rounding=ROUND_HALF_UP)
    total_buy = sell_qty * buy_price
    total_sell = sell_qty * sell_price
    return TradeResult(
        symbol=symbol,
        amount_operated=sell_qty,
        buy_price=buy_price,
        sell_price=sell_price,
        total_buy=total_buy,
        total_sell=total_sell,
        buy_fee_usd=buy_fee_usd,
        sell_fee_usd=sell_fee_value,
        net_profit=total_sell - total_buy - buy_fee_usd - sell_fee_value,
        buy_time=buy_time,
        sell_time=sell_time,
    )
