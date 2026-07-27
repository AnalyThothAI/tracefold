from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from tracefold.app.bootstrap import Runtime, bootstrap
from tracefold.app.http.exceptions import (
    ApiBadRequest,
    ApiUnauthorized,
    api_bad_request_response,
    api_unauthorized_response,
)
from tracefold.app.http.http import create_api_router
from tracefold.app.http.responses import _validated_json
from tracefold.app.http.schemas import ReadinessData
from tracefold.app.http.ws import PublicWebSocketHub
from tracefold.news import NewsInterface, attach_pipeline_runtime_health
from tracefold.platform.config.settings import Settings, load_settings
from tracefold.platform.observability import PROMETHEUS_CONTENT_TYPE
from tracefold.platform.postgres.postgres_client import postgres_liveness_check

FRONTEND_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"


class FrontendStaticFiles(StaticFiles):
    """Serve rebuilt Vite assets without letting the local browser pin stale chunks."""

    def file_response(self, full_path: Any, stat_result: Any, scope: Any, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers.setdefault("Cache-Control", FRONTEND_CACHE_CONTROL)
        return response


def create_app(
    settings: Settings | None = None,
    *,
    start_collector: bool = True,
    frontend_dist: str | Path | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = bootstrap(
            resolved_settings,
            start_collector=start_collector,
            publisher_factory=lambda db: PublicWebSocketHub(
                token=resolved_settings.ws_token,
                repository_session=db.api_session,
                default_replay_limit=resolved_settings.api.replay_limit,
            ),
        )
        primary_error: BaseException | None = None
        try:
            await runtime.scheduler.start()
            app.state.service = runtime
            logger.info(
                "Starting Tracefold | "
                f"handles={','.join(resolved_settings.handles) or 'all'} "
                f"channels={','.join(resolved_settings.upstream.channels)} "
                "storage=postgresql"
            )
            yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                await runtime.aclose()
            except Exception as cleanup_exc:
                if primary_error is None:
                    raise
                primary_error.add_note(f"runtime cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}")

    app = FastAPI(title="Tracefold", lifespan=lifespan)
    app.add_exception_handler(ApiUnauthorized, api_unauthorized_response)
    app.add_exception_handler(ApiBadRequest, api_bad_request_response)
    app.include_router(create_api_router(_status_payload))

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok\n"

    @app.get("/readyz", response_model=ReadinessData)
    def readyz() -> JSONResponse:
        runtime = app.state.service
        payload, status_code = _readiness_payload(runtime)
        return _validated_json(ReadinessData, payload, status_code=status_code)

    @app.get("/metrics")
    def metrics() -> Response:
        runtime = app.state.service
        return Response(
            runtime.telemetry.render_prometheus_text(),
            media_type=PROMETHEUS_CONTENT_TYPE,
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await cast(PublicWebSocketHub, app.state.service.hub).handle(websocket)

    _mount_frontend(app, frontend_dist=frontend_dist)

    return app


def _mount_frontend(app: FastAPI, *, frontend_dist: str | Path | None) -> None:
    dist = _frontend_dist_dir(frontend_dist)
    if dist is None:
        return

    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", FrontendStaticFiles(directory=assets), name="frontend-assets")

    if (dist / "favicon.svg").exists():

        async def frontend_favicon() -> FileResponse:
            return FileResponse(
                dist / "favicon.svg",
                headers={"Cache-Control": FRONTEND_CACHE_CONTROL},
            )

        app.add_api_route("/favicon.svg", frontend_favicon, include_in_schema=False)

    async def frontend_index() -> FileResponse:
        return FileResponse(
            dist / "index.html",
            headers={"Cache-Control": FRONTEND_CACHE_CONTROL},
        )

    app.add_api_route("/", frontend_index, include_in_schema=False)
    app.add_api_route("/app", frontend_index, include_in_schema=False)
    app.add_api_route("/app/{path:path}", frontend_index, include_in_schema=False)
    app.add_api_route("/news", frontend_index, include_in_schema=False)
    app.add_api_route("/news/{path:path}", frontend_index, include_in_schema=False)
    app.add_api_route("/macro", frontend_index, include_in_schema=False)
    for macro_path in (
        "overview",
        "research",
        "rates-fed",
        "economy-inflation",
        "liquidity-funding",
        "credit",
        "volatility",
        "cross-asset",
    ):
        app.add_api_route(f"/macro/{macro_path}", frontend_index, include_in_schema=False)
    app.add_api_route("/search", frontend_index, include_in_schema=False)
    app.add_api_route("/search/{path:path}", frontend_index, include_in_schema=False)
    app.add_api_route("/stocks", frontend_index, include_in_schema=False)
    app.add_api_route("/stocks/{path:path}", frontend_index, include_in_schema=False)
    app.add_api_route("/token", frontend_index, include_in_schema=False)
    app.add_api_route("/token/{path:path}", frontend_index, include_in_schema=False)
    app.add_api_route("/watchlist", frontend_index, include_in_schema=False)
    app.add_api_route("/watchlist/{path:path}", frontend_index, include_in_schema=False)


def _frontend_dist_dir(frontend_dist: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if frontend_dist is not None:
        candidates.append(Path(frontend_dist))
    module_path = Path(__file__).resolve()
    candidates.extend(parent / "web" / "dist" for parent in module_path.parents)
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate
    return None


def _readiness_payload(runtime: Runtime) -> tuple[dict[str, Any], int]:
    db_status = _db_status(runtime)
    composition = dict(runtime.snapshot.composition)
    reasons: list[str] = []
    if not db_status.get("ok"):
        reasons.append("database_unhealthy")
    if not composition.get("ok"):
        reasons.append("core_composition_incomplete")
    payload = {
        "ok": not reasons,
        "reasons": reasons,
        "handles": list(runtime.settings.handles),
        "store": "postgresql",
        "db": db_status,
        "composition": composition,
    }
    return payload, 503 if reasons else 200


def _status_payload(runtime: Runtime) -> dict[str, Any]:
    snapshot = runtime.current_snapshot()
    measured_at_ms = int(time.time() * 1000)
    reasons = list(snapshot.degradation_reasons)
    try:
        with runtime.repositories() as repos:
            news_health = NewsInterface(repos.news).health(now_ms=measured_at_ms)
        attach_pipeline_runtime_health(
            news_health,
            worker_status=snapshot.workers.get("news_pipeline"),
            now_ms=measured_at_ms,
        )
    except Exception as exc:
        measured_at_ms = int(time.time() * 1000)
        query_failure = {"status": "degraded", "error": type(exc).__name__}
        news_health = {
            "status": "degraded",
            "reasons": ["news_health_query_failed"],
            "layers": {layer: query_failure for layer in ("ingest", "story", "brief")},
            "measured_at_ms": measured_at_ms,
        }
    if str(news_health["status"]) != "ready":
        reasons.extend(f"news:{reason}" for reason in news_health["reasons"])
    payload = {
        "ok": not reasons,
        "reasons": reasons,
        "snapshot_gate": snapshot.collector.get("snapshot_gate_outcomes", {}),
        "handles": list(runtime.settings.handles),
        "store": "postgresql",
        "db": dict(snapshot.startup_db_status),
        "provider_states": snapshot.provider_states,
        "workers": snapshot.workers,
        "news": news_health,
    }
    return payload


def _db_status(runtime: Runtime) -> dict[str, object]:
    try:
        with runtime.db.api_pool.connection() as conn:
            liveness = postgres_liveness_check(conn)
        startup_schema = dict(runtime.snapshot.startup_db_status)
        schema_ok = bool(startup_schema.get("ok"))
        return {
            **liveness,
            "ok": bool(liveness.get("ok")) and schema_ok,
            "schema": startup_schema,
        }
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}
