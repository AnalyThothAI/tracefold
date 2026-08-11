from __future__ import annotations

import random
import time
from dataclasses import replace

import pytest

from tracefold.market.radar.reducer import RadarEvidenceRevision, reduce_token_radar

NOW_MS = 1_800_000_000_000
MINUTE_MS = 60_000


@pytest.mark.parametrize(
    ("received_lag_ms", "expected_item_count"),
    [
        (120_000, 1),
        (120_001, 0),
        (-1, 0),
    ],
)
def test_event_live_lag_has_an_inclusive_two_minute_boundary(
    received_lag_ms: int,
    expected_item_count: int,
) -> None:
    rows = [
        _revision(
            event_id=f"event-{index}",
            author=f"author-{index}",
            source_minutes_ago=30 - index * 10,
            received_lag_ms=received_lag_ms,
        )
        for index in range(3)
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert len(reduced.snapshot["items"]) == expected_item_count


def test_event_creation_clock_does_not_become_a_hidden_lag_gate() -> None:
    rows = [
        replace(
            _revision(
                event_id=f"event-{index}",
                author=f"author-{index}",
                source_minutes_ago=30 - index * 10,
                received_lag_ms=60_000,
            ),
            event_created_at_ms=NOW_MS - (31 - index * 10) * MINUTE_MS,
        )
        for index in range(3)
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert len(reduced.snapshot["items"]) == 1


@pytest.mark.parametrize(
    ("resolution_lag_ms", "expected_item_count"),
    [
        (120_000, 1),
        (120_001, 0),
        (-1, 0),
    ],
)
def test_resolution_live_lag_has_an_inclusive_two_minute_boundary(
    resolution_lag_ms: int,
    expected_item_count: int,
) -> None:
    rows = [
        _revision(
            event_id=f"event-{index}",
            author=f"author-{index}",
            source_minutes_ago=30 - index * 10,
            resolution_lag_ms=resolution_lag_ms,
        )
        for index in range(3)
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert len(reduced.snapshot["items"]) == expected_item_count


def test_unresolved_event_becomes_positive_when_timely_resolution_arrives() -> None:
    unresolved = _revision(
        event_id="resolved-last",
        author="carol",
        source_minutes_ago=10,
        resolution_status="UNRESOLVED",
        target_type=None,
        target_id=None,
    )
    resolved_at_ms = unresolved.received_at_ms + MINUTE_MS
    resolved = _resolution_revision(
        unresolved,
        resolution_id="resolution-resolved-last",
        effective_at_ms=resolved_at_ms,
    )

    reduced = reduce_token_radar(
        [
            _revision(event_id="first", author="alice", source_minutes_ago=30),
            _revision(event_id="second", author="bob", source_minutes_ago=20),
            unresolved,
            resolved,
        ],
        now_ms=NOW_MS,
    )

    [item] = reduced.snapshot["items"]
    assert item["trigger_event_id"] == "resolved-last"
    assert item["qualified_at_ms"] == resolved_at_ms


def test_timely_retarget_moves_the_event_to_the_new_target() -> None:
    initial = _revision(
        event_id="switch",
        author="carol",
        source_minutes_ago=30,
        target_id="asset-a",
    )
    retarget = _resolution_revision(
        initial,
        resolution_id="resolution-switch-to-b",
        effective_at_ms=initial.received_at_ms + MINUTE_MS,
        target_id="asset-b",
    )
    rows = [
        _revision(
            event_id="b-one",
            author="alice",
            source_minutes_ago=50,
            target_id="asset-b",
        ),
        _revision(
            event_id="b-two",
            author="bob",
            source_minutes_ago=40,
            target_id="asset-b",
        ),
        initial,
        retarget,
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert [item["target"]["target_id"] for item in reduced.snapshot["items"]] == ["asset-b"]
    assert reduced.snapshot["items"][0]["trigger_event_id"] == "switch"


def test_target_to_other_to_target_uses_the_final_timely_binding() -> None:
    initial = _revision(
        event_id="switch-back",
        author="carol",
        source_minutes_ago=10,
        target_id="asset-a",
    )
    to_other = _resolution_revision(
        initial,
        resolution_id="resolution-switch-to-b",
        effective_at_ms=initial.received_at_ms + 30_000,
        target_id="asset-b",
    )
    back_at_ms = initial.received_at_ms + MINUTE_MS
    back_to_initial = _resolution_revision(
        initial,
        resolution_id="resolution-switch-back-to-a",
        effective_at_ms=back_at_ms,
        target_id="asset-a",
    )

    reduced = reduce_token_radar(
        [
            _revision(event_id="a-first", author="alice", source_minutes_ago=30),
            _revision(event_id="a-second", author="bob", source_minutes_ago=20),
            initial,
            to_other,
            back_to_initial,
        ],
        now_ms=NOW_MS,
    )

    [item] = reduced.snapshot["items"]
    assert item["target"]["target_id"] == "asset-a"
    assert item["trigger_event_id"] == "switch-back"
    assert item["qualified_at_ms"] == back_at_ms


def test_late_retarget_removes_the_old_binding_without_adding_the_new_one() -> None:
    initial = _revision(
        event_id="switch",
        author="carol",
        source_minutes_ago=30,
        target_id="asset-a",
    )
    late_retarget = _resolution_revision(
        initial,
        resolution_id="resolution-switch-late-to-b",
        effective_at_ms=initial.received_at_ms + 120_001,
        target_id="asset-b",
    )
    rows = [
        _revision(
            event_id="a-one",
            author="alice",
            source_minutes_ago=50,
            target_id="asset-a",
        ),
        _revision(
            event_id="a-two",
            author="bob",
            source_minutes_ago=40,
            target_id="asset-a",
        ),
        _revision(
            event_id="b-one",
            author="alice",
            source_minutes_ago=50,
            target_id="asset-b",
        ),
        _revision(
            event_id="b-two",
            author="bob",
            source_minutes_ago=40,
            target_id="asset-b",
        ),
        initial,
        late_retarget,
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert reduced.snapshot["items"] == []


def test_late_same_target_revision_is_a_binding_noop() -> None:
    initial = _revision(
        event_id="stable",
        author="carol",
        source_minutes_ago=30,
        target_id="asset-a",
    )
    late_same_target = _resolution_revision(
        initial,
        resolution_id="resolution-stable-late",
        effective_at_ms=initial.received_at_ms + 120_001,
        target_id="asset-a",
    )
    rows = [
        _revision(event_id="a-one", author="alice", source_minutes_ago=50),
        _revision(event_id="a-two", author="bob", source_minutes_ago=40),
        initial,
        late_same_target,
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert [item["target"]["target_id"] for item in reduced.snapshot["items"]] == ["asset-a"]
    assert reduced.snapshot["items"][0]["trigger_event_id"] == "stable"


def test_one_event_with_multiple_intents_uses_a_target_binding_refcount() -> None:
    first_intent = _revision(
        event_id="shared",
        intent_id="intent-shared-one",
        resolution_id="resolution-shared-one",
        author="carol",
        source_minutes_ago=30,
    )
    second_intent = _revision(
        event_id="shared",
        intent_id="intent-shared-two",
        resolution_id="resolution-shared-two",
        author="carol",
        source_minutes_ago=30,
    )
    first_intent_removed = _resolution_revision(
        first_intent,
        resolution_id="resolution-shared-one-ambiguous",
        effective_at_ms=first_intent.received_at_ms + 2 * MINUTE_MS,
        resolution_status="AMBIGUOUS",
        target_type=None,
        target_id=None,
    )
    rows = [
        _revision(event_id="a-one", author="alice", source_minutes_ago=50),
        _revision(event_id="a-two", author="bob", source_minutes_ago=40),
        first_intent,
        second_intent,
        first_intent_removed,
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    assert len(reduced.snapshot["items"]) == 1
    assert reduced.snapshot["items"][0]["why_now"]["current_mentions"] == 3


def test_same_millisecond_window_expiry_is_applied_before_positive_evidence() -> None:
    rows = [
        _revision(event_id="old-expiring", author="old-a", source_minutes_ago=120),
        _revision(event_id="old-staying", author="old-b", source_minutes_ago=100),
        _revision(event_id="current-one", author="alice", source_minutes_ago=50),
        _revision(event_id="current-two", author="bob", source_minutes_ago=40),
        _revision(event_id="current-three", author="carol", source_minutes_ago=30),
        _revision(
            event_id="same-ms-positive",
            author="dave",
            source_minutes_ago=1,
            effective_at_ms=NOW_MS,
        ),
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS)

    # Aging first makes the Gate true, but aging cannot open an episode. Applying
    # the positive fact second therefore sees true -> true and must not qualify.
    assert reduced.snapshot["items"] == []


def test_advancing_the_clock_without_new_facts_cannot_open_an_episode() -> None:
    rows = [
        _revision(event_id="old-expiring", author="old-a", source_minutes_ago=119),
        _revision(event_id="old-staying", author="old-b", source_minutes_ago=100),
        _revision(event_id="current-one", author="alice", source_minutes_ago=50),
        _revision(event_id="current-two", author="bob", source_minutes_ago=40),
        _revision(event_id="current-three", author="carol", source_minutes_ago=30),
    ]

    before_aging = reduce_token_radar(rows, now_ms=NOW_MS)
    after_aging = reduce_token_radar(rows, now_ms=NOW_MS + 2 * MINUTE_MS)

    assert before_aging.snapshot["items"] == []
    assert after_aging.snapshot["items"] == []


def test_expired_episode_stays_suppressed_while_the_gate_remains_true() -> None:
    rows = [
        _revision(event_id="opening-one", author="alice", source_minutes_ago=80),
        _revision(event_id="opening-two", author="bob", source_minutes_ago=70),
        _revision(
            event_id="opening-three",
            author="carol",
            source_minutes_ago=61,
            effective_at_ms=NOW_MS - 60 * MINUTE_MS,
        ),
        *[
            _revision(
                event_id=f"continuing-{index}",
                author=f"continuing-author-{index}",
                source_minutes_ago=minutes_ago,
            )
            for index, minutes_ago in enumerate((50, 40, 30, 20, 10))
        ],
    ]

    just_before_expiry = reduce_token_radar(rows, now_ms=NOW_MS - 1)
    at_expiry = reduce_token_radar(rows, now_ms=NOW_MS)
    after_expiry_with_another_positive = reduce_token_radar(
        [
            *rows,
            _revision(
                event_id="still-continuing",
                author="continuing-author-late",
                source_minutes_ago=-1,
            ),
        ],
        now_ms=NOW_MS + 5 * MINUTE_MS,
    )

    assert just_before_expiry.snapshot["items"][0]["qualified_at_ms"] == NOW_MS - 60 * MINUTE_MS
    assert at_expiry.snapshot["items"] == []
    assert after_expiry_with_another_positive.snapshot["items"] == []


def test_expired_episode_reenters_after_false_then_a_new_positive_crossing() -> None:
    continuing_early = _revision(
        event_id="continuing-early",
        author="continuing-early",
        source_minutes_ago=50,
    )
    continuing_second = _revision(
        event_id="continuing-second",
        author="continuing-second",
        source_minutes_ago=40,
    )
    rows = [
        _revision(event_id="opening-one", author="alice", source_minutes_ago=80),
        _revision(event_id="opening-two", author="bob", source_minutes_ago=70),
        _revision(
            event_id="opening-three",
            author="carol",
            source_minutes_ago=61,
            effective_at_ms=NOW_MS - 60 * MINUTE_MS,
        ),
        continuing_early,
        continuing_second,
        _revision(event_id="continuing-third", author="current-a", source_minutes_ago=30),
        _revision(event_id="continuing-fourth", author="current-b", source_minutes_ago=20),
        _revision(event_id="continuing-fifth", author="current-c", source_minutes_ago=10),
        _resolution_revision(
            continuing_early,
            resolution_id="resolution-continuing-early-removed",
            effective_at_ms=NOW_MS + 2 * MINUTE_MS,
            resolution_status="AMBIGUOUS",
            target_type=None,
            target_id=None,
        ),
        _resolution_revision(
            continuing_second,
            resolution_id="resolution-continuing-second-removed",
            effective_at_ms=NOW_MS + 2 * MINUTE_MS,
            resolution_status="AMBIGUOUS",
            target_type=None,
            target_id=None,
        ),
        _revision(
            event_id="rebound-one",
            author="rebound-one",
            source_minutes_ago=-3,
        ),
        _revision(
            event_id="rebound-two",
            author="rebound-two",
            source_minutes_ago=-4,
        ),
    ]

    reduced = reduce_token_radar(rows, now_ms=NOW_MS + 10 * MINUTE_MS)

    assert len(reduced.snapshot["items"]) == 1
    assert reduced.snapshot["items"][0]["qualified_at_ms"] == NOW_MS + 4 * MINUTE_MS
    assert reduced.snapshot["items"][0]["trigger_event_id"] == "rebound-two"


def test_input_permutations_produce_identical_causal_output() -> None:
    shared_one = _revision(
        event_id="shared",
        intent_id="intent-shared-one",
        resolution_id="resolution-shared-one",
        author="carol",
        source_minutes_ago=30,
    )
    shared_two = _revision(
        event_id="shared",
        intent_id="intent-shared-two",
        resolution_id="resolution-shared-two",
        author="carol",
        source_minutes_ago=30,
    )
    rows = [
        _revision(event_id="a-one", author="alice", source_minutes_ago=50),
        _revision(event_id="a-two", author="bob", source_minutes_ago=40),
        shared_one,
        shared_two,
        _resolution_revision(
            shared_one,
            resolution_id="resolution-shared-one-removed",
            effective_at_ms=shared_one.received_at_ms + MINUTE_MS,
            resolution_status="AMBIGUOUS",
            target_type=None,
            target_id=None,
        ),
    ]
    expected = reduce_token_radar(rows, now_ms=NOW_MS)

    permutations = [list(reversed(rows))]
    rng = random.Random(20260811)
    for _ in range(8):
        permutation = list(rows)
        rng.shuffle(permutation)
        permutations.append(permutation)

    for permutation in permutations:
        actual = reduce_token_radar(permutation, now_ms=NOW_MS)
        assert actual.snapshot == expected.snapshot
        assert actual.selected_keys == expected.selected_keys
        assert actual.input_fingerprint == expected.input_fingerprint


def test_ten_thousand_revision_hot_target_stays_within_the_reducer_budget() -> None:
    rows = [
        _revision(
            event_id=f"hot-{index:05d}",
            intent_id=f"intent-hot-{index:05d}",
            resolution_id=f"resolution-hot-{index:05d}",
            author=f"author-hot-{index:05d}",
            source_minutes_ago=10,
        )
        for index in range(10_000)
    ]

    started_at = time.perf_counter()
    reduced = reduce_token_radar(rows, now_ms=NOW_MS)
    elapsed_seconds = time.perf_counter() - started_at

    assert len(reduced.snapshot["items"]) == 1
    assert reduced.snapshot["items"][0]["why_now"]["current_mentions"] == 10_000
    assert reduced.snapshot["items"][0]["trigger_event_id"] == "hot-00000"
    assert elapsed_seconds <= 2.0


def test_ten_thousand_distinct_targets_stay_within_the_reducer_budget() -> None:
    rows = [
        _revision(
            event_id=f"distinct-{index:05d}",
            author=f"author-distinct-{index:05d}",
            source_minutes_ago=10,
            target_id=f"asset-{index:05d}",
        )
        for index in range(10_000)
    ]

    started_at = time.perf_counter()
    reduced = reduce_token_radar(rows, now_ms=NOW_MS)
    elapsed_seconds = time.perf_counter() - started_at

    assert reduced.snapshot["items"] == []
    assert reduced.input_rows == 10_000
    assert elapsed_seconds <= 2.0


def test_ten_thousand_resolution_history_fanout_stays_within_the_reducer_budget() -> None:
    initial = _revision(
        event_id="fanout",
        author="fanout-author",
        source_minutes_ago=10,
        target_id="asset-00000",
    )
    rows = [
        replace(
            initial,
            resolution_id=f"resolution-fanout-{index:05d}",
            target_id=f"asset-{index:05d}",
            resolution_decision_at_ms=initial.received_at_ms + index * 10,
            resolution_created_at_ms=initial.received_at_ms + index * 10,
        )
        for index in range(10_000)
    ]

    started_at = time.perf_counter()
    reduced = reduce_token_radar(rows, now_ms=NOW_MS)
    elapsed_seconds = time.perf_counter() - started_at

    assert reduced.snapshot["items"] == []
    assert reduced.input_rows == 10_000
    assert elapsed_seconds <= 2.0


def _revision(
    *,
    event_id: str,
    author: str,
    source_minutes_ago: int,
    received_lag_ms: int = 0,
    resolution_lag_ms: int = 0,
    intent_id: str | None = None,
    resolution_id: str | None = None,
    resolution_status: str = "EXACT",
    target_type: str | None = "Asset",
    target_id: str | None = "asset-a",
    effective_at_ms: int | None = None,
    text: str | None = None,
) -> RadarEvidenceRevision:
    source_at_ms = NOW_MS - source_minutes_ago * MINUTE_MS
    received_at_ms = source_at_ms + received_lag_ms
    event_created_at_ms = received_at_ms
    resolution_created_at_ms = received_at_ms + resolution_lag_ms if effective_at_ms is None else effective_at_ms
    return RadarEvidenceRevision(
        event_id=event_id,
        intent_id=intent_id or f"intent-{event_id}",
        resolution_id=resolution_id or f"resolution-{event_id}-{resolution_created_at_ms}",
        source_event_at_ms=source_at_ms,
        received_at_ms=received_at_ms,
        event_created_at_ms=event_created_at_ms,
        action="tweet",
        author_key=author,
        text=text or f"text-{event_id}",
        resolution_status=resolution_status,
        target_type=target_type,
        target_id=target_id,
        resolution_decision_at_ms=resolution_created_at_ms,
        resolution_created_at_ms=resolution_created_at_ms,
    )


def _resolution_revision(
    event: RadarEvidenceRevision,
    *,
    resolution_id: str,
    effective_at_ms: int,
    resolution_status: str = "EXACT",
    target_type: str | None = "Asset",
    target_id: str | None = "asset-a",
) -> RadarEvidenceRevision:
    return replace(
        event,
        resolution_id=resolution_id,
        resolution_status=resolution_status,
        target_type=target_type,
        target_id=target_id,
        resolution_decision_at_ms=effective_at_ms,
        resolution_created_at_ms=effective_at_ms,
    )
