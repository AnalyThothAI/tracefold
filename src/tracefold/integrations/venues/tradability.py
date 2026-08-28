"""Fresh five-venue catalogue verification for post-send single-name cards."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from tracefold.news.market_review.instruments import Instrument, normalize_symbol
from tracefold.news.tradability import (
    REQUIRED_TRADABILITY_VENUES,
    TradabilityMatch,
    TradabilityReview,
    tradability_candidate_identity,
)

from .binance import fetch_binance_instruments_for_candidates
from .bitget import fetch_bitget_instruments
from .hyperliquid import fetch_hyperliquid_instruments
from .lighter import fetch_lighter_instruments
from .okx import fetch_okx_instruments

CatalogFetcher = Callable[[Sequence[str]], Awaitable[Sequence[Instrument]]]


class VenueCatalogTradabilityVerifier:
    """Resolve exact ticker aliases against fresh public catalogues; never infer that a pair exists."""

    def __init__(self, *, fetchers: Mapping[str, CatalogFetcher] | None = None) -> None:
        self._fetchers: dict[str, CatalogFetcher] = dict(
            fetchers
            or {
                "binance": fetch_binance_instruments_for_candidates,
                "hyperliquid": lambda _candidates: fetch_hyperliquid_instruments(strict=True),
                "okx": lambda _candidates: fetch_okx_instruments(),
                "lighter": lambda _candidates: fetch_lighter_instruments(),
                "bitget": lambda _candidates: fetch_bitget_instruments(),
            }
        )

    async def review(
        self,
        *,
        event: Mapping[str, Any],
        verdict: Mapping[str, Any],
        symbols: Sequence[str],
    ) -> TradabilityReview:
        identity = tradability_candidate_identity(event=event, verdict=verdict, symbols=symbols)
        candidates = identity.candidates
        if not candidates or not identity.searchable:
            return TradabilityReview(
                state="incomplete",
                candidates=candidates,
                checked_venues=(),
                failed_venues=(),
                matches=(),
                deletion_safe=False,
                reason_zh="缺少可唯一核验的交易所代码，保留消息等待人工确认。",
            )
        tasks = [self._fetchers[venue](candidates) for venue in REQUIRED_TRADABILITY_VENUES]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        checked: list[str] = []
        failed: list[str] = []
        matches: list[TradabilityMatch] = []
        requested = next((str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()), "")
        candidate_set = {_catalogue_key(value) for value in candidates}
        for venue_family, result in zip(REQUIRED_TRADABILITY_VENUES, results, strict=True):
            if isinstance(result, BaseException):
                failed.append(venue_family)
                continue
            checked.append(venue_family)
            for instrument in result:
                if _catalogue_key(instrument.base_symbol) not in candidate_set:
                    continue
                matches.append(
                    TradabilityMatch(
                        requested_symbol=requested or instrument.base_symbol,
                        venue_family=venue_family,
                        venue=instrument.venue,
                        venue_symbol=_reader_venue_symbol(instrument),
                        price_symbol=instrument.venue_symbol,
                        base_symbol=instrument.base_symbol,
                        quote_asset=instrument.quote_asset,
                        instrument_class=instrument.instrument_class,
                    )
                )
        ordered = tuple(sorted(_dedupe(matches), key=_match_rank)[:20])
        if ordered:
            return TradabilityReview(
                state="matched",
                candidates=candidates,
                checked_venues=tuple(checked),
                failed_venues=tuple(failed),
                matches=ordered,
                deletion_safe=identity.deletion_safe,
                reason_zh=f"已在 {ordered[0].venue} 官方市场目录命中可交易合约。",
            )
        if failed:
            return TradabilityReview(
                state="incomplete",
                candidates=candidates,
                checked_venues=tuple(checked),
                failed_venues=tuple(failed),
                matches=(),
                deletion_safe=identity.deletion_safe,
                reason_zh="部分交易所目录查询失败，按安全规则保留消息。",
            )
        return TradabilityReview(
            state="absent",
            candidates=candidates,
            checked_venues=tuple(checked),
            failed_venues=(),
            matches=(),
            deletion_safe=identity.deletion_safe,
            reason_zh=(
                "Binance、Hyperliquid、OKX、Lighter、Bitget 均未发现可交易合约。"
                if identity.deletion_safe
                else "五个交易所均未命中，但标题代码缺少交易所前缀，保留消息等待人工确认。"
            ),
        )


def _catalogue_key(value: object) -> str:
    return "".join(character for character in normalize_symbol(str(value)) if character.isalnum())


def _reader_venue_symbol(instrument: Instrument) -> str:
    # Lighter's provider query key is numeric; the reader-facing route is the base ticker.
    return instrument.base_symbol if instrument.venue.startswith("lighter.") else instrument.venue_symbol


def _dedupe(matches: Sequence[TradabilityMatch]) -> list[TradabilityMatch]:
    out: list[TradabilityMatch] = []
    seen: set[tuple[str, str]] = set()
    for match in matches:
        identity = (match.venue, match.price_symbol)
        if identity not in seen:
            seen.add(identity)
            out.append(match)
    return out


def _match_rank(match: TradabilityMatch) -> tuple[int, int, str]:
    family_rank = {"binance": 0, "hyperliquid": 1, "okx": 2, "lighter": 3, "bitget": 4}
    spot_penalty = 1 if match.venue.endswith(".spot") else 0
    return (family_rank[match.venue_family], spot_penalty, match.venue_symbol)


__all__ = ["VenueCatalogTradabilityVerifier"]
