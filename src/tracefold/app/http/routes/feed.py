from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

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
    "suppressed_pr_template",
    "suppressed_low_signal",
    "recovery",
}
_DECISIONS = {"push", "escalate", "drop", "throttled", "degraded"}


@router.get("/news/feed", response_model=_FeedEnvelope)
def get_news_feed(
    request: Request,
    family: Annotated[str, Query(max_length=32)] = "",
    admission: Annotated[str, Query(max_length=40)] = "",
    decision: Annotated[str, Query(max_length=16)] = "",
    symbol: Annotated[str, Query(max_length=16)] = "",
    q: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str, Query(max_length=200)] = "",
    outcome: Annotated[str, Query(pattern="^(pushed|held|pending)?$")] = "",
    hours: Annotated[int, Query(ge=0, le=168)] = 0,
) -> Response:
    _validate_query_params(
        request,
        supported={
            "family",
            "admission",
            "decision",
            "symbol",
            "q",
            "limit",
            "cursor",
            "outcome",
            "hours",
            "token",
        },
    )
    if admission and admission not in _ADMISSIONS:
        raise ApiBadRequest("news_feed_admission_invalid", field="admission")
    if decision and decision not in _DECISIONS:
        raise ApiBadRequest("news_feed_decision_invalid", field="decision")
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        try:
            data = repos.news.list_feed(
                family=family or None,
                admission=admission or None,
                decision=decision or None,
                symbol=symbol or None,
                q=q or None,
                limit=limit,
                cursor=cursor or None,
                outcome=outcome or None,
                hours=hours or None,
            )
        except ValueError as exc:
            # Only `list_feed` decodes the cursor. Anything that fails while resolving instruments is a
            # server fault and must not come back as a 400 naming a field the caller got right (#87 review).
            raise ApiBadRequest(str(exc), field="cursor") from exc
        _attach_asset_refs(data["events"], repos.instruments)
        _attach_reactions(data["events"], repos.price, now_ms=int(time.time() * 1000))
    return _etagged(data, request, envelope=_FeedEnvelope)


def _attach_reactions(events: list[dict[str, Any]], price: Any, *, now_ms: int) -> None:
    """One bounded batch for the whole page: at most `limit` Event ids, never one query per row."""

    aggregates = price.event_reaction_aggregates([event["event_id"] for event in events], now_ms=now_ms)
    for event in events:
        event["reaction"] = aggregates.get(str(event["event_id"]))


__all__ = ["router"]
