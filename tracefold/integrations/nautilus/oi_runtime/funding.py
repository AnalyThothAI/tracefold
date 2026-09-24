"""Signed Binance USD-M funding-income reconciliation for PAPER.

GET /fapi/v1/income (FUNDING_FEE) is account cashflow, unlike public funding
rates. A complete paginated read also proves a zero-cashflow interval.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger
from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance.factories import get_cached_binance_http_client
from nautilus_trader.common.component import LiveClock
from nautilus_trader.core.nautilus_pyo3 import HttpMethod

from .config import ActiveRuntimeMode, BinanceRuntimeCredentials, binance_environment
from .venue import read_failure

_FIRST_LOOKBACK_MS = 7 * 86_400_000
_OVERLAP_MS = 5 * 3_600_000
_PAGE_LIMIT = 1000
_MAX_PAGES = 20
FUNDING_READ_INTERVAL_SECONDS = 1800.0
FUNDING_READ_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class FundingCashflow:
    transaction_id: str
    symbol: str
    asset: str
    amount: Decimal
    occurred_at_ms: int

    @classmethod
    def from_venue(cls, row: dict[str, Any]) -> FundingCashflow:
        try:
            if row["incomeType"] != "FUNDING_FEE":
                raise ValueError("funding_income_type_invalid")
            transaction_id = str(row["tranId"])
            symbol = str(row["symbol"])
            asset = str(row["asset"])
            amount = Decimal(str(row["income"]))
            occurred_at_ms = int(row["time"])
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ValueError("funding_income_invalid") from exc
        if (
            not transaction_id.isdigit()
            or not symbol.isalnum()
            or not asset.isalnum()
            or not amount.is_finite()
            or occurred_at_ms <= 0
        ):
            raise ValueError("funding_income_invalid")
        return cls(transaction_id, symbol, asset, amount, occurred_at_ms)


class BinanceFundingIncome:
    def __init__(
        self,
        *,
        mode: ActiveRuntimeMode,
        credentials: BinanceRuntimeCredentials,
        clock: LiveClock | None = None,
        client: Any = None,
    ) -> None:
        if mode != "paper":
            raise ValueError("funding_income_paper_only")
        self._clock = clock or LiveClock()
        self._client = client or get_cached_binance_http_client(
            clock=self._clock,
            account_type=BinanceAccountType.USDT_FUTURES,
            api_key=credentials.api_key,
            api_secret=credentials.api_secret,
            environment=binance_environment(mode),
        )

    async def read(self, start_ms: int, end_ms: int) -> tuple[FundingCashflow, ...]:
        if start_ms <= 0 or end_ms <= start_ms:
            raise ValueError("funding_income_window_invalid")
        flows: dict[str, FundingCashflow] = {}
        for page in range(1, _MAX_PAGES + 1):
            raw = await self._client.sign_request(
                http_method=HttpMethod.GET,
                url_path="/fapi/v1/income",
                payload={
                    "incomeType": "FUNDING_FEE",
                    "startTime": str(start_ms),
                    "endTime": str(end_ms),
                    "page": str(page),
                    "limit": str(_PAGE_LIMIT),
                    "timestamp": str(self._clock.timestamp_ms()),
                    "recvWindow": "60000",
                },
                ratelimiter_keys=["binance:/fapi/v1/income", "binance:global"],
            )
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) > _PAGE_LIMIT:
                raise ValueError("funding_income_response_invalid")
            for item in rows:
                if not isinstance(item, dict):
                    raise ValueError("funding_income_response_invalid")
                flow = FundingCashflow.from_venue(item)
                if not start_ms <= flow.occurred_at_ms <= end_ms:
                    raise ValueError("funding_income_clock_invalid")
                previous = flows.get(flow.transaction_id)
                if previous is not None and previous != flow:
                    raise ValueError("funding_income_identity_conflict")
                flows[flow.transaction_id] = flow
            if len(rows) < _PAGE_LIMIT:
                return tuple(sorted(flows.values(), key=lambda flow: (flow.occurred_at_ms, flow.transaction_id)))
        raise ValueError("funding_income_pagination_incomplete")


async def watch_funding(
    read: Callable[[int, int], Awaitable[tuple[FundingCashflow, ...]]],
    observe: Callable[[FundingCashflow], bool],
    observe_coverage: Callable[[int, int], bool],
    stop: asyncio.Event,
    *,
    interval_seconds: float = FUNDING_READ_INTERVAL_SECONDS,
    timeout_seconds: float = FUNDING_READ_TIMEOUT_SECONDS,
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> None:
    first = True
    while not stop.is_set():
        end_ms = clock_ms()
        start_ms = max(1, end_ms - (_FIRST_LOOKBACK_MS if first else _OVERLAP_MS))
        try:
            flows = await asyncio.wait_for(read(start_ms, end_ms), timeout=timeout_seconds)
            if all(observe(flow) for flow in flows) and observe_coverage(start_ms, end_ms):
                first = False
        except Exception as exc:
            logger.warning("PAPER funding income unavailable: {}", read_failure(exc))
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)


__all__ = ["BinanceFundingIncome", "FundingCashflow", "watch_funding"]
