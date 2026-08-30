"""Read-only provider funding-rate history normalized for the Trading evidence clock."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

import httpx

from .candles import BINANCE_FUTURES_BASE_URL, HYPERLIQUID_BASE_URL
from .errors import VenueExpectedError
from .http import get_json, post_json, price_client

_BINANCE_PAGE_SIZE = 1000
_HYPERLIQUID_PAGE_SIZE = 500
_MAX_PAGES = 20


@dataclass(frozen=True, slots=True)
class VenueFundingRate:
    """One provider-native funding row; App maps it into a business contract."""

    venue: Literal["binance.perp", "hl.perp"]
    provider_instrument_id: str
    funding_at_ms: int
    funding_rate: Decimal


async def fetch_binance_funding_rates(
    provider_instrument_id: str,
    *,
    start_ms: int,
    end_ms: int,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BINANCE_FUTURES_BASE_URL,
) -> tuple[VenueFundingRate, ...]:
    _validate_window(start_ms, end_ms)
    cursor = start_ms
    rows: list[VenueFundingRate] = []
    async with price_client(transport) as client:
        for _page in range(_MAX_PAGES):
            payload = await get_json(
                client,
                f"{base_url.rstrip('/')}/fapi/v1/fundingRate",
                venue="binance.perp",
                params={
                    "symbol": provider_instrument_id,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": _BINANCE_PAGE_SIZE,
                },
            )
            page = _binance_page(payload, provider_instrument_id, start_ms=start_ms, end_ms=end_ms)
            rows.extend(page)
            if len(page) < _BINANCE_PAGE_SIZE:
                return _canonical_rows(rows)
            next_cursor = page[-1].funding_at_ms + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
    raise VenueExpectedError("venue_funding_coverage_unproven", venue="binance.perp")


async def fetch_hyperliquid_funding_rates(
    provider_instrument_id: str,
    *,
    start_ms: int,
    end_ms: int,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = HYPERLIQUID_BASE_URL,
) -> tuple[VenueFundingRate, ...]:
    _validate_window(start_ms, end_ms)
    cursor = start_ms
    rows: list[VenueFundingRate] = []
    async with price_client(transport) as client:
        for _page in range(_MAX_PAGES):
            payload = await post_json(
                client,
                f"{base_url.rstrip('/')}/info",
                {
                    "type": "fundingHistory",
                    "coin": provider_instrument_id,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                },
                venue="hl.perp",
            )
            page = _hyperliquid_page(payload, provider_instrument_id, start_ms=start_ms, end_ms=end_ms)
            rows.extend(page)
            if len(page) < _HYPERLIQUID_PAGE_SIZE:
                return _canonical_rows(rows)
            next_cursor = page[-1].funding_at_ms + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
    raise VenueExpectedError("venue_funding_coverage_unproven", venue="hl.perp")


def _binance_page(
    payload: Any,
    provider_instrument_id: str,
    *,
    start_ms: int,
    end_ms: int,
) -> list[VenueFundingRate]:
    if not isinstance(payload, Sequence) or isinstance(payload, str | bytes):
        raise VenueExpectedError("venue_payload_invalid", venue="binance.perp")
    rows: list[VenueFundingRate] = []
    for value in payload:
        if not isinstance(value, Mapping) or str(value.get("symbol") or "") != provider_instrument_id:
            raise VenueExpectedError("venue_payload_invalid", venue="binance.perp")
        rows.append(
            _rate(
                venue="binance.perp",
                provider_instrument_id=provider_instrument_id,
                funding_at_ms=value.get("fundingTime"),
                funding_rate=value.get("fundingRate"),
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )
    return rows


def _hyperliquid_page(
    payload: Any,
    provider_instrument_id: str,
    *,
    start_ms: int,
    end_ms: int,
) -> list[VenueFundingRate]:
    if not isinstance(payload, Sequence) or isinstance(payload, str | bytes):
        raise VenueExpectedError("venue_payload_invalid", venue="hl.perp")
    rows: list[VenueFundingRate] = []
    for value in payload:
        if not isinstance(value, Mapping) or str(value.get("coin") or "") != provider_instrument_id:
            raise VenueExpectedError("venue_payload_invalid", venue="hl.perp")
        rows.append(
            _rate(
                venue="hl.perp",
                provider_instrument_id=provider_instrument_id,
                funding_at_ms=value.get("time"),
                funding_rate=value.get("fundingRate"),
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )
    return rows


def _rate(
    *,
    venue: str,
    provider_instrument_id: str,
    funding_at_ms: object,
    funding_rate: object,
    start_ms: int,
    end_ms: int,
) -> VenueFundingRate:
    try:
        timestamp = int(cast(Any, funding_at_ms))
        rate = Decimal(str(funding_rate))
    except (InvalidOperation, TypeError, ValueError):
        raise VenueExpectedError("venue_payload_invalid", venue=venue) from None
    if not rate.is_finite() or not start_ms <= timestamp < end_ms:
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return VenueFundingRate(
        venue=cast(Any, venue),
        provider_instrument_id=provider_instrument_id,
        funding_at_ms=timestamp,
        funding_rate=rate,
    )


def _canonical_rows(rows: list[VenueFundingRate]) -> tuple[VenueFundingRate, ...]:
    ordered = tuple(sorted(rows, key=lambda row: row.funding_at_ms))
    if len({row.funding_at_ms for row in ordered}) != len(ordered):
        venue = ordered[0].venue if ordered else "funding"
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return ordered


def _validate_window(start_ms: int, end_ms: int) -> None:
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("venue_funding_window_invalid")


__all__ = ["VenueFundingRate", "fetch_binance_funding_rates", "fetch_hyperliquid_funding_rates"]
