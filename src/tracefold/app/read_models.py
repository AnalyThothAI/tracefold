from __future__ import annotations

import time
from typing import Any

from tracefold.market import (
    MarketTickPersistenceService,
)
from tracefold.news import NewsInterface
from tracefold.platform.config.settings import Settings


def rebuild_market_tick_current_batch(
    settings: Settings,
    *,
    after: tuple[str, str] | None,
    limit: int,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Rebuild one stable-key batch of market current rows from material facts."""
    with _repositories(settings) as repos, repos.transaction():
        result = MarketTickPersistenceService(repos).rebuild_current_batch(
            after=after,
            limit=limit,
            now_ms=_now_ms() if now_ms is None else int(now_ms),
        )
    return {
        "scanned_targets": result.scanned_targets,
        "changed_targets": len(result.changed_targets),
        "next_cursor": (
            {
                "target_type": result.next_cursor[0],
                "target_id": result.next_cursor[1],
            }
            if result.next_cursor is not None
            else None
        ),
        "batch_full": result.scanned_targets == int(limit),
    }


def rebuild_news_story_projection(
    settings: Settings,
    *,
    batch_size: int,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Replay ArticleRevision facts through the production Story seam."""

    projection_now_ms = _now_ms() if now_ms is None else int(now_ms)
    with _repositories(settings) as repos, repos.transaction():
        deleted = NewsInterface(repos.news).reset_story_projection()
    totals = {"processed": 0, "created": 0, "joined": 0, "revised": 0, "ambiguous": 0}
    batches = 0
    while True:
        with _repositories(settings) as repos, repos.transaction():
            counts = NewsInterface(repos.news).project_story_batch(
                now_ms=projection_now_ms,
                limit=batch_size,
            )
        batches += 1
        for key in totals:
            totals[key] += int(counts[key])
        if int(counts["processed"]) < batch_size:
            break
    with _repositories(settings) as repos, repos.transaction():
        refreshed = NewsInterface(repos.news).refresh_story_presentation(
            now_ms=projection_now_ms,
            limit=max(1, totals["created"]),
        )
    return {
        "deleted": deleted,
        "projection": totals,
        "batches": batches,
        "presentation_refreshed": refreshed,
        "identity_order": ["first_seen_at_ms", "revision_id"],
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _repositories(settings: Settings) -> Any:
    from tracefold.app.repositories import repositories

    return repositories(settings)


__all__ = ["rebuild_market_tick_current_batch", "rebuild_news_story_projection"]
