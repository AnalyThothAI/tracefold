"""Public funding history is interval-bounded, typed, and signed for a long."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest

from tracefold.integrations.venues import fetch_binance_funding_rates, fetch_hyperliquid_funding_rates
from tracefold.integrations.venues.errors import VenueExpectedError


def test_binance_funding_history_is_half_open_and_provider_typed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[{"symbol": "BTCUSDT", "fundingTime": 150, "fundingRate": "0.0001", "rateType": "Regular"}],
        )

    rows = asyncio.run(
        fetch_binance_funding_rates(
            "BTCUSDT",
            start_ms=100,
            end_ms=200,
            transport=httpx.MockTransport(handler),
        )
    )

    assert [(row.funding_at_ms, row.funding_rate) for row in rows] == [(150, Decimal("0.0001"))]
    assert seen[0].url.params["endTime"] == "199"


def test_hyperliquid_empty_history_is_a_complete_known_zero_window() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200, json=[])

    rows = asyncio.run(
        fetch_hyperliquid_funding_rates(
            "BTC",
            start_ms=100,
            end_ms=200,
            transport=httpx.MockTransport(handler),
        )
    )

    assert rows == ()
    assert seen == [{"type": "fundingHistory", "coin": "BTC", "startTime": 100, "endTime": 199}]


def test_funding_payload_identity_or_window_drift_fails_closed() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json=[{"coin": "ETH", "time": 150, "fundingRate": "0.0001"}],
        )
    )
    with pytest.raises(VenueExpectedError, match="venue_payload_invalid"):
        asyncio.run(fetch_hyperliquid_funding_rates("BTC", start_ms=100, end_ms=200, transport=transport))
