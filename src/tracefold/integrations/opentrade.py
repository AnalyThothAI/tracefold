"""Pinned OpenTrade read adapter for #185 PR-C1.

This module owns HTTP and token-bearing headers. Trading receives typed, sanitized facts only. The
adapter deliberately contains no capital write in C1: approval can be exercised, but cannot reach a
provider until the separate PR-C2 review gate lands.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from tracefold.trading import (
    ExecutionObservation,
    ExecutionReceipt,
    InstrumentRef,
    LiveExchangeId,
    LivePreflight,
    PreparedOrder,
    RemoteExposure,
    StartupReconciliation,
)

_API = "/open/trader/newsliquid/v1"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ABSENCE_WINDOW_MS = 7 * 86_400_000


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class OpenTradeContractError(RuntimeError):
    """Sanitized provider-contract failure. Never carries a response body or credential."""


class OpenTradeReadAdapter:
    """One concrete provider adapter; no registry and no generic HTTP execution framework."""

    name = "opentrade"
    writes_enabled = False

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        request_timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        if not str(token).strip():
            raise OpenTradeContractError("opentrade_token_empty")
        parsed = urlsplit(str(base_url))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise OpenTradeContractError("opentrade_base_url_invalid")
        self._clock = clock
        self._client = httpx.AsyncClient(
            base_url=str(base_url).rstrip("/"),
            headers={"Authorization": f"Bearer {str(token).strip()}"},
            timeout=httpx.Timeout(float(request_timeout_seconds)),
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def startup(
        self,
        *,
        instrument: InstrumentRef,
        account_ref: str,
    ) -> StartupReconciliation:
        return StartupReconciliation(preflight=await self.preflight(instrument=instrument, account_ref=account_ref))

    async def preflight(self, *, instrument: InstrumentRef, account_ref: str) -> LivePreflight:
        exchange_id = _live_exchange(instrument.exchange_id)
        server_time = await self._server_time()
        status = await self._object("/public/metadata/status", params={"exchangeId": exchange_id})
        metadata = await self._object("/market/metadata", params={"ticker": instrument.base_symbol})
        market = self._select_market(metadata, instrument=instrument, exchange_id=exchange_id)
        provider_symbol = _text(market.get("symbol"))
        exact = InstrumentRef(
            exchange_id=exchange_id,
            venue=instrument.venue,
            provider_symbol=provider_symbol,
            base_symbol=instrument.base_symbol,
            instrument_class=instrument.instrument_class,
            quote_asset=_optional_text(market.get("quoteCurrency")) or instrument.quote_asset,
            observed_at_ms=server_time,
        )
        params = {"exchangeId": exchange_id, "symbol": provider_symbol}
        ticker = await self._object("/market/ticker", params=params)
        account = await self._object(
            "/account/summary",
            params={**params, "accountType": "swap"},
        )
        position_mode = await self._object("/position/mode", params=params)
        leverage = await self._object("/leverage/current", params=params)
        margin = await self._object("/margin/mode", params=params)
        # Account-wide reads close the drift window after startup: an operator or another process
        # opening a different symbol must still block this one-position canary.
        positions = await self._list("/positions", params={"exchangeId": exchange_id})
        orders = await self._list("/orders/open", params={"exchangeId": exchange_id})

        mark = _positive_decimal(ticker.get("last") or ticker.get("markPrice"), "opentrade_mark_invalid")
        bid = _positive_decimal(ticker.get("bid"), "opentrade_bid_invalid")
        ask = _positive_decimal(ticker.get("ask"), "opentrade_ask_invalid")
        if ask < bid:
            raise OpenTradeContractError("opentrade_spread_invalid")
        spread = int(((ask - bid) / mark * Decimal(10_000)).quantize(Decimal("1"), rounding=ROUND_CEILING))
        leverage_value = _positive_int(leverage.get("leverage"), "opentrade_leverage_invalid")
        leverage_margin = _optional_text(leverage.get("marginMode"))
        margin_mode = _text(margin.get("marginMode"))
        if leverage_margin is not None and leverage_margin != margin_mode:
            raise OpenTradeContractError("opentrade_margin_mode_mismatch")
        hedged = position_mode.get("hedged")
        if not isinstance(hedged, bool):
            raise OpenTradeContractError("opentrade_position_mode_invalid")
        if (
            _text(account.get("exchangeId")).lower() != exchange_id
            or _text(account.get("accountType")).lower() != "swap"
        ):
            raise OpenTradeContractError("opentrade_account_scope_mismatch")
        available_currency = _text(account.get("currency")).upper()
        if exact.quote_asset is not None and available_currency != exact.quote_asset.upper():
            raise OpenTradeContractError("opentrade_account_currency_mismatch")

        return LivePreflight(
            provider=self.name,
            observed_at_ms=server_time,
            server_time_ms=server_time,
            venue_healthy=_text(status.get("status")).lower() == "ok",
            instrument=exact,
            mark_price=mark,
            bid_price=bid,
            ask_price=ask,
            spread_bps=spread,
            quantity_step=_first_decimal(market, "quantityStep", "amountStep", "stepSize"),
            min_quantity=_optional_decimal(market.get("amountMin")),
            min_notional=_optional_decimal(market.get("costMin")),
            contract_size=_optional_decimal(market.get("contractSize")) or Decimal("1"),
            requested_account_ref=account_ref,
            # The pinned public OpenTrade account response exposes exchange/type/currency, but no
            # stable account identity. Echoing the configured label here would fabricate proof.
            observed_account_ref=None,
            available_balance=_optional_decimal(account.get("available")),
            available_currency=available_currency,
            hedged=hedged,
            leverage=leverage_value,
            margin_mode=margin_mode,
            positions=tuple(self._position_exposures(positions, exchange_id=exchange_id)),
            open_orders=tuple(self._order_exposures(orders, exchange_id=exchange_id)),
        )

    async def submit(self, order: PreparedOrder) -> ExecutionReceipt:
        del order
        raise OpenTradeContractError("opentrade_writes_disabled_pr_c1")

    async def close(self, order: PreparedOrder, *, quantity: Decimal) -> ExecutionReceipt:
        del order, quantity
        raise OpenTradeContractError("opentrade_writes_disabled_pr_c1")

    async def observe(self, order: PreparedOrder) -> ExecutionObservation:
        try:
            return await self._observe(order)
        except (OpenTradeContractError, ValidationError) as exc:
            error_code = str(exc) if isinstance(exc, OpenTradeContractError) else "opentrade_payload_schema_invalid"
            return ExecutionObservation(
                state="UNKNOWN",
                observed_at_ms=self._clock(),
                remote_order_id=order.remote_order_id,
                evidence={
                    "schema": "tracefold.trading.execution_observation.v1",
                    "error_code": error_code,
                },
            )

    async def _observe(self, order: PreparedOrder) -> ExecutionObservation:
        """Combine every relevant read; a partial or malformed picture raises, never proves absence."""

        exchange_id = _live_exchange(order.instrument.exchange_id)
        params = {"exchangeId": exchange_id, "symbol": order.instrument.provider_symbol}
        server_time = await self._server_time()
        positions = await self._list("/positions", params=params)
        open_orders = await self._list("/orders/open", params=params)
        closed_orders = await self._list("/orders/closed", params={**params, "days": 7})
        trades = await self._list("/trades/history", params={**params, "days": 7})
        position_history = await self._list("/positions/history", params={**params, "days": 7})
        position_rows = self._matching(positions, order=order)
        open_rows = self._matching(open_orders, order=order)
        closed_rows = self._matching(closed_orders, order=order)
        trade_rows = self._matching(trades, order=order)
        history_rows = self._matching(position_history, order=order)
        remote_id = self._remote_id(order)
        evidence = {
            "schema": "tracefold.trading.execution_observation.v1",
            "position_count": len(position_rows),
            "open_order_ids": _ids(open_rows, "orderId"),
            "closed_order_ids": _ids(closed_rows, "orderId"),
            "trade_ids": _ids(trade_rows, "tradeId"),
            "position_history_count": len(history_rows),
        }
        if remote_id is None:
            return ExecutionObservation(state="UNKNOWN", observed_at_ms=server_time, evidence=evidence)
        if len(position_rows) > 1:
            return ExecutionObservation(state="UNKNOWN", observed_at_ms=server_time, evidence=evidence)
        if position_rows:
            # PR-C1 captures the composite read but never adopts live exposure. Exact entry/fill and
            # native-protection correlation belongs to C2's reviewed lifecycle evidence.
            position = position_rows[0]
            quantity = _positive_decimal(position.get("contracts"), "opentrade_position_quantity_invalid")
            return ExecutionObservation(
                state="UNKNOWN",
                observed_at_ms=server_time,
                remote_order_id=remote_id,
                actual_position_quantity=quantity,
                filled_quantity=quantity,
                average_price=_optional_decimal(position.get("entryPrice")),
                evidence=evidence,
            )
        if open_rows or closed_rows or trade_rows or history_rows:
            return ExecutionObservation(
                state="UNKNOWN",
                observed_at_ms=server_time,
                remote_order_id=remote_id,
                evidence=evidence,
            )
        within_complete_window = 0 <= server_time - order.instrument.observed_at_ms <= _ABSENCE_WINDOW_MS
        return ExecutionObservation(
            state="ABSENT_CONFIRMED" if remote_id and within_complete_window else "UNKNOWN",
            observed_at_ms=server_time,
            remote_order_id=remote_id,
            evidence=evidence,
        )

    async def _server_time(self) -> int:
        payload = await self._mapping("/market/time")
        value = payload.get("timestamp")
        if value is None and isinstance(payload.get("data"), Mapping):
            value = cast(Mapping[str, Any], payload["data"]).get("timestamp")
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            raise OpenTradeContractError("opentrade_server_time_invalid") from None
        if abs(self._clock() - timestamp) > 30_000:
            raise OpenTradeContractError("opentrade_server_time_stale")
        return timestamp

    async def _mapping(self, path: str, *, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        try:
            async with self._client.stream("GET", f"{_API}{path}", params=dict(params or {})) as response:
                if response.status_code in {401, 403}:
                    raise OpenTradeContractError("opentrade_authentication_failed")
                if response.status_code == 429:
                    raise OpenTradeContractError("opentrade_rate_limited")
                if response.status_code >= 400:
                    raise OpenTradeContractError("opentrade_http_error")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise OpenTradeContractError("opentrade_payload_too_large")
        except httpx.TimeoutException:
            raise OpenTradeContractError("opentrade_timeout") from None
        except httpx.HTTPError:
            raise OpenTradeContractError("opentrade_http_error") from None
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, ValueError):
            raise OpenTradeContractError("opentrade_payload_invalid") from None
        if not isinstance(payload, Mapping) or payload.get("success") is False:
            raise OpenTradeContractError("opentrade_payload_invalid")
        return cast(Mapping[str, Any], payload)

    async def _object(self, path: str, *, params: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = await self._mapping(path, params=params)
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise OpenTradeContractError("opentrade_payload_invalid")
        return cast(Mapping[str, Any], data)

    async def _list(self, path: str, *, params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        payload = await self._mapping(path, params=params)
        data = payload.get("data")
        if not isinstance(data, list) or any(not isinstance(row, Mapping) for row in data):
            raise OpenTradeContractError("opentrade_payload_invalid")
        return [cast(Mapping[str, Any], row) for row in data]

    @staticmethod
    def _select_market(
        metadata: Mapping[str, Any],
        *,
        instrument: InstrumentRef,
        exchange_id: LiveExchangeId,
    ) -> Mapping[str, Any]:
        rows = metadata.get("markets")
        if not isinstance(rows, list):
            raise OpenTradeContractError("opentrade_metadata_invalid")
        base = instrument.base_symbol.upper()
        quote = str(instrument.quote_asset or "").upper()
        matches = [
            cast(Mapping[str, Any], row)
            for row in rows
            if isinstance(row, Mapping)
            and str(_optional_text(row.get("exchangeId")) or "").lower() == exchange_id
            and _text(row.get("baseCurrency")).upper() == base
            and bool(row.get("active", True))
            and bool(row.get("swap"))
            and bool(row.get("contract"))
            and (not quote or _text(row.get("quoteCurrency")).upper() == quote)
        ]
        if len(matches) != 1:
            raise OpenTradeContractError("opentrade_exact_market_unavailable")
        return matches[0]

    @staticmethod
    def _position_exposures(rows: Sequence[Mapping[str, Any]], *, exchange_id: LiveExchangeId) -> list[RemoteExposure]:
        return [
            RemoteExposure(
                kind="position",
                exchange_id=exchange_id,
                provider_symbol=_text(row.get("symbol")),
                side=_text(row.get("side")),
                quantity=_positive_decimal(row.get("contracts"), "opentrade_position_quantity_invalid"),
            )
            for row in rows
            if _decimal_or_zero(row.get("contracts")) > 0
        ]

    @staticmethod
    def _order_exposures(rows: Sequence[Mapping[str, Any]], *, exchange_id: LiveExchangeId) -> list[RemoteExposure]:
        return [
            RemoteExposure(
                kind="open_order",
                exchange_id=exchange_id,
                provider_symbol=_text(row.get("symbol")),
                side=_text(row.get("side")),
                quantity=_positive_decimal(row.get("amount"), "opentrade_order_quantity_invalid"),
                remote_id=_optional_text(row.get("orderId")),
            )
            for row in rows
        ]

    @staticmethod
    def _matching(rows: Sequence[Mapping[str, Any]], *, order: PreparedOrder) -> list[Mapping[str, Any]]:
        matches: list[Mapping[str, Any]] = []
        for row in rows:
            symbol = _text(row.get("symbol"))
            exchange_id = _text(row.get("exchange") or row.get("exchangeId")).lower()
            if symbol == order.instrument.provider_symbol and exchange_id == order.instrument.exchange_id:
                matches.append(row)
        return matches

    @staticmethod
    def _remote_id(order: PreparedOrder) -> str | None:
        return order.remote_order_id


def _live_exchange(value: object) -> LiveExchangeId:
    normalized = str(value)
    if normalized not in {"binance", "hyperliquid"}:
        raise OpenTradeContractError("opentrade_exchange_unsupported")
    return cast(LiveExchangeId, normalized)


def _text(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise OpenTradeContractError("opentrade_field_missing")
    return normalized


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_decimal(value: object, code: str) -> Decimal:
    parsed = _optional_decimal(value)
    if parsed is None:
        raise OpenTradeContractError(code)
    return parsed


def _decimal_or_zero(value: object) -> Decimal:
    return _optional_decimal(value) or Decimal(0)


def _first_decimal(row: Mapping[str, Any], *keys: str) -> Decimal | None:
    return next((value for key in keys if (value := _optional_decimal(row.get(key))) is not None), None)


def _positive_int(value: object, code: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise OpenTradeContractError(code) from None
    if parsed <= 0:
        raise OpenTradeContractError(code)
    return parsed


def _ids(rows: Sequence[Mapping[str, Any]], key: str) -> list[str]:
    return [value for row in rows if (value := _optional_text(row.get(key))) is not None]


__all__ = ["OpenTradeContractError", "OpenTradeReadAdapter"]
