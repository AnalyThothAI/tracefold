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
    WINDOW_MS,
    OiPolicy,
    OiSignal,
    evaluate_oi,
    parse_oi_signal,
)
from tracefold.news.similarity import similarity
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

    qualifying = evaluate_oi(_signal(), earlier_at_ms=[], now_ms=0).verdict
    pushed = decide(qualifying, _FACTS, None)
    assert (pushed.final, pushed.override_rule) == ("push", "telemetry_deterministic")

    rejected = evaluate_oi(_signal(whale_oi_ratio_bps=3_000), earlier_at_ms=[], now_ms=0).verdict
    held = decide(rejected, _FACTS, None)
    assert (held.final, held.override_rule) == ("drop", "telemetry_deterministic")


def test_reader_text_carries_the_numbers_and_the_position_in_the_run() -> None:
    """The deterministic numbers form one complete title, with no duplicate body sentence."""

    verdict = evaluate_oi(_signal(), earlier_at_ms=[], now_ms=0).verdict
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
        earlier_at_ms=[1],
        now_ms=2,
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
        earlier_at_ms=[1],
        now_ms=2,
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
    first = evaluate_oi(_signal(symbol="BTC"), earlier_at_ms=[], now_ms=0).verdict
    told = [{"dir": "bullish", "headline_zh": first.headline_zh, "assets": [{"symbol": "BTC", "role": "primary"}]}]
    status = storyline_status("asset:BTC", told=told)

    other_symbol = evaluate_oi(_signal(symbol="ETH", oi_change_bps=451), earlier_at_ms=[], now_ms=0).verdict
    assert similarity(first.headline_zh, other_symbol.headline_zh) >= DEFAULT_POLICY_SIMILARITY
    assert decide(other_symbol, facts, status).final == "push"

    second = evaluate_oi(_signal(symbol="BTC", oi_change_bps=620), earlier_at_ms=[1], now_ms=2)
    assert similarity(first.headline_zh, second.verdict.headline_zh) >= DEFAULT_POLICY_SIMILARITY
    assert second.rank_in_window == 2
    assert decide(second.verdict, facts, status).final == "push", "the reader asked for the first two"

    third = evaluate_oi(_signal(symbol="BTC", oi_change_bps=700), earlier_at_ms=[1, 2], now_ms=3)
    held = decide(third.verdict, facts, status)
    assert third.rank_in_window == 3
    assert held.final == "drop" and held.override_rule == "telemetry_deterministic", (
        "the arithmetic rank ceiling is what stops the run"
    )


def test_only_the_gate_admission_earns_the_exemption() -> None:
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
