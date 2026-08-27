"""Open-interest telemetry lane: parsing, basis points, and the rank rule.

The frames in these fixtures are verbatim production titles from 2026-08-22, including the two
single-character tickers (`S`, `4`) and the prose sentence about open interest that must NOT parse.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.support.news_judgment import scored_judgment
from tracefold.news.oi_signals import (
    DEFAULT_OI_POLICY,
    OiPolicy,
    OiSignal,
    evaluate_oi,
    oi_judgment_trace,
    oi_parse_failure,
    parse_oi_signal,
)
from tracefold.news.similarity import similarity
from tracefold.news.storage.feed import OI_FILTERS, OI_OUTCOMES, _oi_summary
from tracefold.news.triage_rules import DEFAULT_POLICY, GateFacts, storyline_status
from tracefold.news.triage_rules import decide as production_decide

DEFAULT_POLICY_SIMILARITY = DEFAULT_POLICY.similarity_max

FRAME = "TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"


def _signal(**over: object) -> OiSignal:
    base = dict(
        symbol="TRUMP",
        direction="rise",
        oi_change_bps=455,
        oi_value_usd=32_170_000,
        whale_long_profit_bps=8021,
        whale_oi_ratio_bps=10_071,
    )
    base.update(over)
    return OiSignal(**base)  # type: ignore[arg-type]


def test_parses_the_production_template_into_exact_basis_points() -> None:
    signal = parse_oi_signal(FRAME)
    assert signal == _signal()


@pytest.mark.parametrize(("wire", "direction"), [("Rise", "rise"), ("Fall", "fall"), ("Drop", "fall")])
def test_production_direction_words_keep_their_domain_direction(wire: str, direction: str) -> None:
    signal = parse_oi_signal(FRAME.replace("Rise", wire))
    assert signal is not None and signal.direction == direction


@pytest.mark.parametrize(
    ("percent", "bps"),
    [("4.55", 455), ("100.71", 10_071), ("0.5", 50), ("1438.2", 143_820), ("-3.5", -350)],
)
def test_percentages_are_exact_integers_not_floats(percent: str, bps: int) -> None:
    """These numbers key a stored read model and a threshold comparison; 0.1 + 0.2 must not appear."""

    frame = f"BTC OI Rise {percent}%, OI Value 1M, Whale Long Profit 1%, Whale/OI Ratio 1%"
    signal = parse_oi_signal(frame)
    assert signal is not None and signal.oi_change_bps == bps


def test_the_symbol_is_the_titles_own_subject_normalized() -> None:
    """The title's leading token decides, and the `XYZ-` prefix is stripped the way every other
    consumer of provider coin tags strips it.

    A provider tag is not preferred over it: the two agree in this feed, and when they did not, the
    tag would key the row and the card header to an asset the frame is not about. Provider tags are
    also unbounded where `TriageAsset.symbol` is capped at 16 characters.
    """

    assert parse_oi_signal(FRAME) == _signal()
    prefixed = parse_oi_signal("XYZ-UNITREE OI Rise 1%, OI Value 1M, Whale Long Profit 1%, Whale/OI Ratio 1%")
    assert prefixed is not None and prefixed.symbol == "UNITREE"
    single = parse_oi_signal("S OI Rise 3.04%, OI Value 3.86M, Whale Long Profit 92.31%, Whale/OI Ratio 31.42%")
    assert single is not None and single.symbol == "S"


@pytest.mark.parametrize(
    "title",
    [
        "HIP-3 has lost $820M in open interest over the past 5 days, down from its peak of $4.57B.",
        "Bitcoin open interest hits a record",
        "TRUMP OI Rise 4.55%, OI Value 32.17M",
        "",
    ],
)
def test_anything_that_is_not_the_template_is_not_a_signal(title: str) -> None:
    """Prose *about* open interest carries no numbers this lane can act on, and must reach nothing."""

    assert parse_oi_signal(title) is None


def test_units_scale_to_whole_dollars() -> None:
    for unit, expected in (("K", 3_860), ("M", 3_860_000), ("B", 3_860_000_000)):
        frame = f"BTC OI Rise 1%, OI Value 3.86{unit}, Whale Long Profit 1%, Whale/OI Ratio 1%"
        signal = parse_oi_signal(frame)
        assert signal is not None and signal.oi_value_usd == expected


def test_a_provider_number_that_cannot_fit_the_ledger_is_not_a_signal() -> None:
    too_large = "9" * 100
    frame = f"BTC OI Rise {too_large}%, OI Value 1M, Whale Long Profit 1%, Whale/OI Ratio 1%"
    assert parse_oi_signal(frame) is None


def test_rank_counts_only_eligible_signals_in_the_sliding_window() -> None:
    first = evaluate_oi(_signal(), earlier_eligible_count=0)
    assert (first.rule, first.rank_in_window) == ("opening_move_with_whale_concentration", 1)
    assert first.verdict.decision == "push"

    second = evaluate_oi(_signal(), earlier_eligible_count=1)
    assert second.verdict.decision == "push" and second.rank_in_window == 2

    third = evaluate_oi(_signal(), earlier_eligible_count=2)
    assert third.rule == "beyond_window_rank" and third.rank_in_window == 3
    assert third.verdict.decision == "drop"


def test_ineligible_frames_between_eligible_signals_do_not_consume_rank() -> None:
    eligible_count = 0
    outcomes: list[tuple[int, str]] = []
    for ratio_bps in (4_000, 9_100, 6_000, 9_200):
        judgment = evaluate_oi(_signal(whale_oi_ratio_bps=ratio_bps), earlier_eligible_count=eligible_count)
        outcomes.append((judgment.rank_in_window, judgment.verdict.decision))
        eligible_count += int(judgment.rule == "opening_move_with_whale_concentration")

    assert outcomes == [(1, "drop"), (1, "push"), (2, "drop"), (2, "push")]


def test_whale_concentration_is_a_strict_threshold() -> None:
    at_threshold = evaluate_oi(_signal(whale_oi_ratio_bps=8_000), earlier_eligible_count=0)
    assert at_threshold.rule == "whale_ratio_below_threshold" and at_threshold.verdict.decision == "drop"
    just_over = evaluate_oi(_signal(whale_oi_ratio_bps=8_001), earlier_eligible_count=0)
    assert (just_over.rank_in_window, just_over.verdict.decision) == (1, "push")


def test_every_threshold_is_operator_owned() -> None:
    quiet = _signal(whale_oi_ratio_bps=3_000)
    assert evaluate_oi(quiet, earlier_eligible_count=0).verdict.decision == "drop"
    loud = evaluate_oi(quiet, earlier_eligible_count=0, policy=OiPolicy(whale_oi_ratio_above_bps=0))
    assert loud.verdict.decision == "push"
    assert evaluate_oi(_signal(), earlier_eligible_count=4).verdict.decision == "drop"
    wide = evaluate_oi(_signal(), earlier_eligible_count=4, policy=OiPolicy(max_rank_in_window=9))
    assert wide.verdict.decision == "push"


_FACTS = GateFacts(
    grounded_assets=(),
    watchlist_symbols=frozenset(),
    admission="telemetry_deterministic",
)


def decide(verdict: object, facts: GateFacts, status: object) -> object:
    origin = "telemetry_deterministic" if facts.admission == "telemetry_deterministic" else "model"
    return production_decide(
        scored_judgment(verdict, editorial_origin=origin),  # type: ignore[arg-type]
        facts,
        status,  # type: ignore[arg-type]
    )


def test_the_verdict_reaches_the_intended_answer_through_decide_unchanged() -> None:
    """The point of speaking `TriageVerdict` instead of a private decision type.

    A qualifying frame has to arrive at `push` through an ordinary rule, and a rejected one has to be
    retain its arithmetic push/drop intent under the deterministic admission. If either stopped being true,
    this lane would need its own decision plane, which is exactly what it exists not to have.
    """

    qualifying = evaluate_oi(_signal(), earlier_eligible_count=0).verdict
    pushed = decide(qualifying, _FACTS, None)
    assert (pushed.final, pushed.override_rule) == ("push", "telemetry_deterministic")

    rejected = evaluate_oi(_signal(whale_oi_ratio_bps=3_000), earlier_eligible_count=0).verdict
    held = decide(rejected, _FACTS, None)
    assert (held.final, held.override_rule) == ("drop", "telemetry_deterministic")


def test_reader_text_carries_the_numbers_and_the_position_in_the_run() -> None:
    """The deterministic numbers form one complete title, with no duplicate body sentence."""

    verdict = evaluate_oi(_signal(), earlier_eligible_count=0).verdict
    assert verdict.headline_zh == ("▲ TRUMP 持仓异动4.55%｜持仓3217万｜鲸鱼占比100.7%｜鲸鱼多头盈利80.2%｜4h内第1次")
    assert verdict.why_zh == ""
    assert len(verdict.headline_zh) <= 60
    assert "AI" not in verdict.headline_zh and "模型" not in verdict.headline_zh


def test_reader_title_compacts_long_symbols_without_losing_any_measurement() -> None:
    verdict = evaluate_oi(
        _signal(
            symbol="SIXTEENCHARACTRS",
            oi_change_bps=143_820,
            oi_value_usd=3_860_000_000,
            whale_oi_ratio_bps=21097,
            whale_long_profit_bps=8060,
        ),
        earlier_eligible_count=1,
    ).verdict

    assert len(verdict.headline_zh) <= 60
    for fact in ("SIXTEENCHARACTRS", "1438.20%", "38.60亿", "211.0%", "80.6%", "4h#2"):
        assert fact in verdict.headline_zh


def test_reader_title_is_bounded_at_the_bigint_storage_limit() -> None:
    maximum = 2**63 - 1
    verdict = evaluate_oi(
        _signal(
            symbol="SIXTEENCHARACTRS",
            oi_change_bps=maximum,
            oi_value_usd=maximum,
            whale_oi_ratio_bps=maximum,
            whale_long_profit_bps=-maximum,
        ),
        earlier_eligible_count=1,
    ).verdict

    assert len(verdict.headline_zh) <= 60
    for fact in ("SIXTEENCHARACTRS", "Δ9e16%", "O9e18", "W9e16%", "P-9e16%", "4h#2"):
        assert fact in verdict.headline_zh


def test_repeats_are_bounded_by_the_rank_ceiling_not_by_content_similarity() -> None:
    """The rank ceiling *is* this lane's duplicate protection, and running the content check as well
    would silently halve it.

    Every telemetry headline is one template, so bigram similarity reads unrelated frames as repeats:
    two symbols score 0.33 and two frames for one symbol score 0.41, both above the 0.25 threshold.
    `WINDOW_MS` and `TOLD_WINDOW_MS` are both 4 h, so a rank-2 frame is *always* inside its rank-1
    sibling's ledger — "the first two per symbol" would have shipped as "one per symbol", and the
    40-a-day measurement would not have held.

    An earlier version of this test asserted the throttle instead, from a fixture that happened to
    agree with it. Two frames for one symbol are two different observations, and the reader asked for
    the opening ones by count.
    """

    facts = replace(_FACTS, grounded_assets=())
    first = evaluate_oi(_signal(symbol="BTC"), earlier_eligible_count=0).verdict
    told = [{"dir": "bullish", "headline_zh": first.headline_zh, "assets": [{"symbol": "BTC", "role": "primary"}]}]
    status = storyline_status("asset:BTC", told=told)

    other_symbol = evaluate_oi(_signal(symbol="ETH", oi_change_bps=451), earlier_eligible_count=0).verdict
    assert similarity(first.headline_zh, other_symbol.headline_zh) >= DEFAULT_POLICY_SIMILARITY
    assert decide(other_symbol, facts, status).final == "push"

    second = evaluate_oi(_signal(symbol="BTC", oi_change_bps=620), earlier_eligible_count=1)
    assert similarity(first.headline_zh, second.verdict.headline_zh) >= DEFAULT_POLICY_SIMILARITY
    assert second.rank_in_window == 2
    assert decide(second.verdict, facts, status).final == "push", "the reader asked for the first two"

    third = evaluate_oi(_signal(symbol="BTC", oi_change_bps=700), earlier_eligible_count=2)
    held = decide(third.verdict, facts, status)
    assert third.rank_in_window == 3
    assert held.final == "drop" and held.override_rule == "telemetry_deterministic", (
        "the arithmetic rank ceiling is what stops the run"
    )


def test_only_the_gate_admission_earns_the_exemption() -> None:
    """A frame that merely looks like telemetry gets no exemption: the admission is Gate-derived from
    the provider's strategy id, and the text is not evidence of anything."""

    first = evaluate_oi(_signal(symbol="BTC"), earlier_eligible_count=0).verdict
    second = evaluate_oi(_signal(symbol="ETH", oi_change_bps=451), earlier_eligible_count=0).verdict
    told = [{"dir": "bullish", "headline_zh": first.headline_zh, "assets": [{"symbol": "BTC", "role": "primary"}]}]
    status = storyline_status("asset:BTC", told=told)
    unadmitted = replace(_FACTS, admission="candidate", grounded_assets=())
    assert decide(second, unadmitted, status).final == "throttled"


def test_default_policy_keeps_the_shipped_thresholds() -> None:

    assert DEFAULT_OI_POLICY.as_dict() == {
        "window_ms": 4 * 3_600_000,
        "max_rank_in_window": 2,
        "whale_oi_ratio_above_bps": 8_000,
        "oi_change_at_least_bps": 0,
    }


# ---------------------------------------------------------------- #207: what the 持仓异动 monitor reads


def test_the_feed_folds_the_judge_trace_back_without_reparsing_the_title() -> None:
    """The console's `oi` block is `oi_judgment_trace()` read back, never a second parse.

    Every field has to survive the round trip, because the browser is not allowed to re-derive any of
    them from `leader_title`: a second copy of `oi_signal_parser_v1` in the page would drift from the
    judge the moment either side changed.
    """

    judgment = evaluate_oi(_signal(), earlier_eligible_count=0)
    summary = _oi_summary(oi_judgment_trace(judgment, policy=DEFAULT_OI_POLICY))

    assert summary is not None
    assert summary["parsed"] is True
    assert summary["rule"] == "opening_move_with_whale_concentration"
    # The frame's parsed subject comes from the judgment trace. Public `assets` is a later projection of the
    # durable Event-asset ledger; it does not replace this structured OI fact or justify parsing the title again.
    assert summary["symbol"] == judgment.signal.symbol
    assert summary["oi_change_bps"] == judgment.signal.oi_change_bps
    assert summary["oi_value_usd"] == judgment.signal.oi_value_usd
    assert summary["whale_long_profit_bps"] == judgment.signal.whale_long_profit_bps
    assert summary["whale_oi_ratio_bps"] == judgment.signal.whale_oi_ratio_bps
    assert summary["eligible_rank_in_window"] == 1
    assert summary["rank_semantics"] == "eligible_rank_v1"
    # The thresholds this frame ran under, from its own trace: retuning `news.oi` must not rewrite the
    # history of a decision it did not make.
    assert summary["whale_oi_ratio_above_bps"] == DEFAULT_OI_POLICY.whale_oi_ratio_above_bps
    assert summary["max_rank_in_window"] == DEFAULT_OI_POLICY.max_rank_in_window
    assert summary["window_ms"] == DEFAULT_OI_POLICY.window_ms
    # The provider's identity stays behind: the console does not display Strategy IDs.
    assert {"strategy_id", "provider", "provider_source"}.isdisjoint(summary)


def test_an_unparseable_frame_folds_to_its_failure_shape_and_no_measurements() -> None:
    _, trace = oi_parse_failure("PENGU OI Rise 3.4%, OI Value --", provider_source="x")
    summary = _oi_summary(trace)

    assert summary is not None
    assert summary["parsed"] is False
    assert summary["rule"] == "oi_parse_failed"
    assert summary["failure_stage"] == "template_match"
    assert summary["parser_version"] == "oi_signal_parser_v1"
    assert summary["title_sha256"]
    # Nothing was measured, so nothing is reported — including the symbol, which the template never
    # matched. A zero or an empty string here would read as a real reading.
    assert summary["symbol"] is None
    for key in ("oi_change_bps", "oi_value_usd", "whale_long_profit_bps", "whale_oi_ratio_bps"):
        assert summary[key] is None
    assert {"strategy_id", "provider", "provider_source"}.isdisjoint(summary)


def test_a_row_from_any_other_admission_carries_no_oi_block() -> None:
    assert _oi_summary(None) is None


def test_the_feed_oi_filter_groups_exactly_the_rules_the_judge_can_write() -> None:
    """The monitor's tabs are the judge's rules, grouped — the console owns no vocabulary of its own.

    A rule `evaluate_oi` can produce that no group covers would be silently unreachable from every tab,
    which is how a gate stops being visible without anyone noticing.
    """

    produced = {
        evaluate_oi(_signal(whale_oi_ratio_bps=100), earlier_eligible_count=0).rule,
        evaluate_oi(_signal(), earlier_eligible_count=0).rule,
        evaluate_oi(_signal(), earlier_eligible_count=9).rule,
        evaluate_oi(
            _signal(oi_change_bps=1),
            earlier_eligible_count=0,
            policy=OiPolicy(oi_change_at_least_bps=500),
        ).rule,
        "oi_parse_failed",
    }
    grouped = [rule for rules in OI_FILTERS.values() for rule in rules]
    assert produced == set(grouped)
    # The groups partition the rules: no rule may appear under two tabs.
    assert len(grouped) == len(set(grouped))


def test_the_monitors_unfiltered_tab_is_a_value_the_caller_sends_not_an_omission() -> None:
    """`all` is an accepted `oi` value that narrows nothing, and that is the point.

    Omitting the parameter is indistinguishable from any other feed request, so the server would keep
    paying for the outcome-group aggregate on the tab the monitor displays most — a count describing the
    feed's task tabs, which this page does not have and never reads.
    """

    assert "all" in OI_OUTCOMES
    assert "all" not in OI_FILTERS
    assert set(OI_OUTCOMES) == {"all"} | set(OI_FILTERS)
