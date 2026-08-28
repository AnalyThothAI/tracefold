"""The deterministic ReaderCard copy contract (#306 Phase 1).

This is where the lint's own evidence lives. The recorded audit corpus cannot hold it: every card in that
fixture is a redaction marker, so it can prove the metric is reproducible and nothing at all about what the
metric now reads out of reader copy.
"""

from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.learning.card_lint import (
    GATE_CHECKS,
    SCORED_CHECKS,
    card_lint_receipt,
    lint_reader_card,
)

_GOOD_HEADLINE = "花旗年内推出数字资产托管，首批支持比特币"
_GOOD_WHY = "美国大型银行首次把比特币纳入自营托管，机构客户多了一条合规持币通道"


def _outcome(result: Any, check: str) -> str:
    return dict(result.outcomes)[check]


def test_a_conforming_card_passes_every_applicable_check() -> None:
    result = lint_reader_card(
        headline_zh=_GOOD_HEADLINE,
        why_zh=_GOOD_WHY,
        source_title="Wall Street Banking Giant Citi to Launch Digital Asset Custody Later This Year",
    )

    assert result.gate == ""
    assert result.score == 1.0
    assert result.feedback == ()
    assert {name for name, _ in result.outcomes} == set(SCORED_CHECKS)


@pytest.mark.parametrize(
    ("headline", "why", "gate"),
    [
        ("详情见 https://example.com/a", _GOOD_WHY, "card_lint_url"),
        (_GOOD_HEADLINE, "详见 www.example.com 的公告", "card_lint_url"),
        (_GOOD_HEADLINE, "作为 AI，我无法确认这笔托管的规模", "card_lint_self_description"),
    ],
)
def test_the_two_gates_are_the_things_that_are_not_reader_copy_at_all(headline: str, why: str, gate: str) -> None:
    result = lint_reader_card(headline_zh=headline, why_zh=why, source_title="")

    assert result.gate == gate
    # A gated card publishes the gate and no per-check outcomes: the remaining checks would enter the
    # component denominator of a case whose score is already zero.
    assert result.outcomes == ()
    assert result.score is None
    assert len(result.feedback) == 1


def test_the_gate_set_is_exactly_two_entries_and_language_is_not_one_of_them() -> None:
    """Everything else costs a point instead.

    The language boundary is the interesting case. It is a real contract rule, and it is scored rather than
    gated on purpose: a gate would zero a card for a defect that leaves the rest of it measurable, and it
    is the check most likely to fire on a card that is structurally fine.
    """

    assert set(GATE_CHECKS) == {"card_lint_url", "card_lint_self_description"}
    assert "headline_language" in SCORED_CHECKS

    english = lint_reader_card(headline_zh="Citi launches digital asset custody", why_zh=_GOOD_WHY)
    assert english.gate == ""
    assert _outcome(english, "headline_language") == "lint_fail"
    assert 0.0 < (english.score or 0.0) < 1.0


def test_a_headline_that_drops_the_originals_numbers_fails_the_retention_check() -> None:
    source = "Santos guides 2026 production of 99-105 MMBOE at a unit cost of $6.95-7.45"
    kept = lint_reader_card(
        headline_zh="Santos 2026 年产量指引 99-105 MMBOE，单位成本 6.95-7.45 美元",
        why_zh=_GOOD_WHY,
        source_title=source,
    )
    dropped = lint_reader_card(headline_zh="Santos 发布本年度的产量与成本指引", why_zh=_GOOD_WHY, source_title=source)

    assert _outcome(kept, "headline_number_retention") == "lint_pass"
    assert _outcome(dropped, "headline_number_retention") == "lint_fail"
    assert any("99" in line for line in dropped.feedback)


def test_number_retention_reads_standalone_numbers_and_not_digits_inside_identifiers() -> None:
    """A digit that continues a word is not a number the headline promised to carry.

    Without this the check fails on the thing it is meant to protect: an identifier, a hex digest or a
    model name in the source headline would each become a required literal, and a faithful Chinese
    rendering that carried every real number would still be marked as having dropped one.
    """

    result = lint_reader_card(
        headline_zh="安全团队确认漏洞已修复，未发现资金损失",
        why_zh=_GOOD_WHY,
        source_title="Advisory COVID19 patch shipped for build a1b2c3d4e5f6a7b8",
    )

    assert _outcome(result, "headline_number_retention") == "lint_not_applicable"


def test_number_retention_survives_thousands_separators_and_full_width_digits() -> None:
    result = lint_reader_card(
        headline_zh="现货钯金上涨近 3%，报 1328.68 美元/盎司",
        why_zh=_GOOD_WHY,
        source_title="Spot Palladium Rises Nearly 3% to $1,328.68/Oz",
    )

    assert _outcome(result, "headline_number_retention") == "lint_pass"


@pytest.mark.parametrize(
    ("check", "headline", "why"),
    [
        ("headline_length", "花旗推出托管", _GOOD_WHY),
        ("banned_filler", _GOOD_HEADLINE, "这条托管公告值得关注，对银行板块有影响"),
        ("meta_opening", _GOOD_HEADLINE, "该消息把比特币纳入自营托管，机构客户多了一条合规通道"),
        ("why_single_sentence", _GOOD_HEADLINE, "托管上线了。机构客户多了一条合规持币通道。"),
        ("no_emoji", _GOOD_HEADLINE, "机构客户多了一条合规持币通道 🚀"),
    ],
)
def test_each_scored_check_fails_on_its_own_defect_and_costs_exactly_one_point(
    check: str, headline: str, why: str
) -> None:
    result = lint_reader_card(headline_zh=headline, why_zh=why, source_title="")

    assert result.gate == ""
    assert _outcome(result, check) == "lint_fail"
    assert result.passed == len(result.applicable) - 1
    assert result.feedback, "a failed check has to tell the writer what to repair"


def test_a_decimal_is_not_a_sentence_end() -> None:
    """The one-sentence check must not fire on the numbers the headline contract requires be kept."""

    result = lint_reader_card(
        headline_zh=_GOOD_HEADLINE,
        why_zh="托管规模约为 3.5 亿美元，占该行数字资产业务的一半",
        source_title="",
    )

    assert _outcome(result, "why_single_sentence") == "lint_pass"


def test_banned_filler_matching_ignores_spacing() -> None:
    spaced = lint_reader_card(headline_zh=_GOOD_HEADLINE, why_zh="这属于 RWA 叙事的一部分", source_title="")
    joined = lint_reader_card(headline_zh=_GOOD_HEADLINE, why_zh="这属于RWA叙事的一部分", source_title="")

    assert _outcome(spaced, "banned_filler") == _outcome(joined, "banned_filler") == "lint_fail"


def test_the_receipt_publishes_the_gate_split_and_the_tables_themselves() -> None:
    receipt = card_lint_receipt()

    assert receipt["hard_gates"] == list(GATE_CHECKS)
    assert receipt["scored_checks"] == list(SCORED_CHECKS)
    assert "值得关注" in receipt["banned_filler"]
    assert receipt["headline_chars"] == {"min": 15, "max": 60}
