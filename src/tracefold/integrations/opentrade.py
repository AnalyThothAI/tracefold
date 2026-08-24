"""Pinned OpenTrade reviewed-execution adapter for #185.

This module owns HTTP and token-bearing headers. Trading receives typed, sanitized facts only. The
only writes are the pinned market-entry and full-position-close contracts; the durable Trading kernel
claims either attempt before it calls this adapter and never retries an ambiguous answer.
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
    NativeProtection,
    OrderSide,
    PreparedOrder,
    RemoteExposure,
    StartupReconciliation,
)

_API = "/open/trader/newsliquid/v1"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ABSENCE_WINDOW_MS = 7 * 86_400_000
_MAX_SERVER_CLOCK_SKEW_MS = 30_000
_MAX_TICKER_AGE_MS = 10_000
_STOP_ORDER_TYPES = frozenset({"stop", "stop_market", "stop_loss", "stop_loss_market"})


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class OpenTradeContractError(RuntimeError):
    """Sanitized provider-contract failure. Never carries a response body or credential."""


class OpenTradeAdapter:
    """One concrete provider adapter; no registry and no generic HTTP execution framework."""

    name = "opentrade"
    writes_enabled = True

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
        status = await self._object("/public/metadata/status", params={"exchangeId": exchange_id})
        metadata = await self._object("/market/metadata", params={"ticker": instrument.base_symbol})
        market = self._select_market(metadata, instrument=instrument, exchange_id=exchange_id)
        provider_symbol = _text(market.get("symbol"))
        params = {"exchangeId": exchange_id, "symbol": provider_symbol}
        ticker = await self._object("/market/ticker", params=params)
        server_time = await self._server_time()
        ticker_exchange = _aliased_text(
            ticker,
            "exchangeId",
            "exchange",
            code="opentrade_ticker_scope_mismatch",
        )
        ticker_symbol = _optional_text(ticker.get("symbol"))
        if ticker_exchange is None or ticker_exchange.lower() != exchange_id or ticker_symbol != provider_symbol:
            raise OpenTradeContractError("opentrade_ticker_scope_mismatch")
        ticker_timestamp = _optional_timestamp(ticker.get("timestamp"))
        ticker_age_ms = server_time - ticker_timestamp if ticker_timestamp is not None else -1
        if ticker_age_ms < 0 or ticker_age_ms > _MAX_TICKER_AGE_MS:
            raise OpenTradeContractError("opentrade_ticker_timestamp_invalid")
        exact = InstrumentRef(
            exchange_id=exchange_id,
            venue=instrument.venue,
            provider_symbol=provider_symbol,
            base_symbol=instrument.base_symbol,
            instrument_class=instrument.instrument_class,
            quote_asset=_optional_text(market.get("quoteCurrency")) or instrument.quote_asset,
            observed_at_ms=server_time,
        )
        account = await self._object(
            "/account/summary",
            params={**params, "accountType": "swap"},
        )
        position_mode = await self._object("/position/mode", params=params)
        leverage = await self._object("/leverage/current", params=params)
        margin = await self._object("/margin/mode", params=params)
        # Account-wide reads close the drift window after startup: an operator or another process
        # opening a different symbol must still block this one-position canary. Orders come first so
        # an order that becomes a position between the two reads is visible in at least one snapshot.
        orders = await self._list("/orders/open", params={"exchangeId": exchange_id})
        positions = await self._list("/positions", params={"exchangeId": exchange_id})

        mark = _aliased_decimal(ticker, "last", "markPrice", code="opentrade_mark_invalid")
        if mark is None:
            raise OpenTradeContractError("opentrade_mark_invalid")
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
            quantity_step=_aliased_decimal(
                market,
                "quantityStep",
                "amountStep",
                "stepSize",
                code="opentrade_quantity_step_invalid",
            ),
            price_tick=_aliased_decimal(
                market,
                "priceStep",
                "tickSize",
                code="opentrade_price_tick_invalid",
            ),
            min_quantity=_optional_decimal(market.get("amountMin")),
            min_notional=_optional_decimal(market.get("costMin")),
            contract_size=_positive_decimal(market.get("contractSize"), "opentrade_contract_size_invalid"),
            requested_account_ref=account_ref,
            # The pinned example has neither field, so normal public output remains fail-closed. An
            # exact provider identity is accepted when the real capability response supplies one.
            observed_account_ref=_aliased_text(
                account,
                "accountId",
                "accountRef",
                identifier=True,
                code="opentrade_account_identity_invalid",
            ),
            available_balance=_optional_decimal(account.get("available")),
            available_currency=available_currency,
            hedged=hedged,
            leverage=leverage_value,
            margin_mode=margin_mode,
            positions=tuple(self._position_exposures(positions, exchange_id=exchange_id)),
            open_orders=tuple(self._order_exposures(orders, exchange_id=exchange_id)),
        )

    async def submit(self, order: PreparedOrder) -> ExecutionReceipt:
        return await self._write("/orders", body=_entry_body(order))

    async def close(self, order: PreparedOrder, *, quantity: Decimal) -> ExecutionReceipt:
        return await self._write("/positions/close", body=_close_body(order, quantity=quantity))

    async def _write(self, path: str, *, body: Mapping[str, Any]) -> ExecutionReceipt:
        status, payload = await self._request("POST", path, json_body=body)
        if status in {401, 403}:
            return ExecutionReceipt(state="REJECTED", reason="opentrade_authentication_failed")
        if status == 429:
            return ExecutionReceipt(state="REJECTED", reason="opentrade_rate_limited")
        if status >= 500:
            raise OpenTradeContractError("opentrade_http_error")
        if 400 <= status < 500:
            return ExecutionReceipt(state="REJECTED", reason="opentrade_rejected")
        if not 200 <= status < 300:
            raise OpenTradeContractError("opentrade_http_error")
        if payload.get("success") is False:
            if not _definitive_write_rejection(payload):
                raise OpenTradeContractError("opentrade_write_outcome_conflict")
            return ExecutionReceipt(state="REJECTED", reason="opentrade_rejected")
        if payload.get("success") is not True:
            raise OpenTradeContractError("opentrade_payload_invalid")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise OpenTradeContractError("opentrade_payload_invalid")
        remote_order_id = _optional_identifier(data.get("orderId"))
        if remote_order_id is None:
            raise OpenTradeContractError("opentrade_order_identity_missing")
        return ExecutionReceipt(
            state="ACKNOWLEDGED",
            remote_order_id=remote_order_id,
            filled_quantity=_optional_decimal(data.get("filledQty")),
            average_price=_optional_decimal(data.get("avgPrice")),
            reason="opentrade_ack",
        )

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
                    "schema": "tracefold.trading.execution_observation.v2",
                    "error_code": error_code,
                },
            )

    async def _observe(self, order: PreparedOrder) -> ExecutionObservation:
        """Combine every relevant read; a partial or malformed picture raises, never proves absence."""

        exchange_id = _live_exchange(order.instrument.exchange_id)
        params = {"exchangeId": exchange_id, "symbol": order.instrument.provider_symbol}
        account = await self._object("/account/summary", params={**params, "accountType": "swap"})
        # As in preflight, open orders precede positions so an order-to-position transition cannot
        # disappear between the two account snapshots.
        open_orders = await self._list("/orders/open", params=params)
        positions = await self._list("/positions", params=params)
        closed_orders = await self._list("/orders/closed", params={**params, "days": 7})
        trades = await self._list("/trades/history", params={**params, "days": 7})
        position_history = await self._list("/positions/history", params={**params, "days": 7})
        server_time = await self._server_time()
        position_rows = self._matching(positions, order=order)
        open_rows = self._matching(open_orders, order=order)
        closed_rows = self._matching(closed_orders, order=order)
        trade_rows = self._matching(trades, order=order)
        history_rows = self._matching(position_history, order=order)
        remote_id = self._remote_id(order)
        account_ref = _aliased_text(
            account,
            "accountId",
            "accountRef",
            identifier=True,
            code="opentrade_account_identity_invalid",
        )
        entry_open = _remote_rows(open_rows, remote_id)
        entry_closed = _remote_rows(closed_rows, remote_id)
        entry_trades = _remote_rows(trade_rows, remote_id)
        close_open = _remote_rows(open_rows, order.remote_close_order_id)
        close_closed = _remote_rows(closed_rows, order.remote_close_order_id)
        close_trades = _remote_rows(trade_rows, order.remote_close_order_id)
        correlated_history = _correlated_history_rows(history_rows, remote_id)
        entry_history = _attributed_history_rows(correlated_history, remote_id, entry_side=order.side)
        expected_position_side = "long" if order.side == "buy" else "short"
        expected_positions = [row for row in position_rows if _optional_text(row.get("side")) == expected_position_side]
        unmatched_opening_trades = [
            row
            for row in trade_rows
            if _possible_current_opening_trade(
                row,
                entry_side=order.side,
                lifecycle_started_at_ms=order.instrument.observed_at_ms,
                server_time=server_time,
            )
            and _optional_identifier(row.get("orderId")) != remote_id
        ]
        unmatched_closing_trades = [
            row
            for row in trade_rows
            if _possible_current_closing_trade(
                row,
                entry_side=order.side,
                lifecycle_started_at_ms=order.instrument.observed_at_ms,
                server_time=server_time,
            )
            and _optional_identifier(row.get("orderId")) != remote_id
        ]
        evidence = {
            "schema": "tracefold.trading.execution_observation.v2",
            "position_count": len(position_rows),
            "open_order_count": len(open_rows),
            "open_order_ids": _ids(open_rows, "orderId"),
            "closed_order_count": len(closed_rows),
            "closed_order_ids": _ids(closed_rows, "orderId"),
            "trade_count": len(trade_rows),
            "trade_ids": _ids(trade_rows, "tradeId"),
            "position_history_count": len(history_rows),
            "entry_open_count": len(entry_open),
            "entry_closed_count": len(entry_closed),
            "entry_trade_count": len(entry_trades),
            "close_open_count": len(close_open),
            "close_closed_count": len(close_closed),
            "close_trade_count": len(close_trades),
            "close_terminal_without_fill": bool(
                order.remote_close_order_id
                and close_closed
                and not close_open
                and not close_trades
                and all(
                    str(row.get("status") or "").lower() in {"canceled", "cancelled", "rejected"}
                    for row in close_closed
                )
                and _all_explicit_zero_fills(close_closed)
            ),
            "entry_history_correlated_count": len(correlated_history),
            "entry_history_count": len(entry_history),
            "unmatched_opening_trade_count": len(unmatched_opening_trades),
            "unmatched_closing_trade_count": len(unmatched_closing_trades),
            "account_identity_proven": account_ref is not None,
        }
        if remote_id is None:
            return ExecutionObservation(state="UNKNOWN", observed_at_ms=server_time, evidence=evidence)
        if account_ref != order.account_ref or len(position_rows) > 1 or len(expected_positions) != len(position_rows):
            return ExecutionObservation(state="UNKNOWN", observed_at_ms=server_time, evidence=evidence)
        if expected_positions:
            if correlated_history or unmatched_opening_trades or unmatched_closing_trades:
                return ExecutionObservation(state="UNKNOWN", observed_at_ms=server_time, evidence=evidence)
            position = expected_positions[0]
            quantity = _positive_decimal(position.get("contracts"), "opentrade_position_quantity_invalid")
            if quantity > order.quantity:
                return ExecutionObservation(state="UNKNOWN", observed_at_ms=server_time, evidence=evidence)
            average_price = _positive_decimal(position.get("entryPrice"), "opentrade_entry_price_invalid")
            entry_protection_rows = [
                row
                for row in open_rows
                if str(row.get("type") or "").lower() in _STOP_ORDER_TYPES
                and _aliased_text(
                    row,
                    "parentOrderId",
                    "sourceOrderId",
                    identifier=True,
                    code="opentrade_protection_identity_invalid",
                )
                == remote_id
            ]
            expected_open_rows = [
                row
                for row in open_rows
                if _optional_identifier(row.get("orderId")) == remote_id or row in entry_protection_rows
            ]
            evidence["protection_candidate_count"] = len(entry_protection_rows)
            evidence["unexpected_open_order_count"] = len(open_rows) - len(expected_open_rows)
            if len(expected_open_rows) != len(open_rows) or len(entry_protection_rows) > 1:
                return ExecutionObservation(state="UNKNOWN", observed_at_ms=server_time, evidence=evidence)
            protection = (
                _native_protection(entry_protection_rows[0], account_ref=account_ref) if entry_protection_rows else None
            )
            filled = _filled_quantity(entry_open, entry_closed, entry_trades)
            if (
                filled is None
                or filled < quantity
                or filled > order.quantity
                or filled not in {quantity, order.quantity}
            ):
                return ExecutionObservation(state="UNKNOWN", observed_at_ms=server_time, evidence=evidence)
            if entry_open and filled < order.quantity:
                evidence["entry_remainder_working"] = True
                return ExecutionObservation(
                    state="UNKNOWN",
                    observed_at_ms=server_time,
                    remote_order_id=remote_id,
                    evidence=evidence,
                )
            return ExecutionObservation(
                state=(
                    "PARTIAL"
                    if filled < order.quantity
                    else ("OPEN_PROTECTED" if protection is not None else "OPEN_UNPROTECTED")
                ),
                observed_at_ms=server_time,
                remote_order_id=remote_id,
                actual_position_quantity=quantity,
                filled_quantity=filled,
                average_price=average_price,
                first_fill_at_ms=_first_timestamp(entry_trades, server_time=server_time),
                protection=protection,
                evidence=evidence,
            )
        if entry_history:
            if open_rows or len(correlated_history) != 1 or len(entry_history) != 1:
                return ExecutionObservation(state="UNKNOWN", observed_at_ms=server_time, evidence=evidence)
            history = entry_history[-1]
            if history.get("fullyClosed") is not True:
                return ExecutionObservation(state="UNKNOWN", observed_at_ms=server_time, evidence=evidence)
            first_fill_at_ms = _bounded_optional_timestamp(history.get("openTimestamp"), server_time=server_time)
            closed_at_ms = _bounded_optional_timestamp(history.get("closeTimestamp"), server_time=server_time)
            if closed_at_ms is None:
                raise OpenTradeContractError("opentrade_timestamp_invalid")
            if first_fill_at_ms is not None and closed_at_ms is not None and first_fill_at_ms > closed_at_ms:
                raise OpenTradeContractError("opentrade_timestamp_invalid")
            return ExecutionObservation(
                state="CLOSED",
                observed_at_ms=server_time,
                remote_order_id=remote_id,
                first_fill_at_ms=first_fill_at_ms,
                closed_at_ms=closed_at_ms,
                average_price=_positive_decimal(history.get("entryPrice"), "opentrade_entry_price_invalid"),
                exit_price=_positive_decimal(history.get("closePrice"), "opentrade_exit_price_invalid"),
                evidence=evidence,
            )
        filled = _filled_quantity(entry_open, entry_closed, entry_trades)
        if filled is not None or entry_trades:
            return ExecutionObservation(
                state="UNKNOWN",
                observed_at_ms=server_time,
                remote_order_id=remote_id,
                evidence=evidence,
            )
        if entry_open:
            return ExecutionObservation(
                state="WORKING",
                observed_at_ms=server_time,
                remote_order_id=remote_id,
                evidence=evidence,
            )
        if (
            not open_rows
            and not correlated_history
            and entry_closed
            and all(
                str(row.get("status") or "").lower() in {"canceled", "cancelled", "rejected"} for row in entry_closed
            )
            and _all_explicit_zero_fills(entry_closed)
        ):
            return ExecutionObservation(
                state="REJECTED",
                observed_at_ms=server_time,
                remote_order_id=remote_id,
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
        status, payload = await self._request("GET", "/market/time")
        if status in {401, 403}:
            raise OpenTradeContractError("opentrade_authentication_failed")
        if status == 429:
            raise OpenTradeContractError("opentrade_rate_limited")
        if not 200 <= status < 300:
            raise OpenTradeContractError("opentrade_http_error")
        if "success" in payload and payload.get("success") is not True:
            raise OpenTradeContractError("opentrade_payload_invalid")
        value = payload.get("timestamp")
        if value is None and isinstance(payload.get("data"), Mapping):
            value = cast(Mapping[str, Any], payload["data"]).get("timestamp")
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            raise OpenTradeContractError("opentrade_server_time_invalid") from None
        if abs(self._clock() - timestamp) > _MAX_SERVER_CLOCK_SKEW_MS:
            raise OpenTradeContractError("opentrade_server_time_stale")
        return timestamp

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        try:
            async with self._client.stream(
                method,
                f"{_API}{path}",
                params=dict(params or {}),
                content=None if json_body is None else _json_content(json_body),
                headers=None if json_body is None else {"Content-Type": "application/json"},
            ) as response:
                if response.status_code >= 400:
                    return response.status_code, {}
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
        if not isinstance(payload, Mapping):
            raise OpenTradeContractError("opentrade_payload_invalid")
        return response.status_code, cast(Mapping[str, Any], payload)

    async def _mapping(self, path: str, *, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        status, payload = await self._request("GET", path, params=params)
        if status in {401, 403}:
            raise OpenTradeContractError("opentrade_authentication_failed")
        if status == 429:
            raise OpenTradeContractError("opentrade_rate_limited")
        if not 200 <= status < 300:
            raise OpenTradeContractError("opentrade_http_error")
        if payload.get("success") is not True:
            raise OpenTradeContractError("opentrade_payload_invalid")
        return payload

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
            and row.get("active") is True
            and row.get("swap") is True
            and row.get("contract") is True
            and (not quote or _text(row.get("quoteCurrency")).upper() == quote)
        ]
        if len(matches) != 1:
            raise OpenTradeContractError("opentrade_exact_market_unavailable")
        return matches[0]

    @staticmethod
    def _position_exposures(rows: Sequence[Mapping[str, Any]], *, exchange_id: LiveExchangeId) -> list[RemoteExposure]:
        exposures: list[RemoteExposure] = []
        for row in rows:
            quantity = _nonnegative_decimal(row.get("contracts"), "opentrade_position_quantity_invalid")
            if quantity == 0:
                continue
            exposures.append(
                RemoteExposure(
                    kind="position",
                    exchange_id=exchange_id,
                    provider_symbol=_text(row.get("symbol")),
                    side=_text(row.get("side")),
                    quantity=quantity,
                )
            )
        return exposures

    @staticmethod
    def _order_exposures(rows: Sequence[Mapping[str, Any]], *, exchange_id: LiveExchangeId) -> list[RemoteExposure]:
        return [
            RemoteExposure(
                kind="open_order",
                exchange_id=exchange_id,
                provider_symbol=_text(row.get("symbol")),
                side=_text(row.get("side")),
                quantity=_positive_decimal(row.get("amount"), "opentrade_order_quantity_invalid"),
                remote_id=_optional_identifier(row.get("orderId")),
            )
            for row in rows
        ]

    @staticmethod
    def _matching(rows: Sequence[Mapping[str, Any]], *, order: PreparedOrder) -> list[Mapping[str, Any]]:
        matches: list[Mapping[str, Any]] = []
        for row in rows:
            symbol = _text(row.get("symbol"))
            exchange_id = _text(
                _aliased_text(
                    row,
                    "exchange",
                    "exchangeId",
                    code="opentrade_exchange_identity_invalid",
                )
            ).lower()
            if symbol == order.instrument.provider_symbol and exchange_id == order.instrument.exchange_id:
                matches.append(row)
        return matches

    @staticmethod
    def _remote_id(order: PreparedOrder) -> str | None:
        return order.remote_order_id


def _remote_rows(rows: Sequence[Mapping[str, Any]], remote_id: str | None) -> list[Mapping[str, Any]]:
    if remote_id is None:
        return []
    return [row for row in rows if _optional_identifier(row.get("orderId")) == remote_id]


def _definitive_write_rejection(payload: Mapping[str, Any]) -> bool:
    """A business rejection is definitive only when it carries no execution evidence."""

    data = payload.get("data")
    if data is None:
        return True
    if not isinstance(data, Mapping):
        return False
    if any(key in data for key in ("orderId", "tradeId", "positionId")):
        return False
    for key in ("filledQty", "avgPrice"):
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            if _nonnegative_decimal(value, "opentrade_write_outcome_conflict") != 0:
                return False
        except OpenTradeContractError:
            return False
    return True


def _correlated_history_rows(rows: Sequence[Mapping[str, Any]], remote_id: str | None) -> list[Mapping[str, Any]]:
    if remote_id is None:
        return []
    return [
        row
        for row in rows
        if isinstance(row.get("trades"), list)
        and any(
            isinstance(trade, Mapping) and _optional_identifier(trade.get("orderId")) == remote_id
            for trade in row["trades"]
        )
    ]


def _attributed_history_rows(
    rows: Sequence[Mapping[str, Any]], remote_id: str | None, *, entry_side: OrderSide
) -> list[Mapping[str, Any]]:
    if remote_id is None:
        return []
    close_side = "sell" if entry_side == "buy" else "buy"
    matched: list[Mapping[str, Any]] = []
    for row in rows:
        trades = row.get("trades")
        if not isinstance(trades, list) or not trades or not all(isinstance(trade, Mapping) for trade in trades):
            continue
        opening = [trade for trade in trades if trade.get("reduceOnly") is False]
        closing = [trade for trade in trades if trade.get("reduceOnly") is True]
        if (
            opening
            and closing
            and len(opening) + len(closing) == len(trades)
            and all(
                _optional_identifier(trade.get("orderId")) == remote_id
                and _optional_text(trade.get("side")) == entry_side
                for trade in opening
            )
            and all(
                _optional_identifier(trade.get("orderId")) is not None
                and _optional_text(trade.get("side")) == close_side
                for trade in closing
            )
        ):
            matched.append(row)
    return matched


def _native_protection(row: Mapping[str, Any], *, account_ref: str) -> NativeProtection | None:
    if str(row.get("type") or "").lower() not in _STOP_ORDER_TYPES:
        return None
    side = str(row.get("side") or "").lower()
    if side not in {"buy", "sell"}:
        return None
    parent_id = _aliased_text(
        row,
        "parentOrderId",
        "sourceOrderId",
        identifier=True,
        code="opentrade_protection_identity_invalid",
    )
    remote_id = _optional_identifier(row.get("orderId"))
    exchange_id = _aliased_text(
        row,
        "exchange",
        "exchangeId",
        code="opentrade_exchange_identity_invalid",
    )
    symbol = _optional_text(row.get("symbol"))
    quantity = _optional_decimal(row.get("amount"))
    trigger = _optional_decimal(row.get("triggerPrice"))
    status = _optional_text(row.get("status"))
    reduce_only = row.get("reduceOnly")
    if (
        parent_id is None
        or remote_id is None
        or exchange_id is None
        or symbol is None
        or quantity is None
        or trigger is None
        or status is None
        or not isinstance(reduce_only, bool)
    ):
        return None
    return NativeProtection(
        remote_order_id=remote_id,
        parent_remote_order_id=parent_id,
        account_ref=account_ref,
        exchange_id=_live_exchange(exchange_id.lower()),
        provider_symbol=symbol,
        side=cast(OrderSide, side),
        quantity=quantity,
        trigger_price=trigger,
        reduce_only=reduce_only,
        status=status,
    )


def _filled_quantity(*groups: Sequence[Mapping[str, Any]]) -> Decimal | None:
    reported = [
        value for rows in groups[:2] for row in rows if (value := _optional_decimal(row.get("filledQty"))) is not None
    ]
    traded = sum(
        (value for row in groups[2] if (value := _optional_decimal(row.get("amount"))) is not None),
        start=Decimal(0),
    )
    candidates = [*reported, *([traded] if traded > 0 else [])]
    return max(candidates) if candidates else None


def _possible_current_opening_trade(
    row: Mapping[str, Any], *, entry_side: OrderSide, lifecycle_started_at_ms: int, server_time: int
) -> bool:
    if row.get("reduceOnly") is True:
        return False
    timestamp = _bounded_optional_timestamp(row.get("timestamp"), server_time=server_time)
    if timestamp is not None and timestamp < lifecycle_started_at_ms - _MAX_SERVER_CLOCK_SKEW_MS:
        return False
    side = _optional_text(row.get("side"))
    closing_side = "sell" if entry_side == "buy" else "buy"
    return side is None or side.lower() != closing_side


def _possible_current_closing_trade(
    row: Mapping[str, Any], *, entry_side: OrderSide, lifecycle_started_at_ms: int, server_time: int
) -> bool:
    side = _optional_text(row.get("side"))
    closing_side = "sell" if entry_side == "buy" else "buy"
    if row.get("reduceOnly") is not True and side is not None and side.lower() != closing_side:
        return False
    timestamp = _bounded_optional_timestamp(row.get("timestamp"), server_time=server_time)
    return timestamp is None or timestamp >= lifecycle_started_at_ms - _MAX_SERVER_CLOCK_SKEW_MS


def _all_explicit_zero_fills(rows: Sequence[Mapping[str, Any]]) -> bool:
    return all(_nonnegative_decimal(row.get("filledQty"), "opentrade_fill_quantity_invalid") == 0 for row in rows)


def _first_timestamp(rows: Sequence[Mapping[str, Any]], *, server_time: int) -> int | None:
    timestamps = [
        value
        for row in rows
        if (value := _bounded_optional_timestamp(row.get("timestamp"), server_time=server_time)) is not None
    ]
    return min(timestamps) if timestamps else None


def _optional_timestamp(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bounded_optional_timestamp(value: object, *, server_time: int) -> int | None:
    if value is None or value == "":
        return None
    parsed = _optional_timestamp(value)
    if parsed is None or not 0 <= server_time - parsed <= _ABSENCE_WINDOW_MS:
        raise OpenTradeContractError("opentrade_timestamp_invalid")
    return parsed


def _entry_body(order: PreparedOrder) -> dict[str, Any]:
    payload = order.payload
    allowed = {
        "exchangeId",
        "symbol",
        "side",
        "type",
        "quantity",
        "hedged",
        "stopLossPrice",
        "_tracefoldExecutionContractSha256",
        "_tracefoldPriceTick",
    }
    if set(payload) - allowed:
        raise OpenTradeContractError("opentrade_order_payload_invalid")
    hedged = payload.get("hedged")
    if (
        payload.get("exchangeId") != order.instrument.exchange_id
        or payload.get("symbol") != order.instrument.provider_symbol
        or payload.get("side") != order.side
        or payload.get("type") != "market"
        or not isinstance(hedged, bool)
        or order.take_profit_price is not None
        or _positive_decimal(payload.get("quantity"), "opentrade_order_payload_invalid") != order.quantity
        or _positive_decimal(payload.get("stopLossPrice"), "opentrade_order_payload_invalid") != order.stop_price
    ):
        raise OpenTradeContractError("opentrade_order_payload_invalid")
    body: dict[str, Any] = {
        "exchangeId": order.instrument.exchange_id,
        "symbol": order.instrument.provider_symbol,
        "side": order.side,
        "type": "market",
        "quantity": order.quantity,
        "hedged": hedged,
        "stopLossPrice": order.stop_price,
    }
    return body


def _close_body(order: PreparedOrder, *, quantity: Decimal) -> dict[str, Any]:
    hedged = order.payload.get("hedged")
    if not isinstance(hedged, bool) or quantity <= 0:
        raise OpenTradeContractError("opentrade_close_payload_invalid")
    return {
        "exchangeId": order.instrument.exchange_id,
        "symbol": order.instrument.provider_symbol,
        "side": "long" if order.side == "buy" else "short",
        "quantity": quantity,
        "hedged": hedged,
    }


def _json_decimal(value: Decimal) -> str:
    if not value.is_finite() or value <= 0:
        raise OpenTradeContractError("opentrade_number_invalid")
    return format(value, "f")


def _json_content(body: Mapping[str, Any]) -> bytes:
    """Encode the pinned flat write contract without routing Decimal through binary floats."""

    fields: list[str] = []
    for key, value in body.items():
        encoded = _json_decimal(value) if isinstance(value, Decimal) else json.dumps(value, allow_nan=False)
        fields.append(f"{json.dumps(key)}:{encoded}")
    return ("{" + ",".join(fields) + "}").encode()


def _live_exchange(value: object) -> LiveExchangeId:
    normalized = str(value)
    if normalized not in {"binance", "hyperliquid"}:
        raise OpenTradeContractError("opentrade_exchange_unsupported")
    return cast(LiveExchangeId, normalized)


def _text(value: object) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise OpenTradeContractError("opentrade_field_missing")
    return normalized


def _optional_text(value: object) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def _optional_identifier(value: object) -> str | None:
    normalized = _optional_text(value)
    if normalized is not None:
        return normalized
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    return None


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _positive_decimal(value: object, code: str) -> Decimal:
    parsed = _optional_decimal(value)
    if parsed is None:
        raise OpenTradeContractError(code)
    return parsed


def _nonnegative_decimal(value: object, code: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OpenTradeContractError(code) from None
    if not parsed.is_finite() or parsed < 0:
        raise OpenTradeContractError(code)
    return parsed


def _aliased_text(
    row: Mapping[str, Any],
    *keys: str,
    identifier: bool = False,
    code: str,
) -> str | None:
    values: list[str] = []
    parser = _optional_identifier if identifier else _optional_text
    for key in keys:
        if key not in row:
            continue
        value = parser(row[key])
        if value is None:
            raise OpenTradeContractError(code)
        values.append(value)
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise OpenTradeContractError(code)
    return values[0]


def _aliased_decimal(row: Mapping[str, Any], *keys: str, code: str) -> Decimal | None:
    values: list[Decimal] = []
    for key in keys:
        if key not in row:
            continue
        value = _optional_decimal(row[key])
        if value is None:
            raise OpenTradeContractError(code)
        values.append(value)
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise OpenTradeContractError(code)
    return values[0]


def _positive_int(value: object, code: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise OpenTradeContractError(code) from None
    if parsed <= 0:
        raise OpenTradeContractError(code)
    return parsed


def _ids(rows: Sequence[Mapping[str, Any]], key: str) -> list[str]:
    return sorted({value for row in rows if (value := _optional_identifier(row.get(key))) is not None})


__all__ = ["OpenTradeAdapter", "OpenTradeContractError"]
