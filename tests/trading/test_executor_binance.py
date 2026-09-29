"""Signed DEMO adapter assertions use a transport that cannot reach a real account."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest

from tracefold.integrations.trading.binance import BinanceFailure, DemoBinance


def test_execution_adapter_refuses_live_credentials() -> None:
    with pytest.raises(ValueError, match="execution_requires_demo_credentials"):
        DemoBinance(environment="LIVE", api_key="fixture", api_secret="fixture")


def test_market_and_algo_orders_have_distinct_ids_and_correct_quantity_semantics() -> None:
    async def run() -> None:
        sent: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            assert request.url.host == "demo-fapi.binance.com"
            assert request.headers["X-MBX-APIKEY"] == "test-key"
            assert "signature" in request.url.params
            if request.url.path == "/fapi/v1/order":
                return httpx.Response(
                    200, json={"status": "FILLED", "clientOrderId": request.url.params["newClientOrderId"]}
                )
            if request.url.path == "/fapi/v1/algoOrder":
                return httpx.Response(
                    200, json={"algoStatus": "NEW", "clientAlgoId": request.url.params["clientAlgoId"]}
                )
            raise AssertionError(request.url.path)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            venue = DemoBinance(
                environment="DEMO", api_key="test-key", api_secret="test-secret", client=client, clock_ms=lambda: 1_000
            )
            await venue.market_order(
                symbol="SOLUSDT", side="BUY", quantity=Decimal("1.25"), client_id="tf" + "a" * 30, reduce_only=False
            )
            await venue.protection_order(
                symbol="SOLUSDT", side="SELL", leg="sl", trigger_price=Decimal("98"), client_id="tf" + "b" * 30
            )
            await venue.protection_order(
                symbol="SOLUSDT",
                side="SELL",
                leg="tp",
                trigger_price=Decimal("104"),
                client_id="tf" + "c" * 30,
                quantity=Decimal("1.25"),
            )
        entry, stop, take = (request.url.params for request in sent)
        assert entry["newOrderRespType"] == "RESULT" and entry["quantity"] == "1.25"
        assert entry["reduceOnly"] == "false"
        assert stop["closePosition"] == "true" and "quantity" not in stop and "reduceOnly" not in stop
        assert stop["workingType"] == "MARK_PRICE"
        assert take["quantity"] == "1.25" and take["reduceOnly"] == "true"
        assert "closePosition" not in take

    asyncio.run(run())


def test_order_not_found_is_distinct_from_ambiguous_503() -> None:
    async def run() -> None:
        status = 400

        async def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"code": -2013 if status == 400 else -1000, "msg": "fixture"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            venue = DemoBinance(environment="DEMO", api_key="test-key", api_secret="test-secret", client=client)
            assert await venue.query_order("SOLUSDT", "tf" + "a" * 30) is None
            status = 503
            with pytest.raises(BinanceFailure) as caught:
                await venue.query_order("SOLUSDT", "tf" + "a" * 30)
            assert caught.value.transient

    asyncio.run(run())
