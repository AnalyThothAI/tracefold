from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from tracefold.app.bootstrap import WorkerRuntime, bootstrap_workers
from tracefold.platform.config.settings import Settings, load_settings
from tracefold.platform.observability import PROMETHEUS_CONTENT_TYPE


def create_workers_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or load_settings(require_ws_token=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = bootstrap_workers(resolved_settings)
        try:
            await runtime.supervisor.start()
            app.state.service = runtime
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(
        title="Tracefold Workers",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok\n"

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        runtime: WorkerRuntime = app.state.service
        return JSONResponse(
            {
                "ok": True,
                "runtime_role": runtime.role,
                "runtime_id": runtime.runtime_id,
            }
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        runtime: WorkerRuntime = app.state.service
        return Response(
            runtime.telemetry.render_prometheus_text(),
            media_type=PROMETHEUS_CONTENT_TYPE,
        )

    return app
