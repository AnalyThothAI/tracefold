from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_json
from tracefold.app.http.validators import _limit
from tracefold.app.llm import litellm_proxy_model_name, llm_is_configured
from tracefold.news import NewsAnalysisContract, StoryInterface

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
    verification_status: Annotated[str, Query()] = "",
    source: Annotated[str, Query()] = "",
) -> JSONResponse:
    _validate_query_params(
        request,
        supported={"limit", "cursor", "q", "verification_status", "source", "token"},
    )
    runtime = _authenticated_runtime(request)
    try:
        with runtime.repositories() as repos:
            data = _story_interface(runtime, repos).list_stories(
                limit=_limit(limit, maximum=200),
                cursor=cursor or None,
                q=q or None,
                verification_status=verification_status or None,
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
        data = _story_interface(runtime, repos).get_story(story_id=story_id)
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


@router.get(
    "/news/sources",
    response_model=api_schemas.ApiEnvelope[api_schemas.NewsSourcesData],
)
def list_news_sources(request: Request) -> JSONResponse:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = _story_interface(runtime, repos).list_sources()
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.NewsSourcesData],
        {"ok": True, "data": data},
    )


def _story_interface(runtime: Any, repos: Any) -> StoryInterface:
    analysis_contract = None
    if (
        runtime.settings.news.enabled
        and runtime.settings.workers.news_analysis.enabled
        and llm_is_configured(runtime.settings)
    ):
        analysis_contract = NewsAnalysisContract(
            model=litellm_proxy_model_name(
                runtime.settings.workers.news_analysis.model,
                base_url=runtime.settings.llm.base_url,
            )
        )
    return StoryInterface(
        repos.news,
        analysis_contract=analysis_contract,
    )


def _validate_query_params(request: Request, *, supported: set[str]) -> None:
    for name in request.query_params:
        if name not in supported:
            raise ApiBadRequest("unsupported_query_param", field=name)


__all__ = ["router"]
