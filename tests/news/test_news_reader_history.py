from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.contracts import ArmManifest
from tracefold.news.learning.evaluate import CandidateEvaluator
from tracefold.news.learning.evaluation_history import ArmState, Receipt
from tracefold.news.program.contracts import TriageContext
from tracefold.news.reader_history import (
    READER_HISTORY_SHA256,
    RECENT_HISTORY_WINDOW_MS,
    TARGETED_HISTORY_WINDOW_MS,
    build_reader_history,
    news_retrieval_sha256,
)
from tracefold.news.told_context import TOLD_SELECTOR_SHA256
from tracefold.news.triage_rules import DEFAULT_POLICY

NOW_MS = 2_000_000_000_000
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_reader_history_v1.json"
DISCOVERY = (
    Path(__file__).resolve().parents[2] / "docs" / "research" / "news-reader-history-48h-snapshot-2026-08-24.json"
)


def test_reader_history_and_composite_retrieval_identities_are_content_addressed() -> None:
    composite = news_retrieval_sha256(told_selector_sha256=TOLD_SELECTOR_SHA256)

    assert len(READER_HISTORY_SHA256) == 64
    assert composite != READER_HISTORY_SHA256
    assert composite != TOLD_SELECTOR_SHA256
    assert composite != news_retrieval_sha256(told_selector_sha256="f" * 64)


def test_evaluator_and_production_contexts_share_targeted_history_while_policy_seen_stays_recent() -> None:
    class _Connection:
        def execute(self, sql: str) -> SimpleNamespace:
            assert sql == "SELECT alias, base_symbol FROM news_symbol_aliases"
            return SimpleNamespace(fetchall=lambda: [{"alias": "9988", "base_symbol": "BABA"}])

    policy = DEFAULT_POLICY.as_dict()
    evaluator = CandidateEvaluator(
        _Connection(),
        stable=ArmManifest(
            program_version="news_semantic_program_v5",
            program_sha256="a" * 64,
            runtime_model_bindings_sha256="b" * 64,
            retrieval_sha256="c" * 64,
            policy=policy,
            policy_sha256=canonical_sha(policy),
        ),
        judges={},
    )
    receipts = (
        Receipt(
            event_id="recent",
            at_ms=NOW_MS - 60_000,
            storyline_key="asset:BABA",
            magnitude=2,
            direction="bearish",
            headline_zh="recent",
            comparison_fingerprint="recent",
            family="general",
            grounded_assets=("9988",),
            canonical_assets=("BABA",),
        ),
        Receipt(
            event_id="targeted",
            at_ms=NOW_MS - 24 * 3_600_000,
            storyline_key="asset:BABA",
            magnitude=2,
            direction="bearish",
            headline_zh="targeted",
            comparison_fingerprint="same",
            family="general",
            grounded_assets=("9988",),
            canonical_assets=("BABA",),
        ),
    )
    event = {
        "event_id": "current",
        "evidence_version": 1,
        "evidence_sha256": "e" * 64,
        "focus_fact_id": "fact",
        "leader_title": "same fact",
        "leader_description": "",
        "leader_published_at_ms": NOW_MS,
        "family": "general",
        "comparison_title": "same fact",
        "comparison_fingerprint": "same",
        "storyline_key": "asset:BABA",
        "grounded_assets": ["9988"],
    }
    case = {
        "snapshot": {"card": event, "focus_fact": {"fact_id": "fact", "text": "same fact", "context": ""}},
        "opened_at_ms": NOW_MS,
        "watchlist": (),
    }
    state = ArmState()
    state.receipts.extend(receipts)

    offline = evaluator._datasets.build_context(case, state)
    online_history = build_reader_history(
        [receipt.as_told_row() for receipt in receipts],
        now_ms=NOW_MS,
        family="general",
        comparison_fingerprint="same",
        canonical_assets=("BABA",),
    )
    online = TriageContext.from_card(
        event,
        watchlist=(),
        told_rows=[row.as_told_row() for row in online_history.told_source_rows],
        now_ms=NOW_MS,
        queue_lag_ms=0,
    )

    assert offline.model_dump(mode="json") == online.model_dump(mode="json")
    assert [entry.event_id for entry in offline.told.entries] == ["targeted", "recent"]
    metric = evaluator._datasets._policy_metric_projection(case, state, context=offline)
    assert [row["event_id"] for row in metric["seen"]] == ["recent"]


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


def test_reader_history_preserves_an_explicit_empty_canonical_asset_projection() -> None:
    prior = _row("verdict-only", NOW_MS - RECENT_HISTORY_WINDOW_MS - 1)
    prior["grounded_assets"] = ["BABA"]
    prior["assets"] = ["BABA"]
    prior["canonical_assets"] = []

    history = build_reader_history(
        (prior,),
        now_ms=NOW_MS,
        comparison_fingerprint="current-fingerprint",
        canonical_assets=("BABA",),
    )

    assert history.targeted_told_rows == ()


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


def test_fixed_duplicate_discovery_report_matches_the_frozen_fixture() -> None:
    cases = [
        case
        for case in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        if case["expected_novelty"] == "restatement"
    ]
    reasons: dict[str, int] = {}
    recalled_48h = 0
    recalled_7d = 0
    lost_48h_to_7d = []
    for case in cases:
        target = case["prior_rows"][0]
        history = build_reader_history(
            case["prior_rows"],
            now_ms=case["now_ms"],
            comparison_fingerprint=case["current"]["comparison_fingerprint"],
            canonical_assets=case["current"]["canonical_assets"],
        )
        if history.targeted_told_rows:
            recalled_48h += 1
            reason = history.targeted_told_rows[0].reason
            reasons[reason] = reasons.get(reason, 0) + 1
        age_ms = case["now_ms"] - target["at_ms"]
        if age_ms <= 7 * 24 * 3_600_000:
            recalled_7d += 1
            if age_ms > TARGETED_HISTORY_WINDOW_MS:
                lost_48h_to_7d.append(case["case_id"])

    report = json.loads(DISCOVERY.read_text(encoding="utf-8"))["fixed_duplicate_discovery"]
    assert report == {
        "cases": [case["case_id"] for case in cases],
        "denominator": len(cases),
        "current_4h": {"source_recall": "0/2", "selected_recall_at_16": "0/2"},
        "proposed_48h": {
            "source_recall": f"{recalled_48h}/{len(cases)}",
            "selected_recall_at_16": f"{recalled_48h}/{len(cases)}",
            "reason_counts": reasons,
        },
        "exploratory_7d": {
            "source_recall": f"{recalled_7d}/{len(cases)}",
            "selected_recall_at_16": f"{recalled_7d}/{len(cases)}",
        },
        "targets_only_in_48h_to_7d": lost_48h_to_7d,
        "targets_only_in_48h_to_7d_count": len(lost_48h_to_7d),
        "fixture_selection_note": (
            "each frozen duplicate case has one recalled source row, so source rank equals selected rank under "
            "the 16-row cap"
        ),
    }
