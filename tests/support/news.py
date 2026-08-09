from __future__ import annotations

from typing import Any, cast

from tracefold.news.projection import (
    NewsProjectionSnapshot,
    _build_public_clusters_for_population,
    _score_story_rows,
    _select_public_population,
    compute_news_story_projection,
)
from tracefold.news.ranking import select_top_stories


def rebuild_news_projection(repository: Any, *, now_ms: int) -> dict[str, Any]:
    """Run the maintained Story load/compute/publish seam in one test transaction."""

    payload = repository.load_story_projection(now_ms=now_ms)
    snapshot = NewsProjectionSnapshot(
        input_fingerprint=str(payload["input_fingerprint"]),
        scoring_epoch_ms=int(payload["scoring_epoch_ms"]),
        current_input_fingerprint=(
            str(payload["current_input_fingerprint"]) if payload.get("current_input_fingerprint") is not None else None
        ),
        rows=tuple(dict(row) for row in payload["rows"]),
    )
    projection = compute_news_story_projection(snapshot)
    return cast(
        dict[str, Any],
        repository.publish_story_projection(
            snapshot=snapshot,
            projection=projection,
            now_ms=now_ms,
        ),
    )


def compute_news_public_clusters(snapshot: NewsProjectionSnapshot) -> list[dict[str, Any]]:
    """Expose the selector-consumed public clusters to differential tests."""

    rows, capped_rss_rows = _select_public_population(
        snapshot.rows,
        now_ms=snapshot.scoring_epoch_ms,
    )
    scored = _score_story_rows(rows, now_ms=snapshot.scoring_epoch_ms)
    public_clusters = _build_public_clusters_for_population(
        rows=scored.rows,
        capped_rss_rows=capped_rss_rows,
    )
    # The pinned selector's entity-corroboration pass mutates every candidate
    # before scoring, including candidates that are not selected. Run that
    # same production entry point so differential tests observe the exact
    # selector input state that ``compute_news_story_projection`` produces.
    select_top_stories(public_clusters, now_ms=snapshot.scoring_epoch_ms)
    return public_clusters
