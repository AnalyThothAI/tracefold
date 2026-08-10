from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime, _now_ms
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_json
from tracefold.app.http.validators import (
    _limit,
    _target_type,
    _window,
)
from tracefold.market import StocksRadarService, live_market_snapshot, served_token_radar_snapshot

router = APIRouter()
_TokenRadarEnvelope = api_schemas.ApiEnvelope[api_schemas.TokenRadarData]
_TOKEN_RADAR_OPENAPI_HEADERS: dict[str, dict[str, object]] = {
    "Cache-Control": {
        "description": "Requires revalidation before reuse.",
        "schema": {"type": "string"},
    },
    "ETag": {
        "description": "Strong validator for the complete served snapshot.",
        "schema": {"type": "string"},
    },
}


@router.get(
    "/token-radar",
    response_model=_TokenRadarEnvelope,
    responses={
        200: {"headers": _TOKEN_RADAR_OPENAPI_HEADERS},
        304: {"description": "Not Modified", "headers": _TOKEN_RADAR_OPENAPI_HEADERS},
    },
    openapi_extra={
        "parameters": [
            {
                "in": "header",
                "name": "If-None-Match",
                "required": False,
                "schema": {
                    "title": "If-None-Match",
                    "type": "string",
                },
            }
        ]
    },
)
def token_radar(request: Request) -> Response:
    _validate_token_radar_query(request)
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = served_token_radar_snapshot(repos.token_radar_current.current())
    return _etagged_token_radar(data, request)


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


def _reject_removed_scope(request: Request) -> None:
    if "scope" in request.query_params:
        raise ApiBadRequest("unsupported_query_param", field="scope")


def _validate_token_radar_query(request: Request) -> None:
    for name in request.query_params:
        if name != "token":
            raise ApiBadRequest("unsupported_query_param", field=name)


def _etagged_token_radar(data: dict[str, object], request: Request) -> Response:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    etag = f'"{hashlib.sha256(encoded).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response = _validated_json(_TokenRadarEnvelope, {"ok": True, "data": data})
    response.headers.update(headers)
    return response
