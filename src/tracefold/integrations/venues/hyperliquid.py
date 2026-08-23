"""Hyperliquid instrument catalogue: main perps, spot tokens, and the HIP-3 builder DEXs.

The builder DEXs are where the 币股 live. ``perpDexs`` lists them (``xyz``, ``para``, ``km``, ``mkts``, ``vntl``,
``cash``, ``flx``, ``hyna``, ``io``, ``abcd``); each is then queried with ``{"type":"meta","dex":"<name>"}``. The
``xyz`` DEX is the one OpenNews mirrors in its ``XYZ-`` prefixed coin tags, and ``vntl`` carries pre-IPO names
(SPACEX, OPENAI, ANTHROPIC).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from tracefold.news.market_review.instruments import Instrument, classify, is_valid_symbol, normalize_symbol

from .errors import VenueExpectedError

HYPERLIQUID_BASE_URL: Final = "https://api.hyperliquid.xyz"
_TIMEOUT_SECONDS: Final = 20.0
_MAX_BYTES: Final = 16 * 1024 * 1024
# A runaway `perpDexs` response must not fan out into hundreds of requests.
_MAX_BUILDER_DEXS: Final = 32


async def fetch_hyperliquid_instruments(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = HYPERLIQUID_BASE_URL,
) -> tuple[Instrument, ...]:
    """Main perps + spot tokens + every HIP-3 builder DEX. A failing builder DEX is skipped, not fatal."""

    url = f"{base_url.rstrip('/')}/info"
    out: list[Instrument] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_TIMEOUT_SECONDS), follow_redirects=False, transport=transport
    ) as client:
        meta = await _post(client, url, {"type": "meta"}, venue="hl.perp")
        out.extend(_parse_universe(meta, venue="hl.perp"))
        spot = await _post(client, url, {"type": "spotMeta"}, venue="hl.spot")
        out.extend(_parse_spot(spot))
        dexs = await _post_list(client, url, {"type": "perpDexs"}, venue="hl.perp")
        for entry in dexs[:_MAX_BUILDER_DEXS]:
            if not isinstance(entry, Mapping):
                continue  # the main DEX is a null entry in this list
            name = str(entry.get("name") or "").strip().lower()
            if not name or not name.isalnum():
                continue
            venue = f"hl.{name}"
            try:
                dex_meta = await _post(client, url, {"type": "meta", "dex": name}, venue=venue)
            except VenueExpectedError:
                continue  # one builder DEX must not cost us the rest of the universe
            out.extend(_parse_universe(dex_meta, venue=venue))
    return tuple(out)


def _parse_universe(payload: Mapping[str, Any], *, venue: str) -> tuple[Instrument, ...]:
    universe = payload.get("universe")
    if not isinstance(universe, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[Instrument] = []
    seen: set[str] = set()
    for entry in universe:
        if not isinstance(entry, Mapping) or entry.get("isDelisted"):
            continue
        venue_symbol = str(entry.get("name") or "").strip()
        base = normalize_symbol(venue_symbol)
        if not venue_symbol or not is_valid_symbol(base) or venue_symbol in seen:
            continue
        seen.add(venue_symbol)
        out.append(
            Instrument(
                venue=venue,
                venue_symbol=venue_symbol,
                base_symbol=base,
                instrument_class=classify(base, venue=venue),
            )
        )
    return tuple(out)


def _parse_spot(payload: Mapping[str, Any]) -> tuple[Instrument, ...]:
    """`universe` is the tradeable half of `spotMeta`; `tokens` is a registry whose names nothing can price.

    Hyperliquid quotes and candles take the *market* key — `@107`, or `PURR/USDC` for the one canonical
    pair — never the token name. Ingesting `tokens` therefore produced 491 rows of which 326 were markets
    (and one was `USDC` itself), each carrying a `venue_symbol` no provider request accepts (#88 §3).

    The base is the market's first token and the quote asset its second, both resolved through the token
    registry, so a pair keeps naming the asset News joins on while the venue symbol stays queryable.
    """

    tokens = payload.get("tokens")
    universe = payload.get("universe")
    if not isinstance(tokens, Sequence) or not isinstance(universe, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue="hl.spot")
    names: dict[int, str] = {}
    for entry in tokens:
        if not isinstance(entry, Mapping):
            continue
        index, name = entry.get("index"), str(entry.get("name") or "").strip()
        if isinstance(index, int) and not isinstance(index, bool) and name:
            names[index] = name
    out: list[Instrument] = []
    seen: set[str] = set()
    for entry in universe:
        if not isinstance(entry, Mapping):
            continue
        venue_symbol = str(entry.get("name") or "").strip()
        pair = entry.get("tokens")
        if not venue_symbol or venue_symbol in seen or not isinstance(pair, Sequence) or len(pair) < 2:
            continue
        base = normalize_symbol(_token_name(names, pair[0]))
        quote = _token_name(names, pair[1]).upper()
        if not is_valid_symbol(base):
            continue
        seen.add(venue_symbol)
        out.append(
            Instrument(
                venue="hl.spot",
                venue_symbol=venue_symbol,
                base_symbol=base,
                instrument_class=classify(base, venue="hl.spot"),
                quote_asset=quote or None,
            )
        )
    return tuple(out)


def _token_name(names: Mapping[int, str], index: Any) -> str:
    return names.get(index, "") if isinstance(index, int) and not isinstance(index, bool) else ""


async def _post(client: httpx.AsyncClient, url: str, body: Mapping[str, Any], *, venue: str) -> Mapping[str, Any]:
    payload = await _post_json(client, url, body, venue=venue)
    if not isinstance(payload, Mapping):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return payload


async def _post_list(client: httpx.AsyncClient, url: str, body: Mapping[str, Any], *, venue: str) -> list[Any]:
    payload = await _post_json(client, url, body, venue=venue)
    if not isinstance(payload, list):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return payload


async def _post_json(client: httpx.AsyncClient, url: str, body: Mapping[str, Any], *, venue: str) -> Any:
    try:
        response = await client.post(url, json=dict(body))
    except httpx.TimeoutException:
        raise VenueExpectedError("venue_timeout", venue=venue) from None
    except httpx.HTTPError:
        raise VenueExpectedError("venue_http_error", venue=venue) from None
    if response.status_code in {403, 451}:
        raise VenueExpectedError("venue_blocked", venue=venue, status_code=response.status_code)
    if response.status_code == 429:
        raise VenueExpectedError("venue_rate_limited", venue=venue, status_code=response.status_code)
    if response.status_code >= 400:
        raise VenueExpectedError("venue_http_error", venue=venue, status_code=response.status_code)
    if len(response.content) > _MAX_BYTES:
        raise VenueExpectedError("venue_payload_too_large", venue=venue)
    try:
        return response.json()
    except ValueError:
        raise VenueExpectedError("venue_payload_invalid", venue=venue) from None


__all__ = ["HYPERLIQUID_BASE_URL", "fetch_hyperliquid_instruments"]
