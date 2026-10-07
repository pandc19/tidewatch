"""A stand-in for python-binance's AsyncClient that returns Binance-shaped dicts.

Records every call so tests can assert what was (or was not) sent.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from binance.exceptions import BinanceAPIException

SOL_INFO: dict[str, Any] = {
    "symbol": "SOLUSDT",
    "baseAsset": "SOL",
    "quoteAsset": "USDT",
    "status": "TRADING",
    "ocoAllowed": True,
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01000000", "maxPrice": "10000.00000000", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "minQty": "0.00100000", "maxQty": "90000.00000000", "stepSize": "0.00100000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyMinToMarket": True,
         "maxNotional": "9000000.00000000", "applyMaxToMarket": False, "avgPriceMins": 5},
        {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "1.2", "bidMultiplierDown": "0.5",
         "askMultiplierUp": "2", "askMultiplierDown": "0.8", "avgPriceMins": 5},
    ],
}


class _Response:
    def __init__(self, text: str):
        self.text = text
        self.request = None


def api_error(code: int, msg: str = "error") -> BinanceAPIException:
    text = f'{{"code": {code}, "msg": "{msg}"}}'
    return BinanceAPIException(_Response(text), 400, text)


def order_dict(
    order_id: int,
    client_id: str,
    *,
    symbol: str = "SOLUSDT",
    side: str = "BUY",
    type_: str = "LIMIT",
    status: str = "NEW",
    price: str = "120.41",
    stop_price: str = "0.00000000",
    qty: str = "0.130",
    executed: str = "0.000",
    quote: str = "0.00000000",
    list_id: int = -1,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "orderId": order_id,
        "orderListId": list_id,
        "clientOrderId": client_id,
        "price": price,
        "stopPrice": stop_price,
        "origQty": qty,
        "executedQty": executed,
        "cummulativeQuoteQty": quote,
        "status": status,
        "type": type_,
        "side": side,
    }


def trade_dict(trade_id: int, order_id: int, *, price: str, qty: str, commission: str, asset: str) -> dict[str, Any]:
    quote = str(__import__("decimal").Decimal(price) * __import__("decimal").Decimal(qty))
    return {
        "id": trade_id,
        "orderId": order_id,
        "price": price,
        "qty": qty,
        "quoteQty": quote,
        "commission": commission,
        "commissionAsset": asset,
        "time": 1_790_000_000_000,
        "isMaker": True,
    }


class FakeBinance:
    """Stateful: orders it accepts persist, fills and cancels move balances, OCO legs are linked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.symbol_info: dict[str, Any] | None = SOL_INFO
        self.avg = "120.00000000"
        self.bid, self.ask = "120.41000000", "120.42000000"
        self.balances: dict[str, str] = {"USDT": "16.00000000"}
        self.orders: dict[int, dict[str, Any]] = {}
        self.trades: dict[int, list[dict[str, Any]]] = {}
        self.open: list[dict[str, Any]] | None = None  # None: computed from live orders
        self.raise_on: dict[str, Exception] = {}
        self.raise_once: dict[str, list[Exception]] = {}  # consumed one per call
        self.fee_rate = Decimal("0.001")
        self.fill_on_create = False  # like the testnet: a buy at the ask fills at once
        self._next_id, self._next_leg, self._next_list = 1000, 2000, 54

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    def _rec(self, name: str, kw: dict[str, Any]) -> None:
        self.calls.append((name, kw))
        if self.raise_once.get(name):
            raise self.raise_once[name].pop(0)
        if name in self.raise_on:
            raise self.raise_on[name]

    # --- test controls ---------------------------------------------------------------------

    def balance(self, asset: str) -> Decimal:
        return Decimal(self.balances.get(asset, "0"))

    def _add(self, asset: str, amount: Decimal) -> None:
        self.balances[asset] = str(self.balance(asset) + amount)

    def fill(
        self, order_id: int, *, price: str | None = None, qty: str | None = None,
        fee: str | None = None, fee_asset: str | None = None,
    ) -> None:
        """Fill (or partly fill) an order, move balances, record the trade, update OCO siblings."""
        o = self.orders[order_id]
        base = o["symbol"].removesuffix("USDT")
        price_d = Decimal(price or o["price"])
        qty_d = Decimal(qty or o["origQty"])
        already = Decimal(o["executedQty"])
        total = already + qty_d
        o["executedQty"] = str(total)
        o["cummulativeQuoteQty"] = str(Decimal(o["cummulativeQuoteQty"]) + qty_d * price_d)
        done = total >= Decimal(o["origQty"])
        o["status"] = "FILLED" if done else "PARTIALLY_FILLED"
        if o["side"] == "BUY":
            fee_asset = fee_asset or base
            fee_d = Decimal(fee) if fee is not None else (qty_d * self.fee_rate if fee_asset == base else qty_d * price_d * self.fee_rate)
            self._add("USDT", -qty_d * price_d)
            self._add(base, qty_d - (fee_d if fee_asset == base else 0))
            if fee_asset != base:
                self._add(fee_asset, -fee_d)
        else:
            fee_asset = fee_asset or "USDT"
            fee_d = Decimal(fee) if fee is not None else qty_d * price_d * self.fee_rate
            self._add(base, -qty_d)
            self._add("USDT", qty_d * price_d - (fee_d if fee_asset == "USDT" else 0))
        tid = 10_000 + sum(len(v) for v in self.trades.values()) + 1
        self.trades.setdefault(order_id, []).append(
            trade_dict(tid, order_id, price=str(price_d), qty=str(qty_d), commission=str(fee_d), asset=fee_asset)
        )
        if done and o["orderListId"] != -1:  # the other OCO leg is cancelled by the exchange
            self._cancel_list(o["orderListId"], except_id=order_id, status=self.counterpart_status)

    counterpart_status = "CANCELED"  # Binance's wording for the losing OCO leg is not confirmed

    def cancel_externally(self, order_id: int) -> None:
        """Someone cancels in the Binance app: no bot involved."""
        o = self.orders[order_id]
        if o["orderListId"] != -1:
            self._cancel_list(o["orderListId"], status="CANCELED")
        else:
            o["status"] = "CANCELED"

    def _cancel_list(self, list_id: int, *, status: str = "CANCELED", except_id: int | None = None) -> None:
        for oid, o in self.orders.items():
            if o["orderListId"] == list_id and oid != except_id and o["status"] in ("NEW", "PARTIALLY_FILLED"):
                o["status"] = status

    # --- public
    async def get_symbol_info(self, symbol):
        self._rec("get_symbol_info", {"symbol": symbol})
        return self.symbol_info

    async def get_avg_price(self, **kw):
        self._rec("get_avg_price", kw)
        return {"mins": 5, "price": self.avg}

    async def get_orderbook_ticker(self, **kw):
        self._rec("get_orderbook_ticker", kw)
        return {"symbol": kw["symbol"], "bidPrice": self.bid, "askPrice": self.ask}

    # --- account
    async def get_asset_balance(self, **kw):
        self._rec("get_asset_balance", kw)
        free = self.balances.get(kw["asset"])
        return None if free is None else {"asset": kw["asset"], "free": free, "locked": "0.00000000"}

    async def get_account(self, **kw):
        self._rec("get_account", kw)
        return {
            "makerCommission": 10,
            "takerCommission": 10,
            "commissionRates": {"maker": "0.00100000", "taker": "0.00100000",
                                "buyer": "0.00000000", "seller": "0.00000000"},
            "balances": [],
        }

    async def get_trade_fee(self, **kw):  # a /sapi endpoint: must never be called (see the test)
        raise AssertionError("get_trade_fee is a /sapi call and would bypass the testnet")

    # --- orders
    async def create_order(self, **kw):
        self._rec("create_order", kw)
        self._next_id += 1
        o = order_dict(
            self._next_id, kw["newClientOrderId"], symbol=kw["symbol"], side=kw["side"], type_=kw["type"],
            price=kw["price"], qty=kw["quantity"],
        )
        self.orders[self._next_id] = o
        if self.fill_on_create:
            self.fill(self._next_id)
        return dict(o)

    async def create_oco_order(self, **kw):
        self._rec("create_oco_order", kw)
        self._next_list += 1
        self._next_leg += 1
        tp = order_dict(self._next_leg, kw["aboveClientOrderId"], side="SELL", type_="LIMIT_MAKER",
                        price=kw["abovePrice"], qty=kw["quantity"], list_id=self._next_list)
        self._next_leg += 1
        sl = order_dict(self._next_leg, kw["belowClientOrderId"], side="SELL", type_="STOP_LOSS_LIMIT",
                        price=kw["belowPrice"], stop_price=kw["belowStopPrice"], qty=kw["quantity"],
                        list_id=self._next_list)
        self.orders[tp["orderId"]], self.orders[sl["orderId"]] = tp, sl
        return {"orderListId": self._next_list, "listClientOrderId": kw["listClientOrderId"],
                "orderReports": [dict(tp), dict(sl)]}

    async def cancel_order(self, **kw):
        self._rec("cancel_order", kw)
        o = self.orders.get(kw.get("orderId"))
        if o is None:
            return order_dict(kw.get("orderId", 1), "x", status="CANCELED")
        if o["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise api_error(-2011, "Unknown order sent.")  # what Binance says for a finished order
        if o["orderListId"] != -1:  # cancelling one OCO leg cancels both; the reply is the list
            self._cancel_list(o["orderListId"])
            legs = [dict(x) for x in self.orders.values() if x["orderListId"] == o["orderListId"]]
            return {"orderListId": o["orderListId"], "listStatusType": "ALL_DONE",
                    "listOrderStatus": "ALL_DONE", "orders": [], "orderReports": legs}
        o["status"] = "CANCELED"
        return dict(o)

    async def get_order(self, **kw):
        self._rec("get_order", kw)
        if "orderId" in kw:
            found = self.orders.get(kw["orderId"])
        else:
            found = next((o for o in self.orders.values() if o["clientOrderId"] == kw["origClientOrderId"]), None)
        if found is None:
            raise api_error(-2013, "Order does not exist.")
        return dict(found)

    async def get_open_orders(self, **kw):
        self._rec("get_open_orders", kw)
        if self.open is not None:
            return self.open
        return [dict(o) for o in self.orders.values() if o["status"] in ("NEW", "PARTIALLY_FILLED")]

    async def get_my_trades(self, **kw):
        self._rec("get_my_trades", kw)
        return self.trades.get(kw["orderId"], [])

    async def close_connection(self):
        self._rec("close_connection", {})
