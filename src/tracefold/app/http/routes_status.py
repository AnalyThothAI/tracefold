from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime, _runtime
from tracefold.app.http.responses import _validated_json

router = APIRouter()


@router.get("/bootstrap", response_model=api_schemas.ApiEnvelope[api_schemas.BootstrapData])
def bootstrap(request: Request) -> JSONResponse:
    runtime = _runtime(request)
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.BootstrapData],
        {
            "ok": True,
            "data": {"ws_token": runtime.settings.ws_token},
        },
    )


def create_router(status_payload: Callable[[Any], dict[str, Any]]) -> APIRouter:
    status_router = APIRouter()
    status_router.include_router(router)

    @status_router.get("/status", response_model=api_schemas.ApiEnvelope[api_schemas.StatusData])
    def status(request: Request) -> JSONResponse:
        runtime = _authenticated_runtime(request)
        payload = status_payload(runtime)
        return _validated_json(
            api_schemas.ApiEnvelope[api_schemas.StatusData],
            {"ok": True, "data": payload},
        )

    return status_router
