from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from tracefold.news import projection as projection_module
from tracefold.news.identity import normalize_story_text
from tracefold.news.projection import (
    NewsProjectionInputExceeded,
    NewsProjectionSnapshot,
    compute_news_story_projection,
)
from tracefold.news.repository import NewsRepository
from tracefold.news.story_store import (
    NEWS_STORY_INPUT_BYTES_CAP,
    NEWS_STORY_INPUT_ROW_CAP,
    load_story_projection,
)


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
        "canonical_url": None,
        "reporting_origin": reporting_origin,
        "title": title,
        "description": "",
        "published_at_ms": published_at_ms,
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


def test_empty_normalized_titles_use_the_public_per_item_sentinel_identity() -> None:
    projection = compute_news_story_projection(
        _snapshot(
            _row("emoji", "👑👑👑", published_at_ms=10, reporting_origin="aeyakovenko"),
            _row("punctuation", "!!!", published_at_ms=11, reporting_origin="reuters"),
        )
    )

    story_ids = {story["canonical_title"]: story["story_id"] for story in projection["stories"]}
    assert story_ids == {
        "👑👑👑": hashlib.sha256("untrackable:aeyakovenko:👑👑👑".encode()).hexdigest(),
        "!!!": hashlib.sha256(b"untrackable:reuters:!!!").hexdigest(),
    }
    assert len(projection["memberships"]) == 2


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


def test_bounded_story_snapshot_accepts_current_full_window_shape() -> None:
    rows = tuple(
        _row(
            f"item-{index:04d}",
            f"Market update {index} " + ("x" * 380),
            published_at_ms=1_000 + index,
            reporting_origin=f"origin-{index % 9}",
        )
        for index in range(8_000)
    )

    projection_module._require_bounded_snapshot(_snapshot(*rows))


def test_bounded_story_snapshot_rejects_one_row_over_the_hard_cap() -> None:
    rows = tuple(
        _row(
            f"item-{index:05d}",
            f"Market update {index}",
            published_at_ms=1_000 + index,
            reporting_origin="wire",
        )
        for index in range(NEWS_STORY_INPUT_ROW_CAP + 1)
    )

    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_row_cap"):
        projection_module._require_bounded_snapshot(_snapshot(*rows))


def test_bounded_story_snapshot_accepts_exactly_the_hard_row_cap() -> None:
    rows = tuple(
        _row(
            f"item-{index:05d}",
            "Market update",
            published_at_ms=1_000 + index,
            reporting_origin="wire",
        )
        for index in range(NEWS_STORY_INPUT_ROW_CAP)
    )

    projection_module._require_bounded_snapshot(_snapshot(*rows))


def test_bounded_story_snapshot_rejects_input_over_the_byte_cap() -> None:
    oversized = _row(
        "oversized",
        "x" * NEWS_STORY_INPUT_BYTES_CAP,
        published_at_ms=1_000,
        reporting_origin="wire",
    )

    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_byte_cap"):
        projection_module._require_bounded_snapshot(_snapshot(oversized))


def test_repository_rebuild_cannot_bypass_the_story_input_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = NewsRepository(None)
    monkeypatch.setattr(
        repository,
        "load_story_projection",
        lambda *, now_ms: {
            "input_fingerprint": "over-cap",
            "cutoff_ms": now_ms,
            "scoring_epoch_ms": now_ms,
            "current_input_fingerprint": None,
            "rows": ({"item_id": str(index)} for index in range(NEWS_STORY_INPUT_ROW_CAP + 1)),
        },
    )

    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_row_cap"):
        repository.rebuild_stories(now_ms=1_000)


def test_story_load_rejects_the_cap_plus_one_sentinel_before_returning() -> None:
    class _Connection:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _query: str, _params: object = None) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 1:
                assert "news_projection_summary" in _query
                return SimpleNamespace(fetchone=lambda: None)
            assert self.calls == 2
            return SimpleNamespace(
                fetchone=lambda: {
                    "item_count": NEWS_STORY_INPUT_ROW_CAP + 1,
                    "minimum_input_bytes": 0,
                }
            )

    conn = _Connection()
    repository = SimpleNamespace(conn=conn, stable_json_hash=lambda _value: "unreachable")

    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_row_cap"):
        load_story_projection(repository, now_ms=1_000)
    assert conn.calls == 2


def test_story_load_rejects_wide_input_before_fetching_rows() -> None:
    class _Connection:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _query: str, _params: object = None) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 1:
                assert "news_projection_summary" in _query
                return SimpleNamespace(fetchone=lambda: None)
            assert self.calls == 2
            return SimpleNamespace(
                fetchone=lambda: {
                    "item_count": 1,
                    "minimum_input_bytes": NEWS_STORY_INPUT_BYTES_CAP + 1,
                }
            )

    conn = _Connection()
    repository = SimpleNamespace(conn=conn, stable_json_hash=lambda _value: "unreachable")

    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_byte_cap"):
        load_story_projection(repository, now_ms=1_000)
    assert conn.calls == 2
