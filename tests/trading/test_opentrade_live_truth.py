"""OpenTrade's read-only execution truth seam for #185 PR-C1."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest

from tracefold.integrations.opentrade import OpenTradeContractError, OpenTradeReadAdapter
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


def _response(request: httpx.Request, *, external_position: bool = False) -> httpx.Response:
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

    adapter = OpenTradeReadAdapter(
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


def test_amount_min_is_not_guessed_to_be_the_quantity_step() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = _response(request)
        if request.url.path.endswith("/market/metadata"):
            body = response.json()
            del body["data"]["markets"][0]["quantityStep"]
            return httpx.Response(200, json=body, request=request)
        return response

    adapter = OpenTradeReadAdapter(
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


def test_preflight_reads_account_wide_exposure_not_only_the_candidate_symbol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = _response(request, external_position=request.url.path.endswith("/positions"))
        if request.url.path.endswith("/positions"):
            body = response.json()
            body["data"][0]["symbol"] = "BTC/USDT:USDT"
            return httpx.Response(200, json=body, request=request)
        return response

    adapter = OpenTradeReadAdapter(
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
    adapter = OpenTradeReadAdapter(
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
    assert adapter.writes_enabled is False


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
        payload={"symbol": "DOGE/USDT:USDT"},
    )


def test_composite_negative_read_is_the_only_path_to_absent_confirmed() -> None:
    adapter = OpenTradeReadAdapter(
        base_url="https://example.invalid",
        token="secret-token",
        transport=httpx.MockTransport(_response),
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


def test_missing_identity_or_malformed_provider_output_is_unknown_not_absent() -> None:
    adapter_without_identity = OpenTradeReadAdapter(
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

    adapter_malformed = OpenTradeReadAdapter(
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


def test_provider_response_is_streamed_into_a_hard_size_limit() -> None:
    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1), request=request)

    adapter = OpenTradeReadAdapter(
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
