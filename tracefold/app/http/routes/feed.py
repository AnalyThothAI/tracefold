from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.news import (
    ASSERTION_STATUSES,
    CHANGE_STATES,
    EVENT_FAMILIES,
    EVENT_KINDS,
    IPTC_SUBJECT_CODES,
    SOURCE_AUTHORITIES,
)

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged
from ..schemas import common as api_schemas
from ..schemas import feed as feed_schemas
from .events import _attach_asset_refs

router = APIRouter()
_FeedEnvelope = api_schemas.ApiEnvelope[feed_schemas.NewsFeedData]

_ADMISSIONS = {
    "candidate",
    "listing_deterministic",
    "telemetry_deterministic",
    "liquidation_deterministic",
    "unsupported_market_contract",
    "suppressed_pr_template",
    "suppressed_low_signal",
    "recovery",
}
_FINAL_DECISIONS = ("push", "escalate", "drop", "throttled")
# #207: the deterministic OI lane's outcome, for the 持仓异动 monitor's tabs. `final_decision` cannot express it —
# a frame held by a threshold and one whose provider template stopped parsing are both `drop` — and the
# browser must not split them by filtering a loaded page, which would leave the tab counts describing the
# whole window while the rows below described one page of it.
_OI_OUTCOMES = {"all", "pushed", "withheld", "parse_failed"}
_DIRECTIONS = ("bullish", "bearish", "neutral")


@router.get("/news/feed", response_model=_FeedEnvelope)
def get_news_feed(
    request: Request,
    event_family: Annotated[str, Query(max_length=320)] = "",
    change_state: Annotated[str, Query(max_length=128)] = "",
    assertion_status: Annotated[str, Query(max_length=96)] = "",
    source_authority: Annotated[str, Query(max_length=128)] = "",
    subject_code: Annotated[str, Query(max_length=768)] = "",
    final_decision: Annotated[str, Query(max_length=64)] = "",
    event_kind: Annotated[str, Query(max_length=128)] = "",
    admission: Annotated[str, Query(max_length=40)] = "",
    symbol: Annotated[str, Query(max_length=32)] = "",
    q: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str, Query(max_length=200)] = "",
    outcome: Annotated[str, Query(pattern="^(pushed|held|pending)?$")] = "",
    hours: Annotated[int, Query(ge=0, le=168)] = 0,
    # Wide enough to hold a full rule key: someone reaching for `whale_ratio_below_threshold` should get
    # the named `news_feed_oi_invalid` rather than a shape error that says nothing about the vocabulary.
    oi: Annotated[str, Query(max_length=40)] = "",
    direction: Annotated[str, Query(max_length=40)] = "",
) -> Response:
    _validate_query_params(
        request,
        supported={
            "event_family",
            "change_state",
            "assertion_status",
            "source_authority",
            "subject_code",
            "final_decision",
            "event_kind",
            "admission",
            "symbol",
            "q",
            "limit",
            "cursor",
            "outcome",
            "hours",
            "oi",
            "direction",
            "token",
        },
    )
    if admission and admission not in _ADMISSIONS:
        raise ApiBadRequest("news_feed_admission_invalid", field="admission")
    if oi and oi not in _OI_OUTCOMES:
        raise ApiBadRequest("news_feed_oi_invalid", field="oi")
    event_families = _parse_csv_filter(
        event_family,
        allowed=EVENT_FAMILIES,
        error="news_feed_event_family_invalid",
        field="event_family",
    )
    change_states = _parse_csv_filter(
        change_state,
        allowed=CHANGE_STATES,
        error="news_feed_change_state_invalid",
        field="change_state",
    )
    assertion_statuses = _parse_csv_filter(
        assertion_status,
        allowed=ASSERTION_STATUSES,
        error="news_feed_assertion_status_invalid",
        field="assertion_status",
    )
    source_authorities = _parse_csv_filter(
        source_authority,
        allowed=SOURCE_AUTHORITIES,
        error="news_feed_source_authority_invalid",
        field="source_authority",
    )
    subject_codes = _parse_csv_filter(
        subject_code,
        allowed=IPTC_SUBJECT_CODES,
        error="news_feed_subject_code_invalid",
        field="subject_code",
    )
    final_decisions = _parse_csv_filter(
        final_decision,
        allowed=_FINAL_DECISIONS,
        error="news_feed_final_decision_invalid",
        field="final_decision",
    )
    event_kinds = _parse_csv_filter(
        event_kind,
        allowed=EVENT_KINDS,
        error="news_feed_event_kind_invalid",
        field="event_kind",
    )
    directions = _parse_csv_filter(
        direction, allowed=_DIRECTIONS, error="news_feed_direction_invalid", field="direction"
    )
    if q.strip() and symbol.strip():
        raise ApiBadRequest("news_feed_search_conflict", field="q")
    runtime = _authenticated_runtime(request)
    search_started_at = time.perf_counter()
    with runtime.repositories() as repos:
        search = repos.compile_news_search(q=q or None, symbol=symbol or None)
        try:
            data = repos.news.list_feed(
                event_family=event_families,
                change_state=change_states,
                assertion_status=assertion_statuses,
                source_authority=source_authorities,
                subject_code=subject_codes,
                final_decision=final_decisions,
                event_kind=event_kinds,
                admission=admission or None,
                search=search,
                limit=limit,
                cursor=cursor or None,
                outcome=outcome or None,
                hours=hours or None,
                oi=oi or None,
                directions=directions,
            )
        except ValueError as exc:
            # Only `list_feed` decodes the cursor. Anything that fails while resolving instruments is a
            # server fault and must not come back as a 400 naming a field the caller got right (#87 review).
            raise ApiBadRequest(str(exc), field="cursor") from exc
        _attach_asset_refs(data["events"], repos.news, repos.instruments)
        _attach_reactions(data["events"], repos.price, now_ms=int(time.time() * 1000))
    if search is not None and not cursor:
        runtime.telemetry.record_news_search(
            search.mode,
            result="zero" if not data["events"] else "nonzero",
            seconds=time.perf_counter() - search_started_at,
        )
    return _etagged(data, request, envelope=_FeedEnvelope)


def _parse_csv_filter(value: str, *, allowed: tuple[str, ...], error: str, field: str) -> tuple[str, ...] | None:
    if not value:
        return None
    requested = value.split(",")
    if any(not item or item not in allowed for item in requested) or len(set(requested)) != len(requested):
        raise ApiBadRequest(error, field=field)
    selected = tuple(item for item in allowed if item in requested)
    return selected or None


def _attach_reactions(events: list[dict[str, Any]], price: Any, *, now_ms: int) -> None:
    """One bounded batch for the whole page: at most `limit` Event ids, never one query per row."""

    aggregates = price.event_reaction_aggregates([event["event_id"] for event in events], now_ms=now_ms)
    for event in events:
        event["reaction"] = aggregates.get(str(event["event_id"]))


__all__ = ["router"]
