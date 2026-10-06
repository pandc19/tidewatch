"""A stand-in for python-binance's AsyncClient that returns Binance-shaped dicts.

Records every call so tests can assert what was (or was not) sent.
"""

from __future__ import annotations

from typing import Any

from binance.exceptions import BinanceAPIException

SOL_INFO: dict[str, Any] = {
    "symbol": "SOLUSDT",
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
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.symbol_info: dict[str, Any] | None = SOL_INFO
        self.avg = "120.00000000"
        self.bid, self.ask = "120.41000000", "120.42000000"
        self.balances = {"USDT": "16.00000000"}
        self.orders: dict[int, dict[str, Any]] = {}
        self.trades: dict[int, list[dict[str, Any]]] = {}
        self.open: list[dict[str, Any]] = []
        self.raise_on: dict[str, Exception] = {}
        self.oco_leg_ids: tuple[int, ...] = ()

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    def _rec(self, name: str, kw: dict[str, Any]) -> None:
        self.calls.append((name, kw))
        if name in self.raise_on:
            raise self.raise_on[name]

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
        return order_dict(
            1001, kw["newClientOrderId"], symbol=kw["symbol"], side=kw["side"], type_=kw["type"],
            price=kw["price"], qty=kw["quantity"],
        )

    async def create_oco_order(self, **kw):
        self._rec("create_oco_order", kw)
        legs = [
            order_dict(2001, kw["aboveClientOrderId"], side="SELL", type_="LIMIT_MAKER",
                       price=kw["abovePrice"], qty=kw["quantity"], list_id=55),
            order_dict(2002, kw["belowClientOrderId"], side="SELL", type_="STOP_LOSS_LIMIT",
                       price=kw["belowPrice"], stop_price=kw["belowStopPrice"], qty=kw["quantity"], list_id=55),
        ]
        return {"orderListId": 55, "listClientOrderId": kw["listClientOrderId"], "orderReports": legs}

    async def cancel_order(self, **kw):
        self._rec("cancel_order", kw)
        if kw.get("orderId") in self.oco_leg_ids:  # an OCO leg: Binance answers with the whole list
            legs = [order_dict(i, f"leg-{i}", status="CANCELED", list_id=55) for i in self.oco_leg_ids]
            return {"orderListId": 55, "listStatusType": "ALL_DONE", "listOrderStatus": "ALL_DONE",
                    "orders": [], "orderReports": legs}
        return order_dict(kw.get("orderId", 1), "x", status="CANCELED")

    async def get_order(self, **kw):
        self._rec("get_order", kw)
        if "orderId" in kw:
            found = self.orders.get(kw["orderId"])
        else:
            found = next((o for o in self.orders.values() if o["clientOrderId"] == kw["origClientOrderId"]), None)
        if found is None:
            raise api_error(-2013, "Order does not exist.")
        return found

    async def get_open_orders(self, **kw):
        self._rec("get_open_orders", kw)
        return self.open

    async def get_my_trades(self, **kw):
        self._rec("get_my_trades", kw)
        return self.trades.get(kw["orderId"], [])

    async def close_connection(self):
        self._rec("close_connection", {})
