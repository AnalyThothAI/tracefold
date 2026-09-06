"""The two process-host helpers the Workers and Nautilus roots both need.

Both roots are one supervised asyncio process behind one internal HTTP port: they take SIGINT and
SIGTERM away from uvicorn so their own shutdown path owns the deadline, and they publish liveness
and readiness on the same two routes. Those parts were byte-identical in the two roots and in the
two probe modules, so they live here once (#589 P-F16). Everything that differs -- what readiness
means, whether the process exports `/metrics`, which port it binds -- stays with its owner.
"""

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
) -> FastAPI:
    """Liveness, readiness and -- only where the process exports one -- a Prometheus route.

    `/healthz` never calls `readiness`: it answers whether the process is running at all. `/readyz`
    answers the owner's question and is 200 or 503 on its `ok`, and nothing else.
    """

    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok\n"

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        payload = readiness()
        return JSONResponse(payload, status_code=200 if payload["ok"] else 503)

    if render_metrics is not None:

        @app.get("/metrics")
        def metrics() -> Response:
            return Response(render_metrics(), media_type=PROMETHEUS_CONTENT_TYPE)

    return app


__all__ = ["create_probe_app", "install_signal_handlers", "remove_signal_handlers"]
