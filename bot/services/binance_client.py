"""Binance Spot REST wrapper.

The only module that talks to Binance's REST API. Everything above it works with
the small `Decimal` dataclasses defined here, never with raw Binance dicts.

Rules enforced at this lowest layer, so no caller can bypass them:
  * Every order is checked against the symbol's filters first (`precision.py`).
  * `dry_run` never reaches an order endpoint: order methods return a synthetic
    result instead of sending anything.
  * Prices and quantities are sent as plain decimal strings, never floats.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from binance import AsyncClient
from binance.exceptions import BinanceAPIException

from bot.config.precision import FilterViolation, SymbolFilters, format_decimal, to_decimal
from bot.config.settings import Settings
from bot.database.models import OrderStatus

log = logging.getLogger(__name__)

ZERO = Decimal(0)
MAX_CLIENT_ID_LEN = 36
OCO_SUFFIX_LEN = 3  # "-tp" / "-sl"

# Binance error codes we react to.
ERR_UNKNOWN_ORDER = -2011  # cancel of an order that does not exist
ERR_ORDER_DOES_NOT_EXIST = -2013  # query of an order that does not exist

# Binance order status -> ours. None means "no change": the order is still live.
_STATUS_MAP: dict[str, OrderStatus | None] = {
    "NEW": OrderStatus.NEW,
    "PENDING_NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
    "REJECTED": OrderStatus.REJECTED,
    "PENDING_CANCEL": None,
}


# Codes meaning "Binance looked at this order and refused it": nothing was placed.
# -2010 includes "relationship of the prices for the orders is not correct" (an OCO whose
# take-profit is already below the market). Any other failure (timeout, 5xx, -1021) leaves
# the outcome unknown: reconcile by client order id before assuming anything.
REJECTION_CODES = frozenset({-2010, -1013, -1111, -1100, -1102, -1106, -1116, -1117, -1121})


class ExchangeError(RuntimeError):
    """A Binance request failed in a way the caller should handle."""

    def __init__(self, message: str = "", code: int | None = None):
        super().__init__(message)
        self.code = code


class OrderRejected(ExchangeError):
    """Binance refused to place the order; nothing exists on the exchange."""


class OrderNotFound(ExchangeError):
    """Binance has no such order (-2013 / -2011)."""


def to_order_status(raw: str) -> OrderStatus | None:
    """Map a Binance status string; unknown values raise rather than being guessed."""
    try:
        return _STATUS_MAP[raw]
    except KeyError:
        raise ExchangeError(f"unknown Binance order status {raw!r}") from None


@dataclass(frozen=True)
class ExchangeOrder:
    symbol: str
    order_id: int | None  # None for dry-run orders
    client_order_id: str
    order_list_id: int | None
    side: str
    order_type: str
    status: str  # raw Binance status
    price: Decimal
    stop_price: Decimal | None
    orig_qty: Decimal
    executed_qty: Decimal
    cumulative_quote_qty: Decimal

    @classmethod
    def from_binance(cls, d: dict[str, Any]) -> ExchangeOrder:
        list_id = d.get("orderListId", -1)
        stop = to_decimal(d.get("stopPrice", "0"))
        return cls(
            symbol=d["symbol"],
            order_id=int(d["orderId"]),
            client_order_id=d["clientOrderId"],
            order_list_id=None if list_id in (None, -1) else int(list_id),
            side=d["side"],
            order_type=d["type"],
            status=d["status"],
            price=to_decimal(d.get("price", "0")),
            stop_price=stop if stop > ZERO else None,
            orig_qty=to_decimal(d["origQty"]),
            executed_qty=to_decimal(d.get("executedQty", "0")),
            cumulative_quote_qty=to_decimal(d.get("cummulativeQuoteQty", "0")),
        )


@dataclass(frozen=True)
class Fill:
    """One execution of an order (a row of `myTrades`)."""

    trade_id: int
    order_id: int
    price: Decimal
    quantity: Decimal
    quote_quantity: Decimal
    commission: Decimal
    commission_asset: str
    time: datetime
    is_maker: bool

    @classmethod
    def from_binance(cls, d: dict[str, Any]) -> Fill:
        return cls(
            trade_id=int(d["id"]),
            order_id=int(d["orderId"]),
            price=to_decimal(d["price"]),
            quantity=to_decimal(d["qty"]),
            quote_quantity=to_decimal(d["quoteQty"]),
            commission=to_decimal(d["commission"]),
            commission_asset=d["commissionAsset"],
            time=datetime.fromtimestamp(int(d["time"]) / 1000, tz=UTC),
            is_maker=bool(d.get("isMaker", False)),
        )


@dataclass(frozen=True)
class BookTicker:
    symbol: str
    bid: Decimal
    ask: Decimal

    @property
    def spread_pct(self) -> Decimal:
        """(ask - bid) / mid price, as a fraction (0.002 = 0.2%)."""
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid if mid > ZERO else Decimal(1)


@dataclass(frozen=True)
class TradeFee:
    """Standard account commission rates as fractions (0.001 = 0.1%); BNB discounts not applied."""

    maker: Decimal
    taker: Decimal


@dataclass(frozen=True)
class OcoResult:
    order_list_id: int | None  # None in dry-run
    list_client_order_id: str
    take_profit: ExchangeOrder
    stop_loss: ExchangeOrder


class ExchangeClient:
    def __init__(
        self,
        client: AsyncClient,
        *,
        dry_run: bool,
        order_id_prefix: str = "bot",
        has_credentials: bool = True,
    ):
        self._client = client
        self.dry_run = dry_run
        self.order_id_prefix = order_id_prefix
        self._has_credentials = has_credentials
        self._filters: dict[str, SymbolFilters] = {}

    @classmethod
    async def create(cls, settings: Settings) -> ExchangeClient:
        """Public data needs no keys. Signed calls raise a clear error when keys are missing."""
        key = settings.binance_api_key.get_secret_value() or None
        secret = settings.binance_api_secret.get_secret_value() or None
        client = await AsyncClient.create(
            api_key=key,
            api_secret=None if settings.binance_private_key_path else secret,
            testnet=settings.binance_testnet,
            private_key=settings.binance_private_key_path,
        )
        return cls(
            client,
            dry_run=settings.dry_run,
            order_id_prefix=settings.order_id_prefix,
            has_credentials=settings.has_binance_credentials,
        )

    async def close(self) -> None:
        await self._client.close_connection()

    @property
    def raw(self) -> AsyncClient:
        """The underlying client, for the user-data stream manager."""
        return self._client

    def new_client_order_id(self, reserve: int = 0) -> str:
        """`<prefix>-<hex>`, at most 36 chars (Binance allows [A-Za-z0-9-_]{1,36}).

        `reserve` leaves room for a suffix the caller will append (the OCO legs add `-tp`/`-sl`).
        """
        hex_len = MAX_CLIENT_ID_LEN - len(self.order_id_prefix) - 1 - reserve
        return f"{self.order_id_prefix}-{uuid.uuid4().hex[:hex_len]}"

    def owns(self, client_order_id: str) -> bool:
        return client_order_id.startswith(f"{self.order_id_prefix}-")

    # --- public market data (no keys) ------------------------------------------------------

    async def symbol_filters(self, symbol: str) -> SymbolFilters:
        """Cached for the life of the process. Filters rarely change; restart refreshes them."""
        if symbol not in self._filters:
            info = await self._client.get_symbol_info(symbol)
            if info is None:
                raise ExchangeError(f"unknown symbol {symbol}")
            if info.get("status") != "TRADING":
                raise ExchangeError(f"{symbol} is not trading (status {info.get('status')})")
            self._filters[symbol] = SymbolFilters.from_symbol_info(info)
        return self._filters[symbol]

    async def avg_price(self, symbol: str) -> Decimal:
        """Binance's rolling average price, the reference for the PERCENT_PRICE band."""
        return to_decimal((await self._client.get_avg_price(symbol=symbol))["price"])

    async def book_ticker(self, symbol: str) -> BookTicker:
        d = await self._client.get_orderbook_ticker(symbol=symbol)
        return BookTicker(symbol, to_decimal(d["bidPrice"]), to_decimal(d["askPrice"]))

    # --- account (signed) ------------------------------------------------------------------

    async def free_balance(self, asset: str) -> Decimal:
        self._need_credentials()
        d = await self._client.get_asset_balance(asset=asset)
        return to_decimal(d["free"]) if d else ZERO

    async def account_fees(self) -> TradeFee:
        """The account's maker/taker rates, from `/api/v3/account` (`commissionRates`).

        Deliberately not python-binance's `get_trade_fee`: that is a `/sapi` endpoint, and
        the library has no testnet routing for `/sapi`, so with `testnet=True` it would send
        the key to the real exchange. Never use `/sapi` helpers from this client.
        """
        self._need_credentials()
        rates = (await self._client.get_account()).get("commissionRates")
        if not rates:
            raise ExchangeError("account response has no commissionRates")
        return TradeFee(to_decimal(rates["maker"]), to_decimal(rates["taker"]))

    # --- orders ----------------------------------------------------------------------------

    async def place_limit_buy(
        self,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        client_order_id = client_order_id or self.new_client_order_id()
        filters = await self.symbol_filters(symbol)
        filters.validate_order("BUY", price, quantity, avg_price=await self.avg_price(symbol))

        if self.dry_run:
            log.info("DRY_RUN buy %s %s @ %s (%s): not sent", symbol, quantity, price, client_order_id)
            return self._synthetic(symbol, client_order_id, "BUY", "LIMIT", price, None, quantity)

        self._need_credentials()
        d = await self._send(
            self._client.create_order(
                symbol=symbol,
                side="BUY",
                type="LIMIT",
                timeInForce="GTC",
                quantity=format_decimal(quantity),
                price=format_decimal(price),
                newClientOrderId=client_order_id,
                newOrderRespType="RESULT",
            )
        )
        return ExchangeOrder.from_binance(d)

    async def place_oco_sell(
        self,
        symbol: str,
        quantity: Decimal,
        take_profit_price: Decimal,
        stop_price: Decimal,
        stop_limit_price: Decimal,
        list_client_order_id: str | None = None,
    ) -> OcoResult:
        """Take-profit limit above, stop-loss-limit below, sharing one quantity.

        Uses `POST /api/v3/orderList/oco` (the old `/order/oco` is deprecated).
        The take-profit is a LIMIT_MAKER: Binance rejects it if it would fill
        immediately, i.e. when the market is already at or above the target.
        """
        if not stop_limit_price <= stop_price < take_profit_price:
            raise ExchangeError(
                "OCO prices must satisfy stop_limit <= stop < take_profit "
                f"(got {stop_limit_price} / {stop_price} / {take_profit_price})"
            )
        base = list_client_order_id or self.new_client_order_id(reserve=OCO_SUFFIX_LEN)
        if len(base) + OCO_SUFFIX_LEN > MAX_CLIENT_ID_LEN:
            raise ExchangeError(f"list client order id {base!r} is too long to add leg suffixes")
        tp_id, sl_id = f"{base}-tp", f"{base}-sl"

        filters = await self.symbol_filters(symbol)
        avg = await self.avg_price(symbol)
        problems = filters.violations("SELL", take_profit_price, quantity, avg)
        problems += filters.violations("SELL", stop_limit_price, quantity, avg)
        if (stop_price - filters.min_price) % filters.tick_size != ZERO:
            problems.append(f"PRICE_FILTER: stop price {stop_price} not a multiple of tick")
        if problems:
            raise FilterViolation(problems)

        if self.dry_run:
            log.info(
                "DRY_RUN oco %s %s tp=%s stop=%s/%s (%s): not sent",
                symbol, quantity, take_profit_price, stop_price, stop_limit_price, base,
            )
            return OcoResult(
                None,
                base,
                self._synthetic(symbol, tp_id, "SELL", "LIMIT_MAKER", take_profit_price, None, quantity),
                self._synthetic(
                    symbol, sl_id, "SELL", "STOP_LOSS_LIMIT", stop_limit_price, stop_price, quantity
                ),
            )

        self._need_credentials()
        d = await self._send(
            self._client.create_oco_order(
                symbol=symbol,
                side="SELL",
                quantity=format_decimal(quantity),
                listClientOrderId=base,
                aboveType="LIMIT_MAKER",
                abovePrice=format_decimal(take_profit_price),
                aboveClientOrderId=tp_id,
                belowType="STOP_LOSS_LIMIT",
                belowStopPrice=format_decimal(stop_price),
                belowPrice=format_decimal(stop_limit_price),
                belowTimeInForce="GTC",
                belowClientOrderId=sl_id,
                newOrderRespType="RESULT",
            )
        )
        reports = {r["clientOrderId"]: ExchangeOrder.from_binance(r) for r in d["orderReports"]}
        try:
            return OcoResult(int(d["orderListId"]), base, reports[tp_id], reports[sl_id])
        except KeyError as exc:
            raise ExchangeError(f"OCO response is missing leg {exc}: {d!r}") from exc

    async def cancel_order(
        self, symbol: str, *, order_id: int | None = None, client_order_id: str | None = None
    ) -> ExchangeOrder | None:
        """Cancel one order. Returns None if Binance no longer knows it (already gone).

        Cancelling either leg of an OCO pair cancels **both** (checked on the testnet:
        Binance answers with the whole list, and the list goes to ALL_DONE). The order
        returned is the leg you asked for.
        """
        if self.dry_run:
            log.info("DRY_RUN cancel %s %s: not sent", symbol, order_id or client_order_id)
            return None
        self._need_credentials()
        try:
            d = await self._client.cancel_order(symbol=symbol, **self._order_ref(order_id, client_order_id))
        except BinanceAPIException as exc:
            if exc.code == ERR_UNKNOWN_ORDER:
                return None
            raise ExchangeError(str(exc), exc.code) from exc
        if "orderId" not in d:  # an OCO leg: Binance replies with the whole order list
            reports = d.get("orderReports") or []
            wanted = next(
                (r for r in reports if r.get("orderId") == order_id or r.get("clientOrderId") == client_order_id),
                reports[0] if reports else None,
            )
            if wanted is None:
                raise ExchangeError(f"cancel response has no order reports: {d!r}")
            d = wanted
        return ExchangeOrder.from_binance(d)

    async def get_order(
        self, symbol: str, *, order_id: int | None = None, client_order_id: str | None = None
    ) -> ExchangeOrder:
        self._need_credentials()
        try:
            d = await self._client.get_order(symbol=symbol, **self._order_ref(order_id, client_order_id))
        except BinanceAPIException as exc:
            if exc.code == ERR_ORDER_DOES_NOT_EXIST:
                raise OrderNotFound(f"{symbol} {order_id or client_order_id}") from exc
            raise ExchangeError(str(exc), exc.code) from exc
        return ExchangeOrder.from_binance(d)

    async def open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        self._need_credentials()
        params = {"symbol": symbol} if symbol else {}
        return [ExchangeOrder.from_binance(d) for d in await self._client.get_open_orders(**params)]

    async def order_fills(self, symbol: str, order_id: int) -> list[Fill]:
        self._need_credentials()
        rows = await self._client.get_my_trades(symbol=symbol, orderId=order_id)
        return [Fill.from_binance(r) for r in rows]

    # --- helpers ---------------------------------------------------------------------------

    @staticmethod
    async def _send(call) -> dict[str, Any]:
        """Await an order-placing call, turning Binance's refusals into `OrderRejected`."""
        try:
            return await call
        except BinanceAPIException as exc:
            if exc.code in REJECTION_CODES:
                raise OrderRejected(f"order rejected (code {exc.code}): {exc.message}", exc.code) from exc
            raise ExchangeError(str(exc), exc.code) from exc

    def _need_credentials(self) -> None:
        if not self._has_credentials:
            raise ExchangeError("this call needs BINANCE_API_KEY and BINANCE_API_SECRET")

    @staticmethod
    def _order_ref(order_id: int | None, client_order_id: str | None) -> dict[str, Any]:
        if order_id is not None:
            return {"orderId": order_id}
        if client_order_id:
            return {"origClientOrderId": client_order_id}
        raise ValueError("order_id or client_order_id is required")

    @staticmethod
    def _synthetic(
        symbol: str,
        client_order_id: str,
        side: str,
        order_type: str,
        price: Decimal,
        stop_price: Decimal | None,
        quantity: Decimal,
    ) -> ExchangeOrder:
        return ExchangeOrder(
            symbol=symbol,
            order_id=None,
            client_order_id=client_order_id,
            order_list_id=None,
            side=side,
            order_type=order_type,
            status="NEW",
            price=price,
            stop_price=stop_price,
            orig_qty=quantity,
            executed_qty=ZERO,
            cumulative_quote_qty=ZERO,
        )
