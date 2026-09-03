from __future__ import annotations

import hmac
import time
from typing import Any

from fastapi import Request

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
    """Authenticate a mutation from the bearer header only; query tokens stay reads only.

    The one write route takes the same session token the reads take (#520 PR-B). A second 0600 file
    split nothing an attacker could reach separately - both credentials live on the same LAN host and
    are pasted into the same console - and its only reliable effect was a console that could read but
    not flatten. What still holds: a URL-visible `?token=` never writes, and the body must be JSON.
    """

    runtime = _runtime(request)
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    supplied = value.strip() if scheme.lower() == "bearer" else ""
    expected = runtime.settings.ws_token or ""
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
