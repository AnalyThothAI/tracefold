from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.gzip import GZipMiddleware

from tracefold.app.http.exceptions import (
    ApiBadRequest,
    ApiConflict,
    ApiUnauthorized,
    ApiUnavailable,
    api_bad_request_response,
    api_conflict_response,
    api_unauthorized_response,
    api_unavailable_response,
)
from tracefold.app.http.http import create_api_router
from tracefold.app.http.responses import _validated_json
from tracefold.app.http.schemas import ReadinessData
from tracefold.app.serve_runtime import ServeRuntime, bootstrap_serve
from tracefold.platform.config.settings import Settings, load_settings
from tracefold.platform.observability import PROMETHEUS_CONTENT_TYPE

FRONTEND_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"
REVIEW_MUTATION_BODY_MAX_BYTES = 32_768


class FrontendStaticFiles(StaticFiles):
    """Serve rebuilt Vite assets without letting the local browser pin stale chunks."""

    def file_response(self, full_path: Any, stat_result: Any, scope: Any, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers.setdefault("Cache-Control", FRONTEND_CACHE_CONTROL)
        return response


def create_app(
    settings: Settings | None = None,
    *,
    frontend_dist: str | Path | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = bootstrap_serve(resolved_settings)
        primary_error: BaseException | None = None
        try:
            app.state.service = runtime
            logger.info("Starting Tracefold serve runtime | storage=postgresql")
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
    app.add_middleware(GZipMiddleware, minimum_size=1_024, compresslevel=5)

    @app.middleware("http")
    async def review_mutation_envelope_guard(request: Request, call_next: Any) -> Response:
        """Bound the two ReviewDesk writes before FastAPI reads JSON.

        Review bodies are tiny operator judgments, so requiring a truthful
        Content-Length is simpler and safer than accepting an unbounded
        chunked stream and trying to interrupt Pydantic after allocation.
        """

        path = request.url.path
        is_review_write = request.method == "POST" and (
            path == "/api/news/review/external-misses"
            or (path.startswith("/api/news/review/tasks/") and path.endswith("/responses"))
        )
        if is_review_write:
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return api_bad_request_response(
                    request,
                    ApiBadRequest("news_review_content_type_invalid", field="Content-Type"),
                )
            raw_length = request.headers.get("content-length")
            if raw_length is None:
                return api_bad_request_response(request, ApiBadRequest("news_review_content_length_required"))
            try:
                body_size = int(raw_length)
            except ValueError:
                return api_bad_request_response(request, ApiBadRequest("news_review_content_length_invalid"))
            if body_size < 0 or body_size > REVIEW_MUTATION_BODY_MAX_BYTES:
                return api_bad_request_response(request, ApiBadRequest("news_review_body_too_large"))
        return await call_next(request)

    app.add_exception_handler(ApiUnauthorized, api_unauthorized_response)
    app.add_exception_handler(ApiBadRequest, api_bad_request_response)
    app.add_exception_handler(ApiConflict, api_conflict_response)
    app.add_exception_handler(ApiUnavailable, api_unavailable_response)
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


def _readiness_payload(runtime: ServeRuntime) -> tuple[dict[str, Any], int]:
    payload = runtime.readiness_payload()
    return payload, 200 if payload["ok"] else 503


def _status_payload(runtime: ServeRuntime) -> dict[str, Any]:
    return runtime.status_payload()
