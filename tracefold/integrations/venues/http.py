"""Shared venue HTTP plumbing for the price adapters: bounded, timed, and expected-error classified.

The catalogue adapters predate this and keep their own copies with their own size caps (a 17 MB
`exchangeInfo` needs a different ceiling than a ticker batch). Everything the Price Review plane fetches is
small, frequent and unauthenticated, so it shares one client policy: no redirects, one timeout, one response
ceiling, and every provider failure surfaced as `VenueExpectedError` so a single venue can be skipped
without touching the others.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import httpx

from .errors import VenueExpectedError

PRICE_TIMEOUT_SECONDS: Final = 8.0
PRICE_MAX_BYTES: Final = 8 * 1024 * 1024


def price_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(PRICE_TIMEOUT_SECONDS), follow_redirects=False, transport=transport)


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    venue: str,
    params: Mapping[str, Any] | None = None,
) -> Any:
    try:
        response = await client.get(url, params=dict(params or {}))
    except httpx.TimeoutException:
        raise VenueExpectedError("venue_timeout", venue=venue) from None
    except httpx.HTTPError:
        raise VenueExpectedError("venue_http_error", venue=venue) from None
    return _payload(response, venue=venue)


async def post_json(client: httpx.AsyncClient, url: str, body: Mapping[str, Any], *, venue: str) -> Any:
    try:
        response = await client.post(url, json=dict(body))
    except httpx.TimeoutException:
        raise VenueExpectedError("venue_timeout", venue=venue) from None
    except httpx.HTTPError:
        raise VenueExpectedError("venue_http_error", venue=venue) from None
    return _payload(response, venue=venue)


def _payload(response: httpx.Response, *, venue: str) -> Any:
    if response.status_code in {403, 451}:
        raise VenueExpectedError("venue_blocked", venue=venue, status_code=response.status_code)
    if response.status_code in {418, 429}:
        raise VenueExpectedError("venue_rate_limited", venue=venue, status_code=response.status_code)
    if response.status_code >= 400:
        raise VenueExpectedError("venue_http_error", venue=venue, status_code=response.status_code)
    if len(response.content) > PRICE_MAX_BYTES:
        raise VenueExpectedError("venue_payload_too_large", venue=venue)
    try:
        return response.json()
    except ValueError:
        raise VenueExpectedError("venue_payload_invalid", venue=venue) from None


__all__ = ["PRICE_MAX_BYTES", "PRICE_TIMEOUT_SECONDS", "get_json", "post_json", "price_client"]
