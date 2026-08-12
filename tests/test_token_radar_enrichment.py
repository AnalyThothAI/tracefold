from __future__ import annotations

import math

import pytest

from tracefold.market.radar.constants import TOKEN_RADAR_INPUT_BYTE_CAP, TOKEN_RADAR_INPUT_ROW_CAP
from tracefold.market.radar.reducer import (
    RadarEvidenceRevision,
    TokenRadarInputOverflow,
    enrich_token_radar,
    reduce_token_radar,
    token_radar_text_fingerprint,
)
from tracefold.market.radar.snapshot_repository import TokenRadarCurrentRepository

NOW_MS = 1_800_000_000_000
MINUTE_MS = 60_000


def test_v4_enrichment_adds_identity_exact_signal_price_and_independent_market_clocks() -> None:
    reduced = reduce_token_radar(_eligible_revisions(), now_ms=NOW_MS)

    enriched = enrich_token_radar(
        reduced,
        [
            _presentation(
                signal_price_usd="10",
                price_usd="12",
                price_observed_at_ms=NOW_MS - MINUTE_MS,
                market_cap_usd="12000000",
                market_cap_observed_at_ms=NOW_MS - 2 * MINUTE_MS,
            )
        ],
        now_ms=NOW_MS,
    )

    assert enriched.snapshot == {
        "schema_version": "token_radar_snapshot_v4",
        "social_evidence_as_of_ms": NOW_MS - 10 * MINUTE_MS + 3_000,
        "eligible_total": 1,
        "items": [
            {
                "target": {
                    "target_type": "Asset",
                    "target_id": "asset-1",
                    "symbol": "PEPE",
                    "name": "Pepe",
                    "logo_url": f"/api/token-images/{'a' * 64}",
                    "chain": "solana",
                    "exchange": None,
                    "address": "mint-1",
                },
                "trigger_event_id": "event-2",
                "trigger_source_event_at_ms": NOW_MS - 10 * MINUTE_MS,
                "qualified_at_ms": NOW_MS - 10 * MINUTE_MS + 3_000,
                "why_now": {
                    "current_mentions": 3,
                    "prior_mentions": 0,
                    "mention_delta": 3,
                },
                "evidence": {
                    "independent_author_count": 3,
                    "independent_text_count": 3,
                    "time_to_nth_author_ms": 20 * MINUTE_MS,
                    "duplicate_share": 0.0,
                },
                "market": {
                    "price_usd": 12.0,
                    "price_observed_at_ms": NOW_MS - MINUTE_MS,
                    "price_change_since_signal": pytest.approx(0.2),
                    "market_cap_usd": 12_000_000.0,
                    "market_cap_observed_at_ms": NOW_MS - 2 * MINUTE_MS,
                },
            }
        ],
    }
    assert enriched.state_fingerprint != reduced.state_fingerprint


def test_v4_enrichment_drops_invalid_or_stale_presentation_fields_independently() -> None:
    reduced = reduce_token_radar(_eligible_revisions(), now_ms=NOW_MS)

    invalid = enrich_token_radar(
        reduced,
        [
            {
                **_presentation(),
                "logo_url": "https://remote.example/pepe.png",
                "price_usd": math.inf,
                "price_observed_at_ms": NOW_MS - MINUTE_MS,
                "market_cap_usd": "12000000",
                "market_cap_observed_at_ms": NOW_MS - MINUTE_MS,
            }
        ],
        now_ms=NOW_MS,
    ).snapshot["items"][0]
    stale = enrich_token_radar(
        reduced,
        [
            _presentation(
                signal_price_usd="10",
                price_usd="12",
                price_observed_at_ms=NOW_MS - 5 * MINUTE_MS - 1,
                market_cap_usd="12000000",
                market_cap_observed_at_ms=NOW_MS + 1,
            )
        ],
        now_ms=NOW_MS,
    ).snapshot["items"][0]

    assert invalid["target"]["logo_url"] is None
    assert invalid["market"] == {
        "price_usd": None,
        "price_observed_at_ms": None,
        "price_change_since_signal": None,
        "market_cap_usd": 12_000_000.0,
        "market_cap_observed_at_ms": NOW_MS - MINUTE_MS,
    }
    assert stale["market"] == {
        "price_usd": None,
        "price_observed_at_ms": None,
        "price_change_since_signal": None,
        "market_cap_usd": None,
        "market_cap_observed_at_ms": None,
    }


def test_v4_enrichment_keeps_an_exact_asset_address_without_inventing_a_symbol() -> None:
    address = "J7o48eA9qftqHpod2CsUbBH4q1Tzq3doTRXFDA4wpump"
    reduced = reduce_token_radar(_eligible_revisions(), now_ms=NOW_MS)

    enriched = enrich_token_radar(
        reduced,
        [_presentation(symbol=None, name=None, address=address)],
        now_ms=NOW_MS,
    )

    assert enriched.snapshot["items"][0]["target"] == {
        "target_type": "Asset",
        "target_id": "asset-1",
        "symbol": address,
        "name": None,
        "logo_url": f"/api/token-images/{'a' * 64}",
        "chain": "solana",
        "exchange": None,
        "address": address,
    }


def test_reducer_selects_exact_first_fifty_before_presentation_hydration() -> None:
    rows: list[RadarEvidenceRevision] = []
    for target_index in range(60):
        for author_index, minutes_ago in enumerate((30, 20, 10)):
            rows.append(
                _revision(
                    target_id=f"asset-{target_index:02d}",
                    event_id=f"event-{target_index:02d}-{author_index}",
                    author=f"author-{target_index:02d}-{author_index}",
                    minutes_ago=minutes_ago,
                )
            )

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert reduced.snapshot["schema_version"] == "token_radar_snapshot_v4"
    assert reduced.eligible_rows == 60
    assert reduced.snapshot["eligible_total"] == 60
    assert len(reduced.snapshot["items"]) == 50
    assert [item["target"]["target_id"] for item in reduced.snapshot["items"]] == [
        f"asset-{index:02d}" for index in range(50)
    ]
    assert len(reduced.selected_keys) == 50


def test_material_input_repository_returns_typed_evidence_revisions() -> None:
    source_event_at_ms = NOW_MS - MINUTE_MS
    connection = _Connection(
        [
            {
                "event_id": "event-1",
                "intent_id": "intent-1",
                "resolution_id": "resolution-1",
                "source_event_at_ms": source_event_at_ms,
                "received_at_ms": source_event_at_ms + 1_000,
                "event_created_at_ms": source_event_at_ms + 2_000,
                "action": "tweet",
                "author_handle": "@Alice",
                "text_fingerprint": token_radar_text_fingerprint(" evidence "),
                "resolution_status": "EXACT",
                "target_type": "Asset",
                "target_id": "asset-1",
                "resolution_decision_at_ms": source_event_at_ms + 2_000,
                "resolution_created_at_ms": source_event_at_ms + 3_000,
            }
        ]
    )

    rows = TokenRadarCurrentRepository(connection).load_material_inputs(now_ms=NOW_MS)

    assert rows == [
        RadarEvidenceRevision(
            event_id="event-1",
            intent_id="intent-1",
            resolution_id="resolution-1",
            source_event_at_ms=source_event_at_ms,
            received_at_ms=source_event_at_ms + 1_000,
            event_created_at_ms=source_event_at_ms + 2_000,
            action="tweet",
            author_key="alice",
            text_fingerprint=token_radar_text_fingerprint("evidence"),
            resolution_status="EXACT",
            target_type="Asset",
            target_id="asset-1",
            resolution_decision_at_ms=source_event_at_ms + 2_000,
            resolution_created_at_ms=source_event_at_ms + 3_000,
        )
    ]


def test_material_input_enforces_row_and_byte_caps_while_streaming() -> None:
    valid_row = _material_row()
    with pytest.raises(TokenRadarInputOverflow, match="row_overflow"):
        TokenRadarCurrentRepository(_Connection([valid_row] * (TOKEN_RADAR_INPUT_ROW_CAP + 1))).load_material_inputs(
            now_ms=NOW_MS
        )

    oversized = {**valid_row, "target_id": "x" * TOKEN_RADAR_INPUT_BYTE_CAP}
    with pytest.raises(TokenRadarInputOverflow, match="byte_overflow"):
        TokenRadarCurrentRepository(_Connection([oversized])).load_material_inputs(now_ms=NOW_MS)


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def fetchmany(self, size: int) -> list[dict[str, object]]:
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _sql: str, _params: object) -> None:
        return None


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def cursor(self, *, name: str) -> _Cursor:
        assert name == "token_radar_material_inputs"
        return _Cursor(list(self.rows))


def _eligible_revisions() -> list[RadarEvidenceRevision]:
    return [
        _revision(event_id=f"event-{index}", author=f"author-{index}", minutes_ago=minutes_ago)
        for index, minutes_ago in enumerate((30, 20, 10))
    ]


def _revision(
    *,
    event_id: str,
    author: str,
    minutes_ago: int,
    target_id: str = "asset-1",
) -> RadarEvidenceRevision:
    source_event_at_ms = NOW_MS - minutes_ago * MINUTE_MS
    return RadarEvidenceRevision(
        event_id=event_id,
        intent_id=f"intent-{event_id}",
        resolution_id=f"resolution-{event_id}",
        source_event_at_ms=source_event_at_ms,
        received_at_ms=source_event_at_ms + 1_000,
        event_created_at_ms=source_event_at_ms + 2_000,
        action="tweet",
        author_key=author,
        text_fingerprint=token_radar_text_fingerprint(f"independent {event_id}"),
        resolution_status="EXACT",
        target_type="Asset",
        target_id=target_id,
        resolution_decision_at_ms=source_event_at_ms + 2_000,
        resolution_created_at_ms=source_event_at_ms + 3_000,
    )


def _material_row(**overrides: object) -> dict[str, object]:
    source_event_at_ms = NOW_MS - MINUTE_MS
    return {
        "event_id": "event-1",
        "intent_id": "intent-1",
        "resolution_id": "resolution-1",
        "source_event_at_ms": source_event_at_ms,
        "received_at_ms": source_event_at_ms + 1_000,
        "event_created_at_ms": source_event_at_ms + 2_000,
        "action": "tweet",
        "author_handle": "alice",
        "text_fingerprint": token_radar_text_fingerprint("evidence"),
        "resolution_status": "EXACT",
        "target_type": "Asset",
        "target_id": "asset-1",
        "resolution_decision_at_ms": source_event_at_ms + 2_000,
        "resolution_created_at_ms": source_event_at_ms + 3_000,
        **overrides,
    }


def _presentation(**overrides: object) -> dict[str, object]:
    return {
        "target_type": "Asset",
        "target_id": "asset-1",
        "symbol": "PEPE",
        "name": "Pepe",
        "logo_url": f"/api/token-images/{'a' * 64}",
        "chain": "solana",
        "exchange": None,
        "address": "mint-1",
        "signal_price_usd": None,
        "price_usd": None,
        "price_observed_at_ms": None,
        "market_cap_usd": None,
        "market_cap_observed_at_ms": None,
        **overrides,
    }
