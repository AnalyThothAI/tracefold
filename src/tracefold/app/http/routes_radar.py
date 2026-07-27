from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime, _now_ms
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_json
from tracefold.app.http.validators import (
    _limit,
    _target_type,
    _token_radar_venue,
    _window,
)
from tracefold.market import AssetFlowService, StocksRadarService, TokenProfileReadModel, live_market_snapshot

router = APIRouter()


@router.get("/token-radar", response_model=api_schemas.ApiEnvelope[api_schemas.TokenRadarData])
def token_radar(
    request: Request,
    window: Annotated[str, Query()] = "1h",
    limit: Annotated[int, Query()] = 20,
    venue: Annotated[str, Query()] = "all",
) -> JSONResponse:
    _reject_removed_scope(request)
    runtime = _authenticated_runtime(request)
    parsed_window = _window(window)
    parsed_venue = _token_radar_venue(venue)
    data = _token_radar_data(
        runtime,
        window=parsed_window,
        limit=_limit(limit),
        venue=parsed_venue,
        now_ms=_now_ms(),
    )
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.TokenRadarData],
        {"ok": True, "data": {"window": parsed_window, "venue": parsed_venue, **data}},
    )


@router.get("/stocks-radar", response_model=api_schemas.ApiEnvelope[api_schemas.StocksRadarData])
def stocks_radar(
    request: Request,
    window: Annotated[str, Query()] = "1h",
    limit: Annotated[int, Query()] = 20,
) -> JSONResponse:
    _reject_removed_scope(request)
    runtime = _authenticated_runtime(request)
    parsed_window = _window(window)
    with runtime.repositories() as repos:
        data = StocksRadarService(
            conn=repos.conn,
        ).stocks_radar(
            window=parsed_window,
            limit=_limit(limit),
            now_ms=_now_ms(),
        )
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.StocksRadarData],
        {"ok": True, "data": data},
    )


@router.get("/live-market", response_model=api_schemas.ApiEnvelope[api_schemas.LiveMarketData])
def live_market(
    request: Request,
    target_type: Annotated[str, Query()] = "",
    target_id: Annotated[str, Query()] = "",
) -> JSONResponse:
    runtime = _authenticated_runtime(request)
    parsed_target_type = _target_type(target_type)
    if not parsed_target_type or not target_id:
        raise ApiBadRequest("target_required", field="target_id")
    with runtime.repositories() as repos:
        row = repos.token_targets.latest_market_tick(
            target_type=parsed_target_type,
            target_id=target_id,
        )
    snapshot = live_market_snapshot(
        row,
        target_type=parsed_target_type,
        target_id=target_id,
        now_ms=_now_ms(),
    )
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.LiveMarketData],
        {"ok": True, "data": snapshot},
    )


def _token_radar_data(
    runtime: Any,
    *,
    window: str,
    limit: int,
    venue: str,
    now_ms: int,
) -> dict[str, Any]:
    with runtime.repositories() as repos:
        profiles = TokenProfileReadModel(token_profiles=repos.token_profiles)
        data = AssetFlowService(
            token_radar=repos.token_radar,
            profiles=profiles,
        ).asset_flow(
            window=window,
            limit=limit,
            venue=venue,
            now_ms=now_ms,
        )
        return data


def _reject_removed_scope(request: Request) -> None:
    if "scope" in request.query_params:
        raise ApiBadRequest("unsupported_query_param", field="scope")
