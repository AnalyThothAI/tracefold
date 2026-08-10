from __future__ import annotations

import pytest

from tracefold.market.radar import reducer as reducer_module
from tracefold.market.radar.reducer import (
    TOKEN_RADAR_INPUT_BYTE_CAP,
    TOKEN_RADAR_INPUT_ROW_CAP,
    TOKEN_RADAR_OUTPUT_BYTE_CAP,
    TokenRadarInputOverflow,
    TokenRadarOutputOverflow,
    reduce_token_radar,
)

NOW_MS = 1_800_000_000_000
MINUTE_MS = 60_000


def test_reducer_emits_exact_compact_packet_with_equal_author_evidence() -> None:
    rows = [
        _row(event_id="prior-a", author="alice", minutes_ago=90, text="old thesis"),
        _row(event_id="prior-a-duplicate", author="alice", minutes_ago=80, text="old thesis again"),
        _row(event_id="current-a", author="alice", minutes_ago=30, text="first thesis"),
        _row(event_id="current-a-repeat", author="alice", minutes_ago=25, text="first thesis"),
        _row(event_id="current-b", author="bob", minutes_ago=20, text="independent thesis"),
        _row(
            event_id="current-c",
            author="carol",
            minutes_ago=10,
            text="third thesis",
            signal_price_usd="10",
            latest_price_usd="12",
            latest_price_observed_at_ms=NOW_MS - MINUTE_MS,
        ),
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert reduced.input_rows == len(rows)
    assert reduced.eligible_rows == 1
    assert reduced.ruleset_version == "token_radar_rules_v1"
    assert reduced.ruleset_fingerprint.startswith("sha256:")
    assert reduced.output_bytes <= TOKEN_RADAR_OUTPUT_BYTE_CAP
    assert reduced.snapshot == {
        "schema_version": "token_radar_snapshot_v1",
        "evidence_as_of_ms": NOW_MS - MINUTE_MS,
        "eligible_total": 1,
        "items": [
            {
                "target": {
                    "target_type": "Asset",
                    "target_id": "asset-1",
                    "symbol": "PEPE",
                    "chain": "solana",
                    "exchange": None,
                    "address": "mint-1",
                },
                "trigger_event_id": "current-c",
                "triggered_at_ms": NOW_MS - 10 * MINUTE_MS,
                "why_now": {
                    "current_mentions": 4,
                    "prior_mentions": 2,
                    "mention_delta": 2,
                },
                "evidence": {
                    "new_independent_author_count": 2,
                    "independent_text_count": 3,
                    "time_to_nth_author_ms": 20 * MINUTE_MS,
                    "duplicate_share": pytest.approx(1 / 4),
                },
                "market": {
                    "status": "confirmed",
                    "price_change_since_signal": pytest.approx(0.2),
                },
                "counter_evidence": None,
            }
        ],
    }


def test_reducer_excludes_unresolved_or_weak_targets_and_never_returns_more_than_eight() -> None:
    rows: list[dict[str, object]] = []
    for index in range(10):
        target_id = f"asset-{index:02d}"
        for author_index, minutes_ago in enumerate((30, 20, 10)):
            rows.append(
                _row(
                    target_id=target_id,
                    symbol=f"T{index:02d}",
                    event_id=f"{target_id}-{author_index}",
                    author=f"author-{index}-{author_index}",
                    minutes_ago=minutes_ago,
                    text=f"independent {index} {author_index}",
                )
            )
    rows.extend(
        [
            _row(
                target_id="unresolved",
                symbol="NOPE",
                event_id=f"unresolved-{index}",
                author=f"unresolved-author-{index}",
                minutes_ago=30 - index * 10,
                text=f"unresolved text {index}",
                resolution_status="AMBIGUOUS",
            )
            for index in range(3)
        ]
    )

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert reduced.snapshot["eligible_total"] == 10
    assert reduced.eligible_rows == 10
    assert len(reduced.snapshot["items"]) == 8
    assert [item["target"]["target_id"] for item in reduced.snapshot["items"]] == [
        f"asset-{index:02d}" for index in range(8)
    ]


def test_reducer_is_order_independent_and_input_fingerprint_tracks_window_membership() -> None:
    rows = [
        _row(event_id="event-a", author="alice", minutes_ago=50, text="one"),
        _row(event_id="event-b", author="bob", minutes_ago=40, text="two"),
        _row(event_id="event-c", author="carol", minutes_ago=30, text="three"),
    ]

    first = reduce_token_radar(rows, now_ms=NOW_MS)
    reordered = reduce_token_radar(list(reversed(rows)), now_ms=NOW_MS)
    moved_to_prior = reduce_token_radar(rows, now_ms=NOW_MS + 31 * MINUTE_MS)

    assert first.snapshot == reordered.snapshot
    assert first.input_fingerprint == reordered.input_fingerprint
    assert moved_to_prior.input_fingerprint != first.input_fingerprint


def test_reducer_reports_market_unavailable_as_the_single_counter_evidence() -> None:
    rows = [
        _row(event_id="event-a", author="alice", minutes_ago=30, text="one"),
        _row(event_id="event-b", author="bob", minutes_ago=20, text="two"),
        _row(event_id="event-c", author="carol", minutes_ago=10, text="three"),
    ]

    item = reduce_token_radar(rows, now_ms=NOW_MS).snapshot["items"][0]

    assert item["market"] == {"status": "unavailable", "price_change_since_signal": None}
    assert item["counter_evidence"] == "market_confirmation_unavailable"


@pytest.mark.parametrize(
    ("latest_observed_at_ms", "expected_status"),
    [
        (NOW_MS - 5 * MINUTE_MS, "confirmed"),
        (NOW_MS - 5 * MINUTE_MS - 1, "unavailable"),
        (NOW_MS + 1, "unavailable"),
    ],
)
def test_market_confirmation_requires_a_nonfuture_tick_fresh_within_five_minutes(
    latest_observed_at_ms: int,
    expected_status: str,
) -> None:
    rows = [
        _row(event_id="event-a", author="alice", minutes_ago=30, text="one"),
        _row(event_id="event-b", author="bob", minutes_ago=20, text="two"),
        _row(
            event_id="event-c",
            author="carol",
            minutes_ago=10,
            text="three",
            signal_price_usd="10",
            latest_price_usd="11",
            latest_price_observed_at_ms=latest_observed_at_ms,
        ),
    ]

    snapshot = reduce_token_radar(rows, now_ms=NOW_MS).snapshot
    item = snapshot["items"][0]

    assert item["market"]["status"] == expected_status
    assert snapshot["evidence_as_of_ms"] <= NOW_MS
    assert item["counter_evidence"] == (None if expected_status == "confirmed" else "market_confirmation_unavailable")


def test_trigger_is_first_event_where_all_four_prefix_rules_are_true() -> None:
    rows = [
        _row(event_id="event-a", author="alice", minutes_ago=30, text="same"),
        _row(event_id="event-b", author="bob", minutes_ago=25, text="same"),
        _row(event_id="event-c", author="carol", minutes_ago=20, text="same"),
        _row(event_id="event-d", author="dave", minutes_ago=10, text="independent"),
    ]

    item = reduce_token_radar(rows, now_ms=NOW_MS).snapshot["items"][0]

    assert item["trigger_event_id"] == "event-d"
    assert item["triggered_at_ms"] == NOW_MS - 10 * MINUTE_MS
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
            {
                **_row(
                    event_id=f"event-{index:05d}",
                    author=author,
                    minutes_ago=0,
                    text="duplicate" if index < TOKEN_RADAR_INPUT_ROW_CAP // 2 else f"text-{index}",
                ),
                "received_at_ms": NOW_MS - 20 * MINUTE_MS + index,
            }
        )

    monotonic_calls = 0

    def _monotonic() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        return 0.0

    monkeypatch.setattr(reducer_module.time, "monotonic", _monotonic)

    reduced = reduce_token_radar(rows, now_ms=NOW_MS, deadline_monotonic=1.0)

    assert reduced.snapshot["items"][0]["trigger_event_id"] == "event-09999"
    assert monotonic_calls >= 150


def test_target_is_not_eligible_when_later_evidence_breaks_an_admission_rule() -> None:
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


def test_fresh_sampled_evidence_advances_header_even_when_nothing_is_eligible() -> None:
    weak = _row(
        event_id="weak",
        author="alice",
        minutes_ago=5,
        text="one resolved mention",
    )

    snapshot = reduce_token_radar([weak], now_ms=NOW_MS).snapshot

    assert snapshot == {
        "schema_version": "token_radar_snapshot_v1",
        "evidence_as_of_ms": NOW_MS - 5 * MINUTE_MS,
        "eligible_total": 0,
        "items": [],
    }


def test_exact_one_hour_boundary_belongs_only_to_current_window() -> None:
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

    oversized = {**one_row, "text": "x" * (TOKEN_RADAR_INPUT_BYTE_CAP + 1)}
    with pytest.raises(TokenRadarInputOverflow, match="token_radar_input_byte_overflow"):
        reduce_token_radar([oversized], now_ms=NOW_MS)


def test_reducer_fails_closed_when_public_snapshot_exceeds_twenty_kibibytes() -> None:
    long_value = "x" * 9_000
    rows = [
        {
            **_row(
                event_id=f"event-{index}",
                author=f"author-{index}",
                minutes_ago=30 - index * 10,
                text=f"independent-{index}",
                target_id=long_value,
                symbol=long_value,
            ),
            "address": long_value,
        }
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
    symbol: str = "PEPE",
    resolution_status: str = "EXACT",
    signal_price_usd: str | None = None,
    latest_price_usd: str | None = None,
    latest_price_observed_at_ms: int | None = None,
) -> dict[str, object]:
    return {
        "target_type": "Asset",
        "target_id": target_id,
        "symbol": symbol,
        "chain": "solana",
        "exchange": None,
        "address": "mint-1",
        "resolution_status": resolution_status,
        "event_id": event_id,
        "received_at_ms": NOW_MS - minutes_ago * MINUTE_MS,
        "author_handle": author,
        "text": text,
        "signal_price_usd": signal_price_usd,
        "latest_price_usd": latest_price_usd,
        "latest_price_observed_at_ms": latest_price_observed_at_ms,
    }
