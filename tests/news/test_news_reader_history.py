from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tracefold.news.reader_history import (
    NEWS_RETRIEVAL_SHA256,
    READER_HISTORY_SHA256,
    RECENT_HISTORY_WINDOW_MS,
    TARGETED_HISTORY_WINDOW_MS,
    build_reader_history,
)
from tracefold.news.semantic_contract import TOLD_SELECTOR_SHA256

NOW_MS = 2_000_000_000_000
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_reader_history_v1.json"


def test_reader_history_and_composite_retrieval_identities_are_content_addressed() -> None:
    assert len(READER_HISTORY_SHA256) == 64
    assert NEWS_RETRIEVAL_SHA256 != READER_HISTORY_SHA256
    assert NEWS_RETRIEVAL_SHA256 != TOLD_SELECTOR_SHA256


def _row(
    event_id: str, at_ms: int, *, fingerprint: str = "other", canonical_assets: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "at_ms": at_ms,
        "storyline_key": "asset:BABA",
        "comparison_title": event_id,
        "comparison_fingerprint": fingerprint,
        "family": "general",
        "grounded_assets": list(canonical_assets),
        "canonical_assets": list(canonical_assets),
        "assets": list(canonical_assets),
        "event_type": "filing",
        "magnitude": 2,
        "direction": "bearish",
        "headline_zh": event_id,
    }


def test_reader_history_uses_disjoint_4h_and_48h_boundaries() -> None:
    rows = (
        _row("recent-boundary", NOW_MS - RECENT_HISTORY_WINDOW_MS, canonical_assets=("BABA",)),
        _row(
            "targeted-after-boundary",
            NOW_MS - RECENT_HISTORY_WINDOW_MS - 1,
            fingerprint="same",
            canonical_assets=("BABA",),
        ),
        _row(
            "targeted-outer-boundary",
            NOW_MS - TARGETED_HISTORY_WINDOW_MS,
            fingerprint="same",
            canonical_assets=("BABA",),
        ),
        _row(
            "expired",
            NOW_MS - TARGETED_HISTORY_WINDOW_MS - 1,
            fingerprint="same",
            canonical_assets=("BABA",),
        ),
    )

    history = build_reader_history(
        rows,
        now_ms=NOW_MS,
        comparison_fingerprint="same",
        canonical_assets=("BABA",),
    )

    assert [row.event_id for row in history.recent_seen_rows] == ["recent-boundary"]
    assert [row.event_id for row in history.targeted_told_rows] == [
        "targeted-after-boundary",
        "targeted-outer-boundary",
    ]
    assert [row.scope for row in history.told_source_rows] == ["targeted", "targeted", "recent"]


def test_reader_history_caps_each_targeted_reason_and_exact_never_falls_through_to_asset() -> None:
    exact = [
        _row(
            f"exact-{index:02d}",
            NOW_MS - RECENT_HISTORY_WINDOW_MS - 1 - index,
            fingerprint="same",
            canonical_assets=("BABA",),
        )
        for index in range(9)
    ]
    asset = [
        _row(f"asset-{index:02d}", NOW_MS - RECENT_HISTORY_WINDOW_MS - 100 - index, canonical_assets=("BABA",))
        for index in range(25)
    ]

    history = build_reader_history(
        (*exact, *asset),
        now_ms=NOW_MS,
        comparison_fingerprint="same",
        canonical_assets=("BABA",),
    )

    assert len(history.targeted_told_rows) == 32
    assert [row.event_id for row in history.targeted_told_rows[:8]] == [f"exact-{index:02d}" for index in range(8)]
    assert {row.reason for row in history.targeted_told_rows[:8]} == {"exact_fingerprint"}
    assert [row.event_id for row in history.targeted_told_rows[8:]] == [f"asset-{index:02d}" for index in range(24)]
    assert {row.reason for row in history.targeted_told_rows[8:]} == {"canonical_asset_overlap"}


def test_reader_history_caps_recent_and_orders_equal_times_by_event_id() -> None:
    rows = [_row(f"recent-{index:03d}", NOW_MS - 1_000) for index in range(129, -1, -1)]

    history = build_reader_history(
        rows,
        now_ms=NOW_MS,
        comparison_fingerprint="current-fingerprint",
        canonical_assets=(),
    )

    assert len(history.recent_seen_rows) == 128
    assert [row.event_id for row in history.recent_seen_rows[:3]] == ["recent-000", "recent-001", "recent-002"]
    assert history.recent_seen_rows[-1].event_id == "recent-127"


def test_reader_history_matches_frozen_canonical_assets_not_raw_aliases() -> None:
    rows = (
        _row("same-issuer", NOW_MS - RECENT_HISTORY_WINDOW_MS - 1, canonical_assets=("BABA",)),
        _row("different-issuer", NOW_MS - RECENT_HISTORY_WINDOW_MS - 2, canonical_assets=("TENCENT",)),
    )
    rows[0]["grounded_assets"] = ["9988"]
    rows[1]["grounded_assets"] = ["0700"]

    history = build_reader_history(
        rows,
        now_ms=NOW_MS,
        comparison_fingerprint="current-fingerprint",
        canonical_assets=("BABA",),
    )

    assert [(row.event_id, row.grounded_assets, row.canonical_assets) for row in history.targeted_told_rows] == [
        ("same-issuer", ("9988",), ("BABA",))
    ]


def test_reader_history_exact_fingerprint_uses_the_same_family_as_postgres() -> None:
    prior = _row(
        "other-family",
        NOW_MS - RECENT_HISTORY_WINDOW_MS - 1,
        fingerprint="same",
    )
    prior["family"] = "filing"

    history = build_reader_history(
        (prior,),
        now_ms=NOW_MS,
        family="general",
        comparison_fingerprint="same",
        canonical_assets=(),
    )

    assert history.targeted_told_rows == ()


def test_reader_history_frozen_cases_keep_target_availability_and_semantic_expectation() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]

    observed = {}
    for case in cases:
        history = build_reader_history(
            case["prior_rows"],
            now_ms=case["now_ms"],
            family=case["current"].get("family", "general"),
            comparison_fingerprint=case["current"]["comparison_fingerprint"],
            canonical_assets=case["current"]["canonical_assets"],
            include_targeted=case.get("include_targeted", True),
        )
        observed[case["case_id"]] = {
            "targeted": [row.event_id for row in history.targeted_told_rows],
            "reasons": [row.reason for row in history.targeted_told_rows],
            "expected_novelty": case["expected_novelty"],
        }

    assert observed == {
        "alibaba_overnight_restatement": {
            "targeted": ["alibaba-prior"],
            "reasons": ["canonical_asset_overlap"],
            "expected_novelty": "restatement",
        },
        "ordinary_24h_restatement": {
            "targeted": ["ordinary-prior"],
            "reasons": ["exact_fingerprint"],
            "expected_novelty": "restatement",
        },
        "material_progression": {
            "targeted": ["progression-prior"],
            "reasons": ["canonical_asset_overlap"],
            "expected_novelty": "progression",
        },
        "same_asset_new_fact": {
            "targeted": ["same-asset-prior"],
            "reasons": ["canonical_asset_overlap"],
            "expected_novelty": "new_fact",
        },
        "direction_reversal": {
            "targeted": ["reversal-prior"],
            "reasons": ["canonical_asset_overlap"],
            "expected_novelty": "progression",
        },
        "telemetry_control": {"targeted": [], "reasons": [], "expected_novelty": "new_fact"},
    }
