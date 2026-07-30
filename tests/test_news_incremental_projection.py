from __future__ import annotations

import pytest

from tracefold.news.projection import (
    NewsShardOversized,
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
