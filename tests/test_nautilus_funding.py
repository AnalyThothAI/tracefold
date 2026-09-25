from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from tracefold.integrations.nautilus.oi_runtime.config import BinanceRuntimeCredentials
from tracefold.integrations.nautilus.oi_runtime.funding import BinanceFundingIncome, FundingCashflow, watch_funding


def test_signed_demo_income_keeps_venue_transaction_identity() -> None:
    class Clock:
        def timestamp_ms(self) -> int:
            return 1_000_000

    class Client:
        async def sign_request(self, **kwargs: object) -> bytes:
            assert kwargs["url_path"] == "/fapi/v1/income"
            assert kwargs["payload"]["incomeType"] == "FUNDING_FEE"
            return json.dumps(
                [
                    {
                        "tranId": 42,
                        "symbol": "SOLUSDT",
                        "asset": "USDT",
                        "incomeType": "FUNDING_FEE",
                        "income": "-0.125",
                        "time": 1000,
                    },
                ]
            ).encode()

    reader = BinanceFundingIncome(
        mode="paper",
        credentials=BinanceRuntimeCredentials(api_key="fixture", api_secret="fixture"),
        clock=Clock(),
        client=Client(),
    )
    flows = asyncio.run(reader.read(900, 1100))
    assert flows == (FundingCashflow("42", "SOLUSDT", "USDT", Decimal("-0.125"), 1000),)


def test_funding_scan_records_coverage_only_after_all_cashflows_offer() -> None:
    async def exercise(accept: bool) -> list[str]:
        stop = asyncio.Event()
        events: list[str] = []

        async def read(_start: int, _end: int) -> tuple[FundingCashflow, ...]:
            stop.set()
            return (FundingCashflow("42", "SOLUSDT", "USDT", Decimal("1"), 1000),)

        def observe(_flow: FundingCashflow) -> bool:
            events.append("cashflow")
            return accept

        def coverage(_start: int, _end: int) -> bool:
            events.append("coverage")
            return True

        await watch_funding(read, observe, coverage, stop, clock_ms=lambda: 2_000)
        return events

    assert asyncio.run(exercise(True)) == ["cashflow", "coverage"]
    assert asyncio.run(exercise(False)) == ["cashflow"]


def test_ambiguous_or_nonfinite_income_refuses_scan() -> None:
    with pytest.raises(ValueError, match="funding_income_invalid"):
        FundingCashflow.from_venue(
            {
                "tranId": 42,
                "symbol": "SOLUSDT",
                "asset": "USDT",
                "incomeType": "FUNDING_FEE",
                "income": "NaN",
                "time": 1000,
            }
        )


def test_signed_income_paginates_and_refuses_conflicting_transaction_identity() -> None:
    class Clock:
        def timestamp_ms(self) -> int:
            return 1_000_000

    class Client:
        def __init__(self) -> None:
            self.pages: list[int] = []

        async def sign_request(self, **kwargs: object) -> bytes:
            page = int(kwargs["payload"]["page"])
            self.pages.append(page)
            rows = (
                [
                    {
                        "tranId": index,
                        "symbol": "SOLUSDT",
                        "asset": "USDT",
                        "incomeType": "FUNDING_FEE",
                        "income": "0.1",
                        "time": 1000,
                    }
                    for index in range(1, 1001)
                ]
                if page == 1
                else [
                    {
                        "tranId": 1001,
                        "symbol": "SOLUSDT",
                        "asset": "USDT",
                        "incomeType": "FUNDING_FEE",
                        "income": "0.2",
                        "time": 1001,
                    }
                ]
            )
            return json.dumps(rows).encode()

    client = Client()
    reader = BinanceFundingIncome(
        mode="paper",
        credentials=BinanceRuntimeCredentials(api_key="fixture", api_secret="fixture"),
        clock=Clock(),
        client=client,
    )
    flows = asyncio.run(reader.read(900, 1100))
    assert len(flows) == 1001
    assert client.pages == [1, 2]

    class Conflict(Client):
        async def sign_request(self, **_kwargs: object) -> bytes:
            return json.dumps(
                [
                    {
                        "tranId": 42,
                        "symbol": "SOLUSDT",
                        "asset": "USDT",
                        "incomeType": "FUNDING_FEE",
                        "income": amount,
                        "time": 1000,
                    }
                    for amount in ("0.1", "0.2")
                ]
            ).encode()

    conflicting = BinanceFundingIncome(
        mode="paper",
        credentials=BinanceRuntimeCredentials(api_key="fixture", api_secret="fixture"),
        clock=Clock(),
        client=Conflict(),
    )
    with pytest.raises(ValueError, match="funding_income_identity_conflict"):
        asyncio.run(conflicting.read(900, 1100))
