"""Read-only access to robinhoodtrenches.com, which is the roster's authority and nothing else (#572 PR-1).

The site is the only place that knows *which* wallets are worth following: the addresses are hand-curated
by its operator, and the handle, follower count and seven-day statistics live nowhere on chain. What the
site is deliberately **not** used for is fills. Its `tape` endpoint is missing about two thirds of the
closes its own ledger reports (#572 §3.1), so trades come from chain logs and only the roster comes from
here.

Two endpoints, both unauthenticated:

* `GET /api/traders?window=7d&stocks=false` -- one row per tracked address with `closed_trades`,
  `realized_pnl`, `win_rate`, `open_cost` and the handle.
* `GET /api/trader/{handle}?stocks=false` -- the per-trader document, whose `stats.profit_factor` is the
  quality criterion and exists on no other endpoint. It is keyed by handle: an address returns `404`.

Calls are paced at least `PACE_SECONDS` apart because this is somebody's small public site, and the
caller only asks for a per-trader document when the list row already passed the closed-trade floor.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from tracefold.integrations.http_bounds import ResponseTooLarge, read_bounded

ROBINHOODTRENCHES_BASE_URL: Final = "https://robinhoodtrenches.com"
# Deliberate courtesy floor between two calls to one small site. Not a rate limit it published.
PACE_SECONDS: Final = 0.25

_CONNECT_TIMEOUT_SECONDS: Final = 5.0
_READ_TIMEOUT_SECONDS: Final = 15.0
_MAX_BYTES: Final = 16 * 1024 * 1024
ROSTER_USER_AGENT: Final = "tracefold-news-chain-tape/1.0 (+https://github.com/AnalyThothAI/tracefold)"


class RosterProviderError(RuntimeError):
    """An anticipated site failure. The loop keeps the previous roster version and ends the refresh."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class RosterCandidate:
    """One row of the tracked-trader list, in the fields the roster rules read."""

    address: str
    handle: str
    followers: int
    realized_pnl: float
    closed_trades: int
    win_rate: float
    open_cost: float


@dataclass(frozen=True, slots=True)
class TraderStats:
    """The per-handle window statistics. `profit_factor` is the reason this endpoint is called at all."""

    handle: str
    closed_trades: int
    realized_pnl: float
    profit_factor: float | None


class RobinhoodTrenchesClient:
    """One paced, bounded, read-only session against the roster provider."""

    def __init__(
        self,
        *,
        base_url: str = ROBINHOODTRENCHES_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        pace_seconds: float = PACE_SECONDS,
        read_timeout_seconds: float = _READ_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.pace_seconds = max(0.0, float(pace_seconds))
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
            transport=transport,
            headers={"user-agent": ROSTER_USER_AGENT, "accept": "application/json"},
        )
        self._next_call_at = 0.0
        self._last_bytes = 0

    @property
    def last_response_bytes(self) -> int:
        return self._last_bytes

    async def aclose(self) -> None:
        await self._client.aclose()

    async def traders(self, *, window: str = "7d") -> tuple[RosterCandidate, ...]:
        payload = await self._get("/api/traders", {"window": window, "stocks": "false"})
        if not isinstance(payload, list):
            raise RosterProviderError("roster_payload_invalid")
        rows = []
        for item in payload:
            candidate = _candidate(item)
            if candidate is not None:
                rows.append(candidate)
        if not rows:
            # A list that parses to nothing is a broken answer, not "nobody is tracked any more".
            raise RosterProviderError("roster_payload_empty")
        return tuple(rows)

    async def trader(self, handle: str) -> TraderStats | None:
        """One trader document, or `None` when the site does not know that handle."""

        name = str(handle or "").strip()
        if not name:
            return None
        payload = await self._get(f"/api/trader/{name}", {"stocks": "false"}, missing_is_none=True)
        if payload is None:
            return None
        if not isinstance(payload, Mapping):
            raise RosterProviderError("roster_payload_invalid")
        stats = payload.get("stats")
        if not isinstance(stats, Mapping):
            raise RosterProviderError("roster_payload_invalid")
        return TraderStats(
            handle=str(payload.get("handle") or name),
            closed_trades=_int(stats.get("closed_trades")),
            realized_pnl=_float(stats.get("realized_pnl")),
            profit_factor=_optional_float(stats.get("profit_factor")),
        )

    async def _get(
        self,
        path: str,
        params: Mapping[str, str],
        *,
        missing_is_none: bool = False,
    ) -> Any:
        await self._pace()
        try:
            async with self._client.stream("GET", f"{self.base_url}{path}", params=dict(params)) as response:
                if missing_is_none and response.status_code == 404:
                    self._last_bytes = 0
                    return None
                if response.status_code in {401, 403, 451}:
                    raise RosterProviderError("roster_blocked", status_code=response.status_code)
                if response.status_code in {418, 429}:
                    raise RosterProviderError("roster_rate_limited", status_code=response.status_code)
                if response.status_code >= 400:
                    raise RosterProviderError("roster_http_error", status_code=response.status_code)
                # Streamed, so the ceiling stops the read rather than describing it afterwards.
                raw = await read_bounded(response, max_bytes=_MAX_BYTES)
        except httpx.TimeoutException:
            raise RosterProviderError("roster_timeout") from None
        except ResponseTooLarge:
            raise RosterProviderError("roster_payload_too_large") from None
        except httpx.HTTPError:
            raise RosterProviderError("roster_transport_error") from None
        self._last_bytes = len(raw)
        try:
            return json.loads(raw)
        except ValueError:
            raise RosterProviderError("roster_payload_invalid") from None

    async def _pace(self) -> None:
        if self.pace_seconds <= 0:
            return
        now = time.monotonic()
        wait = self._next_call_at - now
        if wait > 0:
            await asyncio.sleep(wait)
            now = time.monotonic()
        self._next_call_at = now + self.pace_seconds


def _candidate(item: Any) -> RosterCandidate | None:
    if not isinstance(item, Mapping):
        return None
    address = str(item.get("address") or "").strip().lower()
    if not address.startswith("0x") or len(address) != 42:
        return None
    return RosterCandidate(
        address=address,
        handle=str(item.get("handle") or "").strip(),
        followers=_int(item.get("followers")),
        realized_pnl=_float(item.get("realized_pnl")),
        closed_trades=_int(item.get("closed_trades")),
        win_rate=_float(item.get("win_rate")),
        open_cost=_float(item.get("open_cost")),
    )


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "PACE_SECONDS",
    "ROBINHOODTRENCHES_BASE_URL",
    "RobinhoodTrenchesClient",
    "RosterCandidate",
    "RosterProviderError",
    "TraderStats",
]
