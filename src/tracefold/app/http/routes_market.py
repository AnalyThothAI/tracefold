from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime, _now_ms
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_json
from tracefold.app.http.validators import _target_type
from tracefold.market import live_market_snapshot

router = APIRouter()


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
