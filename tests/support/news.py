from __future__ import annotations

from typing import Any, cast

from tracefold.news.projection import (
    NewsStoryFactSnapshot,
    build_story_projection,
)


def rebuild_news_projection(
    repository: Any,
    *,
    now_ms: int,
) -> dict[str, Any]:
    """Run the maintained Story load/compute/publish seam in one test transaction."""

    payload = repository.load_story_projection(now_ms=now_ms)
    snapshot = NewsStoryFactSnapshot(
        material_snapshot_fingerprint=str(payload["material_snapshot_fingerprint"]),
        evaluation_time_ms=int(payload["evaluation_time_ms"]),
        published_material_snapshot_fingerprint=(
            str(payload["published_material_snapshot_fingerprint"])
            if payload.get("published_material_snapshot_fingerprint") is not None
            else None
        ),
        rows=tuple(dict(row) for row in payload["rows"]),
    )
    projection = build_story_projection(snapshot)
    return cast(
        dict[str, Any],
        repository.publish_story_projection(
            snapshot=snapshot,
            projection=projection.as_payload(),
            now_ms=now_ms,
        ),
    )
