from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from tracefold.integrations.nautilus import funding


class _Clock:
    def timestamp_ms(self) -> int:
        return 2_000


class _BinanceHttp:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.requests: list[dict[str, object]] = []

    async def sign_request(self, **kwargs: object) -> bytes:
        self.requests.append(kwargs)
        return json.dumps(self.payload).encode()


def test_binance_funding_ledger_sums_signed_provider_cashflows() -> None:
    http = _BinanceHttp(
        [
            {
                "symbol": "SOLUSDT",
                "incomeType": "FUNDING_FEE",
                "income": "-0.12",
                "asset": "USDT",
                "time": 1_100,
                "tranId": 11,
                "tradeId": "",
            },
            {
                "symbol": "SOLUSDT",
                "incomeType": "FUNDING_FEE",
                "income": "0.02",
                "asset": "USDT",
                "time": 1_200,
                "tranId": 12,
                "tradeId": "",
            },
        ]
    )
    client = SimpleNamespace(_http_client=http, _clock=_Clock())

    value = asyncio.run(
        funding._binance_funding_cashflows(
            client,  # type: ignore[arg-type]
            symbol="SOLUSDT",
            opened_at_ms=1_000,
            verified_at_ms=1_500,
        )
    )

    assert value == {"USDT": "-0.10"}
    assert http.requests[0]["payload"] == {
        "symbol": "SOLUSDT",
        "incomeType": "FUNDING_FEE",
        "startTime": "1000",
        "endTime": "1500",
        "limit": "1000",
        "recvWindow": "5000",
        "timestamp": "2000",
    }


def test_binance_empty_success_is_known_zero_but_duplicate_identity_fails_closed() -> None:
    empty = SimpleNamespace(_http_client=_BinanceHttp([]), _clock=_Clock())
    assert (
        asyncio.run(
            funding._binance_funding_cashflows(
                empty,  # type: ignore[arg-type]
                symbol="SOLUSDT",
                opened_at_ms=1_000,
                verified_at_ms=1_500,
            )
        )
        == {}
    )
    row = {
        "symbol": "SOLUSDT",
        "incomeType": "FUNDING_FEE",
        "income": "-0.01",
        "asset": "USDT",
        "time": 1_100,
        "tranId": 11,
        "tradeId": "",
    }
    duplicate = SimpleNamespace(_http_client=_BinanceHttp([row, row]), _clock=_Clock())
    with pytest.raises(RuntimeError, match="nautilus_binance_funding_payload_invalid"):
        asyncio.run(
            funding._binance_funding_cashflows(
                duplicate,  # type: ignore[arg-type]
                symbol="SOLUSDT",
                opened_at_ms=1_000,
                verified_at_ms=1_500,
            )
        )


def test_hyperliquid_funding_ledger_filters_exact_core_or_hip3_coin() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {
                    "time": 1_100,
                    "hash": "0x1",
                    "delta": {"type": "funding", "coin": "xyz:CL", "usdc": "-0.3"},
                },
                {
                    "time": 1_200,
                    "hash": "0x2",
                    "delta": {"type": "funding", "coin": "SOL", "usdc": "4"},
                },
            ],
        )

    client = SimpleNamespace(
        _account_address="0x" + "1" * 40,
        _config=SimpleNamespace(proxy_url=None),
    )
    value = asyncio.run(
        funding._hyperliquid_funding_cashflows(
            client,  # type: ignore[arg-type]
            coin="xyz:CL",
            opened_at_ms=1_000,
            verified_at_ms=1_500,
            transport=httpx.MockTransport(handler),
        )
    )

    assert value == {"USDC": "-0.3"}
    assert seen == [
        {
            "type": "userFunding",
            "user": "0x" + "1" * 40,
            "startTime": 1_000,
            "endTime": 1_500,
        }
    ]
    assert funding._hyperliquid_coin("main:SOL") == "SOL"
    assert funding._hyperliquid_coin("dex:xyz:XYZ-CL") == "xyz:CL"


def test_hyperliquid_transport_or_truncation_never_becomes_known_zero() -> None:
    client = SimpleNamespace(
        _account_address="0x" + "1" * 40,
        _config=SimpleNamespace(proxy_url=None),
    )

    def truncated(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{}] * 500)

    with pytest.raises(RuntimeError, match="nautilus_hyperliquid_funding_coverage_unproven"):
        asyncio.run(
            funding._hyperliquid_funding_cashflows(
                client,  # type: ignore[arg-type]
                coin="SOL",
                opened_at_ms=1_000,
                verified_at_ms=1_500,
                transport=httpx.MockTransport(truncated),
            )
        )

    def failed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            funding._hyperliquid_funding_cashflows(
                client,  # type: ignore[arg-type]
                coin="SOL",
                opened_at_ms=1_000,
                verified_at_ms=1_500,
                transport=httpx.MockTransport(failed),
            )
        )
