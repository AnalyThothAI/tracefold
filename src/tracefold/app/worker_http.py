from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from tracefold.platform.observability import PROMETHEUS_CONTENT_TYPE


def _create_workers_probe_app(
    *,
    readiness: Callable[[], dict[str, Any]],
    render_metrics: Callable[[], str],
) -> FastAPI:
    app = FastAPI(
        title="Tracefold Workers Probe",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok\n"

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        payload = readiness()
        return JSONResponse(payload, status_code=200 if payload["ok"] else 503)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(
            render_metrics(),
            media_type=PROMETHEUS_CONTENT_TYPE,
        )

    return app


__all__: list[str] = []
