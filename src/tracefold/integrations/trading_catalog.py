"""Credential-free Binance USD-M and Hyperliquid perp catalog adapters (#350).

This integration belongs only to Trading.  News venue discovery remains in ``integrations.venues``;
keeping the adapters separate preserves the sibling business boundary while App composes both.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

import httpx

from tracefold.trading import VenueInstrumentCatalogEntryV1

from .venues.errors import VenueExpectedError

BINANCE_USDM_BASE_URL: Final = "https://fapi.binance.com"
HYPERLIQUID_BASE_URL: Final = "https://api.hyperliquid.xyz"
_TIMEOUT_SECONDS: Final = 20.0
_MAX_BYTES: Final = 48 * 1024 * 1024
_MAX_BUILDER_DEXS: Final = 32
_ASSET = re.compile(r"^[^\s\x00-\x1f]{1,32}$")


async def fetch_binance_usdm_catalog(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BINANCE_USDM_BASE_URL,
) -> tuple[VenueInstrumentCatalogEntryV1, ...]:
    """Return every public USD-M instrument row, including inactive and malformed rows."""

    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            return await _fetch_binance_usdm_catalog(transport=transport, base_url=base_url)
    except TimeoutError:
        raise VenueExpectedError("venue_timeout", venue="binance.usdm") from None


async def _fetch_binance_usdm_catalog(
    *,
    transport: httpx.AsyncBaseTransport | None,
    base_url: str,
) -> tuple[VenueInstrumentCatalogEntryV1, ...]:
    async with _client(transport) as client:
        payload = await _get(client, f"{base_url.rstrip('/')}/fapi/v1/exchangeInfo", venue="binance.usdm")
    symbols = payload.get("symbols")
    if not isinstance(symbols, Sequence) or isinstance(symbols, str | bytes):
        raise VenueExpectedError("venue_payload_invalid", venue="binance.usdm")
    return tuple(_binance_entry(entry) for entry in symbols)


async def fetch_hyperliquid_perp_catalog(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = HYPERLIQUID_BASE_URL,
) -> tuple[VenueInstrumentCatalogEntryV1, ...]:
    """Return every main/HIP-3 perp row; any missing DEX leaves the last-good snapshot active."""

    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            return await _fetch_hyperliquid_perp_catalog(transport=transport, base_url=base_url)
    except TimeoutError:
        raise VenueExpectedError("venue_timeout", venue="hyperliquid.perp") from None


async def _fetch_hyperliquid_perp_catalog(
    *,
    transport: httpx.AsyncBaseTransport | None,
    base_url: str,
) -> tuple[VenueInstrumentCatalogEntryV1, ...]:
    url = f"{base_url.rstrip('/')}/info"
    rows: list[VenueInstrumentCatalogEntryV1] = []
    async with _client(transport) as client:
        meta = await _post_mapping(client, url, {"type": "meta"}, venue="hyperliquid.perp")
        rows.extend(_hyperliquid_universe(meta, namespace="main"))
        dexs = await _post_list(client, url, {"type": "perpDexs"}, venue="hyperliquid.perp")
        if len(dexs) > _MAX_BUILDER_DEXS:
            raise VenueExpectedError("venue_catalog_incomplete", venue="hyperliquid.perp")
        for entry in dexs:
            if entry is None:
                continue
            if not isinstance(entry, Mapping):
                raise VenueExpectedError("venue_payload_invalid", venue="hyperliquid.perp")
            name = str(entry.get("name") or "").strip().lower()
            if not name or not name.isalnum():
                raise VenueExpectedError("venue_payload_invalid", venue="hyperliquid.perp")
            dex_meta = await _post_mapping(
                client,
                url,
                {"type": "meta", "dex": name},
                venue=f"hyperliquid.dex:{name}",
            )
            rows.extend(_hyperliquid_universe(dex_meta, namespace=f"dex:{name}"))
    return tuple(rows)


def _binance_entry(entry: Any) -> VenueInstrumentCatalogEntryV1:
    raw = dict(entry) if isinstance(entry, Mapping) else {"value": entry}
    raw_sha = _canonical_sha256(raw)
    if not isinstance(entry, Mapping):
        return VenueInstrumentCatalogEntryV1(
            provider_instrument_id=f"unknown:{raw_sha}",
            provider_symbol="",
            venue="binance.usdm",
            product_kind="unknown",
            active=False,
            raw_metadata_sha256=raw_sha,
            normalization_error="provider_instrument_row_invalid",
        )
    symbol = str(entry.get("symbol") or "").strip().upper()
    base = str(entry.get("baseAsset") or "").strip().upper()
    quote = str(entry.get("quoteAsset") or "").strip().upper()
    margin = str(entry.get("marginAsset") or "").strip().upper()
    error = None
    if not symbol:
        error = "provider_instrument_identity_missing"
    elif not _valid_asset(base):
        error = "canonical_asset_invalid"
    contract_type = str(entry.get("contractType") or "").strip().upper()
    if contract_type in {"PERPETUAL", "TRADIFI_PERPETUAL"}:
        product_kind = "linear_perpetual"
    elif contract_type in {"CURRENT_QUARTER", "NEXT_QUARTER", "CURRENT_QUARTER_DELIVERING"}:
        product_kind = "delivery_future"
    else:
        product_kind = "unknown"
    filters = entry.get("filters")
    by_type = (
        {str(item.get("filterType") or ""): item for item in filters if isinstance(item, Mapping)}
        if isinstance(filters, Sequence) and not isinstance(filters, str | bytes)
        else {}
    )
    price = by_type.get("PRICE_FILTER", {})
    lot = by_type.get("LOT_SIZE", {})
    notional = by_type.get("MIN_NOTIONAL", {})
    pair = str(entry.get("pair") or "").strip().upper()
    aliases = tuple(sorted({value for value in (symbol, pair) if value and value != symbol}))
    return VenueInstrumentCatalogEntryV1(
        provider_instrument_id=symbol or f"unknown:{raw_sha}",
        provider_symbol=symbol,
        venue="binance.usdm",
        canonical_asset=base if _valid_asset(base) else None,
        canonical_namespace="native" if _valid_asset(base) else None,
        product_kind=product_kind,
        active=str(entry.get("status") or "").upper() == "TRADING",
        listed_at_ms=_nonnegative_int(entry.get("onboardDate")),
        delisted_at_ms=_nonnegative_int(entry.get("deliveryDate")) if product_kind == "delivery_future" else None,
        settlement_asset=quote or None,
        margin_asset=margin or None,
        multiplier="1",
        aliases=aliases,
        price_increment=_text(price.get("tickSize")),
        size_increment=_text(lot.get("stepSize")),
        min_quantity=_text(lot.get("minQty")),
        min_notional=_text(notional.get("notional")),
        raw_metadata_sha256=raw_sha,
        normalization_error=error,
    )


def _hyperliquid_universe(payload: Mapping[str, Any], *, namespace: str) -> tuple[VenueInstrumentCatalogEntryV1, ...]:
    universe = payload.get("universe")
    if not isinstance(universe, Sequence) or isinstance(universe, str | bytes):
        raise VenueExpectedError("venue_payload_invalid", venue="hyperliquid.perp")
    out: list[VenueInstrumentCatalogEntryV1] = []
    for entry in universe:
        raw = dict(entry) if isinstance(entry, Mapping) else {"value": entry}
        raw_sha = _canonical_sha256({"namespace": namespace, "entry": raw})
        if not isinstance(entry, Mapping):
            out.append(
                VenueInstrumentCatalogEntryV1(
                    provider_instrument_id=f"unknown:{raw_sha}",
                    provider_symbol="",
                    venue="hyperliquid.perp",
                    product_kind="unknown",
                    active=False,
                    raw_metadata_sha256=raw_sha,
                    normalization_error="provider_instrument_row_invalid",
                )
            )
            continue
        symbol = str(entry.get("name") or "").strip()
        base = _normalize_asset(symbol)
        error = None if symbol and _valid_asset(base) else "provider_instrument_identity_invalid"
        size_decimals = entry.get("szDecimals")
        size_increment = (
            format(Decimal(1).scaleb(-size_decimals), "f")
            if isinstance(size_decimals, int) and not isinstance(size_decimals, bool) and size_decimals >= 0
            else None
        )
        out.append(
            VenueInstrumentCatalogEntryV1(
                provider_instrument_id=f"{namespace}:{symbol}" if symbol else f"unknown:{raw_sha}",
                provider_symbol=symbol,
                venue="hyperliquid.perp",
                canonical_asset=base if error is None else None,
                canonical_namespace=namespace if error is None else None,
                product_kind="linear_perpetual",
                active=not bool(entry.get("isDelisted")),
                settlement_asset="USDC",
                margin_asset="USDC",
                multiplier="1",
                aliases=(symbol,),
                size_increment=size_increment,
                raw_metadata_sha256=raw_sha,
                normalization_error=error,
            )
        )
    return tuple(out)


def _client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(_TIMEOUT_SECONDS), follow_redirects=False, transport=transport)


async def _get(client: httpx.AsyncClient, url: str, *, venue: str) -> Mapping[str, Any]:
    payload = await _request(client.get, url, venue=venue)
    if not isinstance(payload, Mapping):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return payload


async def _post_mapping(
    client: httpx.AsyncClient, url: str, body: Mapping[str, Any], *, venue: str
) -> Mapping[str, Any]:
    payload = await _request(client.post, url, venue=venue, json=dict(body))
    if not isinstance(payload, Mapping):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return payload


async def _post_list(client: httpx.AsyncClient, url: str, body: Mapping[str, Any], *, venue: str) -> list[Any]:
    payload = await _request(client.post, url, venue=venue, json=dict(body))
    if not isinstance(payload, list):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return payload


async def _request(call: Any, url: str, *, venue: str, **kwargs: Any) -> Any:
    try:
        response = await call(url, **kwargs)
    except httpx.TimeoutException:
        raise VenueExpectedError("venue_timeout", venue=venue) from None
    except httpx.HTTPError:
        raise VenueExpectedError("venue_http_error", venue=venue) from None
    if response.status_code in {403, 451}:
        raise VenueExpectedError("venue_blocked", venue=venue, status_code=response.status_code)
    if response.status_code in {418, 429}:
        raise VenueExpectedError("venue_rate_limited", venue=venue, status_code=response.status_code)
    if response.status_code >= 400:
        raise VenueExpectedError("venue_http_error", venue=venue, status_code=response.status_code)
    if len(response.content) > _MAX_BYTES:
        raise VenueExpectedError("venue_payload_too_large", venue=venue)
    try:
        return response.json()
    except ValueError:
        raise VenueExpectedError("venue_payload_invalid", venue=venue) from None


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _normalize_asset(symbol: str) -> str:
    value = symbol.strip().upper()
    if value.startswith("XYZ-"):
        value = value[4:]
    if ":" in value:
        value = value.split(":", 1)[1]
    return value


def _valid_asset(value: str) -> bool:
    return bool(_ASSET.fullmatch(value))


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _nonnegative_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


__all__ = [
    "BINANCE_USDM_BASE_URL",
    "HYPERLIQUID_BASE_URL",
    "VenueExpectedError",
    "fetch_binance_usdm_catalog",
    "fetch_hyperliquid_perp_catalog",
]
