from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

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


def test_non_binance_funding_client_is_rejected_before_provider_io() -> None:
    with pytest.raises(RuntimeError, match="nautilus_funding_client_unsupported"):
        asyncio.run(
            funding.load_funding_cashflows(
                SimpleNamespace(),
                provider_instrument_id="SOLUSDT",
                opened_at_ms=1_000,
                verified_at_ms=1_500,
            )
        )
