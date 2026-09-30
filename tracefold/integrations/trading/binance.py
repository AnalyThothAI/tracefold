"""Small signed REST adapter for the Binance USD-M DEMO account.

No order method retries. A transport timeout or 503 is ambiguous; the runner must
query the recorded client ID before deciding what happened.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from tracefold.integrations.binance_catalogue import BinanceCatalogue

_BASE = "https://demo-fapi.binance.com"
_CLIENT_ID = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")


class BinanceFailure(RuntimeError):
    def __init__(self, status: int, code: int | None, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message[:200]
        super().__init__(f"binance_http_{status}_code_{code}")

    @property
    def transient(self) -> bool:
        return self.status >= 500 or self.status == 429 or self.code in (-1000, -1001, -1003, -1006, -1007)

    def evidence(self) -> dict[str, Any]:
        # Provider messages may echo signed requests. Persist codes, never a URL,
        # request body or arbitrary provider text.
        return {
            "http_status": self.status,
            "error_code": self.code,
            "submission": "unknown" if self.transient else "rejected",
        }


class DemoBinance:
    def __init__(
        self,
        *,
        environment: str,
        api_key: str,
        api_secret: str,
        client: httpx.AsyncClient | None = None,
        clock_ms: Any = None,
    ) -> None:
        if environment != "DEMO" or not api_key or not api_secret:
            raise ValueError("execution_requires_demo_credentials")
        self._key = api_key
        self._secret = api_secret.encode()
        self._client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self._owns_client = client is None
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._offset_ms = 0
        self.catalogue = BinanceCatalogue(self._catalogue_get, clock_ms=self._clock_ms)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def sync_time(self) -> None:
        sent = self._clock_ms()
        response = await self._client.get(_BASE + "/fapi/v1/time")
        response.raise_for_status()
        received = self._clock_ms()
        self._offset_ms = int(response.json()["serverTime"]) - (sent + received) // 2

    async def _signed(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        fields = dict(params or {})
        fields.update(recvWindow=5_000, timestamp=self._clock_ms() + self._offset_ms)
        query = urlencode(fields)
        signature = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        response = await self._client.request(
            method, _BASE + path + "?" + query + "&signature=" + signature, headers={"X-MBX-APIKEY": self._key}
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceFailure(response.status_code, None, "invalid_json") from exc
        if response.status_code >= 400:
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("msg", "") if isinstance(payload, dict) else ""
            raise BinanceFailure(response.status_code, int(code) if isinstance(code, int) else None, str(message))
        return payload

    async def positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self._signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol} if symbol else None)

    async def account(self) -> dict[str, Any]:
        return await self._signed("GET", "/fapi/v3/account")

    async def account_config(self) -> dict[str, Any]:
        return await self._signed("GET", "/fapi/v1/accountConfig")

    async def symbol_config(self, symbol: str) -> dict[str, Any]:
        rows = await self._signed("GET", "/fapi/v1/symbolConfig", {"symbol": symbol})
        matches = [row for row in rows if row.get("symbol") == symbol]
        if len(matches) != 1:
            raise ValueError("symbol_margin_config_unavailable")
        return matches[0]

    async def mark_price(self, symbol: str) -> Decimal:
        response = await self._client.get(_BASE + "/fapi/v1/premiumIndex", params={"symbol": symbol})
        response.raise_for_status()
        price = Decimal(str(response.json()["markPrice"]))
        if not price.is_finite() or price <= 0:
            raise ValueError("mark_price_invalid")
        return price

    async def commission_rate(self, symbol: str) -> Decimal:
        row = await self._signed("GET", "/fapi/v1/commissionRate", {"symbol": symbol})
        rate = Decimal(str(row["takerCommissionRate"]))
        if not rate.is_finite() or rate < 0:
            raise ValueError("commission_rate_invalid")
        return rate

    async def cancel_order(self, symbol: str, client_id: str) -> dict[str, Any]:
        self._validate_id(client_id)
        return await self._signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id})

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self._signed("GET", "/fapi/v1/openOrders", {"symbol": symbol} if symbol else None)

    async def open_algo_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self._signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol} if symbol else None)

    async def user_trades(
        self,
        symbol: str,
        *,
        from_id: int | None = None,
        start_time_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"symbol": symbol, "limit": 1_000}
        if from_id is not None:
            params["fromId"] = from_id
        elif start_time_ms is not None:
            params["startTime"] = start_time_ms
        return await self._signed("GET", "/fapi/v1/userTrades", params)

    async def query_order(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        self._validate_id(client_id)
        try:
            return await self._signed("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id})
        except BinanceFailure as exc:
            if exc.code == -2013:
                return None
            raise

    async def query_algo(self, client_id: str) -> dict[str, Any] | None:
        self._validate_id(client_id)
        try:
            return await self._signed("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_id})
        except BinanceFailure as exc:
            if exc.code == -2013:
                return None
            raise

    async def market_order(
        self, *, symbol: str, side: Literal["BUY", "SELL"], quantity: Decimal, client_id: str, reduce_only: bool
    ) -> dict[str, Any]:
        self._validate_id(client_id)
        if quantity <= 0:
            raise ValueError("order_quantity_invalid")
        return await self._signed(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": str(quantity),
                "reduceOnly": "true" if reduce_only else "false",
                "newOrderRespType": "RESULT",
                "newClientOrderId": client_id,
            },
        )

    async def protection_order(
        self,
        *,
        symbol: str,
        side: Literal["BUY", "SELL"],
        leg: Literal["sl", "tp"],
        trigger_price: Decimal,
        client_id: str,
        quantity: Decimal | None = None,
    ) -> dict[str, Any]:
        self._validate_id(client_id)
        if trigger_price <= 0 or (quantity is not None and quantity <= 0):
            raise ValueError("protection_price_or_quantity_invalid")
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET" if leg == "sl" else "TAKE_PROFIT_MARKET",
            "triggerPrice": str(trigger_price),
            "workingType": "MARK_PRICE",
            "clientAlgoId": client_id,
            "newOrderRespType": "RESULT",
        }
        if quantity is None:
            params["closePosition"] = "true"
        else:
            params["quantity"] = str(quantity)
            params["reduceOnly"] = "true"
        return await self._signed("POST", "/fapi/v1/algoOrder", params)

    async def cancel_algo(self, client_id: str) -> dict[str, Any]:
        self._validate_id(client_id)
        return await self._signed("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_id})

    async def cancel_symbol_orders(self, symbol: str) -> dict[str, Any]:
        return await self._signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    async def cancel_symbol_algo_orders(self, symbol: str) -> dict[str, Any]:
        return await self._signed("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    async def _catalogue_get(self, url: str, *, params: dict[str, Any], receipts: list[dict[str, Any]]) -> Any:
        started = time.monotonic()
        response = await self._client.get(url, params=params)
        receipts.append(
            {
                "endpoint": "/fapi/v1/exchangeInfo",
                "http_status": response.status_code,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "cache_hit": False,
            }
        )
        response.raise_for_status()
        return response.json()

    async def exchange_info(self) -> dict[str, Any]:
        return (await self.catalogue.read("demo", deadline_at_monotonic=time.monotonic() + 5)).payload

    async def book_ticker(self, symbol: str) -> dict[str, Any]:
        response = await self._client.get(_BASE + "/fapi/v1/ticker/bookTicker", params={"symbol": symbol})
        response.raise_for_status()
        return response.json()

    async def live_book_ticker(self, symbol: str) -> dict[str, Any]:
        response = await self._client.get(
            "https://fapi.binance.com/fapi/v1/ticker/bookTicker", params={"symbol": symbol}
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _validate_id(value: str) -> None:
        if _CLIENT_ID.fullmatch(value) is None:
            raise ValueError("client_order_id_invalid")


__all__ = ["BinanceFailure", "DemoBinance"]
