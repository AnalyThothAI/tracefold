"""The reader-history bands' shared boundaries, caps, ordering and projection rules (pure).

PostgreSQL selects each band's candidate rows (`DecisionStorage.reader_history`); `assemble_reader_history`
applies one set of window boundaries, reason precedence, caps and ordering to them. The SQL band split
itself is exercised against PostgreSQL in `tests/integration/test_news_reader_history.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.reader_history import (
    RECENT_HISTORY_WINDOW_MS,
    TARGETED_HISTORY_WINDOW_MS,
    assemble_reader_history,
)

NOW_MS = 2_000_000_000_000


def _row(
    event_id: str, at_ms: int, *, fingerprint: str = "other", canonical_assets: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "at_ms": at_ms,
        "storyline_key": "asset:BABA",
        "comparison_title": event_id,
        "comparison_fingerprint": fingerprint,
        "dedupe_family": "general",
        "grounded_assets": list(canonical_assets),
        "canonical_assets": list(canonical_assets),
        "assets": list(canonical_assets),
        "direction": "bearish",
        "headline_zh": event_id,
        "why_zh": f"why {event_id}",
    }


def test_reader_history_uses_disjoint_4h_and_48h_boundaries() -> None:
    rows = (
        _row("recent-boundary", NOW_MS - RECENT_HISTORY_WINDOW_MS, canonical_assets=("BABA",)),
        _row("targeted-after-boundary", NOW_MS - RECENT_HISTORY_WINDOW_MS - 1, fingerprint="same"),
        _row("targeted-outer-boundary", NOW_MS - TARGETED_HISTORY_WINDOW_MS, fingerprint="same"),
        _row("expired", NOW_MS - TARGETED_HISTORY_WINDOW_MS - 1, fingerprint="same"),
    )

    history = assemble_reader_history(recent_rows=rows, exact_rows=rows, now_ms=NOW_MS)

    assert [row.event_id for row in history.recent_seen_rows] == ["recent-boundary"]
    assert [row.event_id for row in history.targeted_told_rows] == [
        "targeted-after-boundary",
        "targeted-outer-boundary",
    ]
    assert [row.scope for row in history.told_source_rows] == ["targeted", "targeted", "recent"]


def test_reader_history_caps_each_targeted_reason_and_exact_never_falls_through_to_asset() -> None:
    exact = [
        _row(f"exact-{index:02d}", NOW_MS - RECENT_HISTORY_WINDOW_MS - 1 - index, fingerprint="same")
        for index in range(9)
    ]
    asset = [
        _row(f"asset-{index:02d}", NOW_MS - RECENT_HISTORY_WINDOW_MS - 100 - index, canonical_assets=("BABA",))
        for index in range(25)
    ]

    # An exact match beyond the exact cap is still an exact match: it never takes an asset slot.
    history = assemble_reader_history(recent_rows=(), exact_rows=exact, asset_rows=(*exact, *asset), now_ms=NOW_MS)

    assert len(history.targeted_told_rows) == 32
    assert [row.event_id for row in history.targeted_told_rows[:8]] == [f"exact-{index:02d}" for index in range(8)]
    assert {row.reason for row in history.targeted_told_rows[:8]} == {"exact_fingerprint"}
    assert [row.event_id for row in history.targeted_told_rows[8:]] == [f"asset-{index:02d}" for index in range(24)]
    assert {row.reason for row in history.targeted_told_rows[8:]} == {"canonical_asset_overlap"}


def test_reader_history_caps_recent_and_orders_equal_times_by_event_id() -> None:
    rows = [_row(f"recent-{index:03d}", NOW_MS - 1_000) for index in range(129, -1, -1)]

    history = assemble_reader_history(recent_rows=rows, now_ms=NOW_MS)

    assert len(history.recent_seen_rows) == 128
    assert [row.event_id for row in history.recent_seen_rows[:3]] == ["recent-000", "recent-001", "recent-002"]
    assert history.recent_seen_rows[-1].event_id == "recent-127"


def test_reader_history_rejects_an_incomplete_current_projection() -> None:
    prior = _row("incomplete", NOW_MS - 1)
    del prior["canonical_assets"]

    with pytest.raises(ValueError, match="news_reader_history_fields_missing:canonical_assets"):
        assemble_reader_history(recent_rows=(prior,), now_ms=NOW_MS)


def test_the_title_band_ranks_by_trigram_similarity_and_spends_no_row_twice() -> None:
    same = {**_row("same-story", NOW_MS - RECENT_HISTORY_WINDOW_MS - 10), "comparison_title": "Alibaba places shares"}
    other = {**_row("other-story", NOW_MS - RECENT_HISTORY_WINDOW_MS - 20), "comparison_title": "Fed holds rates"}
    recent = {**_row("recent", NOW_MS - 10), "comparison_title": "Alibaba places shares"}

    history = assemble_reader_history(
        recent_rows=(recent,),
        similar_rows=(same, other, recent),
        comparison_title="Alibaba places new shares",
        now_ms=NOW_MS,
    )

    # Closest first; the recent row is already in the recent band and is not spent again here.
    assert [row.event_id for row in history.similar_told_rows] == ["same-story", "other-story"]
    assert {row.reason for row in history.similar_told_rows} == {"title_similarity"}
