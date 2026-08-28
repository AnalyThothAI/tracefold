"""Bounded Binance USD-M Demo adapter for the manual execution authority.

The base URL is deliberately not configurable: this first release cannot reach mainnet.  Every provider
write carries a durable caller-supplied client id, and an unknown transport/503 outcome is reported as
ambiguous so the caller must query that id before considering another submission.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final, Literal
from urllib.parse import urlencode

import httpx

from tracefold.integrations.binance_usdm_account import (
    BINANCE_USDM_DEMO_BASE_URL,
    BinanceUsdmAccountIdentityClient,
    BinanceUsdmAccountIdentityError,
)
from tracefold.trading import (
    ManualExecutionPlan,
    ManualOrderLeg,
    ManualVenueAccount,
    ManualVenueError,
    ManualVenueInstrument,
    ManualVenueOrderReceipt,
    ManualVenuePosition,
)

_TIMEOUT_SECONDS: Final = 6.5
_RECV_WINDOW_MS: Final = 5_000
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{3,256}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,40}$")
_CLIENT_ID_RE = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")


class BinanceManualClient:
    """Synchronous signed REST client; the process root owns its thread and lifetime."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        transport: httpx.BaseTransport | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        normalized_key = str(api_key or "").strip()
        normalized_secret = str(api_secret or "").strip()
        if _API_KEY_RE.fullmatch(normalized_key) is None or not 3 <= len(normalized_secret) <= 512:
            raise ValueError("binance_manual_credentials_invalid")
        self._api_key = normalized_key
        self._api_secret = normalized_secret.encode()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._client = httpx.Client(
            base_url=BINANCE_USDM_DEMO_BASE_URL,
            timeout=httpx.Timeout(_TIMEOUT_SECONDS),
            follow_redirects=False,
            headers={"Accept": "application/json", "X-MBX-APIKEY": normalized_key},
            transport=transport,
        )
        self._identity_client = BinanceUsdmAccountIdentityClient(
            api_key=normalized_key,
            api_secret=normalized_secret,
            transport=transport,
            clock_ms=self._clock_ms,
        )

    def account(self) -> ManualVenueAccount:
        payload = self._signed("GET", "/fapi/v3/account")
        try:
            equity = _positive_decimal(payload.get("totalMarginBalance"), "binance_manual_account_invalid")
            can_trade = payload.get("canTrade")
            if not isinstance(can_trade, bool):
                raise ManualVenueError("binance_manual_account_invalid")
            provider_fingerprint = self._identity_client.provider_account_fingerprint()
        except (BinanceUsdmAccountIdentityError, ManualVenueError, ValueError) as exc:
            raise ManualVenueError("binance_manual_account_invalid", retryable=True) from exc
        return ManualVenueAccount(
            equity_usd=equity,
            can_trade=can_trade,
            provider_account_fingerprint=provider_fingerprint,
        )

    def execution_ready(self) -> bool:
        return self.one_way_mode()

    def one_way_mode(self) -> bool:
        payload = self._signed("GET", "/fapi/v1/positionSide/dual")
        dual = payload.get("dualSidePosition")
        if not isinstance(dual, bool):
            raise ManualVenueError("binance_manual_position_mode_invalid", retryable=True)
        return not dual

    def position(self, symbol: str) -> ManualVenuePosition:
        normalized_symbol = _symbol(symbol)
        payload = self._signed_sequence("GET", "/fapi/v3/positionRisk", {"symbol": normalized_symbol})
        rows = [row for row in payload if isinstance(row, Mapping) and row.get("symbol") == normalized_symbol]
        if len(rows) != 1:
            raise ManualVenueError("binance_manual_position_invalid", retryable=True)
        row = rows[0]
        try:
            leverage = int(row.get("leverage"))
        except (TypeError, ValueError) as exc:
            raise ManualVenueError("binance_manual_position_invalid", retryable=True) from exc
        try:
            return ManualVenuePosition(
                symbol=normalized_symbol,
                quantity=_decimal(row.get("positionAmt"), "binance_manual_position_invalid"),
                entry_price=_decimal(row.get("entryPrice"), "binance_manual_position_invalid"),
                leverage=leverage,
            )
        except ManualVenueError as exc:
            raise ManualVenueError("binance_manual_position_invalid", retryable=True) from exc

    def instrument(self, symbol: str) -> ManualVenueInstrument:
        normalized_symbol = _symbol(symbol)
        payload = self._public("GET", "/fapi/v1/exchangeInfo")
        symbols = payload.get("symbols")
        if isinstance(symbols, str | bytes) or not isinstance(symbols, Sequence):
            raise ManualVenueError("binance_manual_exchange_info_invalid", retryable=True)
        rows = [row for row in symbols if isinstance(row, Mapping) and row.get("symbol") == normalized_symbol]
        if len(rows) != 1 or rows[0].get("status") != "TRADING":
            raise ManualVenueError("binance_manual_instrument_unavailable")
        filters = rows[0].get("filters")
        if isinstance(filters, str | bytes) or not isinstance(filters, Sequence):
            raise ManualVenueError("binance_manual_exchange_info_invalid", retryable=True)
        by_kind = {
            str(value.get("filterType")): value
            for value in filters
            if isinstance(value, Mapping) and value.get("filterType")
        }
        try:
            return ManualVenueInstrument(
                symbol=normalized_symbol,
                tick_size=_positive_decimal(by_kind["PRICE_FILTER"].get("tickSize"), "invalid"),
                quantity_step=_positive_decimal(by_kind["LOT_SIZE"].get("stepSize"), "invalid"),
                min_quantity=_positive_decimal(by_kind["LOT_SIZE"].get("minQty"), "invalid"),
                min_notional=_positive_decimal(by_kind["MIN_NOTIONAL"].get("notional"), "invalid"),
            )
        except (KeyError, ManualVenueError) as exc:
            raise ManualVenueError("binance_manual_exchange_info_invalid", retryable=True) from exc

    def apply_execution_setting(self, plan: ManualExecutionPlan) -> None:
        self.change_leverage(symbol=plan.symbol, leverage=plan.leverage)

    def query_leg(self, plan: ManualExecutionPlan, leg: ManualOrderLeg) -> ManualVenueOrderReceipt | None:
        if leg == "entry":
            return self.query_order(symbol=plan.symbol, client_order_id=plan.entry_client_order_id)
        return self.query_algo_order(client_algo_id=getattr(plan, f"{leg}_client_order_id"))

    def submit_leg(self, plan: ManualExecutionPlan, leg: ManualOrderLeg) -> ManualVenueOrderReceipt:
        if leg == "entry":
            return self.submit_market_order(
                symbol=plan.symbol,
                side=plan.entry_side,
                quantity=str(plan.quantity),
                client_order_id=plan.entry_client_order_id,
            )
        return self.submit_close_algo_order(
            symbol=plan.symbol,
            side=plan.close_side,
            order_type="TAKE_PROFIT_MARKET" if leg == "take_profit" else "STOP_MARKET",
            trigger_price=str(plan.take_profit_trigger if leg == "take_profit" else plan.stop_loss_trigger),
            client_algo_id=getattr(plan, f"{leg}_client_order_id"),
        )

    def change_leverage(self, *, symbol: str, leverage: int) -> int:
        if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= 125:
            raise ValueError("binance_manual_leverage_invalid")
        payload = self._signed(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": _symbol(symbol), "leverage": leverage},
            write=True,
        )
        observed = payload.get("leverage")
        if isinstance(observed, bool) or not isinstance(observed, int) or observed != leverage:
            raise ManualVenueError("binance_manual_leverage_response_invalid", ambiguous=True)
        return observed

    def submit_market_order(
        self,
        *,
        symbol: str,
        side: Literal["BUY", "SELL"],
        quantity: str,
        client_order_id: str,
    ) -> ManualVenueOrderReceipt:
        if side not in {"BUY", "SELL"}:
            raise ValueError("binance_manual_order_side_invalid")
        _positive_decimal(quantity, "binance_manual_order_quantity_invalid")
        client_id = _client_id(client_order_id)
        payload = self._signed(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": _symbol(symbol),
                "side": side,
                "type": "MARKET",
                "quantity": str(quantity),
                "newClientOrderId": client_id,
                "newOrderRespType": "RESULT",
            },
            write=True,
        )
        try:
            return _order_receipt(payload, client_key="clientOrderId", id_key="orderId")
        except ManualVenueError as exc:
            raise ManualVenueError(exc.code, ambiguous=True) from exc

    def query_order(self, *, symbol: str, client_order_id: str) -> ManualVenueOrderReceipt | None:
        try:
            payload = self._signed(
                "GET",
                "/fapi/v1/order",
                {"symbol": _symbol(symbol), "origClientOrderId": _client_id(client_order_id)},
            )
        except ManualVenueError as exc:
            if exc.provider_code == -2013:
                return None
            raise
        try:
            return _order_receipt(payload, client_key="clientOrderId", id_key="orderId")
        except ManualVenueError as exc:
            raise ManualVenueError(exc.code, retryable=True) from exc

    def submit_close_algo_order(
        self,
        *,
        symbol: str,
        side: Literal["BUY", "SELL"],
        order_type: Literal["STOP_MARKET", "TAKE_PROFIT_MARKET"],
        trigger_price: str,
        client_algo_id: str,
    ) -> ManualVenueOrderReceipt:
        if side not in {"BUY", "SELL"} or order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise ValueError("binance_manual_algo_order_invalid")
        _positive_decimal(trigger_price, "binance_manual_trigger_price_invalid")
        payload = self._signed(
            "POST",
            "/fapi/v1/algoOrder",
            {
                "algoType": "CONDITIONAL",
                "symbol": _symbol(symbol),
                "side": side,
                "type": order_type,
                "triggerPrice": str(trigger_price),
                "workingType": "MARK_PRICE",
                "closePosition": "true",
                "clientAlgoId": _client_id(client_algo_id),
                "newOrderRespType": "RESULT",
            },
            write=True,
        )
        try:
            return _order_receipt(payload, client_key="clientAlgoId", id_key="algoId", status_key="algoStatus")
        except ManualVenueError as exc:
            raise ManualVenueError(exc.code, ambiguous=True) from exc

    def query_algo_order(self, *, client_algo_id: str) -> ManualVenueOrderReceipt | None:
        try:
            payload = self._signed(
                "GET",
                "/fapi/v1/algoOrder",
                {"clientAlgoId": _client_id(client_algo_id)},
            )
        except ManualVenueError as exc:
            if exc.provider_code in {-2011, -2013}:
                return None
            raise
        try:
            return _order_receipt(payload, client_key="clientAlgoId", id_key="algoId", status_key="algoStatus")
        except ManualVenueError as exc:
            raise ManualVenueError(exc.code, retryable=True) from exc

    def _public(self, method: str, path: str) -> Mapping[str, Any]:
        return self._request_mapping(method, path, content=None, write=False)

    def _signed(
        self,
        method: str,
        path: str,
        params: Mapping[str, object] | None = None,
        *,
        write: bool = False,
    ) -> Mapping[str, Any]:
        payload = self._signed_request(method, path, params, write=write)
        if not isinstance(payload, Mapping):
            raise ManualVenueError(
                "binance_manual_response_invalid",
                ambiguous=write,
                retryable=not write,
            )
        return payload

    def _signed_sequence(
        self,
        method: str,
        path: str,
        params: Mapping[str, object] | None = None,
    ) -> Sequence[Any]:
        payload = self._signed_request(method, path, params, write=False)
        if isinstance(payload, str | bytes) or not isinstance(payload, Sequence):
            raise ManualVenueError("binance_manual_response_invalid", retryable=True)
        return payload

    def _signed_request(
        self,
        method: str,
        path: str,
        params: Mapping[str, object] | None,
        *,
        write: bool,
    ) -> Any:
        pairs = [(key, _parameter(value)) for key, value in (params or {}).items()]
        pairs.extend((("recvWindow", str(_RECV_WINDOW_MS)), ("timestamp", str(int(self._clock_ms())))))
        unsigned = urlencode(pairs)
        signature = hmac.new(self._api_secret, unsigned.encode(), hashlib.sha256).hexdigest()
        signed = f"{unsigned}&signature={signature}"
        return self._request_json(
            method,
            path,
            content=signed.encode() if method != "GET" else None,
            query=signed if method == "GET" else None,
            write=write,
        )

    def _request_mapping(self, method: str, path: str, *, content: bytes | None, write: bool) -> Mapping[str, Any]:
        payload = self._request_json(method, path, content=content, query=None, write=write)
        if not isinstance(payload, Mapping):
            raise ManualVenueError(
                "binance_manual_response_invalid",
                ambiguous=write,
                retryable=not write,
            )
        return payload

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None,
        query: str | None,
        write: bool,
    ) -> Any:
        try:
            response = self._client.request(
                method,
                path,
                content=content,
                params=query,
                headers={"Content-Type": "application/x-www-form-urlencoded"} if content is not None else None,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            raise ManualVenueError(
                "binance_manual_write_ambiguous" if write else "binance_manual_transport_failed",
                ambiguous=write,
                retryable=not write,
            ) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code == 200:
            return payload
        provider_code = payload.get("code") if isinstance(payload, Mapping) else None
        uncertain_status = response.status_code in {408, 429} or response.status_code >= 500
        ambiguous = bool(write and uncertain_status)
        retryable = bool(not write and uncertain_status)
        raise ManualVenueError(
            "binance_manual_write_ambiguous"
            if ambiguous
            else "binance_manual_read_retryable"
            if retryable
            else "binance_manual_provider_rejected",
            ambiguous=ambiguous,
            retryable=retryable,
            provider_code=(
                provider_code if isinstance(provider_code, int) and not isinstance(provider_code, bool) else None
            ),
        )

    def close(self) -> None:
        self._client.close()
        self._identity_client.close()


def _order_receipt(
    payload: Mapping[str, Any],
    *,
    client_key: str,
    id_key: str,
    status_key: str = "status",
) -> ManualVenueOrderReceipt:
    client_id = payload.get(client_key)
    provider_id = payload.get(id_key)
    status = payload.get(status_key)
    if (
        not isinstance(client_id, str)
        or not client_id
        or provider_id is None
        or not isinstance(status, str)
        or not status
    ):
        raise ManualVenueError("binance_manual_order_response_invalid")
    executed = _optional_decimal(payload.get("executedQty") or payload.get("actualQty"))
    average = _optional_decimal(payload.get("avgPrice") or payload.get("actualPrice"))
    return ManualVenueOrderReceipt(
        client_id=client_id,
        provider_id=str(provider_id),
        status=status,
        executed_quantity=executed,
        average_price=average,
    )


def _symbol(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if _SYMBOL_RE.fullmatch(normalized) is None:
        raise ValueError("binance_manual_symbol_invalid")
    return normalized


def _client_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _CLIENT_ID_RE.fullmatch(normalized) is None:
        raise ValueError("binance_manual_client_id_invalid")
    return normalized


def _parameter(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _decimal(value: object, code: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise ManualVenueError(code) from exc
    if not parsed.is_finite():
        raise ManualVenueError(code)
    return parsed


def _positive_decimal(value: object, code: str) -> Decimal:
    parsed = _decimal(value, code)
    if parsed <= 0:
        raise ManualVenueError(code)
    return parsed


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


__all__ = [
    "BINANCE_USDM_DEMO_BASE_URL",
    "BinanceManualClient",
]
