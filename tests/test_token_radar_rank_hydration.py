from __future__ import annotations

from typing import Any

import tracefold.market.radar.token_radar_projector as projector_module
from tracefold.market.radar.token_radar_projector import (
    build_token_radar_current_closure,
    rank_token_radar_closure,
)


def test_rank_closure_selects_top_n_before_wide_hydration(
    monkeypatch,
) -> None:
    rows = [_compact_row(identity_id=f"asset-{index}", rank_score=100 - index) for index in range(6)]
    ranked = rank_token_radar_closure(
        {
            "target_type": "Asset",
            "target_id": "not-in-cohort",
            "window": "5m",
            "now_ms": 1_800_000_000_000,
            "feature": None,
            "compact_inputs": rows,
            "venues": ["all"],
            "rank_limit": 2,
        }
    )

    assert ranked["source_rows_by_venue"] == {"all": 6}
    assert ranked["selected_identities"] == [
        ["resolved", "Asset", "asset-0"],
        ["resolved", "Asset", "asset-1"],
    ]

    monkeypatch.setattr(
        projector_module,
        "_row_from_target_feature",
        lambda row, *, venue: {
            "identity_id": row["identity_id"],
            "lane": row["lane"],
            "venue": venue,
        },
    )
    monkeypatch.setattr(
        projector_module,
        "_patch_ranked_current_row",
        lambda row, ranked_row: {**row, "rank": ranked_row["rank"]},
    )
    selected = set(tuple(identity) for identity in ranked["selected_identities"])
    hydrated = [
        {**row, "wide_payload_marker": row["identity_id"]}
        for row in rows
        if (
            row["lane"],
            row["target_type_key"],
            row["identity_id"],
        )
        in selected
    ]
    closure = build_token_radar_current_closure(
        {
            "feature": None,
            "selected_by_venue": ranked["selected_by_venue"],
            "hydrated_inputs": hydrated,
        }
    )

    assert [row["identity_id"] for row in closure["rows_by_venue"]["all"]] == [
        "asset-0",
        "asset-1",
    ]


def _compact_row(*, identity_id: str, rank_score: int) -> dict[str, Any]:
    return {
        "projection_version": "token-radar-v1",
        "window": "5m",
        "lane": "resolved",
        "target_type_key": "Asset",
        "identity_id": identity_id,
        "target_type": "Asset",
        "target_id": identity_id,
        "pricefeed_id": None,
        "latest_event_received_at_ms": 1_800_000_000_000,
        "latest_market_observed_at_ms": 1_800_000_000_000,
        "social_heat_raw_score": float(rank_score),
        "social_heat_weight": 1.0,
        "social_propagation_raw_score": None,
        "social_propagation_weight": 0.0,
        "timing_risk_raw_score": None,
        "timing_risk_weight": 0.0,
        "cohort_high_confidence_mentions": 0,
        "cohort_kol_mentions": 0,
        "cohort_followup_authors": 0,
        "cohort_first_seen_global_24h": False,
        "cohort_symbol": identity_id.upper(),
        "social_heat_mentions_1h": rank_score,
        "social_propagation_mentions": 0,
        "social_heat_latest_seen_ms": 1_800_000_000_000 + rank_score,
        "raw_composite_score": rank_score,
        "recommended_decision": "high_alert",
        "gates_max_decision": "high_alert",
        "subject_chain": "eip155:1",
    }
