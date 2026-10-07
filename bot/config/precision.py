"""Binance price / quantity filter handling.

Binance rejects any order that breaks a symbol filter, so every price and
quantity is rounded and checked here before it is sent. All maths is `Decimal`.

Filter shapes were checked against the live `/api/v3/exchangeInfo` (mainnet and
testnet) on 2026-10-06: PRICE_FILTER, LOT_SIZE, NOTIONAL and
PERCENT_PRICE_BY_SIDE. The older MIN_NOTIONAL and PERCENT_PRICE names are
also understood. A filter whose size is zero is disabled, as on Binance.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

ZERO = Decimal(0)


class FilterViolation(ValueError):
    """An order would be rejected by one or more exchange filters."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


def to_decimal(value: Any) -> Decimal:
    """Build a Decimal from a Binance string or an int. Floats are refused on purpose."""
    if isinstance(value, float):
        raise TypeError("floats are not allowed for money; pass a str or Decimal")
    return Decimal(str(value))


def round_to_step(value: Decimal, step: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    """Round `value` to a whole multiple of `step` (down by default)."""
    if step <= ZERO:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


def round_price(price: Decimal, tick_size: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    return round_to_step(price, tick_size, rounding)


def round_quantity(quantity: Decimal, step_size: Decimal) -> Decimal:
    """Always rounds down: asking to sell or buy more than we have or can afford fails."""
    return round_to_step(quantity, step_size, ROUND_DOWN)


def format_decimal(value: Decimal) -> str:
    """Plain decimal string for the API: no exponent, no trailing zeros."""
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


@dataclass(frozen=True)
class PercentPriceBand:
    """Allowed distance from the 5-minute average price, per order side."""

    bid_up: Decimal
    bid_down: Decimal
    ask_up: Decimal
    ask_down: Decimal


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal
    price_band: PercentPriceBand | None
    oco_allowed: bool

    @classmethod
    def from_symbol_info(cls, info: dict[str, Any]) -> SymbolFilters:
        """Build from one symbol entry of exchangeInfo / `client.get_symbol_info(symbol)`."""
        filters = {f["filterType"]: f for f in info["filters"]}
        price = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}

        band = None
        if "PERCENT_PRICE_BY_SIDE" in filters:
            f = filters["PERCENT_PRICE_BY_SIDE"]
            band = PercentPriceBand(
                bid_up=to_decimal(f["bidMultiplierUp"]),
                bid_down=to_decimal(f["bidMultiplierDown"]),
                ask_up=to_decimal(f["askMultiplierUp"]),
                ask_down=to_decimal(f["askMultiplierDown"]),
            )
        elif "PERCENT_PRICE" in filters:
            f = filters["PERCENT_PRICE"]
            up, down = to_decimal(f["multiplierUp"]), to_decimal(f["multiplierDown"])
            band = PercentPriceBand(bid_up=up, bid_down=down, ask_up=up, ask_down=down)

        return cls(
            symbol=info["symbol"],
            base_asset=info["baseAsset"],
            quote_asset=info["quoteAsset"],
            tick_size=to_decimal(price["tickSize"]),
            min_price=to_decimal(price["minPrice"]),
            max_price=to_decimal(price["maxPrice"]),
            step_size=to_decimal(lot["stepSize"]),
            min_qty=to_decimal(lot["minQty"]),
            max_qty=to_decimal(lot["maxQty"]),
            min_notional=to_decimal(notional.get("minNotional", "0")),
            max_notional=to_decimal(notional.get("maxNotional", "0")),
            price_band=band,
            oco_allowed=bool(info.get("ocoAllowed", False)),
        )

    def round_price(self, price: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
        return round_price(price, self.tick_size, rounding)

    def round_quantity(self, quantity: Decimal) -> Decimal:
        return round_quantity(quantity, self.step_size)

    def violations(
        self,
        side: str,
        price: Decimal,
        quantity: Decimal,
        avg_price: Decimal | None = None,
    ) -> list[str]:
        """Every filter this limit order would break; empty means it is acceptable.

        `avg_price` is Binance's 5-minute average (`client.get_avg_price`). Without
        it the price-band check is skipped, so pass it for real orders.
        """
        problems: list[str] = []

        if self.tick_size > ZERO:
            if price < self.min_price:
                problems.append(f"PRICE_FILTER: price {price} below minPrice {self.min_price}")
            if self.max_price > ZERO and price > self.max_price:
                problems.append(f"PRICE_FILTER: price {price} above maxPrice {self.max_price}")
            if (price - self.min_price) % self.tick_size != ZERO:
                problems.append(f"PRICE_FILTER: price {price} not a multiple of tick {self.tick_size}")

        if self.step_size > ZERO:
            if quantity < self.min_qty:
                problems.append(f"LOT_SIZE: quantity {quantity} below minQty {self.min_qty}")
            if self.max_qty > ZERO and quantity > self.max_qty:
                problems.append(f"LOT_SIZE: quantity {quantity} above maxQty {self.max_qty}")
            if (quantity - self.min_qty) % self.step_size != ZERO:
                problems.append(
                    f"LOT_SIZE: quantity {quantity} not a multiple of step {self.step_size}"
                )

        value = price * quantity
        if self.min_notional > ZERO and value < self.min_notional:
            problems.append(f"NOTIONAL: order value {value} below minimum {self.min_notional}")
        if self.max_notional > ZERO and value > self.max_notional:
            problems.append(f"NOTIONAL: order value {value} above maximum {self.max_notional}")

        if self.price_band is not None and avg_price is not None:
            is_buy = side.upper() == "BUY"
            up = self.price_band.bid_up if is_buy else self.price_band.ask_up
            down = self.price_band.bid_down if is_buy else self.price_band.ask_down
            low, high = avg_price * down, avg_price * up
            if not low <= price <= high:
                problems.append(
                    f"PERCENT_PRICE: {side.upper()} price {price} outside {low}..{high} "
                    f"(5-min average {avg_price})"
                )

        return problems

    def validate_order(
        self,
        side: str,
        price: Decimal,
        quantity: Decimal,
        avg_price: Decimal | None = None,
    ) -> None:
        problems = self.violations(side, price, quantity, avg_price)
        if problems:
            raise FilterViolation(problems)
