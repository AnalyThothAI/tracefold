from __future__ import annotations

import time
from typing import Any

from fastapi import Request

from .exceptions import ApiBadRequest, ApiUnauthorized


def _runtime(request: Request) -> Any:
    return request.app.state.service


def _authenticated_runtime(request: Request, *, allow_query_token: bool = True) -> Any:
    runtime = _runtime(request)
    request_token = _request_token(request, allow_query_token=allow_query_token)
    if not runtime.settings.ws_token or request_token != runtime.settings.ws_token:
        raise ApiUnauthorized()
    return runtime


def _request_token(request: Request, *, allow_query_token: bool = True) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    if not allow_query_token:
        return None
    token = request.query_params.get("token")
    return token.strip() if token else None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_query_params(request: Request, *, supported: set[str]) -> None:
    for name in request.query_params:
        if name not in supported:
            raise ApiBadRequest("unsupported_query_param", field=name)
