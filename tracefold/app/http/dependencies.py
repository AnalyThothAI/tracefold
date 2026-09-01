from __future__ import annotations

import hmac
import time
from typing import Any

from fastapi import Request

from tracefold.platform.config.secret_file import SecretFileError, read_secure_distinct_token

from .exceptions import ApiBadRequest, ApiUnauthorized


def _runtime(request: Request) -> Any:
    return request.app.state.service


def _authenticated_runtime(request: Request) -> Any:
    runtime = _runtime(request)
    request_token = _request_token(request)
    if not runtime.settings.ws_token or request_token != runtime.settings.ws_token:
        raise ApiUnauthorized()
    return runtime


def _authenticated_write_runtime(request: Request) -> Any:
    """Authenticate a mutation from the bearer header only; query tokens are reads only."""

    runtime = _runtime(request)
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    supplied = value.strip() if scheme.lower() == "bearer" else ""
    token_path = runtime.settings.trading_console_write_token_file()
    try:
        expected = (
            read_secure_distinct_token(token_path, forbidden_value=runtime.settings.ws_token)
            if token_path is not None
            else ""
        )
    except SecretFileError:
        expected = ""
    if not supplied or not supplied.isascii() or not expected or not hmac.compare_digest(supplied, expected):
        raise ApiUnauthorized()
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        raise ApiBadRequest("content_type_json_required")
    return runtime


def _request_token(request: Request) -> str | None:
    """Bearer first, then the `?token=` the served console uses on a hard reload.

    The header-only variant went with the ReviewDesk writes it guarded (#256). Every route on this surface
    is a read, and a dead switch nothing exercises is worse than no switch: it reads as protection that is
    still in force. A future write route must state its own token policy rather than inherit this one.
    """

    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    token = request.query_params.get("token")
    return token.strip() if token else None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_query_params(request: Request, *, supported: set[str]) -> None:
    for name in request.query_params:
        if name not in supported:
            raise ApiBadRequest("unsupported_query_param", field=name)
