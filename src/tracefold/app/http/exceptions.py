from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from tracefold.app.http.responses import _json


class ApiUnauthorized(Exception):
    pass


class ApiBadRequest(Exception):
    def __init__(self, error: str, *, field: str | None = None):
        super().__init__(error)
        self.error = error
        self.field = field


class ApiUnavailable(Exception):
    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


class ApiConflict(Exception):
    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


def api_unauthorized_response(_: Request, __: ApiUnauthorized) -> JSONResponse:
    return _json({"ok": False, "error": "unauthorized"}, status_code=401)


def api_bad_request_response(_: Request, exc: ApiBadRequest) -> JSONResponse:
    payload = {"ok": False, "error": exc.error}
    if exc.field:
        payload["field"] = exc.field
    return _json(payload, status_code=400)


def api_unavailable_response(_: Request, exc: ApiUnavailable) -> JSONResponse:
    return _json({"ok": False, "error": exc.error}, status_code=503)


def api_conflict_response(_: Request, exc: ApiConflict) -> JSONResponse:
    return _json({"ok": False, "error": exc.error}, status_code=409)
