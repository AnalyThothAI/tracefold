"""Persistence adapter for the pure News Story projection module."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from tracefold.news.story_projection import (
    NewsStoryFactSnapshot,
    NewsStoryProjection,
    build_story_projection,
)
from tracefold.news.story_store import NewsProjectionInputExceeded, _require_bounded_story_rows

NEWS_STORY_LOAD_TIMEOUT_SECONDS = 3.0
NEWS_STORY_COMPUTE_TIMEOUT_SECONDS = 25.0
NEWS_STORY_PUBLISH_TIMEOUT_SECONDS = 8.0
NEWS_STORY_FAILURE_TIMEOUT_SECONDS = 3.0


class NewsProjectionService:
    """Keep database sessions short around one pure complete calculation."""

    def __init__(self, *, db: Any, worker_name: str = "news_story_projection") -> None:
        self.db = db
        self.worker_name = worker_name

    def load(self, *, now_ms: int) -> NewsStoryFactSnapshot:
        with self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=NEWS_STORY_LOAD_TIMEOUT_SECONDS,
        ) as repos:
            payload = repos.news.load_story_projection(now_ms=now_ms)
        snapshot = NewsStoryFactSnapshot(
            material_snapshot_fingerprint=str(payload["material_snapshot_fingerprint"]),
            evaluation_time_ms=int(payload["evaluation_time_ms"]),
            published_material_snapshot_fingerprint=(
                str(payload["published_material_snapshot_fingerprint"])
                if payload.get("published_material_snapshot_fingerprint")
                else None
            ),
            rows=tuple(dict(row) for row in payload["rows"]),
        )
        _require_bounded_snapshot(snapshot)
        return snapshot

    def publish(
        self,
        snapshot: NewsStoryFactSnapshot,
        projection: NewsStoryProjection | Mapping[str, Any],
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        payload = projection.as_payload() if isinstance(projection, NewsStoryProjection) else dict(projection)
        with (
            self.db.worker_session(
                self.worker_name,
                statement_timeout_seconds=5.0,
                transaction_timeout_seconds=NEWS_STORY_PUBLISH_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            return cast(
                dict[str, Any],
                repos.news.publish_story_projection(
                    snapshot=snapshot,
                    projection=payload,
                    now_ms=now_ms,
                ),
            )

    def mark_failed(self, *, now_ms: int, error_code: str) -> None:
        with (
            self.db.worker_session(
                self.worker_name,
                statement_timeout_seconds=NEWS_STORY_FAILURE_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            repos.news.record_story_projection_failure(now_ms=now_ms, error_code=error_code)


def rebuild_all_news_for_maintenance(*, db: Any, now_ms: int) -> dict[str, Any]:
    service = NewsProjectionService(db=db, worker_name="news_maintenance_rebuild")
    snapshot = service.load(now_ms=now_ms)
    if snapshot.unchanged:
        return {
            "projection_status": "unchanged_input",
            "items": len(snapshot.rows),
            "stories": 0,
            "rows_written": 0,
        }
    return service.publish(snapshot, build_story_projection(snapshot), now_ms=now_ms)


def _require_bounded_snapshot(snapshot: NewsStoryFactSnapshot) -> None:
    _require_bounded_story_rows(snapshot.rows)


__all__ = [
    "NEWS_STORY_COMPUTE_TIMEOUT_SECONDS",
    "NEWS_STORY_FAILURE_TIMEOUT_SECONDS",
    "NEWS_STORY_LOAD_TIMEOUT_SECONDS",
    "NEWS_STORY_PUBLISH_TIMEOUT_SECONDS",
    "NewsProjectionInputExceeded",
    "NewsProjectionService",
    "NewsStoryFactSnapshot",
    "NewsStoryProjection",
    "build_story_projection",
    "rebuild_all_news_for_maintenance",
]
