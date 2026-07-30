from __future__ import annotations

import pytest

from tracefold.news.projection import (
    NewsShardOversized,
    compute_news_component_projection,
    compute_news_edge_block,
    compute_news_identity_feature,
)


def test_news_identity_feature_is_pure_stable_and_expiry_bounded():
    payload = {
        "now_ms": 1_000_000,
        "item": {
            "item_id": "item-1",
            "title": "Iran threatens to close Strait of Hormuz",
            "published_at_ms": 900_000,
            "source_enabled": True,
        },
    }
    first = compute_news_identity_feature(payload)
    second = compute_news_identity_feature(payload)

    assert first == second
    assert first["active"] is True
    assert first["expires_at_ms"] == 900_000 + 96 * 60 * 60 * 1000
    assert first["candidate_tokens"]
    assert len(first["feature_fingerprint"]) == 64


def test_news_candidate_pair_compute_block_is_hard_capped_at_4096():
    pair = {
        "left_item_id": "a",
        "right_item_id": "b",
        "left_title": "A material market event",
        "right_title": "A material market event",
        "expires_at_ms": 2_000_000,
    }
    with pytest.raises(NewsShardOversized, match="pair_block_overflow"):
        compute_news_edge_block([pair] * 4_097)


def test_news_component_output_excludes_unrelated_candidate_components():
    rows = [_projection_row(index) for index in range(500)]
    target_id = str(rows[0]["item_id"])

    projection = compute_news_component_projection(
        {
            "now_ms": 1_000_000,
            "target_item_id": target_id,
            "target_feature": _target_feature(rows[0]),
            "rows": rows,
            "existing_edges": [],
            "final_edges": [],
            "previous_memberships": [
                {
                    "item_id": str(row["item_id"]),
                    "story_id": f"story-{row['item_id']}",
                }
                for row in rows
            ],
            "aliases": [],
            "entity_rows": [],
        }
    )

    assert projection["closure_item_ids"] == [target_id]
    assert [row["item_id"] for row in projection["item_updates"]] == [target_id]
    assert len(projection["stories"]) == 1
    assert len(projection["memberships"]) == 1


def test_news_component_output_keeps_both_sides_of_deleted_bridge():
    rows = [_projection_row(index) for index in range(3)]
    target_id = str(rows[0]["item_id"])
    existing_edges = [
        _edge(rows[0], rows[1]),
        _edge(rows[1], rows[2]),
    ]

    projection = compute_news_component_projection(
        {
            "now_ms": 1_000_000,
            "target_item_id": target_id,
            "target_feature": _target_feature(rows[0]),
            "rows": rows,
            "existing_edges": existing_edges,
            "final_edges": [existing_edges[0]],
            "previous_memberships": [
                {
                    "item_id": str(row["item_id"]),
                    "story_id": "story-old",
                }
                for row in rows
            ],
            "aliases": [],
            "entity_rows": [],
        }
    )

    assert projection["closure_item_ids"] == [
        str(row["item_id"])
        for row in rows
    ]
    assert len(projection["memberships"]) == 3
    assert projection["old_story_ids"] == ["story-old"]


def _projection_row(index: int) -> dict[str, object]:
    item_id = f"item-{index:04d}"
    return {
        "item_id": item_id,
        "title": f"Shared market bulletin number {index} distinct-{index}",
        "description": "",
        "canonical_url": f"https://example.com/{index}",
        "source_id": f"source-{index % 3}",
        "tier": 2,
        "source_enabled": True,
        "published_at_ms": 900_000 + index,
        "normalized_title": f"shared market bulletin number {index} distinct {index}",
        "candidate_tokens": ["shared"],
        "expires_at_ms": 2_000_000,
        "feature_active": True,
    }


def _target_feature(row: dict[str, object]) -> dict[str, object]:
    return {
        "item_id": str(row["item_id"]),
        "normalized_title": str(row["normalized_title"]),
        "candidate_tokens": list(row["candidate_tokens"]),
        "expires_at_ms": int(row["expires_at_ms"]),
        "active": True,
    }


def _edge(
    left: dict[str, object],
    right: dict[str, object],
) -> dict[str, object]:
    return {
        "left_item_id": str(left["item_id"]),
        "right_item_id": str(right["item_id"]),
        "similarity": 0.9,
        "expires_at_ms": 2_000_000,
    }
