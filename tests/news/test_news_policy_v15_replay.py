"""Replay the recorded 24 h delivered ledger against the policy-v15 decision table (#675 PR-1).

`tests/fixtures/news/policy_v15_replay_24h.jsonl` is one row per card the reader actually received between
2026-09-21 07:10 and 2026-09-22 07:10 UTC: 417 of them, trimmed to the columns `decide()` reads plus the
label an independent reviewer gave the card under the audit rubric. It is a recording, not a gold set --
the reviewer labels are opinions about whether the reader wanted the interruption, and they are here so a
change to the table can be measured against them rather than only counted.

What this file asserts is the acceptance criterion of #675 §5: the two cards that opened the Issue are
withheld, every escalate and listing outcome is untouched, and the number of delivered cards the table
withholds -- together with how many of those a reviewer wanted kept -- is pinned so that widening or
narrowing a rule shows up as a diff here before it shows up in the reader's channel.

The numbers below are measured, not predicted. The Issue's own estimate was ~71 withheld with at most 4
keep-labelled; this implementation withholds 65 and hits 9 keep-labelled cards. The gap and every one of
the nine are accounted for in `test_the_keep_labelled_cards_the_table_withholds_are_the_known_ones`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.support.news_judgment import news_taxonomy, scored_judgment, trade_relevance
from tracefold.news.models import TriageVerdict
from tracefold.news.triage_rules import (
    DECISION_TABLE_RULES,
    DecisionResult,
    GateFacts,
    StorylineStatus,
    decide,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "policy_v15_replay_24h.jsonl"
NOW = 1_800_000_000_000
TABLE_RULES = frozenset(DECISION_TABLE_RULES)
# The three v14 rules #675 §5 requires to stay byte-identical. `watchlist_objective_guard` is not in this
# recording -- no delivered card in the window took it -- so its invariance is asserted in
# `test_news_v3_pure.py` instead, where the branch can be constructed directly.
UNTOUCHED_RULES = frozenset(
    {"listing_deterministic", "trade_relevance_escalate", "trade_relevance_escalate_uncorroborated"}
)
# The two cards the Issue was opened about: a pure quote on a single stock, and one ministry's claim 97
# minutes into a storyline the reader was already reading.
TENCENT = "eefb4d3b34c88d002a1504f39b0ca6c9e7076ec10c4ba5dbb697e6f847efa438"
REFINERY = "bfab833672132780d4cd95ea92cfcd1120dbca9d878483f30c4b4669f83af9e2"


def _rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def _replay(row: dict[str, Any]) -> DecisionResult:
    """Run the production `decide()` over one recorded row.

    Three inputs are reconstructed rather than recorded, and each one is pinned to the value that cannot
    change the recorded outcome: `restates` is -1 (every row here was delivered, so none of them took the
    restatement drop), the seen ledger is empty (likewise for the similarity check and the storyline
    budget), and `source_age_s` is absent (likewise for the stale-source rule). The reconstruction is
    checked against the recording in `test_the_recording_reproduces_its_own_v14_outcome`.
    """

    judgment = scored_judgment(
        TriageVerdict(
            novelty=row["novelty"],
            restates=-1,
            assets=row["assets"],
            direction=row["direction"],
            scope=row["scope"],
            magnitude=row["magnitude"],
            confidence=0.8,
            headline_zh=row["headline_zh"][:60],
            why_zh="",
        ),
        relevance=trade_relevance(**row["relevance"]),
        taxonomy=news_taxonomy(
            event_family=row["event_family"],
            change_state=row["change_state"],
            assertion_status=row["assertion_status"],
        ),
        source_authority=row["source_authority"],
    )
    facts = GateFacts(
        grounded_assets=tuple(asset["symbol"] for asset in row["assets"]),
        watchlist_symbols=frozenset(),
        admission="listing_deterministic" if row["v14_override_rule"] == "listing_deterministic" else "candidate",
        member_count=row["member_count"],
        independent_text_count=row["independent_text_count"],
        title=row["title"],
    )
    key = row["storyline_key"]
    told = row["told_same_key_4h"]
    status = StorylineStatus(
        key=key,
        told_directions=("neutral",) * told,
        told_assets=(frozenset(),) * told,
        told_keys=(key,) * told,
        told_at_ms=tuple(NOW - (index + 1) * 60_000 for index in range(told)),
    )
    return decide(judgment, facts, status, now_ms=NOW)


@pytest.fixture(scope="module")
def replayed() -> list[tuple[dict[str, Any], DecisionResult]]:
    return [(row, _replay(row)) for row in _rows()]


def _withheld(replayed: list[tuple[dict[str, Any], DecisionResult]]) -> Iterator[tuple[dict[str, Any], str]]:
    for row, result in replayed:
        if result.override_rule in TABLE_RULES:
            yield row, result.override_rule


def test_the_recording_is_the_delivered_day_and_carries_the_reviewer_labels() -> None:
    rows = _rows()
    assert len(rows) == 417
    assert len({row["event_id"] for row in rows}) == 417
    assert {TENCENT, REFINERY} <= {row["event_id"] for row in rows}
    assert all(row["v14_final_decision"] in {"push", "escalate"} for row in rows)
    assert {row["reviewer_verdict"] for row in rows} == {"keep", "borderline", "demote"}
    # Kept small on purpose: this is a regression recording, not a corpus.
    assert FIXTURE.stat().st_size < 600_000


def test_the_recording_reproduces_its_own_v14_outcome(replayed: list[tuple[dict[str, Any], DecisionResult]]) -> None:
    """Whatever the table does not touch must come out of `decide()` exactly as the reader received it.

    One row is a known recording artifact rather than a policy difference: `member_count` is read off the
    Event, and an Event keeps merging arrivals after its verdict settles, so a card that took the
    uncorroborated-escalate downgrade on a single member at decision time can be recorded with two. It is
    named here rather than smoothed away, because a second such row would mean something else moved.
    """

    artifacts = [
        (row["event_id"], row["v14_override_rule"], result.override_rule)
        for row, result in replayed
        if result.override_rule not in TABLE_RULES
        and (result.final, result.override_rule) != (row["v14_final_decision"], row["v14_override_rule"])
    ]
    assert [(rule, replayed_rule) for _, rule, replayed_rule in artifacts] == [
        ("trade_relevance_escalate_uncorroborated", "trade_relevance_escalate")
    ]


def test_the_two_cards_that_opened_the_issue_are_withheld(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    by_id = {row["event_id"]: (row, result) for row, result in replayed}

    tencent, tencent_result = by_id[TENCENT]
    assert tencent["headline_zh"] == "Meta Muse智能体走红，腾讯港股盘中涨超7%"
    assert (tencent_result.final, tencent_result.override_rule) == ("drop", "price_report_without_basis")

    refinery, refinery_result = by_id[REFINERY]
    assert refinery["headline_zh"] == "俄防部：俄军打击克列缅丘格炼油厂，该厂曾为乌军生产燃料"
    assert (refinery_result.final, refinery_result.override_rule) == ("drop", "conflict_claim_uncorroborated")
    # Both reached the reader under v14 through the ordinary realtime branch, not through a guard.
    assert tencent["v14_override_rule"] == refinery["v14_override_rule"] == "trade_relevance_realtime"


def test_every_escalate_and_listing_outcome_is_unchanged(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """#675 §5: v15 appends rows to one branch. The other branches must not be able to reach them."""

    guarded = [(row, result) for row, result in replayed if row["v14_override_rule"] in UNTOUCHED_RULES]
    assert len(guarded) == 41
    assert all(result.override_rule not in TABLE_RULES for _, result in guarded)
    assert all(result.final in {"push", "escalate"} for _, result in guarded)
    # And nothing the table withholds came from anywhere but the realtime branch.
    assert {row["v14_override_rule"] for row, _ in _withheld(replayed)} == {"trade_relevance_realtime"}


def test_the_table_withholds_sixty_five_delivered_cards(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """The measured effect on the recorded day, by rule and by what a reviewer said about each card.

    417 delivered -> 352. Of the 65 withheld, 48 were labelled `demote` (the reader did not want the
    interruption), 8 `borderline` and 9 `keep`. The Issue's estimate was 71/<=4; the shortfall is the
    bilingual level-crossing and quantified-flow vocabulary releasing price reports the audit's first
    simulation dropped, plus the `none` storyline key no longer counting as "the same storyline".
    """

    withheld = list(_withheld(replayed))
    by_rule: dict[str, int] = {}
    for _, rule in withheld:
        by_rule[rule] = by_rule.get(rule, 0) + 1
    assert by_rule == {
        "price_report_without_basis": 39,
        "conflict_claim_uncorroborated": 17,
        "conflict_running_storyline": 9,
    }
    assert len(withheld) == 65

    labels: dict[str, int] = {}
    for row, _ in withheld:
        labels[row["reviewer_verdict"]] = labels.get(row["reviewer_verdict"], 0) + 1
    assert labels == {"demote": 48, "borderline": 8, "keep": 9}


def test_the_keep_labelled_cards_the_table_withholds_are_the_known_ones(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """Nine of the withheld cards had a reviewer who wanted them, and each one is accounted for.

    Two are the rows' real cost, accepted when the Issue was written: `change_state=announced` cannot tell
    an official measure from a spokesman's opinion, so a conflict storyline the reader is already reading
    swallows both. Two are taxonomy errors -- a clinical result and a supply shock classified as price
    moves -- and belong to the classification target, not to this table. Two are the owner's decisions
    (#675 §7): a single stock at 10.4% is a price broadcast, and the third Bessent card of the day is a
    repeat the audit re-review confirmed. Two are quantified position reports whose wording ("空头达 12 亿
    美元", "6.48 亿美元看跌押注离场") is outside the flow vocabulary the Issue enumerated, and widening it to
    catch them is a change to make deliberately rather than by accident. One is a Reuters report carried
    by a relay, which the authority registry cannot name without granting authority to the relay itself.
    """

    keeps = sorted(
        (rule, row["t"], row["headline_zh"]) for row, rule in _withheld(replayed) if row["reviewer_verdict"] == "keep"
    )
    assert keeps == sorted(
        [
            # The two real costs of `conflict_running_storyline`.
            ("conflict_running_storyline", "09-21 10:21", "特朗普施压泽连斯基停止打击俄罗斯炼油厂"),
            ("conflict_running_storyline", "09-21 15:00", "知情人士称俄拟延长柴油出口禁令至9月底后"),
            # Confirmed repeat: the same Bessent announcement, already delivered at 18:09.
            ("conflict_running_storyline", "09-22 01:43", "贝森特称伊朗所有航空公司周三起停运"),
            # Taxonomy errors: the fact is a clinical result and a sanctions warning, not a price move.
            ("price_report_without_basis", "09-21 11:07", "Alkermes股价盘前涨6.5%：ADHD药物早期研究结果积极"),
            ("price_report_without_basis", "09-22 04:22", "贝森特警告伊朗航空停飞后美伊紧张升级，油价上涨"),
            # The owner's decision: a single stock is excluded from the >= 5% exception.
            ("price_report_without_basis", "09-21 17:17", "Meta股价涨幅扩大，最新上涨10.4%"),
            # Position reports outside the enumerated flow vocabulary.
            (
                "price_report_without_basis",
                "09-21 09:10",
                "Lookonchain：Abraxas Capital在Hyperliquid空头达12亿美元，未实现亏损超1亿",
            ),
            ("price_report_without_basis", "09-21 10:30", "比特币触及8.5万美元，空头挤压迫使6.48亿美元看跌押注离场"),
            # A wire report reaching us through a relay the registry deliberately does not name.
            ("conflict_claim_uncorroborated", "09-21 14:51", "路透：美国提议将中美贸易休战延长六个月"),
        ]
    )


def test_what_the_table_withholds_is_mostly_what_the_rubric_called_noise(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """The reviewer's own category for each withheld card, so a rule that starts catching a different kind
    of card is visible as a category shift rather than only as a count."""

    categories: dict[str, int] = {}
    for row, _ in _withheld(replayed):
        categories[row["reviewer_category"]] = categories.get(row["reviewer_category"], 0) + 1
    assert categories["price_report"] == 29
    assert categories["conflict_routine"] == 13
    # The residue is what the rows pay for: eight cards a reviewer read as a real fact.
    assert categories["real_fact"] == 8
    assert sum(categories.values()) == 65


def test_the_table_leaves_the_rest_of_the_day_alone(replayed: list[tuple[dict[str, Any], DecisionResult]]) -> None:
    """352 of the 417 delivered cards are unaffected, and every one of them still reaches the reader."""

    survivors = [(row, result) for row, result in replayed if result.override_rule not in TABLE_RULES]
    assert len(survivors) == 352
    assert all(result.final in {"push", "escalate"} for _, result in survivors)


def test_every_withheld_card_carried_the_classification_the_row_read(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """The table reads a taxonomy or it is silent (#651 §5.3); `test_news_v3_pure.py` asserts the silence."""

    for row, rule in _withheld(replayed):
        assert row["event_family"] and row["change_state"] and row["assertion_status"]
        expected = "market_flow_price" if rule == "price_report_without_basis" else "geopolitical_conflict"
        assert row["event_family"] == expected, (rule, row["event_id"])
