"""OpenTrade's reviewed execution-truth seam for #185 PR-C2."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from tracefold.integrations.opentrade import OpenTradeAdapter, OpenTradeContractError
from tracefold.trading.contracts import InstrumentRef, PreparedOrder

NOW = 1_787_000_000_000


def _instrument() -> InstrumentRef:
    return InstrumentRef(
        exchange_id="binance",
        venue="binance.perp",
        provider_symbol="DOGEUSDT",
        base_symbol="DOGE",
        instrument_class="crypto",
        quote_asset="USDT",
        observed_at_ms=NOW - 60_000,
    )


def _response(
    request: httpx.Request,
    *,
    external_position: bool = False,
    account_identity: bool = False,
) -> httpx.Response:
    path = request.url.path
    if path.endswith("/market/time"):
        payload: object = {"timestamp": NOW}
    elif path.endswith("/public/metadata/status"):
        payload = {"success": True, "data": {"status": "ok", "updated": NOW}}
    elif path.endswith("/market/metadata"):
        payload = {
            "success": True,
            "data": {
                "ticker": "DOGE",
                "markets": [
                    {
                        "exchangeId": "binance",
                        "symbol": "DOGE/USDT:USDT",
                        "active": True,
                        "type": "swap",
                        "baseCurrency": "DOGE",
                        "quoteCurrency": "USDT",
                        "settleCurrency": "USDT",
                        "amountMin": "1",
                        "costMin": "5",
                        "quantityStep": "0.1",
                        "priceStep": "0.01",
                        "contractSize": "1",
                        "spot": False,
                        "swap": True,
                        "contract": True,
                    }
                ],
            },
        }
    elif path.endswith("/market/ticker"):
        payload = {
            "success": True,
            "data": {
                "exchangeId": "binance",
                "symbol": "DOGE/USDT:USDT",
                "last": "10",
                "bid": "9.99",
                "ask": "10.01",
                "timestamp": NOW,
            },
        }
    elif path.endswith("/account/summary"):
        payload = {
            "success": True,
            "data": {
                "exchangeId": "binance",
                "accountType": "swap",
                "available": "100",
                "currency": "USDT",
                **({"accountId": "canary"} if account_identity else {}),
                # This must never enter the persisted audit snapshot.
                "totals": {"USDT": "123.45"},
            },
        }
    elif path.endswith("/position/mode"):
        payload = {"success": True, "data": {"hedged": False, "info": {"dualSidePosition": False}}}
    elif path.endswith("/leverage/current"):
        payload = {"success": True, "data": {"leverage": 1, "marginMode": "cross"}}
    elif path.endswith("/margin/mode"):
        payload = {"success": True, "data": {"marginMode": "cross"}}
    elif path.endswith("/positions"):
        payload = {
            "success": True,
            "data": (
                [
                    {
                        "exchange": "binance",
                        "symbol": "DOGE/USDT:USDT",
                        "side": "long",
                        "contracts": "2",
                        "hedged": False,
                    }
                ]
                if external_position
                else []
            ),
        }
    elif (
        path.endswith("/orders/open")
        or path.endswith("/orders/closed")
        or path.endswith("/trades/history")
        or path.endswith("/positions/history")
    ):
        payload = {"success": True, "data": []}
    else:  # pragma: no cover - names the unexpected provider call in a failure
        raise AssertionError(f"unexpected OpenTrade read: {request.method} {request.url}")
    return httpx.Response(200, json=payload, request=request)


def test_fresh_preflight_replaces_the_research_symbol_price_and_position_mode() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(request)

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        preflight = asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())

    assert preflight.instrument.provider_symbol == "DOGE/USDT:USDT"
    assert preflight.instrument.provider_symbol != _instrument().provider_symbol
    assert preflight.mark_price == Decimal("10")
    assert preflight.spread_bps == 20
    assert preflight.quantity_step == Decimal("0.1")
    assert preflight.min_quantity == Decimal("1")
    assert preflight.contract_size == Decimal("1")
    assert preflight.hedged is False
    assert preflight.leverage == 1
    assert preflight.margin_mode == "cross"
    assert preflight.requested_account_ref == "canary"
    assert preflight.observed_account_ref is None
    assert preflight.positions == ()
    assert preflight.open_orders == ()
    assert all(request.method == "GET" for request in requests)
    assert all(request.headers["Authorization"] == "Bearer secret-token" for request in requests)
    assert "totals" not in str(preflight.audit_payload())
    assert "123.45" not in str(preflight.audit_payload())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exchangeId", None),
        ("exchangeId", "hyperliquid"),
        ("symbol", None),
        ("symbol", "BTC/USDT:USDT"),
    ],
)
def test_preflight_requires_ticker_identity_before_any_write(field: str, value: object) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = _response(request)
        if request.url.path.endswith("/market/ticker"):
            body = response.json()
            if value is None:
                del body["data"][field]
            else:
                body["data"][field] = value
            return httpx.Response(200, json=body, request=request)
        return response

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match="opentrade_ticker_scope_mismatch"):
            asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())
    assert requests
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("timestamp", [None, NOW // 1_000, NOW - 10_001, NOW + 1, NOW + 9_999, NOW + 10_001])
def test_preflight_requires_a_fresh_millisecond_ticker_timestamp_before_any_write(timestamp: object) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = _response(request)
        if request.url.path.endswith("/market/ticker"):
            body = response.json()
            if timestamp is None:
                del body["data"]["timestamp"]
            else:
                body["data"]["timestamp"] = timestamp
            return httpx.Response(200, json=body, request=request)
        return response

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match="opentrade_ticker_timestamp_invalid"):
            asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())
    assert requests
    assert all(request.method == "GET" for request in requests)


def test_preflight_sees_an_external_order_that_becomes_a_position_between_account_reads() -> None:
    reads = {"orders": False, "positions": False}
    request_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        request_paths.append(path)
        if path.endswith("/orders/open"):
            payload = {
                "success": True,
                "data": (
                    []
                    if reads["positions"]
                    else [
                        {
                            "exchange": "binance",
                            "symbol": "SOL/USDT:USDT",
                            "side": "buy",
                            "amount": "1",
                            "orderId": "external-order",
                        }
                    ]
                ),
            }
            reads["orders"] = True
            return httpx.Response(200, json=payload, request=request)
        if path.endswith("/positions"):
            payload = {
                "success": True,
                "data": (
                    [
                        {
                            "exchange": "binance",
                            "symbol": "SOL/USDT:USDT",
                            "side": "long",
                            "contracts": "1",
                            "hedged": False,
                        }
                    ]
                    if reads["orders"]
                    else []
                ),
            }
            reads["positions"] = True
            return httpx.Response(200, json=payload, request=request)
        return _response(request, account_identity=True)

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        preflight = asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())

    assert preflight.open_orders or preflight.positions
    orders_index = next(index for index, path in enumerate(request_paths) if path.endswith("/orders/open"))
    positions_index = next(index for index, path in enumerate(request_paths) if path.endswith("/positions"))
    assert orders_index < positions_index


def test_amount_min_is_not_guessed_to_be_the_quantity_step() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = _response(request)
        if request.url.path.endswith("/market/metadata"):
            body = response.json()
            del body["data"]["markets"][0]["quantityStep"]
            return httpx.Response(200, json=body, request=request)
        return response

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        preflight = asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())
    assert preflight.min_quantity == Decimal("1")
    assert preflight.quantity_step is None


@pytest.mark.parametrize("contract_size", [None, "0", "not-a-number", "Infinity"])
def test_preflight_requires_a_positive_provider_contract_size_before_any_write(contract_size: object) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = _response(request)
        if request.url.path.endswith("/market/metadata"):
            body = response.json()
            body["data"]["markets"][0]["contractSize"] = contract_size
            return httpx.Response(200, json=body, request=request)
        return response

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match="opentrade_contract_size_invalid"):
            asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())
    assert requests
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active", "true"),
        ("active", "false"),
        ("active", 1),
        ("swap", "true"),
        ("swap", "false"),
        ("swap", 1),
        ("contract", "true"),
        ("contract", "false"),
        ("contract", 1),
    ],
)
def test_preflight_requires_literal_true_market_flags_before_any_write(field: str, value: object) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = _response(request)
        if request.url.path.endswith("/market/metadata"):
            body = response.json()
            body["data"]["markets"][0][field] = value
            return httpx.Response(200, json=body, request=request)
        return response

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match="opentrade_exact_market_unavailable"):
            asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())
    assert requests
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("contracts", [None, "not-a-number", "-1", "NaN"])
def test_preflight_rejects_malformed_position_quantity_before_any_write(contracts: object) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = _response(request, external_position=request.url.path.endswith("/positions"))
        if request.url.path.endswith("/positions"):
            body = response.json()
            body["data"][0]["contracts"] = contracts
            return httpx.Response(200, json=body, request=request)
        return response

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match="opentrade_position_quantity_invalid"):
            asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())
    assert requests
    assert all(request.method == "GET" for request in requests)


def test_preflight_ignores_an_explicit_zero_position() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = _response(request, external_position=request.url.path.endswith("/positions"))
        if request.url.path.endswith("/positions"):
            body = response.json()
            body["data"][0]["contracts"] = "0"
            return httpx.Response(200, json=body, request=request)
        return response

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        preflight = asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())
    assert preflight.positions == ()


def test_preflight_reads_account_wide_exposure_not_only_the_candidate_symbol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = _response(request, external_position=request.url.path.endswith("/positions"))
        if request.url.path.endswith("/positions"):
            body = response.json()
            body["data"][0]["symbol"] = "BTC/USDT:USDT"
            return httpx.Response(200, json=body, request=request)
        return response

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        preflight = asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())
    assert [position.provider_symbol for position in preflight.positions] == ["BTC/USDT:USDT"]


def test_startup_inventory_makes_external_exposure_visible_before_any_entry() -> None:
    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(lambda request: _response(request, external_position=True)),
        clock=lambda: NOW,
    )
    try:
        inventory = asyncio.run(adapter.startup(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())
    assert inventory.preflight.observed_at_ms == NOW
    assert inventory.preflight.requested_account_ref == "canary"
    assert inventory.preflight.observed_account_ref is None
    assert [(item.kind, item.exchange_id, item.provider_symbol) for item in inventory.exposures] == [
        ("position", "binance", "DOGE/USDT:USDT")
    ]
    assert adapter.writes_enabled is True


def _order(*, remote_order_id: str | None = "remote-1") -> PreparedOrder:
    return PreparedOrder(
        order_id="order-1",
        case_id="case-1",
        underlying_key="crypto:DOGE",
        account_ref="canary",
        remote_order_id=remote_order_id,
        instrument=_instrument().model_copy(
            update={"provider_symbol": "DOGE/USDT:USDT", "observed_at_ms": NOW - 1_000}
        ),
        mode="live_reviewed",
        side="buy",
        notional_usd=Decimal("10"),
        quantity=Decimal("1"),
        entry_reference=Decimal("10"),
        stop_price=Decimal("9.8"),
        take_profit_price=None,
        must_close_after_ms=1_800_000,
        payload={
            "exchangeId": "binance",
            "symbol": "DOGE/USDT:USDT",
            "side": "buy",
            "type": "market",
            "quantity": "1",
            "hedged": False,
            "stopLossPrice": "9.8",
            "_tracefoldExecutionContractSha256": "a" * 64,
            "_tracefoldPriceTick": "0.01",
        },
    )


def test_composite_negative_read_is_the_only_path_to_absent_confirmed() -> None:
    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(lambda request: _response(request, account_identity=True)),
        clock=lambda: NOW,
    )
    try:
        observation = asyncio.run(adapter.observe(_order()))
    finally:
        asyncio.run(adapter.aclose())
    assert observation.state == "ABSENT_CONFIRMED"
    assert observation.remote_order_id == "remote-1"
    assert observation.evidence["position_count"] == 0
    assert observation.evidence["open_order_ids"] == []


@pytest.mark.parametrize(("close_terminal", "expected"), [(False, False), (True, True)])
def test_exit_retry_requires_the_exact_close_to_be_terminal_with_zero_fill(
    close_terminal: bool, expected: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/positions"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "exchange": "binance",
                            "symbol": "DOGE/USDT:USDT",
                            "side": "long",
                            "contracts": "1",
                            "entryPrice": "10",
                        }
                    ],
                },
                request=request,
            )
        if path.endswith("/orders/closed"):
            rows = (
                [
                    {
                        "exchange": "binance",
                        "symbol": "DOGE/USDT:USDT",
                        "orderId": "close-1",
                        "status": "rejected",
                        "filledQty": 0,
                    }
                ]
                if close_terminal
                else []
            )
            return httpx.Response(200, json={"success": True, "data": rows}, request=request)
        if path.endswith("/trades/history"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "exchange": "binance",
                            "symbol": "DOGE/USDT:USDT",
                            "orderId": "remote-1",
                            "tradeId": "entry-trade",
                            "amount": "1",
                            "timestamp": NOW - 500,
                        }
                    ],
                },
                request=request,
            )
        return _response(request, account_identity=True)

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    order = _order().model_copy(update={"remote_close_order_id": "close-1"})
    try:
        observation = asyncio.run(adapter.observe(order))
    finally:
        asyncio.run(adapter.aclose())

    assert observation.state == "OPEN_UNPROTECTED"
    assert observation.evidence["close_terminal_without_fill"] is expected


def test_composite_snapshot_time_is_captured_after_the_provider_reads() -> None:
    composite_paths = {
        "/account/summary",
        "/positions",
        "/orders/open",
        "/orders/closed",
        "/trades/history",
        "/positions/history",
    }
    seen: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/market/time"):
            timestamp = NOW + 100 if composite_paths <= seen else NOW
            return httpx.Response(200, json={"timestamp": timestamp}, request=request)
        seen.add(next((suffix for suffix in composite_paths if path.endswith(suffix)), path))
        if path.endswith("/positions/history"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "exchange": "binance",
                            "symbol": "DOGE/USDT:USDT",
                            "fullyClosed": True,
                            "entryPrice": "10",
                            "closePrice": "10.2",
                            "openTimestamp": NOW - 1_000,
                            "closeTimestamp": NOW + 50,
                            "trades": [
                                {"orderId": "remote-1", "side": "buy", "reduceOnly": False},
                                {"orderId": "close-1", "side": "sell", "reduceOnly": True},
                            ],
                        }
                    ],
                },
                request=request,
            )
        return _response(request, account_identity=True)

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        observation = asyncio.run(adapter.observe(_order()))
    finally:
        asyncio.run(adapter.aclose())

    assert observation.state == "CLOSED"
    assert observation.observed_at_ms == NOW + 100
    assert observation.closed_at_ms == NOW + 50


def test_reconciliation_sees_an_entry_that_becomes_a_position_between_account_reads() -> None:
    reads = {"orders": False, "positions": False}
    request_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        request_paths.append(path)
        if path.endswith("/orders/open"):
            payload = {
                "success": True,
                "data": (
                    []
                    if reads["positions"]
                    else [
                        {
                            "exchange": "binance",
                            "symbol": "DOGE/USDT:USDT",
                            "side": "buy",
                            "type": "market",
                            "amount": "1",
                            "filledQty": "1",
                            "orderId": "remote-1",
                        }
                    ]
                ),
            }
            reads["orders"] = True
            return httpx.Response(200, json=payload, request=request)
        if path.endswith("/positions"):
            payload = {
                "success": True,
                "data": (
                    [
                        {
                            "exchange": "binance",
                            "symbol": "DOGE/USDT:USDT",
                            "side": "long",
                            "contracts": "1",
                            "entryPrice": "10",
                        }
                    ]
                    if reads["orders"]
                    else []
                ),
            }
            reads["positions"] = True
            return httpx.Response(200, json=payload, request=request)
        if path.endswith("/trades/history"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "exchange": "binance",
                            "symbol": "DOGE/USDT:USDT",
                            "orderId": "remote-1",
                            "tradeId": "trade-1",
                            "amount": "1",
                            "timestamp": NOW - 500,
                        }
                    ],
                },
                request=request,
            )
        return _response(request, account_identity=True)

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        observation = asyncio.run(adapter.observe(_order()))
    finally:
        asyncio.run(adapter.aclose())

    assert observation.state == "OPEN_UNPROTECTED"
    orders_index = next(index for index, path in enumerate(request_paths) if path.endswith("/orders/open"))
    positions_index = next(index for index, path in enumerate(request_paths) if path.endswith("/positions"))
    assert orders_index < positions_index


def test_missing_identity_or_malformed_provider_output_is_unknown_not_absent() -> None:
    adapter_without_identity = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(_response),
        clock=lambda: NOW,
    )
    try:
        assert asyncio.run(adapter_without_identity.observe(_order(remote_order_id=None))).state == "UNKNOWN"
    finally:
        asyncio.run(adapter_without_identity.aclose())

    def malformed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/positions"):
            return httpx.Response(200, json={"success": True, "data": None}, request=request)
        return _response(request)

    adapter_malformed = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(malformed),
        clock=lambda: NOW,
    )
    try:
        observation = asyncio.run(adapter_malformed.observe(_order()))
    finally:
        asyncio.run(adapter_malformed.aclose())
    assert observation.state == "UNKNOWN"
    assert observation.evidence["error_code"] == "opentrade_payload_invalid"


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (302, {"success": True, "data": {}}, "opentrade_http_error"),
        (200, {"data": {}}, "opentrade_payload_invalid"),
    ],
)
def test_preflight_requires_a_2xx_explicit_success(status: int, payload: dict[str, object], error: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/public/metadata/status"):
            return httpx.Response(status, json=payload, request=request)
        return _response(request, account_identity=True)

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match=error):
            asyncio.run(adapter.preflight(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())


def test_provider_response_is_streamed_into_a_hard_size_limit() -> None:
    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1), request=request)

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(oversized),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match="opentrade_payload_too_large"):
            asyncio.run(adapter.startup(instrument=_instrument(), account_ref="canary"))
    finally:
        asyncio.run(adapter.aclose())


def test_reviewed_entry_and_actual_quantity_close_send_only_the_pinned_contract() -> None:
    writes: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        writes.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={"success": True, "data": {"orderId": f"write-{len(writes)}", "filledQty": 0, "avgPrice": 0}},
            request=request,
        )

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        entry = asyncio.run(adapter.submit(_order(remote_order_id=None)))
        close = asyncio.run(adapter.close(_order(), quantity=Decimal("0.4")))
    finally:
        asyncio.run(adapter.aclose())

    assert entry.state == close.state == "ACKNOWLEDGED"
    assert writes == [
        (
            "/open/trader/newsliquid/v1/orders",
            {
                "exchangeId": "binance",
                "symbol": "DOGE/USDT:USDT",
                "side": "buy",
                "type": "market",
                "quantity": 1,
                "hedged": False,
                "stopLossPrice": 9.8,
            },
        ),
        (
            "/open/trader/newsliquid/v1/positions/close",
            {
                "exchangeId": "binance",
                "symbol": "DOGE/USDT:USDT",
                "side": "long",
                "quantity": 0.4,
                "hedged": False,
            },
        ),
    ]


def test_provider_write_serializes_a_decimal_without_binary_float_precision_loss() -> None:
    written: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        written.append(request.content)
        return httpx.Response(
            200,
            json={"success": True, "data": {"orderId": "precise"}},
            request=request,
        )

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    order = _order(remote_order_id=None).model_copy(update={"quantity": Decimal("0.123456789123456789")})
    order = order.model_copy(update={"payload": {**order.payload, "quantity": str(order.quantity)}})
    try:
        receipt = asyncio.run(adapter.submit(order))
    finally:
        asyncio.run(adapter.aclose())
    assert receipt.state == "ACKNOWLEDGED"
    assert json.loads(written[0], parse_float=Decimal)["quantity"] == order.quantity


def test_provider_write_refuses_take_profit_outside_the_reviewed_contract() -> None:
    writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        writes += 1
        return httpx.Response(500, json={}, request=request)

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    order = _order(remote_order_id=None).model_copy(update={"take_profit_price": Decimal("10.4")})
    try:
        with pytest.raises(OpenTradeContractError, match="opentrade_order_payload_invalid"):
            asyncio.run(adapter.submit(order))
    finally:
        asyncio.run(adapter.aclose())
    assert writes == 0


def test_explicit_write_rejection_is_not_ambiguous_but_server_failure_is() -> None:
    def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"success": False}, request=request)

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(rejected),
        clock=lambda: NOW,
    )
    try:
        receipt = asyncio.run(adapter.submit(_order(remote_order_id=None)))
    finally:
        asyncio.run(adapter.aclose())
    assert receipt.state == "REJECTED"
    assert receipt.reason == "opentrade_rejected"

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"success": False}, request=request)),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match="opentrade_http_error"):
            asyncio.run(adapter.submit(_order(remote_order_id=None)))
    finally:
        asyncio.run(adapter.aclose())


@pytest.mark.parametrize(
    "data",
    [
        {"orderId": "accepted-despite-false"},
        {"tradeId": "trade-despite-false"},
        {"positionId": "position-despite-false"},
        {"orderId": 0},
        {"orderId": False},
        {"orderId": []},
        {"orderId": {}},
        {"orderId": None},
        {"orderId": ""},
        {"filledQty": "0.1"},
        {"avgPrice": "10"},
        {"filledQty": "NaN"},
    ],
)
def test_business_rejection_with_execution_evidence_is_ambiguous(data: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "data": data}, request=request)

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match="opentrade_write_outcome_conflict"):
            asyncio.run(adapter.submit(_order(remote_order_id=None)))
    finally:
        asyncio.run(adapter.aclose())


def test_business_rejection_with_explicit_zero_execution_fields_is_definitive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": False, "data": {"filledQty": 0, "avgPrice": 0}},
            request=request,
        )

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        receipt = asyncio.run(adapter.submit(_order(remote_order_id=None)))
    finally:
        asyncio.run(adapter.aclose())
    assert receipt.state == "REJECTED"


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (302, {"success": True, "data": {"orderId": "redirected"}}),
        (302, {"success": False}),
        (200, {"data": {"orderId": "missing-success"}}),
    ],
)
def test_write_ack_requires_a_2xx_explicit_success(status: int, payload: dict[str, object]) -> None:
    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload, request=request)),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match=r"opentrade_(http_error|payload_invalid)"):
            asyncio.run(adapter.submit(_order(remote_order_id=None)))
    finally:
        asyncio.run(adapter.aclose())


@pytest.mark.parametrize("order_id", [{}, [], False, 0, -1, 1.5])
def test_write_ack_rejects_malformed_remote_order_identity(order_id: object) -> None:
    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"success": True, "data": {"orderId": order_id}},
                request=request,
            )
        ),
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(OpenTradeContractError, match="opentrade_order_identity_missing"):
            asyncio.run(adapter.submit(_order(remote_order_id=None)))
    finally:
        asyncio.run(adapter.aclose())


def test_write_ack_normalizes_a_positive_integer_remote_order_identity() -> None:
    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"success": True, "data": {"orderId": 123}},
                request=request,
            )
        ),
        clock=lambda: NOW,
    )
    try:
        receipt = asyncio.run(adapter.submit(_order(remote_order_id=None)))
    finally:
        asyncio.run(adapter.aclose())
    assert receipt.remote_order_id == "123"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, "opentrade_rejected"),
        (401, "opentrade_authentication_failed"),
        (403, "opentrade_authentication_failed"),
        (429, "opentrade_rate_limited"),
    ],
)
def test_non_json_4xx_write_is_an_explicit_rejection(status: int, reason: str) -> None:
    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="provider rejected request", request=request)
        ),
        clock=lambda: NOW,
    )
    try:
        receipt = asyncio.run(adapter.submit(_order(remote_order_id=None)))
    finally:
        asyncio.run(adapter.aclose())
    assert receipt.state == "REJECTED"
    assert receipt.reason == reason


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("working", "WORKING"),
        ("partial", "PARTIAL"),
        ("partial_working", "UNKNOWN"),
        ("partially_reduced", "OPEN_PROTECTED"),
        ("protected", "OPEN_PROTECTED"),
        ("protected_external_order", "UNKNOWN"),
        ("unprotected", "OPEN_UNPROTECTED"),
        ("future_fill", "UNKNOWN"),
        ("external_scale_in_trade", "UNKNOWN"),
        ("external_scale_in_missing_timestamp", "UNKNOWN"),
        ("external_scale_in_seconds_timestamp", "UNKNOWN"),
        ("external_scale_in_too_old_timestamp", "UNKNOWN"),
        ("historical_opening_trade", "OPEN_UNPROTECTED"),
        ("external_reduction_trade", "UNKNOWN"),
        ("external_reduction_missing_timestamp", "UNKNOWN"),
        ("external_reduction_seconds_timestamp", "UNKNOWN"),
        ("historical_closing_trade", "OPEN_UNPROTECTED"),
        ("closed", "CLOSED"),
        ("mixed_order_history", "UNKNOWN"),
        ("mixed_order_history_with_position", "UNKNOWN"),
        ("closed_with_open_order", "UNKNOWN"),
        ("closed_then_external_position", "UNKNOWN"),
        ("external_position_only", "UNKNOWN"),
        ("missing_close_price", "UNKNOWN"),
        ("missing_close_timestamp", "UNKNOWN"),
        ("missing_open_timestamp", "CLOSED"),
        ("reversed_close", "UNKNOWN"),
        ("rejected", "REJECTED"),
        ("rejected_with_open_order", "UNKNOWN"),
        ("rejected_with_unattributed_history", "UNKNOWN"),
        ("rejected_missing_fill", "UNKNOWN"),
        ("rejected_invalid_fill", "UNKNOWN"),
    ],
)
def test_explicit_provider_lifecycle_mapping(scenario: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        response = _response(request, account_identity=True)
        if path.endswith("/positions"):
            quantity = (
                "0.5"
                if scenario in {"partial", "partial_working"}
                else ("0.4" if scenario == "partially_reduced" else "1")
            )
            rows = (
                [
                    {
                        "exchange": "binance",
                        "symbol": "DOGE/USDT:USDT",
                        "side": "long",
                        "contracts": quantity,
                        "entryPrice": "10",
                    }
                ]
                if scenario
                in {
                    "partial",
                    "partial_working",
                    "partially_reduced",
                    "protected",
                    "protected_external_order",
                    "unprotected",
                    "future_fill",
                    "external_scale_in_trade",
                    "external_scale_in_missing_timestamp",
                    "external_scale_in_seconds_timestamp",
                    "external_scale_in_too_old_timestamp",
                    "historical_opening_trade",
                    "external_reduction_trade",
                    "external_reduction_missing_timestamp",
                    "external_reduction_seconds_timestamp",
                    "historical_closing_trade",
                    "mixed_order_history_with_position",
                    "closed_then_external_position",
                    "external_position_only",
                }
                else []
            )
            return httpx.Response(200, json={"success": True, "data": rows}, request=request)
        if path.endswith("/orders/open"):
            rows: list[dict[str, object]] = []
            if scenario == "working":
                rows.append(
                    {
                        "exchange": "binance",
                        "orderId": "remote-1",
                        "symbol": "DOGE/USDT:USDT",
                        "side": "buy",
                        "type": "market",
                        "amount": "1",
                        "status": "open",
                        "filledQty": 0,
                        "reduceOnly": False,
                    }
                )
            if scenario == "partial_working":
                rows.append(
                    {
                        "exchange": "binance",
                        "orderId": "remote-1",
                        "symbol": "DOGE/USDT:USDT",
                        "side": "buy",
                        "type": "market",
                        "amount": "1",
                        "status": "open",
                        "filledQty": "0.5",
                        "reduceOnly": False,
                    }
                )
            if scenario in {"closed_with_open_order", "rejected_with_open_order"}:
                rows.append(
                    {
                        "exchange": "binance",
                        "orderId": "external-1",
                        "symbol": "DOGE/USDT:USDT",
                        "side": "buy",
                        "type": "limit",
                        "amount": "0.2",
                        "status": "open",
                        "reduceOnly": False,
                    }
                )
            if scenario in {"partial", "partially_reduced", "protected", "protected_external_order"}:
                rows.append(
                    {
                        "exchange": "binance",
                        "orderId": "stop-1",
                        "parentOrderId": "remote-1",
                        "symbol": "DOGE/USDT:USDT",
                        "side": "sell",
                        "type": "stop_market",
                        "amount": "1",
                        "triggerPrice": "9.8",
                        "status": "open",
                        "reduceOnly": True,
                    }
                )
            if scenario == "protected_external_order":
                rows.append(
                    {
                        "exchange": "binance",
                        "orderId": "external-1",
                        "symbol": "DOGE/USDT:USDT",
                        "side": "buy",
                        "type": "limit",
                        "amount": "0.2",
                        "status": "open",
                        "reduceOnly": False,
                    }
                )
            return httpx.Response(200, json={"success": True, "data": rows}, request=request)
        if path.endswith("/orders/closed") and scenario in {
            "rejected",
            "rejected_with_open_order",
            "rejected_with_unattributed_history",
            "rejected_missing_fill",
            "rejected_invalid_fill",
        }:
            zero_fill_scenarios = {
                "rejected",
                "rejected_with_open_order",
                "rejected_with_unattributed_history",
            }
            fill: dict[str, object] = (
                {}
                if scenario == "rejected_missing_fill"
                else {"filledQty": 0 if scenario in zero_fill_scenarios else "NaN"}
            )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "exchange": "binance",
                            "orderId": "remote-1",
                            "symbol": "DOGE/USDT:USDT",
                            "status": "rejected",
                            **fill,
                        }
                    ],
                },
                request=request,
            )
        if path.endswith("/trades/history") and scenario in {
            "partial",
            "partial_working",
            "partially_reduced",
            "protected",
            "protected_external_order",
            "unprotected",
            "future_fill",
            "external_scale_in_trade",
            "external_scale_in_missing_timestamp",
            "external_scale_in_seconds_timestamp",
            "external_scale_in_too_old_timestamp",
            "historical_opening_trade",
            "external_reduction_trade",
            "external_reduction_missing_timestamp",
            "external_reduction_seconds_timestamp",
            "historical_closing_trade",
            "mixed_order_history_with_position",
        }:
            amount = "0.5" if scenario in {"partial", "partial_working"} else "1"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "exchange": "binance",
                            "tradeId": "trade-1",
                            "orderId": "remote-1",
                            "symbol": "DOGE/USDT:USDT",
                            "amount": amount,
                            "timestamp": NOW + 1 if scenario == "future_fill" else NOW - 500,
                        },
                        *(
                            [
                                {
                                    "exchange": "binance",
                                    "tradeId": "external-trade-1",
                                    "orderId": "external-order-1",
                                    "symbol": "DOGE/USDT:USDT",
                                    "side": "buy",
                                    "reduceOnly": False,
                                    "amount": "0.5",
                                    **(
                                        {}
                                        if scenario == "external_scale_in_missing_timestamp"
                                        else {
                                            "timestamp": (
                                                (NOW - 1_001) // 1_000
                                                if scenario == "external_scale_in_seconds_timestamp"
                                                else (
                                                    NOW - 8 * 86_400_000
                                                    if scenario == "external_scale_in_too_old_timestamp"
                                                    else NOW - 1_001
                                                )
                                            )
                                        }
                                    ),
                                }
                            ]
                            if scenario
                            in {
                                "external_scale_in_trade",
                                "external_scale_in_missing_timestamp",
                                "external_scale_in_seconds_timestamp",
                                "external_scale_in_too_old_timestamp",
                            }
                            else []
                        ),
                        *(
                            [
                                {
                                    "exchange": "binance",
                                    "tradeId": "historical-trade-1",
                                    "orderId": "historical-order-1",
                                    "symbol": "DOGE/USDT:USDT",
                                    "side": "buy",
                                    "reduceOnly": False,
                                    "amount": "1",
                                    "timestamp": NOW - 31_001,
                                }
                            ]
                            if scenario == "historical_opening_trade"
                            else []
                        ),
                        *(
                            [
                                {
                                    "exchange": "binance",
                                    "tradeId": "external-close-trade-1",
                                    "orderId": "external-close-order-1",
                                    "symbol": "DOGE/USDT:USDT",
                                    "side": "sell",
                                    "reduceOnly": True,
                                    "amount": "0.4",
                                    **(
                                        {}
                                        if scenario == "external_reduction_missing_timestamp"
                                        else {
                                            "timestamp": (
                                                (NOW - 501) // 1_000
                                                if scenario == "external_reduction_seconds_timestamp"
                                                else (
                                                    NOW - 31_001
                                                    if scenario == "historical_closing_trade"
                                                    else NOW - 501
                                                )
                                            )
                                        }
                                    ),
                                }
                            ]
                            if scenario
                            in {
                                "external_reduction_trade",
                                "external_reduction_missing_timestamp",
                                "external_reduction_seconds_timestamp",
                                "historical_closing_trade",
                            }
                            else []
                        ),
                    ],
                },
                request=request,
            )
        if path.endswith("/positions/history") and scenario in {
            "closed",
            "mixed_order_history",
            "mixed_order_history_with_position",
            "closed_with_open_order",
            "closed_then_external_position",
            "missing_close_price",
            "missing_close_timestamp",
            "missing_open_timestamp",
            "reversed_close",
            "rejected_with_unattributed_history",
        }:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "exchange": "binance",
                            "symbol": "DOGE/USDT:USDT",
                            "fullyClosed": True,
                            "entryPrice": "10",
                            **(
                                {}
                                if scenario == "missing_open_timestamp"
                                else {"openTimestamp": NOW - 500 if scenario == "reversed_close" else NOW - 10_000}
                            ),
                            **({} if scenario == "missing_close_price" else {"closePrice": "10.2"}),
                            **({} if scenario == "missing_close_timestamp" else {"closeTimestamp": NOW - 1_000}),
                            "trades": [
                                {"orderId": "remote-1", "side": "buy", "reduceOnly": False},
                                *(
                                    [{"orderId": "external-1", "side": "buy", "reduceOnly": False}]
                                    if scenario
                                    in {
                                        "mixed_order_history",
                                        "mixed_order_history_with_position",
                                        "rejected_with_unattributed_history",
                                    }
                                    else []
                                ),
                                *(
                                    [{"orderId": "close-1", "side": "sell", "reduceOnly": True}]
                                    if scenario != "rejected_with_unattributed_history"
                                    else []
                                ),
                            ],
                        }
                    ],
                },
                request=request,
            )
        return response

    adapter = OpenTradeAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    try:
        observation = asyncio.run(adapter.observe(_order()))
    finally:
        asyncio.run(adapter.aclose())
    assert observation.state == expected
    if scenario in {"partial", "partially_reduced", "protected"}:
        assert observation.protection is not None
        assert observation.protection.parent_remote_order_id == "remote-1"
    if scenario == "partial_working":
        assert observation.evidence["entry_remainder_working"] is True
    if scenario == "closed":
        assert observation.closed_at_ms == NOW - 1_000
        assert observation.exit_price == Decimal("10.2")
    if scenario == "missing_open_timestamp":
        assert observation.first_fill_at_ms is None
        assert observation.closed_at_ms == NOW - 1_000
    if scenario in {"future_fill", "missing_close_timestamp", "reversed_close"}:
        assert observation.evidence["error_code"] == "opentrade_timestamp_invalid"
    if scenario in {
        "external_scale_in_seconds_timestamp",
        "external_scale_in_too_old_timestamp",
        "external_reduction_seconds_timestamp",
    }:
        assert observation.evidence["error_code"] == "opentrade_timestamp_invalid"
    if scenario == "missing_close_price":
        assert observation.evidence["error_code"] == "opentrade_exit_price_invalid"
    if scenario == "external_scale_in_trade":
        assert observation.evidence["unmatched_opening_trade_count"] == 1
    if scenario == "external_scale_in_missing_timestamp":
        assert "error_code" not in observation.evidence
        assert observation.evidence["unmatched_opening_trade_count"] == 1
    if scenario == "historical_opening_trade":
        assert observation.evidence["unmatched_opening_trade_count"] == 0
    if scenario in {"external_reduction_trade", "external_reduction_missing_timestamp"}:
        assert observation.evidence["unmatched_closing_trade_count"] == 1
    if scenario == "historical_closing_trade":
        assert observation.evidence["unmatched_closing_trade_count"] == 0
    if scenario in {"rejected_missing_fill", "rejected_invalid_fill"}:
        assert observation.evidence["error_code"] == "opentrade_fill_quantity_invalid"
    if scenario == "rejected_with_open_order":
        assert "error_code" not in observation.evidence
        assert observation.evidence["open_order_ids"] == ["external-1"]
    if scenario == "rejected_with_unattributed_history":
        assert "error_code" not in observation.evidence
        assert observation.evidence["entry_history_correlated_count"] == 1
        assert observation.evidence["entry_history_count"] == 0


def test_provider_snapshot_identity_is_stable_across_collection_order() -> None:
    def observe(rows: list[dict[str, object]]):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/orders/open"):
                return httpx.Response(200, json={"success": True, "data": rows}, request=request)
            return _response(request, account_identity=True)

        adapter = OpenTradeAdapter(
            base_url="https://example.invalid",
            token="secret-token",
            transport=httpx.MockTransport(handler),
            clock=lambda: NOW,
        )
        try:
            return asyncio.run(adapter.observe(_order()))
        finally:
            asyncio.run(adapter.aclose())

    def row(remote_id: str) -> dict[str, object]:
        return {
            "exchange": "binance",
            "orderId": remote_id,
            "symbol": "DOGE/USDT:USDT",
            "side": "buy",
            "type": "limit",
            "amount": "1",
            "status": "open",
            "reduceOnly": False,
        }

    first = observe([row("external-b"), row("external-a")])
    second = observe([row("external-a"), row("external-b")])
    assert first.evidence["open_order_ids"] == ["external-a", "external-b"]
    assert first.snapshot_sha256 == second.snapshot_sha256
