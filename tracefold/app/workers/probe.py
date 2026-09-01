from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from tracefold.platform.observability import PROMETHEUS_CONTENT_TYPE


def _create_workers_probe_app(
    *,
    readiness: Callable[[], dict[str, Any]],
    render_metrics: Callable[[], str],
    telegram_control: Callable[[Request], Awaitable[Response]] | None = None,
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
    def metrics() -> Response:
        return Response(
            render_metrics(),
            media_type=PROMETHEUS_CONTENT_TYPE,
        )

    if telegram_control is not None:

        @app.post("/telegram/control", include_in_schema=False)
        async def control(request: Request) -> Response:
            return await telegram_control(request)

    return app


__all__: list[str] = []
