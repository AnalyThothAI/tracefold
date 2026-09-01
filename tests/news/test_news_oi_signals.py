"""Open-interest telemetry lane: parsing, basis points, and the one outcome a frame can have.

The frames in these fixtures are verbatim production titles from 2026-08-22, including the two
single-character tickers (`S`, `4`) and the prose sentence about open interest that must NOT parse.

#458 removed this lane's notification rule. What used to be tested here — a whale-concentration
threshold, an opening-rank ceiling, an OI-move floor, and the push each of them gated — no longer
exists, so those cases are not retuned versions of themselves: they are replaced by the assertion
that no measurement changes the answer.
"""

from __future__ import annotations

import pytest

from tracefold.news.oi_contracts import OI_FILTERS, OI_OUTCOMES
from tracefold.news.oi_contracts import OI_STORED_RULE as STORED_RULE
from tracefold.news.oi_signals import (
    PROGRAM_VERSION,
    READER_CONTRACT_VERSION,
    OiSignal,
    evaluate_oi,
    oi_judgment_trace,
    oi_parse_failure,
    parse_oi_signal,
)
from tracefold.news.storage.feed import _oi_summary

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


def test_every_parsed_frame_is_stored_and_none_is_pushed() -> None:
    """One rule, one decision, whatever the four numbers say.

    The values swept here are exactly the ones the retired rule discriminated on: a whale ratio under
    and over the old 80% threshold, a near-zero OI move, and a repeat of a symbol that would have been
    the third in its window. Each used to produce a different rule and two of them a push.
    """

    for signal in (
        _signal(),
        _signal(whale_oi_ratio_bps=100),
        _signal(whale_oi_ratio_bps=8_000),
        _signal(whale_oi_ratio_bps=8_001),
        _signal(oi_change_bps=1),
        _signal(oi_change_bps=-455, direction="fall"),
    ):
        judgment = evaluate_oi(signal)
        assert judgment.rule == STORED_RULE
        assert (judgment.decision.final, judgment.decision.rule_baseline) == ("drop", "drop")
        assert judgment.decision.override_rule == STORED_RULE
        assert judgment.decision.throttled_by is None
        assert judgment.verdict.magnitude == 0


def test_the_frame_is_still_a_complete_directional_presentation() -> None:
    """Storing is not discarding: the verdict keeps the subject, the direction and the measurements.

    A magnitude-0 verdict is the lane saying "not worth interrupting a human for", not "nothing
    happened" — the frame table renders this row and Trading's candidate projection reads it.
    """

    rise = evaluate_oi(_signal()).verdict
    assert rise.direction == "bullish"
    assert [(asset.symbol, asset.role) for asset in rise.assets] == [("TRUMP", "primary")]
    fall = evaluate_oi(_signal(direction="fall")).verdict
    assert fall.direction == "bearish"
    # Retired News contract fields never appear on it, and the verdict never copies the action.
    assert {"event_type", "actionable", "decision", "title_zh"}.isdisjoint(rise.model_dump(mode="json"))


def test_the_judgment_carries_no_rank_and_no_thresholds() -> None:
    """The atom is content-addressed, so a stray field would change every frame's identity.

    `rank_in_window` is gone from it and `policy` is gone from the trace beside it. Both were the
    retired rule's, and the database's own judgment CHECK names the atom's keys exactly.
    """

    judgment = evaluate_oi(_signal())
    assert set(judgment.judgment_atom) == {
        "judgment_contract_version",
        "origin",
        "verdict",
        "signal",
        "rule",
        "decision",
    }
    assert "policy" not in oi_judgment_trace(judgment)
    assert "rank_semantics" not in oi_judgment_trace(judgment)
    # Same frame, same identity: the judge takes no argument that could vary between two runs.
    assert judgment.judgment_sha256 == evaluate_oi(_signal()).judgment_sha256


def test_reader_text_carries_the_four_measurements_and_no_position_in_a_run() -> None:
    """The deterministic numbers form one complete title, with no duplicate body sentence."""

    verdict = evaluate_oi(_signal()).verdict
    assert verdict.headline_zh == "▲ TRUMP 持仓异动4.55%｜持仓3217万｜鲸鱼占比100.7%｜鲸鱼多头盈利80.2%"
    assert verdict.why_zh == ""
    assert len(verdict.headline_zh) <= 60
    # The window clause left with the window (#458): it counted a push queue that no longer exists.
    assert "第" not in verdict.headline_zh and "4h" not in verdict.headline_zh
    assert "AI" not in verdict.headline_zh and "模型" not in verdict.headline_zh


def test_reader_title_compacts_long_symbols_without_losing_any_measurement() -> None:
    verdict = evaluate_oi(
        _signal(
            symbol="SIXTEENCHARACTRS",
            oi_change_bps=143_820,
            oi_value_usd=3_860_000_000,
            whale_oi_ratio_bps=21097,
            whale_long_profit_bps=8060,
        )
    ).verdict

    assert len(verdict.headline_zh) <= 60
    for fact in ("SIXTEENCHARACTRS", "1438.20%", "38.60亿", "211.0%", "80.6%"):
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
        )
    ).verdict

    assert len(verdict.headline_zh) <= 60
    for fact in ("SIXTEENCHARACTRS", "Δ9e16%", "O9e18", "W9e16%", "P-9e16%"):
        assert fact in verdict.headline_zh


def test_the_program_identity_changed_with_the_rule() -> None:
    """v2 judged; v3 stores. A shared identity would let a Case cite thresholds nothing applied."""

    assert (PROGRAM_VERSION, READER_CONTRACT_VERSION) == ("news_oi_signal_v3", "oi_card_v4")


# ---------------------------------------------------------------- #207: what the 持仓异动 monitor reads


def test_the_feed_folds_the_judge_trace_back_without_reparsing_the_title() -> None:
    """The console's `oi` block is `oi_judgment_trace()` read back, never a second parse.

    Every field has to survive the round trip, because the browser is not allowed to re-derive any of
    them from `leader_title`: a second copy of `oi_signal_parser_v1` in the page would drift from the
    judge the moment either side changed.
    """

    judgment = evaluate_oi(_signal())
    summary = _oi_summary(judgment.judgment_atom, oi_judgment_trace(judgment))

    assert summary is not None
    assert judgment.signal is not None
    assert summary["parsed"] is True
    assert summary["rule"] == STORED_RULE
    # The frame's parsed subject comes from the judgment trace. Public `assets` is a later projection of the
    # durable Event-asset ledger; it does not replace this structured OI fact or justify parsing the title again.
    assert summary["symbol"] == judgment.signal.symbol
    assert summary["oi_change_bps"] == judgment.signal.oi_change_bps
    assert summary["oi_value_usd"] == judgment.signal.oi_value_usd
    assert summary["whale_long_profit_bps"] == judgment.signal.whale_long_profit_bps
    assert summary["whale_oi_ratio_bps"] == judgment.signal.whale_oi_ratio_bps
    # No threshold and no rank reach the browser, because the judge applied neither (#458). A console
    # that still had a number here would be printing a gate no code runs.
    assert {
        "eligible_rank_in_window",
        "rank_semantics",
        "whale_oi_ratio_above_bps",
        "max_rank_in_window",
        "oi_change_at_least_bps",
        "window_ms",
    }.isdisjoint(summary)
    # The provider's identity stays behind: the console does not display Strategy IDs.
    assert {"strategy_id", "provider", "provider_source"}.isdisjoint(summary)


def test_an_unparseable_frame_folds_to_its_failure_shape_and_no_measurements() -> None:
    judgment, trace = oi_parse_failure("PENGU OI Rise 3.4%, OI Value --", provider_source="x")
    summary = _oi_summary(judgment.judgment_atom, trace)

    assert judgment.signal is None
    assert (judgment.decision.final, judgment.decision.override_rule) == ("drop", "oi_parse_failed")
    assert summary is not None
    assert summary["parsed"] is False
    assert summary["rule"] == "oi_parse_failed"
    assert summary["failure_stage"] == "source_contract_drift"
    assert summary["parser_version"] == "oi_signal_parser_v1"
    assert summary["title_sha256"]
    # Nothing was measured, so nothing is reported — including the symbol, which the template never
    # matched. A zero or an empty string here would read as a real reading.
    assert summary["symbol"] is None
    for key in ("oi_change_bps", "oi_value_usd", "whale_long_profit_bps", "whale_oi_ratio_bps"):
        assert summary[key] is None
    assert {"strategy_id", "provider", "provider_source"}.isdisjoint(summary)


def test_a_row_from_any_other_admission_carries_no_oi_block() -> None:
    assert _oi_summary(None, None) is None


def test_the_feed_oi_filter_names_the_only_rule_worth_narrowing_to() -> None:
    """The monitor's tabs are the judge's rules — the console owns no vocabulary of its own.

    The lane writes two rules. `oi_parse_failed` is the narrow tab; `stored` is every other row, which
    is what 全部 already shows, so grouping it would make the two tabs 全部 and 全部-minus-one. What
    this pins is that no group names a rule the judge cannot write: a stale key here would render an
    always-empty tab that reads as "nothing was rejected".
    """

    produced = {evaluate_oi(_signal()).rule, "oi_parse_failed"}
    grouped = [rule for rules in OI_FILTERS.values() for rule in rules]
    assert set(grouped) <= produced
    assert grouped == ["oi_parse_failed"]
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
