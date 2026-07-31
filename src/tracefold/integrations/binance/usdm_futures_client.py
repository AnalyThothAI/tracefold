from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx

PROVIDER = "binance"
FEED_TYPE = "cex_swap"
QUOTE_SYMBOL = "USDT"

_EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
_TICKER_24HR_PATH = "/fapi/v1/ticker/24hr"


class BinanceUsdmFuturesClientError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BinanceUsdmRoute:
    provider: str
    feed_type: str
    quote_symbol: str
    native_market_id: str
    base_symbol: str
    multiplier: float | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BinanceUsdmTicker24hr:
    symbol: str
    last_price: float | None
    price_change_percent: float | None
    volume_24h: float | None
    quote_volume_24h: float | None
    open_time_ms: int | None
    close_time_ms: int | None
    raw: dict[str, Any]


class BinanceUsdmFuturesClient:
    def __init__(
        self,
        *,
        base_url: str = "https://fapi.binance.com",
        timeout_seconds: float = 15.0,
        http_client: Any | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "tracefold/1.0"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def exchange_info(self) -> dict[str, Any]:
        payload = self._get_json(_EXCHANGE_INFO_PATH)
        if not isinstance(payload, dict):
            raise BinanceUsdmFuturesClientError("Binance exchangeInfo returned invalid payload")
        return payload

    def usdt_perpetual_routes(self) -> list[BinanceUsdmRoute]:
        symbols = self.exchange_info().get("symbols")
        if not isinstance(symbols, list):
            return []
        routes = [_route_from_exchange_symbol(row) for row in symbols if isinstance(row, dict)]
        return sorted((route for route in routes if route is not None), key=lambda route: route.native_market_id)

    def ticker_24hr(self, symbol: str | None = None) -> BinanceUsdmTicker24hr | list[BinanceUsdmTicker24hr]:
        payload = self._get_json(_TICKER_24HR_PATH, params=_optional_symbol_params(symbol))
        if isinstance(payload, list):
            return [_ticker_24hr_from_row(row) for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            return _ticker_24hr_from_row(payload)
        raise BinanceUsdmFuturesClientError("Binance ticker/24hr returned invalid payload")

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise BinanceUsdmFuturesClientError(f"Binance {path} transport failed:{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise BinanceUsdmFuturesClientError(f"Binance {path} returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise BinanceUsdmFuturesClientError(f"Binance {path} returned non-json response") from exc


def _route_from_exchange_symbol(row: dict[str, Any]) -> BinanceUsdmRoute | None:
    status = _text(row.get("status"))
    contract_type = _text(row.get("contractType"))
    quote = _text(row.get("quoteAsset"))
    symbol = _text(row.get("symbol"))
    base = _text(row.get("baseAsset"))
    if status != "TRADING" or contract_type != "PERPETUAL" or quote != QUOTE_SYMBOL:
        return None
    if not symbol or not base:
        return None
    return BinanceUsdmRoute(
        provider=PROVIDER,
        feed_type=FEED_TYPE,
        quote_symbol=QUOTE_SYMBOL,
        native_market_id=symbol,
        base_symbol=base,
        multiplier=_float(row.get("contractSize")),
        raw=dict(row),
    )


def _ticker_24hr_from_row(row: dict[str, Any]) -> BinanceUsdmTicker24hr:
    return BinanceUsdmTicker24hr(
        symbol=_symbol(row.get("symbol")),
        last_price=_float(row.get("lastPrice")),
        price_change_percent=_float(row.get("priceChangePercent")),
        volume_24h=_float(row.get("volume")),
        quote_volume_24h=_float(row.get("quoteVolume")),
        open_time_ms=_int(row.get("openTime")),
        close_time_ms=_int(row.get("closeTime")),
        raw=dict(row),
    )


def _optional_symbol_params(symbol: str | None) -> dict[str, str] | None:
    return {"symbol": _symbol(symbol)} if symbol is not None else None


def _symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        raise BinanceUsdmFuturesClientError("Binance symbol is required")
    return text


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BinanceUsdmFuturesClientError("Binance numeric field is invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BinanceUsdmFuturesClientError("Binance numeric field is invalid") from exc
    if not math.isfinite(parsed):
        raise BinanceUsdmFuturesClientError("Binance numeric field is invalid")
    return parsed


def _int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BinanceUsdmFuturesClientError("Binance integer field is invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BinanceUsdmFuturesClientError("Binance integer field is invalid") from exc
    if not math.isfinite(parsed) or not parsed.is_integer():
        raise BinanceUsdmFuturesClientError("Binance integer field is invalid")
    return int(parsed)
