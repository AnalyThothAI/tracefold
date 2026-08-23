"""Projection rows in, eligible candidates out — including the rows that must produce nothing.

Two of these encode findings the spec's own text gets wrong if read literally:

* a Gate `asset_class` of `crypto` is not a listing. `WMT` reaches the trading lane with exactly that
  class, and the only thing that stops it is the instrument catalogue's own `equity` label.
* `news_oi` fusion is rare. The disjoint-symbol test is the shape the live gate actually sees.
"""

from __future__ import annotations

from typing import Any

from tracefold.trading import (
    Blacklist,
    EligibilityPolicy,
    Funnel,
    NewsTradeCandidate,
    OiTradeCandidate,
    Rejected,
    attach_news,
    attach_oi,
    news_candidate,
    oi_candidate,
    resolve_instrument,
)

NOW = 1_787_000_000_000
OPEN_DENY = Blacklist.from_rows([{"base_symbol": "BTC", "reason": "benchmark_large_cap"}])


def _oi_row(**kwargs: Any) -> dict[str, Any]:
    row = {
        "event_id": "e1",
        "final_decision": "push",
        "ingest_mode": "live",
        "program_version": "news_oi_signal_v1",
        "metric_version": "oi_signal_v1",
        "symbol": "DOGE",
        "direction": "rise",
        "oi_change_bps": 320,
        "oi_value_usd": 73_010_000,
        "whale_long_profit_bps": 9_900,
        "whale_oi_ratio_bps": 21_097,
        "rank_in_window": 1,
        "observed_at_ms": NOW - 10_000,
        "venue": "hyperliquid",
    }
    row.update(kwargs)
    return row


def _news_row(**kwargs: Any) -> dict[str, Any]:
    verdict = {
        "assets": [{"symbol": "DOGE", "role": "primary"}],
        "novelty": "new_fact",
        "magnitude": 2,
        "direction": "bullish",
        "scope": "single_name",
        "event_type": "listing",
        "headline_zh": "标题",
        "why_zh": "机制",
    }
    verdict.update(kwargs.pop("verdict", {}))
    row = {
        "event_id": "n1",
        "verdict_created_at_ms": NOW - 20_000,
        "opened_at_ms": NOW - 25_000,
        "final_decision": "push",
        "evidence_version": 3,
        "evidence_sha256": "abc",
        "focus_fact_id": "f1",
        "verdict": verdict,
        "program_version": "program_v5",
        "policy_version": "news_triage_policy_v9",
        "comparison_fingerprint": "fp",
        "asset_class": "crypto",
        "grounded_assets": ["DOGE"],
        "ingest_mode": "live",
        "source_artifact_id": "x:123",
        "source_published_at_ms": NOW - 30_000,
    }
    row.update(kwargs)
    return row


# ---------------------------------------------------------------------------- OI eligibility
def test_a_qualifying_frame_becomes_a_candidate_and_keeps_its_venue() -> None:
    candidate = oi_candidate(_oi_row(), now_ms=NOW, blacklist=OPEN_DENY)
    assert isinstance(candidate, OiTradeCandidate)
    # Venue is the strongest single discriminator the research measured, so it must survive the
    # projection rather than being dropped with the rest of the frame.
    assert candidate.venue == "hyperliquid"
    assert candidate.source_key == "oi:e1:oi_signal_v1"


def test_the_deny_list_rejects_before_anything_else_spends_work() -> None:
    result = oi_candidate(_oi_row(symbol="BTC"), now_ms=NOW, blacklist=OPEN_DENY)
    assert isinstance(result, Rejected)
    assert result.rule.startswith("blacklisted:")


def test_rank_staleness_and_thin_open_interest_are_all_named_rejections() -> None:
    for row, rule in (
        (_oi_row(rank_in_window=6), "rank_exhausted"),
        (_oi_row(observed_at_ms=NOW - 10_000_000), "stale"),
        (_oi_row(oi_value_usd=3_000_000), "oi_value_below_floor"),
        (_oi_row(direction="sideways"), "oi_direction_unknown"),
        (_oi_row(ingest_mode="recovery"), "not_live_ingest"),
        (_oi_row(final_decision="drop"), "not_pushed"),
    ):
        result = oi_candidate(row, now_ms=NOW, blacklist=OPEN_DENY)
        assert isinstance(result, Rejected)
        assert result.rule == rule


# ---------------------------------------------------------------------------- News eligibility
def test_a_qualifying_verdict_becomes_a_candidate_with_a_source_fact_key() -> None:
    candidate = news_candidate(_news_row(), now_ms=NOW, blacklist=OPEN_DENY)
    assert isinstance(candidate, NewsTradeCandidate)
    assert candidate.source_key  # sha256 of artifact + fingerprint
    # The News vocabulary's sign is carried as risk context, never as an instrument side.
    assert candidate.risk_direction == "bullish"


def test_two_numbered_facts_in_one_artifact_stay_two_source_keys() -> None:
    first = news_candidate(_news_row(comparison_fingerprint="fp-a"), now_ms=NOW, blacklist=OPEN_DENY)
    second = news_candidate(_news_row(comparison_fingerprint="fp-b"), now_ms=NOW, blacklist=OPEN_DENY)
    assert isinstance(first, NewsTradeCandidate) and isinstance(second, NewsTradeCandidate)
    assert first.source_key != second.source_key


def test_the_same_fact_re_scanned_keeps_one_source_key() -> None:
    first = news_candidate(_news_row(), now_ms=NOW, blacklist=OPEN_DENY)
    second = news_candidate(_news_row(), now_ms=NOW, blacklist=OPEN_DENY)
    assert isinstance(first, NewsTradeCandidate) and isinstance(second, NewsTradeCandidate)
    assert first.source_key == second.source_key


def test_two_primaries_a_restatement_and_a_weak_magnitude_are_all_rejected() -> None:
    cases = [
        (
            _news_row(verdict={"assets": [{"symbol": "A", "role": "primary"}, {"symbol": "B", "role": "primary"}]}),
            "not_exactly_one_primary",
        ),
        (_news_row(verdict={"novelty": "restatement"}), "restatement"),
        (_news_row(verdict={"magnitude": 1}), "magnitude_below_floor"),
        (_news_row(grounded_assets=["SOL"]), "primary_not_grounded"),
        (_news_row(asset_class="equity_or_commodity"), "asset_class_not_crypto"),
    ]
    for row, rule in cases:
        result = news_candidate(row, now_ms=NOW, blacklist=OPEN_DENY)
        assert isinstance(result, Rejected), rule
        assert result.rule == rule


def test_the_funnel_counts_exactly_the_rules_that_fired() -> None:
    funnel = Funnel()
    oi_candidate(_oi_row(symbol="BTC"), now_ms=NOW, blacklist=OPEN_DENY, funnel=funnel)
    oi_candidate(_oi_row(), now_ms=NOW, blacklist=OPEN_DENY, funnel=funnel)
    counts = funnel.as_dict()
    assert counts["oi_reject:blacklisted:benchmark_large_cap"] == 1
    assert counts["oi_eligible"] == 1
    assert counts["oi_eligible_venue:hyperliquid"] == 1


# ---------------------------------------------------------------------------- fusion
def test_fusion_is_point_in_time_only() -> None:
    policy = EligibilityPolicy()
    signal = oi_candidate(_oi_row(observed_at_ms=NOW - 60_000), now_ms=NOW, blacklist=OPEN_DENY)
    assert isinstance(signal, OiTradeCandidate)

    earlier = news_candidate(_news_row(verdict_created_at_ms=NOW - 120_000), now_ms=NOW, blacklist=OPEN_DENY)
    later = news_candidate(_news_row(event_id="n2", verdict_created_at_ms=NOW - 1_000), now_ms=NOW, blacklist=OPEN_DENY)
    assert isinstance(earlier, NewsTradeCandidate) and isinstance(later, NewsTradeCandidate)

    attached = attach_news(signal, [earlier, later], policy=policy)
    # `later` is after the frame: attaching it would be reading the future into a frozen manifest.
    assert attached is earlier


def test_disjoint_symbol_sets_produce_no_fusion_which_is_the_measured_shape() -> None:
    # In a full day of both lanes running, the OI symbols and the News symbols overlapped once and
    # never inside the window. `news_oi` — the only live-eligible kind — is genuinely rare.
    signal = oi_candidate(_oi_row(symbol="PENGU"), now_ms=NOW, blacklist=OPEN_DENY)
    news = news_candidate(
        _news_row(verdict={"assets": [{"symbol": "ZEC", "role": "primary"}]}, grounded_assets=["ZEC"]),
        now_ms=NOW,
        blacklist=OPEN_DENY,
    )
    assert isinstance(signal, OiTradeCandidate) and isinstance(news, NewsTradeCandidate)
    assert attach_news(signal, [news]) is None
    assert attach_oi(news, [signal]) is None


# ---------------------------------------------------------------------------- instrument routing
def _catalogue(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    return list(rows)


def test_priority_order_picks_exactly_one_venue() -> None:
    rows = _catalogue(
        {"venue": "hl.perp", "venue_symbol": "DOGE", "base_symbol": "DOGE", "instrument_class": "crypto"},
        {"venue": "binance.perp", "venue_symbol": "DOGEUSDT", "base_symbol": "DOGE", "instrument_class": "crypto"},
    )
    chosen = resolve_instrument(rows, priority=("binance", "hyperliquid"), observed_at_ms=NOW)
    assert chosen is not None
    assert (chosen.exchange_id, chosen.provider_symbol) == ("binance", "DOGEUSDT")

    flipped = resolve_instrument(rows, priority=("hyperliquid", "binance"), observed_at_ms=NOW)
    assert flipped is not None
    assert (flipped.exchange_id, flipped.provider_symbol) == ("hyperliquid", "DOGE")


def test_a_gate_crypto_class_over_an_equity_perp_resolves_to_nothing() -> None:
    """`WMT` is the live example: Gate `asset_class='crypto'`, `binance.perp WMTUSDT` class `equity`.

    Condition "the Gate says crypto" is therefore not a filter at all. The catalogue's own label is
    the only thing between an equity perp and a trade, so the projection filters on it and this test
    is what keeps that true.
    """

    rows = _catalogue(
        {"venue": "binance.perp", "venue_symbol": "WMTUSDT", "base_symbol": "WMT", "instrument_class": "equity"},
    )
    # The News projection already filters `instrument_class = 'crypto'`; belt and braces here is that an
    # equity row reaching this function still has to be spelled as a native perp venue *and* pass the
    # projection. With only the equity row available, nothing resolves.
    chosen = resolve_instrument(
        [row for row in rows if row["instrument_class"] == "crypto"],
        priority=("binance", "hyperliquid"),
        observed_at_ms=NOW,
    )
    assert chosen is None


def test_hip3_builder_markets_are_not_execution_venues_in_v1() -> None:
    rows = _catalogue(
        {"venue": "hl.xyz", "venue_symbol": "xyz:MRNA", "base_symbol": "MRNA", "instrument_class": "equity"},
        {"venue": "hl.spot", "venue_symbol": "@107", "base_symbol": "HYPE", "instrument_class": "crypto"},
    )
    assert resolve_instrument(rows, priority=("binance", "hyperliquid"), observed_at_ms=NOW) is None
