from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_json
from tracefold.app.http.validators import _handle_set, _limit
from tracefold.market import EventRead

router = APIRouter()


@router.get("/recent", response_model=api_schemas.ApiEnvelope[api_schemas.RecentData])
def recent(
    request: Request,
    limit: Annotated[int, Query()] = 20,
    handles: Annotated[str, Query()] = "",
    ca: Annotated[str, Query()] = "",
    chain: Annotated[str, Query()] = "",
    symbol: Annotated[str, Query()] = "",
) -> JSONResponse:
    _reject_removed_scope(request)
    runtime = _authenticated_runtime(request)
    data = _recent_data(
        runtime,
        limit=_limit(limit),
        handles=_handle_set(handles),
        ca=ca or None,
        chain=chain or None,
        symbol=symbol or None,
    )
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.RecentData],
        {
            "ok": True,
            "data": data,
        },
    )


@router.get(
    "/events/by-ids",
    response_model=api_schemas.ApiEnvelope[api_schemas.SourceEventsByIdsData],
)
def events_by_ids(
    request: Request,
    ids: Annotated[str, Query()] = "",
) -> JSONResponse:
    runtime = _authenticated_runtime(request)
    raw = [token.strip() for token in (ids or "").split(",") if token.strip()]
    if not raw:
        return JSONResponse(
            {"ok": False, "error": "ids_required", "field": "ids"},
            status_code=400,
        )
    if len(raw) > 200:
        return JSONResponse(
            {"ok": False, "error": "too_many_ids", "field": "ids", "limit": 200},
            status_code=400,
        )
    with runtime.repositories() as repos:
        records = repos.evidence.events_by_ids(raw)
        events_payload = [_source_event_detail(records[event_id]) for event_id in raw if event_id in records]
        not_found = [event_id for event_id in raw if event_id not in records]
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.SourceEventsByIdsData],
        {"ok": True, "data": {"events": events_payload, "not_found": not_found}},
    )


def _payloads_for_events(repos: Any, events: list[EventRead]) -> list[dict[str, Any]]:
    event_ids = tuple(str(event["event_id"]) for event in events)
    entities_by_event = repos.entities.entities_for_events(event_ids)
    intents_by_event = repos.token_intents.intents_for_events(event_ids)
    token_resolutions_by_event = repos.event_tokens.for_events(event_ids)
    return [
        {
            "type": "event",
            "event": event,
            "entities": entities_by_event.get(str(event["event_id"]), []),
            "token_intents": intents_by_event.get(str(event["event_id"]), []),
            "token_resolutions": token_resolutions_by_event.get(str(event["event_id"]), []),
        }
        for event in events
    ]


def _source_event_detail(event: EventRead) -> dict[str, Any]:
    raw_handle = event["author_handle"]
    handle = str(raw_handle).lstrip("@").lower() if raw_handle is not None else None
    followers = event["author_followers"]
    return {
        "event_id": str(event["event_id"]),
        "timestamp_ms": int(event["timestamp_ms"]),
        "source_provider": str(event["source_provider"]),
        "channel": str(event["channel"]),
        "action": str(event["action"]),
        "author_handle": handle,
        "author_name": event["author_name"],
        "author_followers": int(followers) if followers is not None else None,
        "text_clean": event["text_clean"],
        "canonical_url": event["canonical_url"],
    }


def _recent_data(
    runtime: Any,
    *,
    limit: int,
    handles: set[str],
    ca: str | None,
    chain: str | None,
    symbol: str | None,
) -> dict[str, Any]:
    with runtime.repositories() as repos:
        events = repos.evidence.recent_events(
            limit=limit,
            handles=handles,
            ca=ca,
            chain=chain,
            symbol=symbol,
        )
        return {
            "events": events,
            "items": _payloads_for_events(repos, events),
        }


def _reject_removed_scope(request: Request) -> None:
    if "scope" in request.query_params:
        raise ApiBadRequest("unsupported_query_param", field="scope")
