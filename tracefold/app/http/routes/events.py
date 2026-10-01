from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import TypeAdapter, ValidationError

from tracefold.news.market_review.instruments import normalize_symbol
from tracefold.news.market_review.pricing import QUOTE_REQUEST_SYMBOL_MAX, QuoteRequest
from tracefold.news.models import market_type_of

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged, _json
from ..schemas import common as api_schemas
from ..schemas import events as event_schemas

router = APIRouter()
_EventEnvelope = api_schemas.ApiEnvelope[event_schemas.NewsEventDetailData]
_ItemRelatedEnvelope = api_schemas.ApiEnvelope[event_schemas.NewsItemRelatedEventsData]
_QuotesEnvelope = api_schemas.ApiEnvelope[event_schemas.NewsQuotesData]


@router.get(
    "/news/events/{event_id}",
    response_model=_EventEnvelope,
    responses={
        404: {"model": _EventEnvelope, "description": "Event does not exist"},
    },
)
def get_news_event(request: Request, event_id: str) -> Response:
    _validate_query_params(request, supported={"token"})
    if not event_id or len(event_id) > 128:
        raise ApiBadRequest("news_event_id_invalid", field="event_id")
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = repos.news.event_detail(event_id)
        if data is not None:
            _attach_asset_refs([data["event"]], repos.news, repos.instruments)
            data["normalization"] = _normalization(data["event"], repos.instruments)
            now_ms = int(time.time() * 1000)
            data["reactions"] = repos.price.event_reactions(event_id)
            data["reaction"] = repos.price.event_reaction_aggregates([event_id], now_ms=now_ms).get(event_id)
    if data is None:
        return _json({"ok": False, "error": "news_event_not_found"}, status_code=404)
    return _etagged(data, request, envelope=_EventEnvelope)


@router.get("/news/items/{item_id}/events", response_model=_ItemRelatedEnvelope)
def get_news_item_related_events(
    request: Request,
    item_id: str,
    after: Annotated[str, Query(max_length=128)] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> Response:
    _validate_query_params(request, supported={"token", "after", "limit"})
    if not item_id or len(item_id) > 256:
        raise ApiBadRequest("news_item_id_invalid", field="item_id")
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = repos.news.item_related_events(item_id=item_id, after_event_id=after or None, limit=limit)
    return _etagged(data, request, envelope=_ItemRelatedEnvelope)


@router.get("/news/quotes", response_model=_QuotesEnvelope)
def get_news_quotes(
    request: Request,
    assets: Annotated[str, Query(max_length=12000)] = "[]",
) -> Response:
    """Current quotes for a bounded typed asset batch (#88).

    Deliberately not part of `/api/news/feed`: a price that changes every few seconds would invalidate the
    feed's ETag on every poll and drag the feed and count queries along with it. The browser derives this
    batch from the `assets[]` the feed already returned, so one query serves every row on screen.
    """

    _validate_query_params(request, supported={"assets", "token"})
    requested = _requested_assets(assets)
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        quotes = repos.price.quotes_for_symbols(
            [asset for asset in requested if asset.market_type != "unknown"], now_ms=now_ms
        )
        by_request = {(row["market_type"], row["requested_symbol"]): row for row in quotes}
        quotes = [
            event_schemas.NewsQuoteData(
                requested_symbol=asset.symbol,
                market_type=asset.market_type,
                symbol=normalize_symbol(asset.symbol),
                base_symbol=normalize_symbol(asset.symbol),
                received_age_ms=None,
                source_age_ms=None,
                effective_age_ms=None,
                freshness_basis=None,
                reference_at_ms=None,
                reference_age_ms=None,
                state="unavailable",
                state_zh="市场未确定",
            ).model_dump(mode="json")
            if asset.market_type == "unknown"
            else by_request[(asset.market_type, asset.symbol)]
            for asset in requested
        ]
    return _etagged({"quotes": quotes, "measured_at_ms": now_ms}, request, envelope=_QuotesEnvelope)


def _attach_asset_refs(events: list[dict[str, Any]], news: Any, instruments: Any) -> None:
    """Resolve current primary assets, or the source ledger before adoption, in bounded owner reads.

    Assembly lives in the route because the two halves have different owners: `NewsRepository` reads which assets
    concern each Event, `InstrumentsRepository` reads what they name. One round trip per owner and response, not
    one per Event. `grounded_assets` remains the provider/Gate evidence and is deliberately untouched (#287).
    """

    source_events = [str(event["event_id"]) for event in events if "assets" not in event]
    symbols_by_event = news.event_asset_symbols(source_events) if source_events else {}
    assets_by_event = {
        str(event["event_id"]): tuple(
            dict.fromkeys(
                QuoteRequest(str(asset["symbol"]).strip(), market_type_of(asset.get("market_type")))
                for asset in event.get(
                    "assets",
                    [
                        {"symbol": symbol, "market_type": "unknown"}
                        for symbol in symbols_by_event.get(str(event["event_id"]), ())
                    ],
                )
                if str(asset.get("symbol") or "").strip()
            )
        )
        for event in events
    }
    refs = instruments.asset_refs(asset for assets in assets_by_event.values() for asset in assets)
    for event in events:
        # Distinct typed questions remain distinct even when their spelling or preferred venue matches.
        event["assets"] = [refs[asset] for asset in assets_by_event[str(event["event_id"])]]


def _requested_assets(raw: str) -> list[QuoteRequest]:
    """A bounded typed batch, deduplicated by the complete question rather than its ticker."""

    try:
        parsed = TypeAdapter(list[event_schemas.NewsQuoteRequestData]).validate_json(raw)
    except ValidationError as exc:
        raise ApiBadRequest("news_quotes_assets_invalid", field="assets") from exc
    out = list(dict.fromkeys(QuoteRequest(asset.symbol.strip(), asset.market_type) for asset in parsed))
    if any(not asset.symbol or any(character.isspace() for character in asset.symbol) for asset in out):
        raise ApiBadRequest("news_quotes_assets_invalid", field="assets")
    if len(out) > QUOTE_REQUEST_SYMBOL_MAX:
        raise ApiBadRequest("news_quotes_assets_too_many", field="assets")
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
