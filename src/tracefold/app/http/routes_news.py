from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http import schemas_news
from tracefold.app.http.dependencies import _authenticated_runtime
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _json, _validated_etag_json
from tracefold.app.workers.runtime import WorkersRuntimeRepository, workers_runtime_status
from tracefold.platform.config.settings import news_model_availability, news_push_availability

router = APIRouter()
_FeedEnvelope = api_schemas.ApiEnvelope[schemas_news.NewsFeedData]
_EventEnvelope = api_schemas.ApiEnvelope[schemas_news.NewsEventDetailData]
_StatusEnvelope = api_schemas.ApiEnvelope[schemas_news.NewsStatusData]

_ADMISSIONS = {
    "candidate",
    "listing_deterministic",
    "suppressed_ungrounded",
    "suppressed_ungrounded_meme",
    "suppressed_meme_low",
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
    priority: Annotated[str, Query(pattern="^(high|normal)?$")] = "",
    decision: Annotated[str, Query(max_length=16)] = "",
    symbol: Annotated[str, Query(max_length=16)] = "",
    q: Annotated[str, Query(max_length=200)] = "",
    sort: Annotated[str, Query(pattern="^(latest|priority)$")] = "latest",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str, Query(max_length=200)] = "",
) -> Response:
    _validate_query_params(
        request,
        supported={"family", "admission", "priority", "decision", "symbol", "q", "sort", "limit", "cursor", "token"},
    )
    if admission and admission not in _ADMISSIONS:
        raise ApiBadRequest("news_feed_admission_invalid", field="admission")
    if decision and decision not in _DECISIONS:
        raise ApiBadRequest("news_feed_decision_invalid", field="decision")
    runtime = _authenticated_runtime(request)
    try:
        with runtime.repositories() as repos:
            data = repos.news.list_feed(
                family=family or None,
                admission=admission or None,
                priority=priority or None,
                decision=decision or None,
                symbol=symbol or None,
                q=q or None,
                sort=sort,
                limit=limit,
                cursor=cursor or None,
            )
    except ValueError as exc:
        raise ApiBadRequest(str(exc), field="cursor") from exc
    return _etagged(data, request, envelope=_FeedEnvelope)


@router.get("/news/events/{event_id}", response_model=_EventEnvelope)
def get_news_event(request: Request, event_id: str) -> Response:
    _validate_query_params(request, supported={"token"})
    if not event_id or len(event_id) > 128:
        raise ApiBadRequest("news_event_id_invalid", field="event_id")
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = repos.news.event_detail(event_id)
    if data is None:
        return _json({"ok": False, "error": "news_event_not_found"}, status_code=404)
    return _etagged(data, request, envelope=_EventEnvelope)


@router.get("/news/status", response_model=_StatusEnvelope)
def get_news_status(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    settings = runtime.settings
    with runtime.repositories() as repos:
        snapshot = repos.news.status_snapshot(now_ms=now_ms)
        workers_state, _ = _news_workers_observation(repos.conn, now_ms=now_ms)
    push = news_push_availability(settings)
    models = news_model_availability(settings)
    observed = dict(snapshot.get("broker") or {})
    broker_data = {
        "configured": bool(settings.news.broker.url),
        "connected": observed.get("connected"),
        "queues": {str(k): v for k, v in (observed.get("queues") or {}).items()},
        "error_code": observed.get("error_code"),
        "observed_at_ms": observed.get("observed_at_ms"),
    }
    ingest = {**snapshot["ingest"], "token_configured": bool(settings.news.opennews_token)}
    pipeline = {**snapshot["pipeline"], "triage_model": models.triage_model, "analyst_model": models.analyst_model}
    delivery = {
        **snapshot["delivery"],
        "delivery_available": push.delivery_available,
        "hourly_cap": int(settings.news.push.hourly_cap),
    }
    state = _derive_state(ingest=ingest, broker=broker_data, workers_state=workers_state, settings=settings)
    data = {
        "state": state,
        "workers_state": workers_state,
        "ingest": ingest,
        "broker": broker_data,
        "pipeline": pipeline,
        "delivery": delivery,
        "control": {
            "paused": bool(snapshot["control"].get("paused")),
            "mutes": list(snapshot["control"].get("mutes") or []),
        },
        "watchlist": sorted(settings.news.watchlist_symbols),
        "measured_at_ms": now_ms,
    }
    return _validated_etag_json(
        _StatusEnvelope,
        {"ok": True, "data": data},
        data=data,
        etag_data=_status_etag_basis(data),
        request=request,
        weak=True,
    )


def _derive_state(*, ingest: dict[str, Any], broker: dict[str, Any], workers_state: str | None, settings: Any) -> str:
    if not settings.news.enabled or not settings.news.opennews_token or not settings.news.broker.url:
        return "unavailable"
    if workers_state != "running":
        return "warming" if workers_state in {None, "recovering"} else "degraded"
    if broker.get("connected") is False or ingest.get("open_incidents"):
        return "degraded"
    if not ingest.get("connected"):
        return "warming"
    return "ready"


def _news_workers_observation(conn: Any, *, now_ms: int) -> tuple[str | None, str | None]:
    row = WorkersRuntimeRepository(conn).read()
    if row is None:
        return None, None
    status = workers_runtime_status(row, now_ms=now_ms)
    state = str(status["state"])
    if state == "running":
        return "running", None
    if state in {"starting", "stopping"}:
        return "recovering", f"workers_runtime_{state}"
    if state == "stale":
        return "stalled", "workers_runtime_heartbeat_stale"
    if state in {"stopped", "failed"}:
        return "stalled", f"workers_runtime_{state}"
    raise RuntimeError("news_workers_runtime_state_invalid")


def _etagged(data: dict[str, Any], request: Request, *, envelope: type[BaseModel]) -> JSONResponse | Response:
    return _validated_etag_json(envelope, {"ok": True, "data": data}, data=data, request=request)


def _validate_query_params(request: Request, *, supported: set[str]) -> None:
    for name in request.query_params:
        if name not in supported:
            raise ApiBadRequest("unsupported_query_param", field=name)


def _status_etag_basis(data: dict[str, Any]) -> dict[str, Any]:
    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in {"measured_at_ms"}}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    return stable(data)


__all__ = ["router"]
