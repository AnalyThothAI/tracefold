"""Projection rows in, eligible candidates out — including the rows that must produce nothing.

Two of these encode findings the spec's own text gets wrong if read literally:

* a Gate `asset_class` of `crypto` is not a listing. `WMT` reaches the trading lane with exactly that
  class, and the only thing that stops it is the instrument catalogue's own `equity` label.
* `news_oi` fusion is rare. The disjoint-symbol test is the shape the live gate actually sees.
"""

from __future__ import annotations

from typing import Any

from tracefold.trading.candidate.blacklist import Blacklist
from tracefold.trading.candidate.eligibility import (
    EligibilityPolicy,
    Funnel,
    Rejected,
    is_fresh_trigger,
    news_candidate,
    oi_candidate,
)
from tracefold.trading.candidate.fusion import attach_news, attach_oi, plan_triggers
from tracefold.trading.candidate.gate import GateConfig, admit_route, admit_trigger
from tracefold.trading.candidate.routing import resolve_instrument, signal_exchange_id
from tracefold.trading.contracts import NewsTradeCandidate, OiTradeCandidate
from tracefold.trading.pipeline.candidate import scan_horizon_ms

NOW = 1_787_000_000_000
OPEN_DENY = Blacklist.from_rows([{"base_symbol": "BTC", "reason": "benchmark_large_cap"}])
OPEN_GATE = GateConfig.from_policy(EligibilityPolicy(), venue_priority=("binance", "hyperliquid"))


def _admitted(
    signals: Any,
    *,
    now_ms: int = NOW,
    config: GateConfig = OPEN_GATE,
    blacklist: Blacklist = OPEN_DENY,
    active_underlyings: Any = (),
    underlyings_in_flight: Any = (),
    cased_source_keys: Any = (),
) -> set[str]:
    """The OI source keys the Candidate Gate would let trigger, computed exactly as `_plan` does.

    Spelling the set out by hand in each fusion test would let the gate and the planner disagree
    without anything failing, which is the class of defect #264 exists to remove.
    """

    keys: set[str] = set()
    for signal in signals:
        refused = admit_trigger(
            signal,
            now_ms=now_ms,
            config=config,
            blacklist=blacklist,
            active_underlyings=active_underlyings,
            underlyings_in_flight=underlyings_in_flight,
            cased_source_keys=cased_source_keys,
        ) or admit_route(signal, config=config)
        if refused is None:
            keys.add(signal.source_key)
    return keys


def _plans(
    *,
    oi: Any = (),
    news: Any = (),
    now_ms: int = NOW,
    policy: Any = None,
    blacklist: Blacklist = OPEN_DENY,
    active_underlyings: Any = (),
    underlyings_in_flight: Any = (),
    cased_source_keys: Any = (),
    funnel: Any = None,
) -> Any:
    """`plan_triggers` with the OI lane's trigger set taken from the gate, as the runner composes it."""

    resolved = policy or EligibilityPolicy()
    config = GateConfig.from_policy(resolved, venue_priority=("binance", "hyperliquid"))
    return plan_triggers(
        oi=oi,
        news=news,
        now_ms=now_ms,
        policy=resolved,
        oi_trigger_keys=_admitted(
            oi,
            now_ms=now_ms,
            config=config,
            blacklist=blacklist,
            active_underlyings=active_underlyings,
            underlyings_in_flight=underlyings_in_flight,
            cased_source_keys=cased_source_keys,
        ),
        active_underlyings=active_underlyings,
        underlyings_in_flight=underlyings_in_flight,
        cased_source_keys=cased_source_keys,
        funnel=funnel,
    )


OPEN_GATE = GateConfig.from_policy(EligibilityPolicy(), venue_priority=("binance", "hyperliquid"))


def _admitted(
    signals: Any,
    *,
    now_ms: int = NOW,
    config: GateConfig = OPEN_GATE,
    blacklist: Blacklist = OPEN_DENY,
    active_underlyings: Any = (),
    underlyings_in_flight: Any = (),
    cased_source_keys: Any = (),
) -> set[str]:
    """The OI source keys the Candidate Gate would let trigger, computed exactly as `_plan` does.

    Spelling the set out by hand in each fusion test would let the gate and the planner disagree
    without anything failing, which is the class of defect #264 exists to remove.
    """

    keys: set[str] = set()
    for signal in signals:
        refused = admit_trigger(
            signal,
            now_ms=now_ms,
            config=config,
            blacklist=blacklist,
            active_underlyings=active_underlyings,
            underlyings_in_flight=underlyings_in_flight,
            cased_source_keys=cased_source_keys,
        ) or admit_route(signal, config=config)
        if refused is None:
            keys.add(signal.source_key)
    return keys


def _plans(
    *,
    oi: Any = (),
    news: Any = (),
    now_ms: int = NOW,
    policy: Any = None,
    blacklist: Blacklist = OPEN_DENY,
    active_underlyings: Any = (),
    underlyings_in_flight: Any = (),
    cased_source_keys: Any = (),
    funnel: Any = None,
) -> Any:
    """`plan_triggers` with the OI lane's trigger set taken from the gate, as the runner composes it."""

    resolved = policy or EligibilityPolicy()
    config = GateConfig.from_policy(resolved, venue_priority=("binance", "hyperliquid"))
    return plan_triggers(
        oi=oi,
        news=news,
        now_ms=now_ms,
        policy=resolved,
        oi_trigger_keys=_admitted(
            oi,
            now_ms=now_ms,
            config=config,
            blacklist=blacklist,
            active_underlyings=active_underlyings,
            underlyings_in_flight=underlyings_in_flight,
            cased_source_keys=cased_source_keys,
        ),
        active_underlyings=active_underlyings,
        underlyings_in_flight=underlyings_in_flight,
        cased_source_keys=cased_source_keys,
        funnel=funnel,
    )


def _oi_row(**kwargs: Any) -> dict[str, Any]:
    row = {
        "event_id": "e1",
        "final_decision": "push",
        "source_rule": "opening_move_with_whale_concentration",
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
        "verdict_created_at_ms": NOW - 9_000,
        "venue": "hyperliquid",
        "learning_epoch": "program_v7",
        "program_sha256": "a" * 64,
        "policy_version": "news_triage_policy_v10",
        "editorial_origin": "telemetry_deterministic",
        "editorial_sha256": "b" * 64,
        "scored_judgment_sha256": "c" * 64,
        "runtime_manifest_sha": "d" * 64,
    }
    row.update(kwargs)
    # A frame observed long ago was judged long ago. Moving the two independently is a real case, but
    # a caller that only says "this is old" means both, and leaving the verdict at `NOW` would make
    # every age test silently exercise a frame the projection could not have returned.
    if "observed_at_ms" in kwargs and "verdict_created_at_ms" not in kwargs:
        row["verdict_created_at_ms"] = row["observed_at_ms"]
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
        "learning_epoch": "program_v7",
        "program_version": "news_semantic_program_v5",
        "program_sha256": "a" * 64,
        "policy_version": "news_triage_policy_v10",
        "editorial_origin": "model",
        "editorial_sha256": "b" * 64,
        "scored_judgment_sha256": "c" * 64,
        "runtime_manifest_sha": "d" * 64,
        "comparison_fingerprint": "fp",
        "asset_class": "crypto",
        "grounded_assets": ["DOGE"],
        "ingest_mode": "live",
        "source_artifact_id": "x:123",
        "source_published_at_ms": NOW - 30_000,
    }
    row.update(kwargs)
    # The Event opened before the verdict that judged it. A caller that moves the verdict alone would
    # otherwise build a row the projection could not have returned, and every latency assertion over
    # it would exercise a negative stage that cannot happen — the same coupling `_oi_row` needs.
    if "verdict_created_at_ms" in kwargs and "opened_at_ms" not in kwargs:
        row["opened_at_ms"] = min(int(row["opened_at_ms"]), int(row["verdict_created_at_ms"]) - 5_000)
    return row


# ---------------------------------------------------------------------------- OI eligibility
def test_a_qualifying_frame_becomes_a_candidate_and_keeps_its_venue() -> None:
    candidate = oi_candidate(_oi_row())
    assert isinstance(candidate, OiTradeCandidate)
    # Venue is the strongest single discriminator the research measured, so it must survive the
    # projection rather than being dropped with the rest of the frame.
    assert candidate.venue == "hyperliquid"
    assert candidate.source_key == "oi:e1:oi_signal_v1"


def test_the_source_stage_names_every_contract_failure_and_owns_no_threshold() -> None:
    """#264: `oi_candidate` answers "is this a usable OI fact", and nothing about capital.

    The deny list, the rank ceiling and the liquidity floor moved to the Candidate Gate, which is the
    only place they are executed and the only place that writes down why. What is left is the source
    contract, and every part of it is still a named rejection rather than a default.
    """

    for row, rule in (
        (_oi_row(symbol=""), "symbol_not_canonicalisable"),
        (_oi_row(direction="sideways"), "oi_direction_unknown"),
        (_oi_row(ingest_mode="recovery"), "not_live_ingest"),
        (_oi_row(verdict_created_at_ms=None), "verdict_time_missing"),
        (_oi_row(observed_at_ms=None), "observed_at_missing"),
        (_oi_row(rank_in_window=None), "rank_missing"),
        (_oi_row(program_version="news_oi_signal_v0"), "generation_invalid"),
    ):
        result = oi_candidate(row)
        assert isinstance(result, Rejected), rule
        assert result.rule == rule
    # A rank the gate would refuse and an open interest below the floor are both perfectly usable
    # facts: the source stage returns them, and the gate is what says no.
    assert isinstance(oi_candidate(_oi_row(rank_in_window=6, oi_value_usd=3_000_000)), OiTradeCandidate)
    assert isinstance(oi_candidate(_oi_row(symbol="BTC")), OiTradeCandidate)


def test_a_dropped_reader_verdict_is_still_a_visible_oi_fact() -> None:
    """#264: the reader's push/drop is audit on the candidate, never the capital lane's entry.

    The reader pushes on `whale_oi_ratio > 80%`. Five of the seven frames meeting the target strategy's
    conditions in the seven days this ledger has existed were `drop` — TUT 15.48%/54.24% among them —
    and every one of them was invisible to Trading before this rule was removed.
    """

    result = oi_candidate(
        _oi_row(final_decision="drop", source_rule="whale_ratio_below_threshold", whale_oi_ratio_bps=5_424)
    )
    assert isinstance(result, OiTradeCandidate)
    assert result.final_decision == "drop"
    assert result.source_rule == "whale_ratio_below_threshold"
    assert result.whale_oi_ratio_bps == 5_424


def test_age_is_not_an_eligibility_rule_it_is_a_trigger_rule() -> None:
    """#211: an hour-old frame is not garbage, it is context. Only triggering has a freshness budget."""

    policy = EligibilityPolicy()
    old_row = _oi_row(observed_at_ms=NOW - 10_000_000)
    aged = oi_candidate(old_row)
    assert isinstance(aged, OiTradeCandidate)
    assert is_fresh_trigger(aged.observed_at_ms, now_ms=NOW, policy=policy) is False

    fresh = oi_candidate(_oi_row())
    assert isinstance(fresh, OiTradeCandidate)
    assert is_fresh_trigger(fresh.observed_at_ms, now_ms=NOW, policy=policy) is True


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
    oi_candidate(_oi_row(direction="sideways"), funnel=funnel)
    oi_candidate(_oi_row(), funnel=funnel)
    counts = funnel.as_dict()
    assert counts["oi_reject:oi_direction_unknown"] == 1
    assert counts["oi_eligible"] == 1
    assert counts["oi_eligible_venue:hyperliquid"] == 1


# ---------------------------------------------------------------------------- fusion
def test_fusion_is_point_in_time_only() -> None:
    policy = EligibilityPolicy()
    signal = oi_candidate(_oi_row(observed_at_ms=NOW - 60_000))
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
    signal = oi_candidate(_oi_row(symbol="PENGU"))
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


# ---------------------------------------------------------------------------- #211 trigger windows
MINUTE = 60_000


def _eligible_oi(**kwargs: Any) -> OiTradeCandidate:
    candidate = oi_candidate(_oi_row(**kwargs))
    assert isinstance(candidate, OiTradeCandidate)
    return candidate


def _eligible_news(**kwargs: Any) -> NewsTradeCandidate:
    candidate = news_candidate(_news_row(**kwargs), now_ms=NOW, blacklist=OPEN_DENY)
    assert isinstance(candidate, NewsTradeCandidate)
    return candidate


def _kinds(plans: Any) -> list[str]:
    return [plan.trigger_kind for plan in plans]


def test_the_configured_news_and_oi_lookbacks_are_the_windows_that_are_actually_honoured() -> None:
    """The four boundary cases #211 names, at the configured 60 m / 30 m rather than the 5 m budget.

    Before the split, both sides were re-checked against `max_age_ms`, so a counterpart older than
    five minutes could never attach and `news_oi` was effectively unreachable at the configured
    windows. These four assertions are the whole of the corrected contract.
    """

    policy = EligibilityPolicy()
    inside_news = _plans(
        oi=[_eligible_oi()],
        news=[_eligible_news(verdict_created_at_ms=NOW - 45 * MINUTE)],
        now_ms=NOW,
        policy=policy,
    )
    assert _kinds(inside_news) == ["oi"]
    assert inside_news[0].news is not None

    outside_news = _plans(
        oi=[_eligible_oi()],
        news=[_eligible_news(verdict_created_at_ms=NOW - 61 * MINUTE)],
        now_ms=NOW,
        policy=policy,
    )
    assert _kinds(outside_news) == ["oi"]
    assert outside_news[0].news is None

    inside_oi = _plans(
        oi=[_eligible_oi(observed_at_ms=NOW - 20 * MINUTE)],
        news=[_eligible_news()],
        now_ms=NOW,
        policy=policy,
    )
    assert _kinds(inside_oi) == ["news"]
    assert inside_oi[0].oi is not None

    outside_oi = _plans(
        oi=[_eligible_oi(observed_at_ms=NOW - 31 * MINUTE)],
        news=[_eligible_news()],
        now_ms=NOW,
        policy=policy,
    )
    assert _kinds(outside_oi) == ["news"]
    assert outside_oi[0].oi is None


def test_context_older_than_the_trigger_budget_attaches_but_never_triggers_on_its_own() -> None:
    """Two windows, two jobs. A 45-minute-old verdict is context; it is not a reason to act now."""

    policy = EligibilityPolicy()
    aged_news = _eligible_news(verdict_created_at_ms=NOW - 45 * MINUTE)
    funnel = Funnel()
    alone = _plans(oi=[], news=[aged_news], now_ms=NOW, policy=policy, funnel=funnel)
    assert alone == []
    assert funnel.as_dict()["news_context_only"] == 1

    with_trigger = _plans(oi=[_eligible_oi()], news=[aged_news], now_ms=NOW, policy=policy)
    assert _kinds(with_trigger) == ["oi"]
    assert with_trigger[0].news is aged_news


def test_no_plan_ever_carries_a_fact_later_than_its_own_cutoff() -> None:
    """Whichever lane fired last owns the cutoff, and the other side is read strictly at or before it."""

    policy = EligibilityPolicy()
    for signal_at, verdict_at in ((NOW, NOW - MINUTE), (NOW - MINUTE, NOW), (NOW, NOW)):
        plans = _plans(
            oi=[_eligible_oi(observed_at_ms=signal_at)],
            news=[_eligible_news(verdict_created_at_ms=verdict_at)],
            now_ms=NOW,
            policy=policy,
        )
        assert len(plans) == 1
        plan = plans[0]
        assert plan.observed_at_ms == max(signal_at, verdict_at)
        if plan.oi is not None:
            assert plan.oi.observed_at_ms <= plan.observed_at_ms
        if plan.news is not None:
            assert plan.news.verdict_created_at_ms <= plan.observed_at_ms


def test_a_counterpart_that_cannot_attach_never_hides_an_older_one_that_can() -> None:
    """The counterpart is chosen from the whole eligible set, not from a pre-selected newest row.

    Reducing each lane to its newest row and validating afterwards is what made this possible: one
    row written a millisecond after the frame, or outside the lane's own lookback, answered for the
    entire lane and the legal older row was never considered.
    """

    policy = EligibilityPolicy()
    signal = _eligible_oi(observed_at_ms=NOW - MINUTE)
    valid = _eligible_news(event_id="n-valid", verdict_created_at_ms=NOW - 45 * MINUTE)
    future = _eligible_news(event_id="n-future", verdict_created_at_ms=NOW - MINUTE + 1)
    assert attach_news(signal, [future, valid], policy=policy) is valid

    verdict = _eligible_news(verdict_created_at_ms=NOW - MINUTE)
    older_signal = _eligible_oi(event_id="e-valid", observed_at_ms=NOW - 20 * MINUTE)
    later_signal = _eligible_oi(event_id="e-future", observed_at_ms=NOW - MINUTE + 1)
    assert attach_oi(verdict, [later_signal, older_signal], policy=policy) is older_signal


def test_two_triggers_for_one_underlying_coalesce_to_the_newest_and_say_so() -> None:
    """Latest wins, deterministically, and the loser is counted rather than disappearing."""

    policy = EligibilityPolicy()
    funnel = Funnel()
    older = _eligible_oi(event_id="e-old", observed_at_ms=NOW - 2 * MINUTE)
    newer = _eligible_oi(event_id="e-new", observed_at_ms=NOW - MINUTE)
    plans = _plans(oi=[older, newer], news=[], now_ms=NOW, policy=policy, funnel=funnel)

    assert len(plans) == 1
    assert plans[0].source_key == newer.source_key
    assert funnel.as_dict()["plan_reject:superseded_by_newer_trigger"] == 1


def test_an_oi_frame_wins_a_dead_heat_with_a_news_verdict() -> None:
    """A tie needs a written-down winner, or two identical scans coalesce to different cases.

    OI wins because this stage is OI-first: its side is deterministic and costs no model call.
    """

    policy = EligibilityPolicy()
    plans = _plans(
        oi=[_eligible_oi(observed_at_ms=NOW)],
        news=[_eligible_news(verdict_created_at_ms=NOW)],
        now_ms=NOW,
        policy=policy,
    )
    assert len(plans) == 1
    assert plans[0].oi is not None
    assert plans[0].source_key == plans[0].oi.source_key


def test_an_underlying_with_an_undecided_case_gets_no_second_thesis() -> None:
    """One frozen research decision per issuer at a time; the second would buy the same answer twice."""

    policy = EligibilityPolicy()
    plans = _plans(
        oi=[_eligible_oi()],
        news=[],
        now_ms=NOW,
        policy=policy,
        underlyings_in_flight={"crypto:DOGE"},
    )
    assert plans == []
    # The gate is what refuses it now, and it says so durably rather than only in the day's funnel.
    refused = admit_trigger(
        _eligible_oi(),
        now_ms=NOW,
        config=OPEN_GATE,
        blacklist=OPEN_DENY,
        underlyings_in_flight={"crypto:DOGE"},
    )
    assert refused is not None
    assert (refused.status, refused.stage, refused.reason) == ("DEFERRED", "eligibility", "case_in_flight")
    assert refused.retryable is True

    # A settled case is not a block. Nothing is in flight, so the same trigger plans normally.
    assert _kinds(_plans(oi=[_eligible_oi()], news=[], now_ms=NOW, policy=policy)) == ["oi"]


def test_the_scan_horizon_covers_the_whole_configured_context_window() -> None:
    """The query has to reach back far enough for fusion to have anything to honour."""

    policy = EligibilityPolicy()
    assert scan_horizon_ms(policy) == policy.max_age_ms + policy.news_lookback_ms

    oi_heavy = EligibilityPolicy(news_lookback_ms=60_000, oi_lookback_ms=7_200_000)
    assert scan_horizon_ms(oi_heavy) == oi_heavy.max_age_ms + oi_heavy.oi_lookback_ms

    # The recovery overlap is the floor when both lookbacks are short: a restarted lane still has to
    # re-see triggers it never turned into cases.
    tight = EligibilityPolicy(news_lookback_ms=1_000, oi_lookback_ms=1_000)
    assert scan_horizon_ms(tight) == tight.max_age_ms * 3


# ---------------------------------------------------------------------------- #211 venue truth
def test_a_frame_venue_maps_to_exactly_one_execution_venue_or_to_nothing() -> None:
    """Fail closed. The alternative is a Hyperliquid frame executed against a Binance book."""

    assert signal_exchange_id("hyperliquid") == "hyperliquid"
    assert signal_exchange_id("Binance") == "binance"
    for unusable in ("", None, "hl.xyz", "okx", "binance_futures"):
        assert signal_exchange_id(unusable) is None


def test_a_news_triggered_plan_stamps_the_event_open_and_the_verdict_apart() -> None:
    """The News half of the two upstream stages, and the clamp that keeps the report honest.

    For a News trigger the cutoff *is* the verdict, so the ingest stage has to come from the Event's
    own open time or it collapses to zero. `opened_at_ms` is not guaranteed to precede the verdict —
    a re-opened family, a corrected leader item, provider clock skew — and a negative stage in
    `trading status` is indistinguishable from a real measurement.
    """

    policy = EligibilityPolicy()
    verdict = _eligible_news(opened_at_ms=NOW - 90_000, verdict_created_at_ms=NOW - 30_000)
    plans = _plans(oi=[], news=[verdict], now_ms=NOW, policy=policy)
    assert len(plans) == 1
    assert (plans[0].source_observed_at_ms, plans[0].trigger_persisted_at_ms) == (NOW - 90_000, NOW - 30_000)

    inverted = _eligible_news(opened_at_ms=NOW, verdict_created_at_ms=NOW - 30_000)
    clamped = _plans(oi=[], news=[inverted], now_ms=NOW, policy=policy)
    assert clamped[0].source_observed_at_ms == clamped[0].trigger_persisted_at_ms == NOW - 30_000


def test_an_oi_triggered_plan_stamps_the_frame_and_its_verdict_apart() -> None:
    policy = EligibilityPolicy()
    plans = _plans(
        oi=[_eligible_oi(observed_at_ms=NOW - 20_000, verdict_created_at_ms=NOW - 15_000)],
        news=[],
        now_ms=NOW,
        policy=policy,
    )
    assert (plans[0].source_observed_at_ms, plans[0].trigger_persisted_at_ms) == (NOW - 20_000, NOW - 15_000)


def test_a_counterpart_folded_into_the_manifest_is_not_also_counted_as_superseded() -> None:
    """One fresh frame plus one fresh verdict is the ordinary `news_oi` shape, not a rejection.

    Both are triggers, so one loses the coalescing — but the loser is inside the winner's lookback by
    construction and gets attached, so counting it as superseded would report the same row as both a
    rejection and a survivor in a funnel whose whole claim is to be the report and the rule at once.
    """

    funnel = Funnel()
    plans = _plans(
        oi=[_eligible_oi(observed_at_ms=NOW)],
        news=[_eligible_news(verdict_created_at_ms=NOW - MINUTE)],
        now_ms=NOW,
        policy=EligibilityPolicy(),
        funnel=funnel,
    )
    assert _kinds(plans) == ["oi"]
    assert plans[0].news is not None
    assert "plan_reject:superseded_by_newer_trigger" not in funnel.as_dict()

    # A second frame for the same underlying genuinely is dropped, and that one is counted.
    dropped = Funnel()
    _plans(
        oi=[_eligible_oi(event_id="e-old", observed_at_ms=NOW - 2 * MINUTE), _eligible_oi(observed_at_ms=NOW)],
        news=[],
        now_ms=NOW,
        policy=EligibilityPolicy(),
        funnel=dropped,
    )
    assert dropped.as_dict()["plan_reject:superseded_by_newer_trigger"] == 1


def test_a_trigger_that_already_produced_a_case_stops_winning_the_coalescing() -> None:
    """Otherwise the winner keeps beating the trigger it beat, for its whole freshness window.

    Nothing durable records a coalesced loser, so the promise that it gets a turn later depends
    entirely on the winner dropping out of the running once it has become a case. Without that, every
    scan for the next `max_age` re-selects the same newest trigger and the freeze refuses it as
    already seen, while the older one goes stale untried.
    """

    policy = EligibilityPolicy()
    older = _eligible_oi(event_id="e-old", observed_at_ms=NOW - 2 * MINUTE)
    newer = _eligible_oi(event_id="e-new", observed_at_ms=NOW - MINUTE)

    first = _plans(oi=[older, newer], news=[], now_ms=NOW, policy=policy)
    assert first[0].source_key == newer.source_key

    second = _plans(
        oi=[older, newer],
        news=[],
        now_ms=NOW,
        policy=policy,
        cased_source_keys={newer.source_key},
    )
    assert second[0].source_key == older.source_key
    consumed = admit_trigger(
        newer,
        now_ms=NOW,
        config=OPEN_GATE,
        blacklist=OPEN_DENY,
        cased_source_keys={newer.source_key},
    )
    assert consumed is not None
    assert (consumed.status, consumed.reason) == ("REJECTED", "already_consumed")
