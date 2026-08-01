from __future__ import annotations

import hashlib

from tracefold.news.identity import normalize_story_text
from tracefold.news.projection import NewsProjectionSnapshot, compute_news_story_projection


def _row(
    item_id: str,
    title: str,
    *,
    published_at_ms: int,
    reporting_origin: str,
    source_id: str = "opennews",
    tier: int = 4,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "source_id": source_id,
        "source_item_key": item_id,
        "canonical_url": None,
        "reporting_origin": reporting_origin,
        "title": title,
        "normalized_title": normalize_story_text(title),
        "description": "",
        "lang": "en",
        "published_at_ms": published_at_ms,
        "content_fingerprint": item_id,
        "brief_excluded": False,
        "source_name": source_id,
        "tier": tier,
    }


def _snapshot(*rows: dict[str, object]) -> NewsProjectionSnapshot:
    return NewsProjectionSnapshot(
        input_fingerprint="a" * 64,
        cutoff_ms=1,
        scoring_epoch_ms=2_000_000_000_000,
        current_input_fingerprint=None,
        rows=rows,
    )


def test_story_id_is_exact_sha256_of_earliest_normalized_title() -> None:
    earliest_title = "Fed holds rates steady"
    projection = compute_news_story_projection(
        _snapshot(
            _row("later", "Fed holds rates steady today", published_at_ms=20, reporting_origin="ap"),
            _row("early", earliest_title, published_at_ms=10, reporting_origin="reuters"),
        )
    )

    assert len(projection["stories"]) == 1
    assert (
        projection["stories"][0]["story_id"]
        == hashlib.sha256(normalize_story_text(earliest_title).encode()).hexdigest()
    )


def test_source_count_uses_reporting_origin_not_acquisition_source() -> None:
    projection = compute_news_story_projection(
        _snapshot(
            _row(
                "rss",
                "Major earthquake strikes coast",
                published_at_ms=10,
                reporting_origin="reuters",
                source_id="rss",
            ),
            _row("wire", "Major earthquake strikes coast", published_at_ms=11, reporting_origin="reuters"),
            _row("ap", "Major earthquake strikes coast", published_at_ms=12, reporting_origin="ap"),
        )
    )

    assert projection["stories"][0]["item_count"] == 3
    assert projection["stories"][0]["source_count"] == 2
    assert projection["stories"][0]["importance_factors"]["reporting_origin_count"] == 2


def test_exact_duplicate_hot_bucket_is_one_story_without_private_rules() -> None:
    rows = tuple(
        _row(
            f"item-{index:04d}",
            "Central bank announces emergency policy meeting",
            published_at_ms=1_000 + index,
            reporting_origin=f"origin-{index % 9}",
        )
        for index in range(1_000)
    )

    projection = compute_news_story_projection(_snapshot(*rows))

    assert len(projection["stories"]) == 1
    assert projection["stories"][0]["item_count"] == 1_000
    assert len(projection["memberships"]) == 1_000


def test_absent_historical_bridge_cannot_union_current_components() -> None:
    left = "Turkey hikes rates to 50% in surprise move"
    expired_bridge = "Turkey hikes interest rates to 50% in surprise move"
    right = "Turkey central bank hikes interest rates"

    current = compute_news_story_projection(
        _snapshot(
            _row("left", left, published_at_ms=10, reporting_origin="reuters"),
            _row("right", right, published_at_ms=11, reporting_origin="ap"),
        )
    )
    with_bridge = compute_news_story_projection(
        _snapshot(
            _row("left", left, published_at_ms=10, reporting_origin="reuters"),
            _row("bridge", expired_bridge, published_at_ms=11, reporting_origin="bloomberg"),
            _row("right", right, published_at_ms=12, reporting_origin="ap"),
        )
    )

    assert len(current["stories"]) == 2
    assert len(with_bridge["stories"]) == 1
