"""Risk rules.

Phase 3 provides only entry sizing, which the engine needs to place a buy. The
kill-switch, spread filter and startup checks arrive in phase 4.
"""

from __future__ import annotations

from decimal import Decimal

from bot.config.precision import SymbolFilters

# Assumed worst-case fee taken in the coin on a buy (0.1% is Binance's standard rate;
# doubled for margin). The sell must still clear the minimum order value after it.
FEE_BUFFER = Decimal("0.002")


def size_entry(
    free_quote: Decimal,
    allocation_pct: Decimal,
    price: Decimal,
    filters: SymbolFilters,
    *,
    stop_loss_pct: Decimal,
    stop_limit_offset_pct: Decimal,
) -> Decimal | None:
    """Quantity to buy with `allocation_pct` of the free quote balance, or None.

    None means no valid trade fits: the quantity rounds below Binance's minimum, or the
    position could not be *protected*. The stop-loss leg sells a little less (after the
    fee) at a lower price, so its value must still reach the minimum order value;
    otherwise the OCO would be refused and the coins left unprotected.
    """
    if free_quote <= 0 or price <= 0:
        return None
    quantity = filters.round_quantity(free_quote * allocation_pct / price)
    if quantity < filters.min_qty or quantity * price < filters.min_notional:
        return None

    worst_case_sellable = filters.round_quantity(quantity * (1 - FEE_BUFFER))
    stop_limit_price = filters.round_price(
        price * (1 - stop_loss_pct) * (1 - stop_limit_offset_pct)
    )
    if worst_case_sellable < filters.min_qty or worst_case_sellable * stop_limit_price < filters.min_notional:
        return None
    return quantity
