"""Read-only DexScreener access, for one question only: what is this token worth now (#572 PR-2).

One endpoint, unauthenticated, one bounded attempt per call:
`GET /latest/dex/tokens/{address}` answers with every pair that trades the token across every chain it
indexes. The price this adapter returns is the `priceUsd` of the **deepest pool on Robinhood Chain** --
a thin pool's last print is a number, not a price, and a token that also trades somewhere else must not
be priced by somewhere else.

This is a price *receipt*, never a trigger and never a gate: a card is already sent by the time anything
here is called, and a failure leaves the receipt due rather than changing anything a reader was told.
That is why there is no retry inside the adapter -- the caller's next turn is the retry, and after a day
of them the horizon is banked as `unavailable` (#572 §11).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx

from tracefold.integrations.http_bounds import ResponseTooLarge, read_bounded

DEXSCREENER_BASE_URL: Final = "https://api.dexscreener.com"
# The chain id DexScreener publishes Robinhood Chain pairs under.
ROBINHOOD_CHAIN_SLUG: Final = "robinhood"

_CONNECT_TIMEOUT_SECONDS: Final = 5.0
_READ_TIMEOUT_SECONDS: Final = 8.0
_MAX_BYTES: Final = 4 * 1024 * 1024
DEXSCREENER_USER_AGENT: Final = "tracefold-news-chain-tape/1.0 (+https://github.com/AnalyThothAI/tracefold)"


class DexScreenerError(RuntimeError):
    """An anticipated failure. The receipt stays due; nothing a reader has already seen changes."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class DexScreenerClient:
    """One bounded, read-only session. Every method makes exactly one attempt."""

    def __init__(
        self,
        *,
        base_url: str = DEXSCREENER_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        read_timeout_seconds: float = _READ_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
            transport=transport,
            headers={"user-agent": DEXSCREENER_USER_AGENT, "accept": "application/json"},
        )
        self._last_bytes = 0

    @property
    def last_response_bytes(self) -> int:
        return self._last_bytes

    async def aclose(self) -> None:
        await self._client.aclose()

    async def token_price(self, address: str) -> Decimal | None:
        """The token's price in the deepest Robinhood Chain pool, or `None` when it is not indexed.

        `None` is an answer, not a failure: a token DexScreener has never seen is a token DexScreener
        cannot price, and the caller falls back to the provider's own mark before giving up.
        """

        token = str(address or "").strip().lower()
        if not token.startswith("0x") or len(token) != 42:
            raise ValueError("dexscreener_address_invalid")
        payload = await self._get(f"/latest/dex/tokens/{token}")
        pairs = payload.get("pairs") if isinstance(payload, Mapping) else None
        if not isinstance(pairs, Sequence) or isinstance(pairs, str | bytes):
            return None
        return _deepest_price(pairs)

    async def _get(self, path: str) -> Any:
        try:
            async with self._client.stream("GET", f"{self.base_url}{path}") as response:
                if response.status_code in {401, 403, 451}:
                    raise DexScreenerError("dexscreener_blocked", status_code=response.status_code)
                if response.status_code in {418, 429}:
                    raise DexScreenerError("dexscreener_rate_limited", status_code=response.status_code)
                if response.status_code >= 400:
                    raise DexScreenerError("dexscreener_http_error", status_code=response.status_code)
                # Streamed, so the ceiling stops the read rather than describing it afterwards.
                raw = await read_bounded(response, max_bytes=_MAX_BYTES)
        except httpx.TimeoutException:
            raise DexScreenerError("dexscreener_timeout") from None
        except ResponseTooLarge:
            raise DexScreenerError("dexscreener_payload_too_large") from None
        except httpx.HTTPError:
            raise DexScreenerError("dexscreener_transport_error") from None
        self._last_bytes = len(raw)
        try:
            return json.loads(raw)
        except ValueError:
            raise DexScreenerError("dexscreener_payload_invalid") from None


def _deepest_price(pairs: Sequence[Any]) -> Decimal | None:
    """The `priceUsd` of the deepest Robinhood Chain pool among these pairs.

    Depth decides, and it decides on this chain only. Taking the first pair would let whichever pool the
    provider happened to list first price the card, and taking any chain would price a Robinhood Chain
    token off a same-symbol pool somewhere else entirely.
    """

    best: tuple[Decimal, Decimal] | None = None
    for pair in pairs:
        if not isinstance(pair, Mapping) or str(pair.get("chainId") or "") != ROBINHOOD_CHAIN_SLUG:
            continue
        price = _decimal(pair.get("priceUsd"))
        if price is None or price <= 0:
            continue
        liquidity = pair.get("liquidity")
        depth = _decimal(liquidity.get("usd")) if isinstance(liquidity, Mapping) else None
        candidate = (depth if depth is not None else Decimal(0), price)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return None if best is None else best[1]


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


__all__ = [
    "DEXSCREENER_BASE_URL",
    "ROBINHOOD_CHAIN_SLUG",
    "DexScreenerClient",
    "DexScreenerError",
]
