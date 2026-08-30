from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qsl

import httpx
import pytest

from tracefold.integrations.binance_manual import BinanceManualClient
from tracefold.trading import (
    ManualExecutionPlan,
    ManualVenueError,
    trading_provider_account_fingerprint,
)
from tracefold.trading.contracts import canonical_sha256


def test_signed_live_requests_use_the_fixed_production_origin_and_keep_credentials_out_of_url() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(200, json={"totalMarginBalance": "123.45"})
        if request.url.path == "/fapi/v1/accountConfig":
            return httpx.Response(200, json={"canTrade": True})
        if request.url.path == "/fapi/v3/balance":
            return httpx.Response(200, json=[{"asset": "USDT", "accountAlias": "demo-account-7"}])
        if request.url.path == "/fapi/v1/order" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "clientOrderId": "tfm-e-abc",
                    "orderId": 123,
                    "status": "NEW",
                    "executedQty": "0",
                    "avgPrice": "0",
                },
            )
        raise AssertionError((request.method, request.url.path))

    client = BinanceManualClient(
        api_key="key-value",
        api_secret="secret-value",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: 1_900_000_000_000,
    )

    account = client.account()
    receipt = client.submit_market_order(
        symbol="BTCUSDT",
        side="BUY",
        quantity="0.010",
        client_order_id="tfm-e-abc",
    )

    assert account.equity_usd == Decimal("123.45") and account.can_trade is True
    assert account.provider_account_fingerprint == trading_provider_account_fingerprint(
        venue="binance_usdm_live",
        provider_account_id="demo-account-7",
    )
    assert receipt.client_id == "tfm-e-abc" and receipt.provider_id == "123"
    for request in requests:
        assert request.url.host == "fapi.binance.com"
        assert "key-value" not in str(request.url) and "secret-value" not in str(request.url)
        assert request.headers["X-MBX-APIKEY"] == "key-value"
        encoded = request.url.query.decode() if request.method == "GET" else request.content.decode()
        pairs = parse_qsl(encoded)
        signature = dict(pairs)["signature"]
        signed = "&".join(f"{key}={value}" for key, value in pairs if key != "signature")
        assert signature == hmac.new(b"secret-value", signed.encode(), hashlib.sha256).hexdigest()
    assert requests[0].content == b""
    assert "timestamp=" in requests[0].url.query.decode()
    assert [request.url.path for request in requests[:3]] == [
        "/fapi/v3/account",
        "/fapi/v1/accountConfig",
        "/fapi/v3/balance",
    ]
    assert dict(parse_qsl(requests[-1].content.decode()))["newClientOrderId"] == "tfm-e-abc"


def test_zero_live_margin_balance_is_a_valid_account_response() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        responses = {
            "/fapi/v3/account": {"totalMarginBalance": "0.00000000"},
            "/fapi/v1/accountConfig": {"canTrade": True},
            "/fapi/v3/balance": [{"asset": "USDT", "accountAlias": "live-account-0"}],
        }
        return httpx.Response(200, json=responses[request.url.path])

    client = BinanceManualClient(
        api_key="key-value",
        api_secret="secret-value",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: 1_900_000_000_000,
    )

    assert client.account().equity_usd == Decimal("0")


def test_tp_and_sl_use_current_algo_endpoint_with_close_all_and_mark_price() -> None:
    observed: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        values = dict(parse_qsl(request.content.decode()))
        observed.append(values)
        return httpx.Response(
            200,
            json={
                "clientAlgoId": values["clientAlgoId"],
                "algoId": 77,
                "algoStatus": "NEW",
            },
        )

    client = BinanceManualClient(
        api_key="key-value",
        api_secret="secret-value",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: 1_900_000_000_000,
    )

    client.submit_close_algo_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        trigger_price="60000.0",
        client_algo_id="tfm-s-abc",
    )
    client.submit_close_algo_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="TAKE_PROFIT_MARKET",
        trigger_price="68000.0",
        client_algo_id="tfm-t-abc",
    )

    assert [values["type"] for values in observed] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert all(values["algoType"] == "CONDITIONAL" for values in observed)
    assert all(values["closePosition"] == "true" and values["workingType"] == "MARK_PRICE" for values in observed)
    assert all("quantity" not in values and "reduceOnly" not in values for values in observed)


def test_unknown_write_timeout_is_ambiguous_and_never_auto_classified_as_failed() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"code": -1000, "msg": "Unknown error, please check your request or try again later."},
        )

    client = BinanceManualClient(
        api_key="key-value",
        api_secret="secret-value",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: 1_900_000_000_000,
    )

    with pytest.raises(ManualVenueError) as caught:
        client.submit_market_order(
            symbol="BTCUSDT",
            side="BUY",
            quantity="0.010",
            client_order_id="tfm-e-abc",
        )

    assert caught.value.code == "binance_manual_write_ambiguous"
    assert caught.value.ambiguous is True
    assert "Unknown error" not in str(caught.value)


def test_explicit_provider_write_rejection_is_not_classified_as_ambiguous() -> None:
    client = BinanceManualClient(
        api_key="key-value",
        api_secret="secret-value",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, json={"code": -2010, "msg": "Order rejected"})
        ),
        clock_ms=lambda: 1_900_000_000_000,
    )

    with pytest.raises(ManualVenueError) as caught:
        client.submit_market_order(
            symbol="BTCUSDT",
            side="BUY",
            quantity="0.010",
            client_order_id="tfm-e-abc",
        )

    assert caught.value.code == "binance_manual_provider_rejected"
    assert caught.value.provider_code == -2010
    assert caught.value.ambiguous is False and caught.value.retryable is False


def test_read_service_failure_is_retryable_without_becoming_ambiguous() -> None:
    client = BinanceManualClient(
        api_key="key-value",
        api_secret="secret-value",
        transport=httpx.MockTransport(lambda _request: httpx.Response(503, json={"code": -1000})),
        clock_ms=lambda: 1_900_000_000_000,
    )

    with pytest.raises(ManualVenueError) as caught:
        client.position("BTCUSDT")

    assert caught.value.code == "binance_manual_read_retryable"
    assert caught.value.retryable is True and caught.value.ambiguous is False


def test_flat_position_uses_symbol_config_and_public_mark_when_v3_omits_the_symbol() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v3/positionRisk":
            return httpx.Response(200, json=[])
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(200, json=[{"symbol": "HYPEUSDT", "leverage": 5}])
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"symbol": "HYPEUSDT", "markPrice": "83.25"})
        raise AssertionError(request.url.path)

    client = BinanceManualClient(
        api_key="key-value",
        api_secret="secret-value",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: 1_900_000_000_000,
    )

    position = client.position("HYPEUSDT")

    assert position.quantity == 0
    assert position.entry_price == 0
    assert position.mark_price == Decimal("83.25")
    assert position.leverage == 5
    assert [request.url.path for request in requests] == [
        "/fapi/v3/positionRisk",
        "/fapi/v1/symbolConfig",
        "/fapi/v1/premiumIndex",
    ]
    assert dict(parse_qsl(requests[-1].url.query.decode())) == {"symbol": "HYPEUSDT"}


def test_open_position_uses_current_symbol_config_leverage_not_removed_v3_field() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v3/positionRisk":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "HYPEUSDT",
                        "positionSide": "BOTH",
                        "positionAmt": "0.12",
                        "entryPrice": "82.50",
                        "markPrice": "83.25",
                        "unRealizedProfit": "0.09",
                        "liquidationPrice": "70.00",
                    }
                ],
            )
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(200, json=[{"symbol": "HYPEUSDT", "leverage": 7}])
        raise AssertionError(request.url.path)

    client = BinanceManualClient(
        api_key="key-value",
        api_secret="secret-value",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: 1_900_000_000_000,
    )

    position = client.position("HYPEUSDT")

    assert position.quantity == Decimal("0.12")
    assert position.entry_price == Decimal("82.50")
    assert position.leverage == 7


def test_algo_cancel_validates_the_current_binance_success_shape() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        values = dict(parse_qsl(request.url.query.decode() if request.method == "GET" else request.content.decode()))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "clientAlgoId": values["clientAlgoId"],
                    "algoId": 77,
                    "algoStatus": "NEW",
                },
            )
        return httpx.Response(
            200,
            json={"clientAlgoId": values["clientAlgoId"], "algoId": 77, "code": "200", "msg": "success"},
        )

    client = BinanceManualClient(
        api_key="key-value",
        api_secret="secret-value",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: 1_900_000_000_000,
    )
    plan_values = {
        "plan_version": "manual_execution_plan_v1",
        "intent_id": "a" * 64,
        "symbol": "BTCUSDT",
        "entry_side": "BUY",
        "close_side": "SELL",
        "leverage": 5,
        "quantity": Decimal("0.01"),
        "stop_loss_trigger": Decimal("99"),
        "take_profit_trigger": Decimal("102"),
        "entry_client_order_id": "tfm-e-abc",
        "take_profit_client_order_id": "tfm-t-abc",
        "stop_loss_client_order_id": "tfm-s-abc",
    }
    plan = ManualExecutionPlan(plan_sha256=canonical_sha256(plan_values), **plan_values)

    assert client.cancel_leg(plan, "take_profit") is True
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/fapi/v1/algoOrder"),
        ("DELETE", "/fapi/v1/algoOrder"),
    ]
