"""One bounded read of the source's complete address list; no ranking or per-handle requests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

import httpx

from tracefold.integrations.http_bounds import ResponseTooLarge, read_bounded, retry_after_ms
from tracefold.news.chain_tape.contracts import RosterMember
from tracefold.news.chain_tape.evm import normalize_address

ROBINHOODTRENCHES_BASE_URL: Final = "https://rhtrenches.com"
ROSTER_USER_AGENT: Final = "tracefold-news-chain-tape/1.0 (+https://github.com/AnalyThothAI/tracefold)"
_MAX_BYTES: Final = 16 * 1024 * 1024


class RosterProviderError(RuntimeError):
    """An expected source failure; retry scheduling belongs to the refresh task."""

    def __init__(self, code: str, *, status_code: int | None = None, retry_after_ms: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after_ms = retry_after_ms


class RobinhoodTrenchesClient:
    def __init__(
        self,
        *,
        base_url: str = ROBINHOODTRENCHES_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        read_timeout_seconds: float = 15.0,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout_seconds, connect=5.0),
            follow_redirects=False,
            transport=transport,
            headers={"user-agent": ROSTER_USER_AGENT, "accept": "application/json"},
        )
        self.last_response_bytes = 0
        self.last_row_count = 0
        self.last_duplicate_count = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def traders(self, *, window: str = "30d") -> tuple[RosterMember, ...]:
        self.last_response_bytes = 0
        self.last_row_count = self.last_duplicate_count = 0
        try:
            async with self._client.stream(
                "GET", f"{self.base_url}/api/traders", params={"window": window, "stocks": "false"}
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise RosterProviderError("roster_redirect", status_code=status)
                if status in {401, 403, 451}:
                    raise RosterProviderError("roster_blocked", status_code=status)
                if status in {418, 429}:
                    raise RosterProviderError(
                        "roster_rate_limited",
                        status_code=status,
                        retry_after_ms=retry_after_ms(response.headers.get("Retry-After")),
                    )
                if status >= 400:
                    raise RosterProviderError(
                        "roster_http_error",
                        status_code=status,
                        retry_after_ms=retry_after_ms(response.headers.get("Retry-After")),
                    )
                raw = await read_bounded(response, max_bytes=_MAX_BYTES)
        except httpx.TimeoutException:
            raise RosterProviderError("roster_timeout") from None
        except ResponseTooLarge:
            raise RosterProviderError("roster_payload_too_large") from None
        except httpx.HTTPError:
            raise RosterProviderError("roster_transport_error") from None
        self.last_response_bytes = len(raw)
        try:
            payload = json.loads(raw)
        except ValueError:
            raise RosterProviderError("roster_payload_invalid") from None
        members = parse_roster(payload)
        self.last_row_count = len(payload)
        self.last_duplicate_count = len(payload) - len(members)
        return members


def parse_roster(payload: Any) -> tuple[RosterMember, ...]:
    """All valid addresses in this response, or a failed response; never a partial parse.

    This source supplies a bare array, not a paginated contract. A valid reduction is
    accepted. There is no evidence with which to infer hidden source-side truncation.
    """
    if not isinstance(payload, list):
        raise RosterProviderError("roster_payload_invalid")
    if not payload:
        raise RosterProviderError("roster_payload_empty")
    members: dict[str, str] = {}
    for row in payload:
        if not isinstance(row, Mapping) or not (address := normalize_address(row.get("address", ""))):
            raise RosterProviderError("roster_address_invalid")
        handle = row.get("handle")
        handle = handle.strip() if isinstance(handle, str) else ""
        # A stable alias for duplicates: prefer a nonempty name, then lexical order.
        prior = members.get(address, "")
        members[address] = min(filter(None, (prior, handle)), default="")
    return tuple(RosterMember(wallet=address, handle=members[address]) for address in sorted(members))
