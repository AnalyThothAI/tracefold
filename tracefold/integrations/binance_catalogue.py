"""One bounded owner for public LIVE/DEMO USD-M catalogue snapshots."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    payload: dict[str, Any]
    received_at_ms: int
    receipts: tuple[dict[str, Any], ...]


class BinanceCatalogue:
    """Reuse the caller's HTTP transport and budget, with environment-separated caches."""

    def __init__(
        self,
        fetch: Callable[..., Awaitable[Any]],
        *,
        clock_ms: Callable[[], int],
    ) -> None:
        self._fetch = fetch
        self._clock_ms = clock_ms
        self._locks = {environment: asyncio.Lock() for environment in ("live", "demo")}
        self._snapshots: dict[str, CatalogueSnapshot] = {}

    async def read(
        self,
        environment: Literal["live", "demo"],
        *,
        max_age_ms: int = 60_000,
        deadline_at_monotonic: float,
    ) -> CatalogueSnapshot:
        if environment not in self._locks or max_age_ms <= 0:
            raise ValueError("catalogue_request_invalid")
        async with asyncio.timeout_at(deadline_at_monotonic):
            async with self._locks[environment]:
                cached = self._snapshots.get(environment)
                if cached is not None and 0 <= self._clock_ms() - cached.received_at_ms <= min(max_age_ms, 60_000):
                    return CatalogueSnapshot(
                        copy.deepcopy(cached.payload),
                        cached.received_at_ms,
                        (
                            {
                                "endpoint": "cache:/fapi/v1/exchangeInfo",
                                "http_status": None,
                                "latency_ms": 0,
                                "request_weight": 0,
                                "cache_hit": True,
                            },
                        ),
                    )
                base = "https://fapi.binance.com" if environment == "live" else "https://demo-fapi.binance.com"
                receipts: list[dict[str, Any]] = []
                raw = await self._fetch(base + "/fapi/v1/exchangeInfo", params={}, receipts=receipts)
                if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
                    raise ValueError("exchange_info_invalid")
                snapshot = CatalogueSnapshot(copy.deepcopy(raw), self._clock_ms(), tuple(receipts))
                self._snapshots[environment] = snapshot
                return CatalogueSnapshot(copy.deepcopy(raw), snapshot.received_at_ms, snapshot.receipts)
