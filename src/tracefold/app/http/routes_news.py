from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_json
from tracefold.app.http.validators import _limit
from tracefold.news import NewsInterface

router = APIRouter()


@router.get(
    "/news/stories",
    response_model=api_schemas.ApiEnvelope[api_schemas.NewsStoryListData],
)
def list_news_stories(
    request: Request,
    limit: Annotated[int, Query()] = 50,
    cursor: Annotated[str, Query()] = "",
    q: Annotated[str, Query()] = "",
    evidence_posture: Annotated[str, Query()] = "",
    source: Annotated[str, Query()] = "",
) -> JSONResponse:
    _validate_query_params(
        request,
        supported={"limit", "cursor", "q", "evidence_posture", "source", "token"},
    )
    runtime = _authenticated_runtime(request)
    try:
        with runtime.repositories() as repos:
            data = _news_interface(repos).list_stories(
                limit=_limit(limit, maximum=200),
                cursor=cursor or None,
                q=q or None,
                evidence_posture=evidence_posture or None,
                source=source or None,
            )
    except ValueError as exc:
        raise ApiBadRequest(str(exc)) from exc
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.NewsStoryListData],
        {"ok": True, "data": data},
    )


@router.get(
    "/news/stories/{story_id}",
    response_model=api_schemas.ApiEnvelope[api_schemas.NewsStoryDetailData],
)
def get_news_story(request: Request, story_id: str) -> JSONResponse:
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = _news_interface(repos).get_story(story_id=story_id)
    if data is None:
        return _validated_json(
            api_schemas.ApiEnvelope[api_schemas.NewsStoryDetailData],
            {"ok": False, "error": "news_story_not_found"},
            status_code=404,
        )
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.NewsStoryDetailData],
        {"ok": True, "data": data},
    )


@router.post(
    "/news/stories/{story_id}/analysis-requests",
    response_model=api_schemas.ApiEnvelope[api_schemas.NewsStoryAnalysisRequestData],
)
def request_news_story_analysis(request: Request, story_id: str) -> JSONResponse:
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos, repos.transaction():
        data = _news_interface(repos).request_story_analysis(
            story_id=story_id,
            now_ms=int(time.time() * 1000),
        )
    if data is None:
        return _validated_json(
            api_schemas.ApiEnvelope[api_schemas.NewsStoryAnalysisRequestData],
            {"ok": False, "error": "news_story_not_found"},
            status_code=404,
        )
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.NewsStoryAnalysisRequestData],
        {"ok": True, "data": data},
        status_code=202,
    )


@router.get(
    "/news/brief",
    response_model=api_schemas.ApiEnvelope[api_schemas.NewsGlobalBriefData],
)
def get_news_global_brief(request: Request) -> JSONResponse:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = _news_interface(repos).get_global_brief()
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.NewsGlobalBriefData],
        {"ok": True, "data": data},
    )


@router.get(
    "/news/brief/history",
    response_model=api_schemas.ApiEnvelope[api_schemas.NewsGlobalBriefHistoryData],
)
def list_news_global_brief_history(
    request: Request,
    limit: Annotated[int, Query()] = 20,
) -> JSONResponse:
    _validate_query_params(request, supported={"limit", "token"})
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = _news_interface(repos).list_global_brief_history(
            limit=_limit(limit, maximum=100),
        )
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.NewsGlobalBriefHistoryData],
        {"ok": True, "data": data},
    )


@router.get(
    "/news/sources",
    response_model=api_schemas.ApiEnvelope[api_schemas.NewsSourcesData],
)
def list_news_sources(request: Request) -> JSONResponse:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = _news_interface(repos).list_sources()
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.NewsSourcesData],
        {"ok": True, "data": data},
    )


def _news_interface(repos: Any) -> NewsInterface:
    return NewsInterface(repos.news)


def _validate_query_params(request: Request, *, supported: set[str]) -> None:
    for name in request.query_params:
        if name not in supported:
            raise ApiBadRequest("unsupported_query_param", field=name)


__all__ = ["router"]
