from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_etag_json, _validated_json
from tracefold.app.workers_runtime import WorkersRuntimeRepository, workers_runtime_status

router = APIRouter()
_FeedEnvelope = api_schemas.ApiEnvelope[api_schemas.NewsFeedData]
_StoryEnvelope = api_schemas.ApiEnvelope[api_schemas.NewsStoryDetailData]
_BriefEnvelope = api_schemas.ApiEnvelope[api_schemas.NewsBriefData]
_SourcesEnvelope = api_schemas.ApiEnvelope[api_schemas.NewsSourcesData]
_StatusEnvelope = api_schemas.ApiEnvelope[api_schemas.NewsStatusData]


@router.get("/news/feed", response_model=_FeedEnvelope)
def get_news_feed(
    request: Request,
    category: Annotated[str, Query()] = "",
    level: Annotated[str, Query()] = "",
    source_id: Annotated[str, Query()] = "",
    reporting_origin: Annotated[str, Query(max_length=128)] = "",
    provider_score_gt: Annotated[float | None, Query()] = None,
    q: Annotated[str, Query(max_length=200)] = "",
    sort: Annotated[str, Query(pattern="^(importance|latest)$")] = "importance",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str, Query()] = "",
) -> Response:
    _validate_query_params(
        request,
        supported={
            "category",
            "level",
            "source_id",
            "reporting_origin",
            "provider_score_gt",
            "q",
            "sort",
            "limit",
            "cursor",
            "token",
        },
    )
    runtime = _authenticated_runtime(request)
    now_ms = _now_ms()
    try:
        with runtime.repositories() as repos:
            data = repos.news.list_feed(
                push_enabled=runtime.settings.news.push.enabled,
                now_ms=now_ms,
                category=category or None,
                level=level or None,
                source_id=source_id or None,
                reporting_origin=reporting_origin or None,
                provider_score_gt=provider_score_gt,
                q=q or None,
                sort=sort,
                limit=limit,
                cursor=cursor or None,
            )
    except ValueError as exc:
        code = str(exc)
        field = {
            "news_feed_provider_score_gt_invalid": "provider_score_gt",
            "news_feed_query_invalid": "q",
            "news_feed_reporting_origin_invalid": "reporting_origin",
        }.get(code, "cursor")
        raise ApiBadRequest(code, field=field) from exc
    return _etagged(data, request, envelope=_FeedEnvelope)


@router.get("/news/stories/{story_id}", response_model=_StoryEnvelope)
def get_news_story(
    request: Request,
    story_id: str,
    members_limit: Annotated[int, Query(ge=1, le=100)] = 100,
    members_cursor: Annotated[str, Query()] = "",
) -> JSONResponse:
    _validate_query_params(
        request,
        supported={"token", "members_limit", "members_cursor"},
    )
    runtime = _authenticated_runtime(request)
    now_ms = _now_ms()
    try:
        with runtime.repositories() as repos:
            data = repos.news.get_story(
                story_id=story_id.strip(),
                push_enabled=runtime.settings.news.push.enabled,
                now_ms=now_ms,
                members_limit=members_limit,
                members_cursor=members_cursor or None,
            )
    except ValueError as exc:
        raise ApiBadRequest(str(exc), field="members_cursor") from exc
    if data is None:
        return _validated_json(
            _StoryEnvelope,
            {"ok": False, "error": "news_story_not_found"},
            status_code=404,
        )
    return _validated_json(_StoryEnvelope, {"ok": True, "data": data})


@router.get("/news/brief", response_model=_BriefEnvelope)
def get_news_world_brief(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = repos.news.get_brief(now_ms=int(time.time() * 1000))
    return _etagged(data, request, envelope=_BriefEnvelope)


@router.get("/news/sources", response_model=_SourcesEnvelope)
def get_news_sources(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str, Query()] = "",
) -> JSONResponse:
    _validate_query_params(request, supported={"token", "limit", "cursor"})
    runtime = _authenticated_runtime(request)
    try:
        with runtime.repositories() as repos:
            data = repos.news.list_sources(
                limit=limit,
                cursor=cursor or None,
            )
    except ValueError as exc:
        raise ApiBadRequest(str(exc), field="cursor") from exc
    return _validated_json(_SourcesEnvelope, {"ok": True, "data": data})


@router.get("/news/status", response_model=_StatusEnvelope)
def get_news_status(request: Request) -> JSONResponse:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    push_settings = runtime.settings.news.push
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        workers_state, workers_reason = _news_workers_observation(
            repos.conn,
            now_ms=now_ms,
        )
        data = repos.news.health_snapshot(
            now_ms=now_ms,
            rss_enabled=runtime.settings.news.rss_enabled,
            configured_strategy_count=len(runtime.settings.news.opennews_strategy_ids),
            push_enabled=push_settings.enabled,
            feishu_webhook_url_configured=bool(push_settings.feishu_webhook_url),
            feishu_signing_secret_configured=bool(push_settings.feishu_signing_secret),
            workers_state=workers_state,
            workers_reason=workers_reason,
        )
    return _validated_json(_StatusEnvelope, {"ok": True, "data": data})


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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _etagged(
    data: dict[str, Any],
    request: Request,
    *,
    envelope: type[BaseModel],
) -> JSONResponse | Response:
    return _validated_etag_json(
        envelope,
        {"ok": True, "data": data},
        data=data,
        request=request,
    )


def _validate_query_params(request: Request, *, supported: set[str]) -> None:
    for name in request.query_params:
        if name not in supported:
            raise ApiBadRequest("unsupported_query_param", field=name)


__all__ = ["router"]
