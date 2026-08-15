from __future__ import annotations

import hashlib
import json

import pytest

from tracefold.news.models import STORY_IDENTITY_VERSION, STORY_SELECTOR_VERSION
from tracefold.news.projection import NewsStoryFactSnapshot, build_story_projection

NOW_MS = 1_785_600_000_000
TITLE = "Magnitude 6.8 earthquake strikes northern Chile"


def _row(
    item_id: str,
    *,
    reporting_origin: str,
    canonical_url: str,
    published_at_ms: int,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "source_id": "news-opennews",
        "canonical_url": canonical_url,
        "reporting_origin": reporting_origin,
        "title": TITLE,
        "description": "",
        "published_at_ms": published_at_ms,
        "tier": 4,
        "source_kind": "opennews",
        "source_position": None,
        "memberships": (),
    }


def test_story_turn_emits_one_complete_canonical_public_selection_snapshot() -> None:
    snapshot = NewsStoryFactSnapshot(
        material_snapshot_fingerprint="f" * 64,
        evaluation_time_ms=NOW_MS,
        published_material_snapshot_fingerprint=None,
        rows=(
            _row(
                "ap",
                reporting_origin="AP News",
                canonical_url="https://example.test/ap",
                published_at_ms=NOW_MS - 5 * 60_000,
            ),
            _row(
                "reuters",
                reporting_origin="Reuters",
                canonical_url="https://example.test/reuters",
                published_at_ms=NOW_MS - 2 * 60_000,
            ),
        ),
    )

    projection = build_story_projection(snapshot)
    selection = projection.selection_snapshot

    assert set(selection) == {
        "projection_revision",
        "selector_evaluated_at_ms",
        "top_stories",
        "selection_stats",
        "selector_version",
        "identity_version",
        "selection_fingerprint",
    }
    assert selection["projection_revision"] == snapshot.material_snapshot_fingerprint
    assert selection["selector_evaluated_at_ms"] == NOW_MS
    assert selection["selector_version"] == STORY_SELECTOR_VERSION
    assert selection["identity_version"] == STORY_IDENTITY_VERSION
    assert selection["selection_stats"] == {
        "considered": 1,
        "admissibility_dropped": 0,
        "source_cap_dropped": 0,
        "overflow_dropped": 0,
        "brief_eligible_considered": 1,
        "brief_eligible_promoted": False,
    }

    assert len(selection["top_stories"]) == 1
    top_story = selection["top_stories"][0]
    assert set(top_story) == {
        "story_id",
        "primary_title",
        "primary_source",
        "primary_link",
        "primary_published_at_ms",
        "source_count",
        "unique_source_count",
        "sources",
        "last_updated_ms",
        "member_titles",
        "source_tier",
        "upstream_importance_score",
        "entity_corroboration",
        "corroboration_source_count",
        "importance_score",
        "effective_importance_score",
        "is_alert",
        "threat_level",
        "category",
    }
    assert top_story["story_id"] == projection.stories[0]["story_id"]
    assert top_story["primary_title"] == TITLE
    assert top_story["primary_source"] == "Reuters"
    assert top_story["primary_link"] == "https://example.test/reuters"
    assert top_story["primary_published_at_ms"] == NOW_MS - 2 * 60_000
    assert top_story["source_count"] == 2
    assert top_story["unique_source_count"] == 2
    assert top_story["sources"] == ["AP News", "Reuters"]
    assert top_story["last_updated_ms"] == NOW_MS - 2 * 60_000
    assert top_story["member_titles"] == [TITLE, TITLE]
    assert top_story["source_tier"] == 1
    assert top_story["upstream_importance_score"] == projection.stories[0]["importance_score"]
    assert top_story["entity_corroboration"] is False
    assert top_story["corroboration_source_count"] == 0
    assert isinstance(top_story["importance_score"], float)
    assert isinstance(top_story["effective_importance_score"], float)
    assert top_story["category"] == "natural_disaster"
    assert top_story["threat_level"] == "elevated"

    fingerprint_payload = {key: value for key, value in selection.items() if key != "selection_fingerprint"}
    encoded = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert selection["selection_fingerprint"] == hashlib.sha256(encoded).hexdigest()
    assert build_story_projection(snapshot).selection_snapshot == selection


def test_public_rank_recency_uses_newest_member_not_older_high_tier_primary() -> None:
    snapshot = NewsStoryFactSnapshot(
        material_snapshot_fingerprint="e" * 64,
        evaluation_time_ms=NOW_MS,
        published_material_snapshot_fingerprint=None,
        rows=(
            _row(
                "reuters-old",
                reporting_origin="Reuters",
                canonical_url="https://example.test/reuters-old",
                published_at_ms=NOW_MS - 60 * 60_000,
            ),
            _row(
                "local-new",
                reporting_origin="Local Wire",
                canonical_url="https://example.test/local-new",
                published_at_ms=NOW_MS - 60_000,
            ),
        ),
    )

    top_story = build_story_projection(snapshot).selection_snapshot["top_stories"][0]

    assert top_story["primary_source"] == "Reuters"
    assert top_story["primary_published_at_ms"] == NOW_MS - 60 * 60_000
    assert top_story["last_updated_ms"] == NOW_MS - 60_000
    assert top_story["effective_importance_score"] == pytest.approx(top_story["importance_score"] * (1 - (1 / 60) / 16))


def test_empty_story_turn_still_emits_the_single_empty_selector_authority() -> None:
    snapshot = NewsStoryFactSnapshot(
        material_snapshot_fingerprint="0" * 64,
        evaluation_time_ms=NOW_MS,
        published_material_snapshot_fingerprint=None,
        rows=(),
    )

    selection = build_story_projection(snapshot).selection_snapshot

    assert selection["top_stories"] == []
    assert selection["selection_stats"] == {
        "considered": 0,
        "admissibility_dropped": 0,
        "source_cap_dropped": 0,
        "overflow_dropped": 0,
        "brief_eligible_considered": 0,
        "brief_eligible_promoted": False,
    }
