from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from tracefold.news.projection import (
    NewsProjectionInputExceeded,
    NewsStoryFactSnapshot,
    _require_bounded_snapshot,
    build_story_projection,
)
from tracefold.news.repository import NewsRepository
from tracefold.news.story_store import (
    NEWS_STORY_INPUT_BYTES_CAP,
    NEWS_STORY_INPUT_ROW_CAP,
    load_story_projection,
)


def _snapshot(*rows: dict[str, object]) -> NewsStoryFactSnapshot:
    return NewsStoryFactSnapshot(
        material_snapshot_fingerprint="a" * 64,
        evaluation_time_ms=2_000_000_000_000,
        published_material_snapshot_fingerprint=None,
        rows=rows,
    )


def _row(index: int, *, title: str = "Market update") -> dict[str, object]:
    return {
        "item_id": f"item-{index:05d}",
        "source_id": "news-opennews",
        "canonical_url": None,
        "reporting_origin": "wire",
        "title": title,
        "description": "",
        "published_at_ms": 1_000 + index,
        "tier": 4,
        "source_kind": "opennews",
        "source_position": None,
        "memberships": (),
        "provider_identity": (),
    }


def test_bounded_story_snapshot_accepts_exactly_the_hard_row_cap() -> None:
    _require_bounded_snapshot(_snapshot(*(_row(index) for index in range(NEWS_STORY_INPUT_ROW_CAP))))


def test_bounded_story_snapshot_rejects_one_row_over_the_hard_cap() -> None:
    rows = tuple(_row(index) for index in range(NEWS_STORY_INPUT_ROW_CAP + 1))
    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_row_cap"):
        _require_bounded_snapshot(_snapshot(*rows))


def test_bounded_story_snapshot_rejects_input_over_the_byte_cap() -> None:
    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_byte_cap"):
        _require_bounded_snapshot(_snapshot(_row(0, title="x" * NEWS_STORY_INPUT_BYTES_CAP)))


def test_pure_interface_cannot_bypass_the_story_input_cap() -> None:
    rows = tuple(_row(index) for index in range(NEWS_STORY_INPUT_ROW_CAP + 1))
    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_row_cap"):
        build_story_projection(_snapshot(*rows))


@pytest.mark.parametrize(
    ("bounds", "error_code"),
    (
        ({"item_count": NEWS_STORY_INPUT_ROW_CAP + 1, "minimum_input_bytes": 0}, "news_story_input_row_cap"),
        ({"item_count": 1, "minimum_input_bytes": NEWS_STORY_INPUT_BYTES_CAP + 1}, "news_story_input_byte_cap"),
    ),
)
def test_story_load_rejects_oversized_input_before_fetching_rows(
    bounds: dict[str, int],
    error_code: str,
) -> None:
    class _Connection:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, query: str, _params: object = None) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 1:
                assert "news_projection_summary" in query
                return SimpleNamespace(fetchone=lambda: None)
            assert self.calls == 2
            return SimpleNamespace(fetchone=lambda: bounds)

    conn = _Connection()
    repository = SimpleNamespace(conn=conn, stable_json_hash=lambda _value: "unreachable")
    with pytest.raises(NewsProjectionInputExceeded, match=error_code):
        load_story_projection(repository, now_ms=1_000)
    assert conn.calls == 2


def _old_cursor(payload: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps({"v": 1, **payload}).encode()).decode().rstrip("=")


def test_story_v2_rejects_pre_cut_feed_cursor_before_querying() -> None:
    class _Connection:
        @staticmethod
        def execute(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("old cursor must fail before a database read")

    repository = NewsRepository(_Connection())
    with pytest.raises(ValueError, match="news_feed_cursor_invalid"):
        repository.list_feed(cursor=_old_cursor({"sort": "importance"}))


def test_story_v2_rejects_pre_cut_member_cursor() -> None:
    class _Connection:
        @staticmethod
        def execute(*_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(fetchone=lambda: {"story_id": "story-v2"})

    repository = NewsRepository(_Connection())
    repository.story_provider_evidence = lambda **_kwargs: {}  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="news_story_members_cursor_invalid"):
        repository.get_story(
            story_id="story-v2",
            members_cursor=_old_cursor({"kind": "story_members", "story_id": "story-v2"}),
        )
