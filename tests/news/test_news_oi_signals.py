"""Open-interest telemetry lane: parsing, basis points, and the rank rule.

The frames in these fixtures are verbatim production titles from 2026-08-22, including the two
single-character tickers (`S`, `4`) and the prose sentence about open interest that must NOT parse.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tracefold.news.oi_signals import (
    DEFAULT_OI_POLICY,
    WINDOW_MS,
    OiPolicy,
    OiSignal,
    evaluate_oi,
    parse_oi_signal,
)
from tracefold.news.similarity import similarity
from tracefold.news.triage_rules import DEFAULT_POLICY, GateFacts, decide, storyline_status

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


def test_rank_counts_every_frame_in_a_sliding_window() -> None:
    """The window slides with the reader rather than resetting on a clock boundary, and a frame the
    policy dropped still happened — it still moves the next one further down the run."""

    now = 10 * WINDOW_MS
    first = evaluate_oi(_signal(), earlier_at_ms=[], now_ms=now)
    assert (first.rule, first.rank_in_window) == ("opening_move_with_whale_concentration", 1)
    assert first.verdict.decision == "push"

    second = evaluate_oi(_signal(), earlier_at_ms=[now - 60_000], now_ms=now)
    assert second.verdict.decision == "push" and second.rank_in_window == 2

    third = evaluate_oi(_signal(), earlier_at_ms=[now - 60_000, now - 120_000], now_ms=now)
    assert third.rule == "beyond_window_rank" and third.rank_in_window == 3
    assert third.verdict.decision == "drop"

    # The same three frames, but the earlier two have aged out of the window.
    aged = evaluate_oi(_signal(), earlier_at_ms=[now - WINDOW_MS - 1, now - WINDOW_MS - 2], now_ms=now)
    assert aged.verdict.decision == "push" and aged.rank_in_window == 1


def test_whale_concentration_is_a_strict_threshold() -> None:
    at_threshold = evaluate_oi(_signal(whale_oi_ratio_bps=8_000), earlier_at_ms=[], now_ms=0)
    assert at_threshold.rule == "whale_ratio_below_threshold" and at_threshold.verdict.decision == "drop"
    just_over = evaluate_oi(_signal(whale_oi_ratio_bps=8_001), earlier_at_ms=[], now_ms=0)
    assert just_over.verdict.decision == "push"


def test_every_threshold_is_operator_owned() -> None:
    quiet = _signal(whale_oi_ratio_bps=3_000)
    assert evaluate_oi(quiet, earlier_at_ms=[], now_ms=0).verdict.decision == "drop"
    loud = evaluate_oi(quiet, earlier_at_ms=[], now_ms=0, policy=OiPolicy(whale_oi_ratio_above_bps=0))
    assert loud.verdict.decision == "push"
    crowded = [1, 2, 3, 4]
    assert evaluate_oi(_signal(), earlier_at_ms=crowded, now_ms=5).verdict.decision == "drop"
    wide = evaluate_oi(_signal(), earlier_at_ms=crowded, now_ms=5, policy=OiPolicy(max_rank_in_window=9))
    assert wide.verdict.decision == "push"


_FACTS = GateFacts(
    grounded_assets=(),
    watchlist_symbols=frozenset(),
    provider_score=None,
    priority="normal",
    admission="telemetry_deterministic",
)


def test_the_verdict_reaches_the_intended_answer_through_decide_unchanged() -> None:
    """The point of speaking `TriageVerdict` instead of a private decision type.

    A qualifying frame has to arrive at `push` through an ordinary rule, and a rejected one has to be
    held by policy v8's `noise` veto — which only fires when the verdict agrees with itself. If either
    stopped being true, this lane would need its own decision plane, which is exactly what it exists
    not to have.
    """

    qualifying = evaluate_oi(_signal(), earlier_at_ms=[], now_ms=0).verdict
    pushed = decide(qualifying, _FACTS, None)
    assert (pushed.final, pushed.override_rule) == ("push", "model_push_actionable")

    rejected = evaluate_oi(_signal(whale_oi_ratio_bps=3_000), earlier_at_ms=[], now_ms=0).verdict
    held = decide(rejected, _FACTS, None)
    assert (held.final, held.override_rule) == ("drop", "noise")


def test_reader_text_carries_the_numbers_and_the_position_in_the_run() -> None:
    """No mechanism sentence: the frame carries four numbers and no causality to state."""

    verdict = evaluate_oi(_signal(), earlier_at_ms=[], now_ms=0).verdict
    assert verdict.headline_zh == "▲ TRUMP 持仓异动 4.55%"
    assert verdict.why_zh == "持仓 3217 万 · 鲸鱼占比 100.7% · 鲸鱼多头盈利 80.2% · 4h 内第 1 次"
    assert "AI" not in verdict.why_zh and "模型" not in verdict.why_zh


def test_two_symbols_are_not_duplicates_of_each_other() -> None:
    """Every telemetry headline is one template, so bigram similarity reads two unrelated symbols as a
    repeat. Measured: `▲ BTC 持仓异动 4.55%` against `▲ ETH 持仓异动 4.51%` scores 0.33 against the 0.25
    threshold, and with a seven-card ledger 78% of qualifying frames would be withheld as duplicates of
    a card about something else.

    An earlier version of this test asserted the opposite from one favourable pair — TRUMP against
    PENGU with far apart numbers scores 0.12 — and the claim did not generalise. Short symbols are the
    common case and they are the ones that collide, so the fixture uses them.
    """

    first = evaluate_oi(_signal(symbol="BTC"), earlier_at_ms=[], now_ms=0).verdict
    second = evaluate_oi(_signal(symbol="ETH", oi_change_bps=451), earlier_at_ms=[], now_ms=0).verdict
    assert similarity(first.headline_zh, second.headline_zh) >= DEFAULT_POLICY_SIMILARITY

    told = [{"dir": "bullish", "headline_zh": first.headline_zh, "assets": [{"symbol": "BTC", "role": "primary"}]}]
    status = storyline_status("asset:BTC", told=told)
    facts = replace(_FACTS, grounded_assets=())
    other = decide(second, facts, status)
    assert other.final == "push", "a card about ETH is not a duplicate of a card about BTC"

    # The same symbol still is one, and the trace names the ledger entry it matched.
    repeat_verdict = evaluate_oi(_signal(symbol="BTC", oi_change_bps=460), earlier_at_ms=[], now_ms=0).verdict
    repeat = decide(repeat_verdict, facts, status)
    assert repeat.final == "throttled" and repeat.throttled_by == "storyline:asset:BTC:seen"


def test_the_exemption_needs_the_gate_admission_not_the_shape_of_the_text() -> None:
    """A frame that merely looks like telemetry gets no exemption: the admission is Gate-derived from
    the provider's strategy id, and the text is not evidence of anything."""

    first = evaluate_oi(_signal(symbol="BTC"), earlier_at_ms=[], now_ms=0).verdict
    second = evaluate_oi(_signal(symbol="ETH", oi_change_bps=451), earlier_at_ms=[], now_ms=0).verdict
    told = [{"dir": "bullish", "headline_zh": first.headline_zh, "assets": [{"symbol": "BTC", "role": "primary"}]}]
    status = storyline_status("asset:BTC", told=told)
    unadmitted = replace(_FACTS, admission="candidate", grounded_assets=())
    assert decide(second, unadmitted, status).final == "throttled"


def test_default_policy_is_the_measured_one() -> None:
    """The shipped defaults are what the 24 h replay was measured against: 40 pushes a day out of 190
    frames. Changing one without re-measuring makes that number a lie."""

    assert DEFAULT_OI_POLICY.as_dict() == {
        "window_ms": 4 * 3_600_000,
        "max_rank_in_window": 2,
        "whale_oi_ratio_above_bps": 8_000,
        "oi_change_at_least_bps": 0,
    }
