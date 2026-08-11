from __future__ import annotations

from dataclasses import replace

import pytest

from tracefold.market.radar import reducer as reducer_module
from tracefold.market.radar.reducer import (
    TOKEN_RADAR_INPUT_BYTE_CAP,
    TOKEN_RADAR_INPUT_ROW_CAP,
    TOKEN_RADAR_OUTPUT_BYTE_CAP,
    RadarEvidenceRevision,
    RadarSelectionKey,
    TokenRadarInputOverflow,
    TokenRadarOutputOverflow,
    reduce_token_radar,
)

NOW_MS = 1_800_000_000_000
MINUTE_MS = 60_000


def test_reducer_emits_the_exact_compact_v3_evidence_packet() -> None:
    rows = [
        _row(event_id="prior-a", author="alice", minutes_ago=90, text="old thesis"),
        _row(
            event_id="prior-a-duplicate",
            author="alice",
            minutes_ago=80,
            text="old thesis again",
        ),
        _row(event_id="current-a", author="alice", minutes_ago=30, text="first thesis"),
        _row(
            event_id="current-a-repeat",
            author="alice",
            minutes_ago=25,
            text="first thesis",
        ),
        _row(event_id="current-b", author="bob", minutes_ago=20, text="independent thesis"),
        _row(event_id="current-c", author="carol", minutes_ago=10, text="third thesis"),
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert reduced.input_rows == len(rows)
    assert reduced.eligible_rows == 1
    assert reduced.ruleset_version == "token_radar_rules_v1"
    assert reduced.ruleset_fingerprint == ("sha256:dc9ba22158c5b9ff0dc0f6dfbf0a7d7eeb861c4ab02e91432778a36d6d6f221c")
    assert reduced.output_bytes <= TOKEN_RADAR_OUTPUT_BYTE_CAP
    assert reduced.selected_keys == (
        RadarSelectionKey(
            target_type="Asset",
            target_id="asset-1",
            trigger_event_id="current-c",
            trigger_intent_id="intent-current-c",
            trigger_resolution_id="resolution-current-c",
        ),
    )
    assert reduced.snapshot == {
        "schema_version": "token_radar_snapshot_v3",
        "social_evidence_as_of_ms": NOW_MS - 10 * MINUTE_MS,
        "eligible_total": 1,
        "items": [
            {
                "target": {"target_type": "Asset", "target_id": "asset-1"},
                "trigger_event_id": "current-c",
                "trigger_source_event_at_ms": NOW_MS - 10 * MINUTE_MS,
                "qualified_at_ms": NOW_MS - 10 * MINUTE_MS,
                "why_now": {
                    "current_mentions": 4,
                    "prior_mentions": 2,
                    "mention_delta": 2,
                },
                "evidence": {
                    "independent_author_count": 3,
                    "independent_text_count": 3,
                    "time_to_nth_author_ms": 20 * MINUTE_MS,
                    "duplicate_share": pytest.approx(1 / 4),
                },
                "market": {
                    "price_usd": None,
                    "price_observed_at_ms": None,
                    "price_change_since_signal": None,
                    "market_cap_usd": None,
                    "market_cap_observed_at_ms": None,
                },
            }
        ],
    }


def test_reducer_selects_the_exact_first_fifty_from_sixty_eligible_targets() -> None:
    rows = [
        _row(
            target_id=f"asset-{target_index:02d}",
            event_id=f"event-{target_index:02d}-{author_index}",
            author=f"author-{target_index:02d}-{author_index}",
            minutes_ago=minutes_ago,
            text=f"independent {target_index} {author_index}",
        )
        for target_index in range(60)
        for author_index, minutes_ago in enumerate((30, 20, 10))
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert reduced.snapshot["schema_version"] == "token_radar_snapshot_v3"
    assert reduced.eligible_rows == 60
    assert reduced.snapshot["eligible_total"] == 60
    assert len(reduced.snapshot["items"]) == 50
    assert [item["target"]["target_id"] for item in reduced.snapshot["items"]] == [
        f"asset-{index:02d}" for index in range(50)
    ]


def test_reducer_order_is_deterministic_and_fingerprint_tracks_fact_content() -> None:
    rows = [
        _row(event_id="event-a", author="alice", minutes_ago=50, text="one"),
        _row(event_id="event-b", author="bob", minutes_ago=40, text="two"),
        _row(event_id="event-c", author="carol", minutes_ago=30, text="three"),
    ]
    changed_rows = [*rows[:-1], replace(rows[-1], text="changed fact")]

    first = reduce_token_radar(rows, now_ms=NOW_MS)
    reordered = reduce_token_radar(list(reversed(rows)), now_ms=NOW_MS)
    changed = reduce_token_radar(changed_rows, now_ms=NOW_MS)

    assert first.snapshot == reordered.snapshot
    assert first.selected_keys == reordered.selected_keys
    assert first.input_fingerprint == reordered.input_fingerprint
    assert changed.input_fingerprint != first.input_fingerprint


def test_first_positive_revision_where_all_gate_rules_pass_is_the_trigger() -> None:
    rows = [
        _row(event_id="event-a", author="alice", minutes_ago=30, text="same"),
        _row(event_id="event-b", author="bob", minutes_ago=25, text="same"),
        _row(event_id="event-c", author="carol", minutes_ago=20, text="same"),
        _row(event_id="event-d", author="dave", minutes_ago=10, text="independent"),
    ]

    item = reduce_token_radar(rows, now_ms=NOW_MS).snapshot["items"][0]

    assert item["trigger_event_id"] == "event-d"
    assert item["trigger_source_event_at_ms"] == NOW_MS - 10 * MINUTE_MS
    assert item["qualified_at_ms"] == NOW_MS - 10 * MINUTE_MS
    assert item["evidence"]["duplicate_share"] == 0.5


def test_text_duplicate_normalization_has_one_reducer_owned_semantics() -> None:
    rows = [
        _row(event_id="event-a", author="alice", minutes_ago=30, text="Hello   World"),
        _row(event_id="event-b", author="bob", minutes_ago=20, text="  hello world  "),
        _row(event_id="event-c", author="carol", minutes_ago=10, text="independent"),
    ]

    item = reduce_token_radar(rows, now_ms=NOW_MS).snapshot["items"][0]

    assert item["evidence"]["duplicate_share"] == pytest.approx(1 / 3)


def test_late_trigger_scans_ten_thousand_rows_with_periodic_deadline_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = []
    for index in range(TOKEN_RADAR_INPUT_ROW_CAP):
        if index == TOKEN_RADAR_INPUT_ROW_CAP - 2:
            author = "bob"
        elif index == TOKEN_RADAR_INPUT_ROW_CAP - 1:
            author = "carol"
        else:
            author = "alice"
        rows.append(
            _row(
                event_id=f"event-{index:05d}",
                author=author,
                minutes_ago=20,
                received_delay_ms=index,
                text=("duplicate" if index < TOKEN_RADAR_INPUT_ROW_CAP // 2 else f"text-{index}"),
            )
        )

    monotonic_calls = 0

    def _monotonic() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        return 0.0

    monkeypatch.setattr(reducer_module.time, "monotonic", _monotonic)

    reduced = reduce_token_radar(rows, now_ms=NOW_MS, deadline_monotonic=1.0)

    assert reduced.snapshot["items"][0]["trigger_event_id"] == "event-09999"
    assert monotonic_calls >= 80


def test_target_leaves_immediately_when_later_evidence_breaks_a_gate_rule() -> None:
    rows = [
        _row(event_id="event-a", author="alice", minutes_ago=30, text="one"),
        _row(event_id="event-b", author="bob", minutes_ago=29, text="two"),
        _row(event_id="event-c", author="carol", minutes_ago=28, text="three"),
        *[
            _row(
                event_id=f"duplicate-{index}",
                author=f"duplicate-author-{index}",
                minutes_ago=20 - index,
                text="one",
            )
            for index in range(4)
        ],
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert reduced.snapshot["eligible_total"] == 0
    assert reduced.eligible_rows == 0


def test_social_freshness_advances_even_when_no_target_is_eligible() -> None:
    weak = _row(
        event_id="weak",
        author="alice",
        minutes_ago=5,
        text="one resolved mention",
    )

    snapshot = reduce_token_radar([weak], now_ms=NOW_MS).snapshot

    assert snapshot == {
        "schema_version": "token_radar_snapshot_v3",
        "social_evidence_as_of_ms": NOW_MS - 5 * MINUTE_MS,
        "eligible_total": 0,
        "items": [],
    }


def test_exact_one_hour_boundary_belongs_only_to_the_current_window() -> None:
    rows = [
        _row(event_id="prior", author="prior", minutes_ago=61, text="prior"),
        _row(event_id="boundary", author="alice", minutes_ago=60, text="one"),
        _row(event_id="event-b", author="bob", minutes_ago=45, text="two"),
        _row(event_id="event-c", author="carol", minutes_ago=35, text="three"),
    ]

    item = reduce_token_radar(rows, now_ms=NOW_MS).snapshot["items"][0]

    assert item["why_now"] == {
        "current_mentions": 3,
        "prior_mentions": 1,
        "mention_delta": 2,
    }


def test_exact_two_hour_boundary_is_excluded_from_the_rolling_input() -> None:
    rows = [
        _row(event_id="expired", author="prior", minutes_ago=120, text="expired"),
        _row(event_id="event-a", author="alice", minutes_ago=50, text="one"),
        _row(event_id="event-b", author="bob", minutes_ago=40, text="two"),
        _row(event_id="event-c", author="carol", minutes_ago=30, text="three"),
    ]

    item = reduce_token_radar(rows, now_ms=NOW_MS).snapshot["items"][0]

    assert item["why_now"] == {
        "current_mentions": 3,
        "prior_mentions": 0,
        "mention_delta": 3,
    }


def test_reducer_fails_closed_on_row_or_byte_overflow_without_truncation() -> None:
    one_row = _row(event_id="event-a", author="alice", minutes_ago=10, text="one")
    with pytest.raises(TokenRadarInputOverflow, match="token_radar_input_row_overflow"):
        reduce_token_radar([one_row] * (TOKEN_RADAR_INPUT_ROW_CAP + 1), now_ms=NOW_MS)

    oversized = replace(one_row, text="x" * (TOKEN_RADAR_INPUT_BYTE_CAP + 1))
    with pytest.raises(TokenRadarInputOverflow, match="token_radar_input_byte_overflow"):
        reduce_token_radar([oversized], now_ms=NOW_MS)


def test_reducer_fails_closed_when_public_snapshot_exceeds_ninety_six_kibibytes() -> None:
    long_target_id = "x" * 100_000
    rows = [
        _row(
            event_id=f"event-{index}",
            author=f"author-{index}",
            minutes_ago=30 - index * 10,
            text=f"independent-{index}",
            target_id=long_target_id,
        )
        for index in range(3)
    ]

    with pytest.raises(TokenRadarOutputOverflow, match="token_radar_output_byte_overflow"):
        reduce_token_radar(rows, now_ms=NOW_MS)


def _row(
    *,
    event_id: str,
    author: str,
    minutes_ago: int,
    text: str,
    target_id: str = "asset-1",
    received_delay_ms: int = 0,
) -> RadarEvidenceRevision:
    source_at_ms = NOW_MS - minutes_ago * MINUTE_MS
    received_at_ms = source_at_ms + received_delay_ms
    return RadarEvidenceRevision(
        event_id=event_id,
        intent_id=f"intent-{event_id}",
        resolution_id=f"resolution-{event_id}",
        source_event_at_ms=source_at_ms,
        received_at_ms=received_at_ms,
        event_created_at_ms=received_at_ms,
        action="tweet",
        author_key=author,
        text=text,
        resolution_status="EXACT",
        target_type="Asset",
        target_id=target_id,
        resolution_decision_at_ms=received_at_ms,
        resolution_created_at_ms=received_at_ms,
    )
