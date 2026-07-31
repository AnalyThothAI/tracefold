from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

import tracefold.market.radar.token_radar_projector as projector_module
from tracefold.market.radar.microbatch import (
    RadarMicroBatchClaim,
    RadarMicroBatchService,
    RadarShardOversized,
    RadarTargetClaim,
    _require_bounded_input,
    _require_bounded_output,
    rank_radar_microbatch,
)
from tracefold.market.radar.output_envelope import (
    OutputRowOversized,
    split_bounded_rows,
)
from tracefold.market.radar.token_radar_projector import (
    build_token_radar_current_closure,
    rank_token_radar_closure,
)


def test_rank_closure_selects_top_n_before_wide_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_compact_row(identity_id=f"asset-{index}", rank_score=100 - index) for index in range(6)]
    ranked = rank_token_radar_closure(
        {
            "target_type": "Asset",
            "target_id": "not-in-cohort",
            "window": "5m",
            "now_ms": 1_800_000_000_000,
            "features": [],
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
            "features": [],
            "selected_by_venue": ranked["selected_by_venue"],
            "hydrated_inputs": hydrated,
        }
    )

    assert [row["identity_id"] for row in closure["rows_by_venue"]["all"]] == [
        "asset-0",
        "asset-1",
    ]


def test_radar_output_cap_splits_one_stable_venue_lane_deterministically() -> None:
    rows = [
        {"lane": "resolved", "payload": "r" * 600_000},
        {"lane": "resolved", "payload": "a" * 600_000},
    ]
    batches = split_bounded_rows(
        rows,
        context={"window_venue_lane": ["all", "resolved"]},
        byte_cap=1024 * 1024,
    )
    assert [[row["payload"][0] for row in batch] for batch in batches] == [
        ["r"],
        ["a"],
    ]

    with pytest.raises(OutputRowOversized, match="output_envelope_single_row_oversized"):
        split_bounded_rows(
            [
                {
                    "lane": "resolved",
                    "payload": "x" * (1024 * 1024),
                }
            ],
            context={"window_venue_lane": ["all", "resolved"]},
            byte_cap=1024 * 1024,
        )


def test_radar_microbatch_retains_input_and_output_byte_envelopes() -> None:
    with pytest.raises(RadarShardOversized, match="radar_input_byte_overflow"):
        _require_bounded_input({"rows": [{"payload": "x" * (4 * 1024 * 1024)}]})

    with pytest.raises(RadarShardOversized, match="radar_output_byte_overflow"):
        _require_bounded_output(
            {
                "rows_by_venue": {
                    "all": [
                        {
                            "lane": "resolved",
                            "payload": "x" * (1024 * 1024),
                        }
                    ]
                }
            }
        )


def test_radar_microbatch_removes_expired_target_from_same_publication() -> None:
    expired = _compact_row(identity_id="expired", rank_score=100)
    retained = _compact_row(identity_id="retained", rank_score=90)

    ranked = rank_radar_microbatch(
        {
            "window": "5m",
            "now_ms": 1_800_000_000_000,
            "venues": ["all"],
            "compact_inputs": [expired, retained],
            "current_stock_features": [],
            "target_projections": [
                {
                    "kind": "token",
                    "target_type": "Asset",
                    "target_id": "expired",
                    "projection": {"feature": None},
                }
            ],
        }
    )

    assert ranked["source_rows_by_venue"] == {"all": 1}
    assert ranked["selected_identities"] == [
        ["resolved", "Asset", "retained"],
    ]


@pytest.mark.parametrize(
    (
        "worker_name",
        "expected_statement_timeout_seconds",
        "expected_transaction_timeout_seconds",
    ),
    [
        ("steady_projection_coordinator", 3.0, 3.0),
        ("radar_maintenance_rebuild", 120.0, 120.0),
    ],
)
def test_radar_publish_applies_its_role_timeout(
    monkeypatch: pytest.MonkeyPatch,
    worker_name: str,
    expected_statement_timeout_seconds: float,
    expected_transaction_timeout_seconds: float,
) -> None:
    class RecordingRepositories:
        @contextmanager
        def transaction(self) -> Iterator[None]:
            yield

    class RecordingDatabase:
        calls: list[dict[str, Any]]

        def __init__(self) -> None:
            self.calls = []

        @contextmanager
        def worker_session(
            self,
            _worker_name: str,
            **kwargs: Any,
        ) -> Iterator[RecordingRepositories]:
            self.calls.append(kwargs)
            yield RecordingRepositories()

    database = RecordingDatabase()
    service = RadarMicroBatchService(db=database, worker_name=worker_name)
    claim = RadarMicroBatchClaim(
        window="5m",
        venue="all",
        runtime_id="runtime",
        targets=(
            RadarTargetClaim(
                target_type="Asset",
                target_id="asset",
                input_fingerprint="fingerprint",
                projection_version="token-radar-v1",
                first_dirty_at_ms=1,
                deadline_at_ms=2,
            ),
        ),
    )
    monkeypatch.setattr(
        RadarMicroBatchService,
        "_lock_claims",
        staticmethod(lambda _repos, _claim: False),
    )

    result = service.publish(
        claim,
        projections={"targets": []},
        ranked={"source_rows_by_venue": {}},
        closure={"rows_by_venue": {}},
        now_ms=2,
    )

    assert result["projection_status"] == "stale_snapshot"
    assert database.calls == [
        {
            "statement_timeout_seconds": expected_statement_timeout_seconds,
            "transaction_timeout_seconds": expected_transaction_timeout_seconds,
        }
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
