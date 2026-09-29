"""Workers process signal handling and HTTP health probes."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from tracefold.platform.observability import PROMETHEUS_CONTENT_TYPE


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    callback: Callable[[], None],
) -> tuple[signal.Signals, ...]:
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, callback)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signum)
    return tuple(installed)


def remove_signal_handlers(loop: asyncio.AbstractEventLoop, installed: Sequence[signal.Signals]) -> None:
    for signum in installed:
        loop.remove_signal_handler(signum)


def create_probe_app(
    *,
    title: str,
    readiness: Callable[[], dict[str, Any]],
    render_metrics: Callable[[], str] | None = None,
    readiness_status_gate: bool = True,
) -> FastAPI:
    """Expose process liveness and readiness; optionally expose Prometheus metrics."""

    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok\n"

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        payload = readiness()
        unavailable = readiness_status_gate and not payload["ok"]
        return JSONResponse(payload, status_code=503 if unavailable else 200)

    if render_metrics is not None:

        @app.get("/metrics")
        def metrics() -> Response:
            return Response(render_metrics(), media_type=PROMETHEUS_CONTENT_TYPE)

    return app


__all__ = ["create_probe_app", "install_signal_handlers", "remove_signal_handlers"]
