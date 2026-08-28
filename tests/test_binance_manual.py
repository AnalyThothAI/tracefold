from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qsl

import httpx
import pytest

from tracefold.integrations.binance_manual import BinanceManualClient
from tracefold.trading import ManualVenueError, trading_provider_account_fingerprint


def test_signed_demo_requests_keep_credentials_out_of_url_and_use_client_ids() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(200, json={"canTrade": True, "totalMarginBalance": "123.45"})
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
        venue="binance_usdm_demo",
        provider_account_id="demo-account-7",
    )
    assert receipt.client_id == "tfm-e-abc" and receipt.provider_id == "123"
    for request in requests:
        assert request.url.host == "demo-fapi.binance.com"
        assert "key-value" not in str(request.url) and "secret-value" not in str(request.url)
        assert request.headers["X-MBX-APIKEY"] == "key-value"
        encoded = request.url.query.decode() if request.method == "GET" else request.content.decode()
        pairs = parse_qsl(encoded)
        signature = dict(pairs)["signature"]
        signed = "&".join(f"{key}={value}" for key, value in pairs if key != "signature")
        assert signature == hmac.new(b"secret-value", signed.encode(), hashlib.sha256).hexdigest()
    assert requests[0].content == b""
    assert "timestamp=" in requests[0].url.query.decode()
    assert dict(parse_qsl(requests[-1].content.decode()))["newClientOrderId"] == "tfm-e-abc"


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
