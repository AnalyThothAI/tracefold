from __future__ import annotations

import math

import pytest

from tracefold.market.radar.reducer import (
    enrich_token_radar,
    reduce_token_radar,
)
from tracefold.market.radar.snapshot_repository import TokenRadarCurrentRepository

NOW_MS = 1_800_000_000_000
MINUTE_MS = 60_000


def test_v2_enrichment_adds_only_fresh_finite_presentation_facts() -> None:
    reduced = reduce_token_radar(_eligible_rows(), now_ms=NOW_MS)

    enriched = enrich_token_radar(
        reduced,
        [
            {
                "target_type": "Asset",
                "target_id": "asset-1",
                "name": "Pepe",
                "logo_url": f"/api/token-images/{'a' * 64}",
                "price_usd": "12",
                "price_observed_at_ms": NOW_MS - MINUTE_MS,
                "market_cap_usd": "12000000",
                "market_cap_observed_at_ms": NOW_MS - 2 * MINUTE_MS,
            }
        ],
        now_ms=NOW_MS,
    )

    assert enriched.snapshot == {
        "schema_version": "token_radar_snapshot_v2",
        "evidence_as_of_ms": NOW_MS - MINUTE_MS,
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
                "triggered_at_ms": NOW_MS - 10 * MINUTE_MS,
                "why_now": {
                    "current_mentions": 3,
                    "prior_mentions": 0,
                    "mention_delta": 3,
                },
                "evidence": {
                    "new_independent_author_count": 3,
                    "independent_text_count": 3,
                    "time_to_nth_author_ms": 20 * MINUTE_MS,
                    "duplicate_share": 0.0,
                },
                "market": {
                    "status": "confirmed",
                    "price_change_since_signal": 0.2,
                    "price_usd": 12.0,
                    "market_cap_usd": 12_000_000.0,
                    "observed_at_ms": NOW_MS - 2 * MINUTE_MS,
                },
                "counter_evidence": None,
            }
        ],
    }
    assert enriched.state_fingerprint != reduced.state_fingerprint


def test_v2_enrichment_rejects_remote_logo_and_degrades_bad_or_stale_metrics_independently() -> None:
    reduced = reduce_token_radar(_eligible_rows(), now_ms=NOW_MS)

    invalid = enrich_token_radar(
        reduced,
        [
            {
                "target_type": "Asset",
                "target_id": "asset-1",
                "name": "Pepe",
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
            {
                "target_type": "Asset",
                "target_id": "asset-1",
                "name": "Pepe",
                "logo_url": f"/api/token-images/{'b' * 64}",
                "price_usd": "12",
                "price_observed_at_ms": NOW_MS - 5 * MINUTE_MS - 1,
                "market_cap_usd": "12000000",
                "market_cap_observed_at_ms": NOW_MS - 5 * MINUTE_MS - 1,
            }
        ],
        now_ms=NOW_MS,
    ).snapshot["items"][0]

    assert invalid["target"]["logo_url"] is None
    assert invalid["market"] == {
        "status": "unavailable",
        "price_change_since_signal": None,
        "price_usd": None,
        "market_cap_usd": 12_000_000.0,
        "observed_at_ms": NOW_MS - MINUTE_MS,
    }
    assert stale["market"] == {
        "status": "unavailable",
        "price_change_since_signal": None,
        "price_usd": None,
        "market_cap_usd": None,
        "observed_at_ms": None,
    }


@pytest.mark.parametrize(
    (
        "price_usd",
        "price_observed_at_ms",
        "market_cap_usd",
        "market_cap_observed_at_ms",
        "expected_status",
        "expected_price",
        "expected_cap",
        "expected_observed_at_ms",
    ),
    [
        (
            "12",
            NOW_MS - MINUTE_MS,
            "12000000",
            NOW_MS - 2 * MINUTE_MS,
            "confirmed",
            12.0,
            12_000_000.0,
            NOW_MS - 2 * MINUTE_MS,
        ),
        (
            "12",
            NOW_MS - MINUTE_MS,
            "12000000",
            NOW_MS - 5 * MINUTE_MS - 1,
            "confirmed",
            12.0,
            None,
            NOW_MS - MINUTE_MS,
        ),
        (
            "12",
            NOW_MS - 5 * MINUTE_MS - 1,
            "12000000",
            NOW_MS - 2 * MINUTE_MS,
            "unavailable",
            None,
            12_000_000.0,
            NOW_MS - 2 * MINUTE_MS,
        ),
        (
            "12",
            NOW_MS + 1,
            "12000000",
            NOW_MS - 2 * MINUTE_MS,
            "unavailable",
            None,
            12_000_000.0,
            NOW_MS - 2 * MINUTE_MS,
        ),
        (
            "12",
            NOW_MS - MINUTE_MS,
            "12000000",
            NOW_MS + 1,
            "confirmed",
            12.0,
            None,
            NOW_MS - MINUTE_MS,
        ),
        (
            "-12",
            NOW_MS - MINUTE_MS,
            "12000000",
            NOW_MS - 2 * MINUTE_MS,
            "unavailable",
            None,
            12_000_000.0,
            NOW_MS - 2 * MINUTE_MS,
        ),
        (
            "12",
            NOW_MS - MINUTE_MS,
            "-12000000",
            NOW_MS - 2 * MINUTE_MS,
            "confirmed",
            12.0,
            None,
            NOW_MS - MINUTE_MS,
        ),
    ],
)
def test_v2_enrichment_validates_price_and_market_cap_freshness_independently(
    price_usd: str,
    price_observed_at_ms: int,
    market_cap_usd: str,
    market_cap_observed_at_ms: int,
    expected_status: str,
    expected_price: float | None,
    expected_cap: float | None,
    expected_observed_at_ms: int,
) -> None:
    item = enrich_token_radar(
        reduce_token_radar(_eligible_rows(), now_ms=NOW_MS),
        [
            {
                "target_type": "Asset",
                "target_id": "asset-1",
                "name": None,
                "logo_url": None,
                "price_usd": price_usd,
                "price_observed_at_ms": price_observed_at_ms,
                "market_cap_usd": market_cap_usd,
                "market_cap_observed_at_ms": market_cap_observed_at_ms,
            }
        ],
        now_ms=NOW_MS,
    ).snapshot["items"][0]

    assert item["market"]["status"] == expected_status
    assert item["market"]["price_usd"] == expected_price
    assert item["market"]["market_cap_usd"] == expected_cap
    assert item["market"]["observed_at_ms"] == expected_observed_at_ms


def test_reducer_selects_exact_first_fifty_from_sixty_eligible_targets() -> None:
    rows: list[dict[str, object]] = []
    for target_index in range(60):
        for author_index, minutes_ago in enumerate((30, 20, 10)):
            rows.append(
                _row(
                    target_id=f"asset-{target_index:02d}",
                    symbol=f"T{target_index:02d}",
                    event_id=f"event-{target_index:02d}-{author_index}",
                    author=f"author-{target_index:02d}-{author_index}",
                    minutes_ago=minutes_ago,
                    signal_price_usd="10" if author_index == 2 else None,
                )
            )

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert reduced.snapshot["schema_version"] == "token_radar_snapshot_v2"
    assert reduced.eligible_rows == 60
    assert reduced.snapshot["eligible_total"] == 60
    assert len(reduced.snapshot["items"]) == 50
    assert [item["target"]["target_id"] for item in reduced.snapshot["items"]] == [
        f"asset-{index:02d}" for index in range(50)
    ]


def test_repository_batch_loads_selected_presentation_facts_in_one_statement() -> None:
    connection = _Connection(
        [
            {
                "target_type": "Asset",
                "target_id": "asset-1",
                "name": "Pepe",
                "logo_url": f"/api/token-images/{'a' * 64}",
                "price_usd": 1,
                "price_observed_at_ms": NOW_MS,
                "market_cap_usd": 1_000_000,
                "market_cap_observed_at_ms": NOW_MS,
            }
        ]
    )

    rows = TokenRadarCurrentRepository(connection).load_presentation_facts(
        [("Asset", "asset-1"), ("Asset", "asset-1"), ("CexToken", "cex-1")],
        now_ms=NOW_MS,
    )

    assert len(connection.calls) == 1
    assert "unnest" in connection.calls[0][0]
    assert connection.calls[0][1] == (
        ["Asset", "CexToken"],
        ["asset-1", "cex-1"],
        NOW_MS - 5 * MINUTE_MS,
        NOW_MS,
    )
    assert rows[0]["name"] == "Pepe"


def test_material_input_cex_identity_uses_the_same_supported_feed_as_current_market() -> None:
    connection = _Connection([])

    TokenRadarCurrentRepository(connection).load_material_inputs(now_ms=NOW_MS)

    sql = connection.calls[0][0]
    assert "price_feed.provider = 'binance'" in sql
    assert "price_feed.feed_type = 'cex_swap'" in sql
    assert "price_feed.quote_symbol = 'USDT'" in sql


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object) -> _Cursor:
        self.calls.append((sql, params))
        return _Cursor(self.rows)


def _eligible_rows() -> list[dict[str, object]]:
    return [
        _row(
            event_id=f"event-{index}",
            author=f"author-{index}",
            minutes_ago=minutes_ago,
            signal_price_usd="10" if index == 2 else None,
        )
        for index, minutes_ago in enumerate((30, 20, 10))
    ]


def _row(
    *,
    event_id: str,
    author: str,
    minutes_ago: int,
    target_id: str = "asset-1",
    symbol: str = "PEPE",
    signal_price_usd: str | None = None,
) -> dict[str, object]:
    return {
        "target_type": "Asset",
        "target_id": target_id,
        "symbol": symbol,
        "chain": "solana",
        "exchange": None,
        "address": "mint-1",
        "resolution_status": "EXACT",
        "event_id": event_id,
        "received_at_ms": NOW_MS - minutes_ago * MINUTE_MS,
        "author_handle": author,
        "text": f"independent {event_id}",
        "signal_price_usd": signal_price_usd,
    }
