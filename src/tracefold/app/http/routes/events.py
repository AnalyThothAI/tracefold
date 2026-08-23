from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.news import QUOTE_REQUEST_SYMBOL_MAX

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged, _json
from ..schemas import common as api_schemas
from ..schemas import events as event_schemas

router = APIRouter()
_EventEnvelope = api_schemas.ApiEnvelope[event_schemas.NewsEventDetailData]
_QuotesEnvelope = api_schemas.ApiEnvelope[event_schemas.NewsQuotesData]


@router.get("/news/events/{event_id}", response_model=_EventEnvelope)
def get_news_event(request: Request, event_id: str) -> Response:
    _validate_query_params(request, supported={"token"})
    if not event_id or len(event_id) > 128:
        raise ApiBadRequest("news_event_id_invalid", field="event_id")
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = repos.news.event_detail(event_id)
        if data is not None:
            _attach_asset_refs([data["event"]], repos.instruments)
            data["normalization"] = _normalization(data["event"], repos.instruments)
            now_ms = int(time.time() * 1000)
            data["reactions"] = repos.price.event_reactions(event_id)
            data["reaction"] = repos.price.event_reaction_aggregates([event_id], now_ms=now_ms).get(event_id)
    if data is None:
        return _json({"ok": False, "error": "news_event_not_found"}, status_code=404)
    return _etagged(data, request, envelope=_EventEnvelope)


@router.get("/news/quotes", response_model=_QuotesEnvelope)
def get_news_quotes(
    request: Request,
    symbols: Annotated[str, Query(max_length=2000)] = "",
) -> Response:
    """Current quotes for a bounded symbol batch (#88).

    Deliberately not part of `/api/news/feed`: a price that changes every few seconds would invalidate the
    feed's ETag on every poll and drag the feed and count queries along with it. The browser derives this
    batch from the `assets[]` the feed already returned, so one query serves every row on screen.
    """

    _validate_query_params(request, supported={"symbols", "token"})
    requested = _requested_symbols(symbols)
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        quotes = repos.price.quotes_for_symbols(requested, now_ms=now_ms)
    return _etagged({"quotes": quotes, "measured_at_ms": now_ms}, request, envelope=_QuotesEnvelope)


def _attach_asset_refs(events: list[dict[str, Any]], instruments: Any) -> None:
    """Resolve every Event's provider coin tags against the instrument universe, in one batch (#87).

    Assembly lives in the route because the two halves have different owners: `NewsRepository` reads the tags off
    `news_events`, `InstrumentsRepository` reads what they name. One round trip per response, not one per Event.
    """

    refs = instruments.asset_refs({symbol for event in events for symbol in (event.get("grounded_assets") or [])})
    for event in events:
        # One entry per instrument named, not per tag: the provider ships both `CL` and `XYZ-CL` for the same
        # contract, and once those resolve they are byte-identical. The browser happens to dedupe before
        # rendering, but a payload that hands out the same chip twice is the API's fault, not the client's.
        seen: set[str] = set()
        assets: list[dict[str, Any]] = []
        for symbol in event.get("grounded_assets") or []:
            # Keyed by the raw tag, exactly as `asset_refs` returns it — upper-casing here would miss a
            # lower-case tag and silently render it as naming nothing.
            ref = refs.get(str(symbol)) or {
                "symbol": str(symbol).upper(),
                "base_symbol": str(symbol).upper(),
                "venue": None,
                "listed": False,
            }
            if str(ref["symbol"]) in seen:
                continue
            seen.add(str(ref["symbol"]))
            assets.append(ref)
        event["assets"] = assets


def _requested_symbols(raw: str) -> list[str]:
    """A deduplicated, bounded symbol list. The server deduplicates again so a noisy client cannot amplify work."""

    out: list[str] = []
    for part in str(raw or "").split(","):
        symbol = part.strip()
        if not symbol:
            continue
        if len(symbol) > 32:
            raise ApiBadRequest("news_quotes_symbol_invalid", field="symbols")
        if symbol not in out:
            out.append(symbol)
    if len(out) > QUOTE_REQUEST_SYMBOL_MAX:
        raise ApiBadRequest("news_quotes_symbols_too_many", field="symbols")
    return out


def _normalization(event: dict[str, Any], instruments: Any) -> list[dict[str, Any]]:
    """The alias groups this Event's assets fall into — only the ones that actually collapse something.

    A base that answers to exactly one name tells the reader nothing; the block exists to explain why several
    contracts share one storyline bucket.

    Venue-derived aliases are excluded (#87 review). `learn_aliases_from_universe` writes an `XYZ-{base}` row
    for every builder-DEX base and a `dex:SYMBOL` form besides, so counting those would fire the block on
    routine commodity and index Events — `GOLD XAU XAUT XYZ-GOLD -> GOLD` explains nothing a reader did not
    already assume. What is worth a row is the operator-owned collapse the storyline identity depends on:
    SKHY / SKHX / SKHYNIX.
    """

    bases = {str(asset["base_symbol"]) for asset in event.get("assets") or []}
    groups = instruments.aliases_by_base(bases, sources=("seed",))
    return [group for _, group in sorted(groups.items()) if len(group.get("aliases") or []) > 1]


__all__ = ["router"]
