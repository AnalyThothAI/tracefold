"""Liveness and narrow readiness for the Nautilus process."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse


def create_nautilus_probe_app(readiness: Callable[[], dict[str, Any]]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok\n"

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        payload = readiness()
        return JSONResponse(payload, status_code=200 if payload["ok"] else 503)

    return app


__all__ = ["create_nautilus_probe_app"]
