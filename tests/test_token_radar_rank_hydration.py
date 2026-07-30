from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import tracefold.market.radar.token_radar_projector as projector_module
from tracefold.market import TOKEN_RADAR_DEFAULT_VENUE, TokenRadarProjector
from tracefold.market.radar.token_radar_rank_source_query import TokenRadarFeatureSourceRequest


class _RankRepository:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.token_radar = self
        self.rows = rows
        self.hydrated_identities: list[tuple[str, str, str]] = []

    def list_compact_rank_inputs_for_rank_set(self, **_: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]

    def hydrate_rank_inputs_for_rank_set(
        self,
        *,
        identities: list[tuple[str, str, str]],
        **_: Any,
    ) -> list[dict[str, Any]]:
        self.hydrated_identities = list(identities)
        selected = set(identities)
        return [
            {**row, "wide_payload_marker": row["identity_id"]}
            for row in self.rows
            if (row["lane"], row["target_type_key"], row["identity_id"]) in selected
        ]


class _TransactionTrackingRepository:
    def __init__(self) -> None:
        self.token_radar = self
        self.in_transaction = False
        self.transaction_count = 0
        self.deleted_lanes: list[str] = []

    @contextmanager
    def transaction(self):
        assert not self.in_transaction
        self.in_transaction = True
        self.transaction_count += 1
        try:
            yield
        finally:
            self.in_transaction = False

    def delete_target_feature(self, *, lane: str, **_: Any) -> int:
        assert self.in_transaction
        self.deleted_lanes.append(lane)
        return 0


def test_rank_set_hydrates_wide_payload_only_after_top_n_selection(monkeypatch) -> None:
    rows = [_compact_row(identity_id=f"asset-{index}", rank_score=100 - index) for index in range(6)]
    repository = _RankRepository(rows)
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
        lambda row, ranked: {**row, "rank": ranked["rank"]},
    )

    projection = TokenRadarProjector(repos=repository).build_rank_set(
        window="5m",
        venue=TOKEN_RADAR_DEFAULT_VENUE,
        now_ms=1_800_000_000_000,
        limit=2,
    )

    assert projection.source_rows == 6
    assert projection.hydrated_rows == 2
    assert len(projection.rows) == 2
    assert repository.hydrated_identities == [
        ("resolved", "Asset", "asset-0"),
        ("resolved", "Asset", "asset-1"),
    ]
    assert [row["identity_id"] for row in projection.rows] == ["asset-0", "asset-1"]


def test_target_feature_computation_finishes_before_write_transaction(monkeypatch) -> None:
    repository = _TransactionTrackingRepository()

    def project_group(*_: Any, **__: Any) -> None:
        assert not repository.in_transaction

    monkeypatch.setattr(projector_module, "_project_group", project_group)
    result = TokenRadarProjector(repos=repository).project_source_request(
        request=TokenRadarFeatureSourceRequest(
            request_key="target-0:test",
            target_type_key="Asset",
            identity_id="asset-0",
            window="5m",
            analysis_since_ms=1_799_999_000_000,
            score_since_ms=1_799_999_700_000,
            now_ms=1_800_000_000_000,
        ),
        target={"target_type_key": "Asset", "identity_id": "asset-0"},
        source_rows=[],
        now_ms=1_800_000_000_000,
    )

    assert result["status"] == "empty"
    assert repository.transaction_count == 1
    assert repository.deleted_lanes == ["resolved", "attention"]
    assert not repository.in_transaction


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
