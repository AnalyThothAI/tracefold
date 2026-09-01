"""Binance USD-M open-interest and candle *history*, for the one-shot research corpus (#459 Stage A).

Separate from `candles.py` because the constraints are different in kind. That module fetches one
recent window per Event on the live Price Review path; this one walks 29 days for every USDT
perpetual on the exchange -- about 12,600 requests -- and the failure it must avoid is not a slow
Event but an IP ban partway through a window Binance will not serve again.

Two endpoints, two independent Binance limiters, so each gets its own budget:

* `/futures/data/openInterestHist` -- its own request allowance, not the weighted IP budget. Paged at
  500 five-minute points, so 29 days is 18 pages per symbol.
* `/fapi/v1/klines` -- the weighted IP budget (2400/min). A 1000-candle page costs weight 5, so the
  cheaper page size wins over the 1500 maximum (weight 10) despite needing more pages.

Both budgets sit deliberately under the published ceilings: the minutes saved by running closer are
worth less than one 418.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any, Final

import httpx

BINANCE_FUTURES_BASE_URL: Final = "https://fapi.binance.com"
FIVE_MIN_MS: Final = 300_000

OPEN_INTEREST_PAGE: Final = 500
CANDLE_PAGE: Final = 1000
_CANDLE_WEIGHT: Final = 5

OPEN_INTEREST_REQUESTS_PER_MIN: Final = 170.0
CANDLE_WEIGHT_PER_MIN: Final = 2000.0

_TIMEOUT_SECONDS: Final = 30.0
_MAX_ATTEMPTS: Final = 6
_RATE_LIMIT_BACKOFF_SECONDS: Final = 65.0


class OpenInterestHistoryError(RuntimeError):
    """A provider failure this client could not retry through."""


class Budget:
    """Leaky bucket over a per-minute allowance, shared by every task on one Binance limiter."""

    def __init__(self, per_minute: float, *, capacity: float | None = None) -> None:
        self._rate = per_minute / 60.0
        self._capacity = capacity if capacity is not None else per_minute / 6.0
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def spend(self, cost: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                wait = (cost - self._tokens) / self._rate
            await asyncio.sleep(wait)

    async def penalize(self, seconds: float) -> None:
        """Empty the bucket and hold it empty: a 429 means the whole window is spent, not one request."""

        async with self._lock:
            self._tokens = 0.0
            self._updated = time.monotonic() + seconds
        await asyncio.sleep(seconds)


def history_client(
    *, max_connections: int = 16, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """A long-lived client for a bulk walk against one host: no redirects, one timeout, pooled."""

    return httpx.AsyncClient(
        timeout=httpx.Timeout(_TIMEOUT_SECONDS),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        transport=transport,
    )


async def get_json(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any],
    *,
    budget: Budget,
    cost: float = 1.0,
    base_url: str = BINANCE_FUTURES_BASE_URL,
) -> Any:
    last = "unknown"
    for attempt in range(_MAX_ATTEMPTS):
        await budget.spend(cost)
        try:
            response = await client.get(base_url + path, params=params)
        except httpx.HTTPError as error:  # transport, timeout, DNS
            last = f"transport:{type(error).__name__}"
            await asyncio.sleep(2.0 + attempt * 3.0)
            continue
        if response.status_code in {418, 429}:
            last = f"rate_limited:{response.status_code}"
            await budget.penalize(_RATE_LIMIT_BACKOFF_SECONDS)
            continue
        if response.status_code >= 400:
            # -1130 (window older than the 30-day retention) and -1121 (unknown symbol) are terminal.
            # Retrying either burns the request budget the rest of the walk needs.
            body = response.text[:200]
            if response.status_code == 400 or "-1130" in body or "-1121" in body:
                raise OpenInterestHistoryError(f"{path} {params.get('symbol')} http_{response.status_code} {body}")
            last = f"http_{response.status_code}"
            await asyncio.sleep(2.0 + attempt * 3.0)
            continue
        try:
            return response.json()
        except ValueError:
            last = "payload_invalid"
            await asyncio.sleep(1.0 + attempt)
    raise OpenInterestHistoryError(f"{path} {params.get('symbol')} exhausted retries: {last}")


async def fetch_usdt_perpetuals(client: httpx.AsyncClient, *, budget: Budget) -> tuple[str, ...]:
    """Every USDT-margined perpetual currently trading, sorted so a corpus is order-stable."""

    payload = await get_json(client, "/fapi/v1/exchangeInfo", {}, budget=budget)
    return tuple(
        sorted(
            str(entry["symbol"])
            for entry in payload.get("symbols", ())
            if entry.get("contractType") == "PERPETUAL"
            and entry.get("quoteAsset") == "USDT"
            and entry.get("status") == "TRADING"
        )
    )


async def fetch_open_interest_history(
    client: httpx.AsyncClient, symbol: str, *, start_ms: int, end_ms: int, budget: Budget
) -> list[dict[str, Any]]:
    """Raw `openInterestHist` rows for one symbol, paged across the window."""

    out: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        page = await get_json(
            client,
            "/futures/data/openInterestHist",
            {
                "symbol": symbol,
                "period": "5m",
                "limit": OPEN_INTEREST_PAGE,
                "startTime": cursor,
                "endTime": min(cursor + OPEN_INTEREST_PAGE * FIVE_MIN_MS - 1, end_ms),
            },
            budget=budget,
        )
        if not page:
            # A hole inside the window -- a symbol listed later, or a provider outage. Step over it
            # rather than stop, so one gap cannot truncate everything after it.
            cursor += OPEN_INTEREST_PAGE * FIVE_MIN_MS
            continue
        out.extend(page)
        cursor = max(int(page[-1]["timestamp"]) + FIVE_MIN_MS, cursor + FIVE_MIN_MS)
    return out


async def fetch_candle_history(
    client: httpx.AsyncClient, symbol: str, *, start_ms: int, end_ms: int, budget: Budget
) -> list[list[Any]]:
    """Raw five-minute `klines` rows for one symbol, paged across the window."""

    out: list[list[Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        page = await get_json(
            client,
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": "5m",
                "limit": CANDLE_PAGE,
                "startTime": cursor,
                "endTime": end_ms,
            },
            budget=budget,
            cost=float(_CANDLE_WEIGHT),
        )
        if not page:
            break
        out.extend(page)
        if len(page) < CANDLE_PAGE:
            break
        cursor = int(page[-1][0]) + FIVE_MIN_MS
    return out


__all__: Sequence[str] = [
    "BINANCE_FUTURES_BASE_URL",
    "CANDLE_PAGE",
    "CANDLE_WEIGHT_PER_MIN",
    "FIVE_MIN_MS",
    "OPEN_INTEREST_PAGE",
    "OPEN_INTEREST_REQUESTS_PER_MIN",
    "Budget",
    "OpenInterestHistoryError",
    "fetch_candle_history",
    "fetch_open_interest_history",
    "fetch_usdt_perpetuals",
    "get_json",
    "history_client",
]
