from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter

from .routes import events, feed, review, status, symbols, system


def create_api_router(status_payload: Callable[[Any], dict[str, Any]]) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])
    router.include_router(system.create_router(status_payload))
    router.include_router(feed.router)
    router.include_router(events.router)
    router.include_router(symbols.router)
    router.include_router(review.router)
    router.include_router(status.router)
    return router
