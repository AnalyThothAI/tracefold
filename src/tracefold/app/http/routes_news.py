from __future__ import annotations

import hashlib
import json
import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_json
from tracefold.news import NewsInterface

router = APIRouter()
_Envelope = api_schemas.ApiEnvelope[api_schemas.JsonObject]


@router.get("/news/feed", response_model=_Envelope)
def get_news_feed(
    request: Request,
    category: Annotated[str, Query()] = "",
    level: Annotated[str, Query()] = "",
    source_id: Annotated[str, Query()] = "",
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
            "sort",
            "limit",
            "cursor",
            "token",
        },
    )
    runtime = _authenticated_runtime(request)
    try:
        with runtime.repositories() as repos:
            data = _news_interface(repos).get_feed(
                category=category or None,
                level=level or None,
                source_id=source_id or None,
                sort=sort,
                limit=limit,
                cursor=cursor or None,
            )
    except ValueError as exc:
        raise ApiBadRequest(str(exc), field="cursor") from exc
    return _etagged(data, request)


@router.get("/news/stories/{story_id}", response_model=_Envelope)
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
    try:
        with runtime.repositories() as repos:
            data = _news_interface(repos).get_story(
                story_id=story_id,
                members_limit=members_limit,
                members_cursor=members_cursor or None,
            )
    except ValueError as exc:
        raise ApiBadRequest(str(exc), field="members_cursor") from exc
    if data is None:
        return _validated_json(
            _Envelope,
            {"ok": False, "error": "news_story_not_found"},
            status_code=404,
        )
    return _validated_json(_Envelope, {"ok": True, "data": data})


@router.get("/news/brief", response_model=_Envelope)
def get_news_world_brief(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = _news_interface(repos).get_world_brief(now_ms=int(time.time() * 1000))
    return _etagged(data, request)


@router.get("/news/sources", response_model=_Envelope)
def get_news_sources(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str, Query()] = "",
) -> JSONResponse:
    _validate_query_params(request, supported={"token", "limit", "cursor"})
    runtime = _authenticated_runtime(request)
    try:
        with runtime.repositories() as repos:
            data = _news_interface(repos).get_sources(
                limit=limit,
                cursor=cursor or None,
            )
    except ValueError as exc:
        raise ApiBadRequest(str(exc), field="cursor") from exc
    return _validated_json(_Envelope, {"ok": True, "data": data})


@router.get("/news/status", response_model=_Envelope)
def get_news_status(request: Request) -> JSONResponse:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = _news_interface(repos).health(now_ms=int(time.time() * 1000))
    return _validated_json(_Envelope, {"ok": True, "data": data})


def _news_interface(repos: Any) -> NewsInterface:
    return NewsInterface(repos.news)


def _etagged(data: dict[str, Any], request: Request) -> JSONResponse | Response:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    etag = f'"{hashlib.sha256(encoded).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response = _validated_json(_Envelope, {"ok": True, "data": data})
    response.headers.update(headers)
    return response


def _validate_query_params(request: Request, *, supported: set[str]) -> None:
    for name in request.query_params:
        if name not in supported:
            raise ApiBadRequest("unsupported_query_param", field=name)


__all__ = ["router"]
