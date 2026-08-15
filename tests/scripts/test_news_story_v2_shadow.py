from __future__ import annotations

from scripts.news_story_v2_shadow import build_shadow_report
from tracefold.news.story_projection import NewsStoryFactSnapshot, build_story_projection


def test_shadow_report_compares_current_memberships_without_writing() -> None:
    rows = tuple(
        {
            "item_id": f"item-{index}",
            "source_id": "news-opennews",
            "canonical_url": None,
            "reporting_origin": f"wire-{index}",
            "title": "Central bank holds rates steady",
            "description": "",
            "published_at_ms": 2_000_000_000_000 + index,
            "title_fingerprint": str(index).rjust(64, "0"),
            "tier": 4,
            "source_kind": "opennews",
            "source_position": None,
            "memberships": (),
            "provider_identity": (),
        }
        for index in range(2)
    )
    snapshot = NewsStoryFactSnapshot(
        material_snapshot_fingerprint="a" * 64,
        evaluation_time_ms=2_000_000_000_100,
        published_material_snapshot_fingerprint=None,
        rows=rows,
    )
    projection = build_story_projection(snapshot)

    report = build_shadow_report(
        snapshot=snapshot,
        projection=projection,
        current_memberships=(
            {"story_id": "old-a", "item_id": "item-0"},
            {"story_id": "old-b", "item_id": "item-1"},
        ),
        compute_seconds=0.1,
        database_revision="test",
        rss_enabled=False,
    )

    assert report["mode"] == "read_only_zero_write"
    assert report["current_to_v2"] == {
        "shared_item_count": 2,
        "current_only_item_count": 0,
        "v2_only_item_count": 0,
        "merge_count": 1,
        "split_count": 0,
    }
    assert report["v2_story_distribution"]["story_count"] == 1
    assert report["v2_story_distribution"]["size_max"] == 2
    assert report["bounds"]["all_passed"] is True
