from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter

from tracefold.app.http import routes_news, routes_status


def create_api_router(status_payload: Callable[[Any], dict[str, Any]]) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])
    router.include_router(routes_status.create_router(status_payload))
    router.include_router(routes_news.router)
    return router
