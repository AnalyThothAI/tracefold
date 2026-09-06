"""Read-only access to robinhoodtrenches.com, which is the roster's authority and nothing else (#572 PR-1).

The site is the only place that knows *which* wallets are worth following: the addresses are hand-curated
by its operator, and the handle, follower count and seven-day statistics live nowhere on chain. What the
site is deliberately **not** used for is fills. Its `tape` endpoint is missing about two thirds of the
closes its own ledger reports (#572 §3.1), so trades come from chain logs and only the roster comes from
here.

Four endpoints, all unauthenticated:

* `GET /api/traders?window=7d&stocks=false` -- one row per tracked address with `closed_trades`,
  `realized_pnl`, `win_rate`, `open_cost` and the handle.
* `GET /api/trader/{handle}?stocks=false` -- the per-trader document, whose `stats.profit_factor` is the
  quality criterion and exists on no other endpoint. It is keyed by handle: an address returns `404`.
* the same document's open positions (#572 PR-2): `amount`, `avg_price`, `cost_usd` and `opened_ts` per
  token. This is the *context* a card carries -- what the wallet paid and when it opened -- and the
  fallback denominator for an exit whose block has already left the RPC's ten-minute state window. It is
  never the trigger: triggers come from chain logs, because the site's own tape is missing about two
  thirds of the closes its ledger reports (#572 §3.1).
* `GET /api/tokens` -- the current `mark` and pool `liquidity` per token, which is what prices a position
  and what a crowding card reports as depth.

The last two are read on demand and cached for `CONTEXT_CACHE_SECONDS`, because a burst of fills in one
token would otherwise ask the same small site the same question once per fill.

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

# How long a bag list or a token mark is reused. Card context, not a trigger: a minute-old entry price
# is the same entry price, and the alternative is one request per fill against somebody's small site.
CONTEXT_CACHE_SECONDS: Final = 60.0

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


@dataclass(frozen=True, slots=True)
class TraderBag:
    """One open position as the site reports it: what the wallet holds, paid and when it opened.

    `token` is the pool/token address where the site publishes one, and `""` where it publishes only a
    symbol -- the caller matches on whichever it has, and a bag it cannot match to a chain address
    simply does not become card context.
    """

    token: str
    symbol: str
    amount: float
    avg_price: float
    cost_usd: float
    opened_at_ms: int


@dataclass(frozen=True, slots=True)
class TokenMark:
    """The site's current price and pool depth for one token."""

    token: str
    symbol: str
    mark: float | None
    liquidity: float | None


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
        self.context_cache_seconds = max(0.0, float(CONTEXT_CACHE_SECONDS))
        self._bags: dict[str, tuple[float, tuple[TraderBag, ...]]] = {}
        self._marks: tuple[float, dict[str, TokenMark]] | None = None

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

    async def bags(self, handle: str) -> tuple[TraderBag, ...]:
        """One trader's open positions, cached briefly. Raises `RosterProviderError` when the site fails.

        The failure is deliberately *not* flattened into an empty tuple. An empty tuple is the site
        saying this wallet holds nothing, which the exit rule reads as a genuine full exit; a site that
        did not answer says nothing at all. Making the two the same value is how a rate-limited request
        during a partial sell becomes a `清仓` card, so the distinction is the adapter's to keep and the
        caller's to act on.

        A handle the site does not know is `()` -- that is an answer, not a failure.
        """

        name = str(handle or "").strip()
        if not name:
            return ()
        cached = self._bags.get(name)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.context_cache_seconds:
            return cached[1]
        payload = await self._get(f"/api/trader/{name}", {"stocks": "false"}, missing_is_none=True)
        rows = _bag_rows(payload)
        self._bags[name] = (now, rows)
        return rows

    async def marks(self) -> Mapping[str, TokenMark]:
        """The site's current mark and liquidity per token, cached briefly.

        Raises like `bags`. A mark is display context -- an absent one costs a card a line and sends the
        position value to the price this very trade printed -- so the caller treats a failure as no
        marks; it is still the caller's decision rather than one this adapter makes for it.
        """

        now = time.monotonic()
        if self._marks is not None and now - self._marks[0] < self.context_cache_seconds:
            return self._marks[1]
        payload = await self._get("/api/tokens", {"stocks": "false"})
        marks = _mark_rows(payload)
        self._marks = (now, marks)
        return marks

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


def _bag_rows(payload: Any) -> tuple[TraderBag, ...]:
    """The `bags` array of a trader document, as the recorded 2026-09-06 answer publishes it.

    One key, not a search over plausible ones. A guess that silently matched nothing would make every
    wallet look like it holds no position, which is the exact input the exit rule reads as a full exit.
    The recorded document is pinned in `tests/fixtures/chain_tape/trader_document.json`, so a shape
    change is a failing test rather than a card that quietly stops carrying its context.
    """

    if not isinstance(payload, Mapping):
        return ()
    rows = payload.get("bags")
    if not isinstance(rows, list):
        return ()
    return tuple(bag for item in rows if (bag := _bag(item)) is not None)


def _bag(item: Any) -> TraderBag | None:
    if not isinstance(item, Mapping):
        return None
    amount = _float(item.get("amount"))
    if amount <= 0:
        return None
    return TraderBag(
        token=_token_address(item),
        symbol=str(item.get("symbol") or "").strip(),
        amount=amount,
        avg_price=_float(item.get("avg_price")),
        cost_usd=_float(item.get("cost_usd")),
        # The site publishes seconds; every stamp inside Tracefold is milliseconds.
        opened_at_ms=_int(item.get("opened_ts")) * 1000,
    )


def _mark_rows(payload: Any) -> dict[str, TokenMark]:
    """`/api/tokens` answers a bare array of token rows; the recorded answer is the pinned shape."""

    if not isinstance(payload, list):
        return {}
    rows = payload
    marks: dict[str, TokenMark] = {}
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        token = _token_address(item)
        if not token:
            continue
        marks[token] = TokenMark(
            token=token,
            symbol=str(item.get("symbol") or "").strip(),
            mark=_optional_float(item.get("mark")),
            liquidity=_optional_float(item.get("liquidity")),
        )
    return marks


def _token_address(item: Mapping[str, Any]) -> str:
    """The row's `token`, normalized, or `""` when it is not an address this chain can carry."""

    value = str(item.get("token") or "").strip().lower()
    return value if value.startswith("0x") and len(value) == 42 else ""


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
    "CONTEXT_CACHE_SECONDS",
    "PACE_SECONDS",
    "ROBINHOODTRENCHES_BASE_URL",
    "RobinhoodTrenchesClient",
    "RosterCandidate",
    "RosterProviderError",
    "TokenMark",
    "TraderBag",
    "TraderStats",
]
