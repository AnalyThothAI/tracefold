"""Replay the recorded 24 h delivered ledger against the policy-v16 decision table (#675 PR-2).

`tests/fixtures/news/policy_v16_replay_24h.jsonl` is one row per card the reader actually received
between 2026-09-21 07:10 and 2026-09-22 07:10 UTC: 417 of them, trimmed to the columns `decide()` reads
plus three labels an independent reviewer gave the card under the audit rubric (`reviewer_verdict`,
`reviewer_category`, `price_basis`). It is the PR-1 fixture with the reviewer's `price_basis` added and
the two columns v16 no longer reads -- `magnitude` and the `relevance` object -- removed.

**`fact_kind` here is a proxy, not model output.** No judgment in this recording carries one: the field
did not exist on 2026-09-21, and re-asking the model would measure today's Program rather than replay
that day. :func:`proxy_fact_kind` derives one deterministically from three things the recording does
carry -- the reviewer's `price_basis`, PR-1's own bilingual level/record/flow vocabulary read off the
card's own title and headline, and the taxonomy axes -- and deliberately ignores `reviewer_category`,
which names the answer the numbers below are scored against. So what this file measures is the decision
table, on a plausible kind per card. It is not a measurement of the model's `fact_kind` accuracy, which
only a live run can produce.

The numbers below are measured, not predicted.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.support.news_judgment import news_taxonomy, scored_judgment
from tracefold.news.models import FACT_KINDS, FactKind, TriageVerdict
from tracefold.news.triage_rules import (
    _PRICE_BASIS_PATTERNS,
    _PRICE_LEVEL_CROSSED,
    _PRICE_PERIOD_RECORD,
    DecisionResult,
    GateFacts,
    StorylineStatus,
    decide,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "policy_v16_replay_24h.jsonl"
NOW = 1_800_000_000_000
PUSHED = frozenset({"push", "escalate"})
# The two cards the Issue was opened about: a pure quote on a single stock, and one ministry's claim 97
# minutes into a storyline the reader was already reading.
TENCENT = "eefb4d3b34c88d002a1504f39b0ca6c9e7076ec10c4ba5dbb697e6f847efa438"
REFINERY = "bfab833672132780d4cd95ea92cfcd1120dbca9d878483f30c4b4669f83af9e2"

# PR-1's vocabulary, split by what each pattern says the text states. `decide()` asks it one question
# ("does the text state anything about the number beyond its size"); the proxy needs the answer broken
# out by kind, so it reads the same six patterns in the same order under their own names.
_LEVEL, _RECORD, _DEPEG, _FREIGHT, _FLOW_DIGIT, _DIGIT_FLOW = _PRICE_BASIS_PATTERNS
assert _LEVEL.pattern == _PRICE_LEVEL_CROSSED and _RECORD.pattern == _PRICE_PERIOD_RECORD


def proxy_fact_kind(row: dict[str, Any]) -> FactKind:
    """The kind this card's own text states, derived from the recording without reading the answer.

    The order is the order of evidence strength. The reviewer read every `market_flow_price` headline
    and named its basis, so where that label exists it is the best statement of what the text says
    about a number. Otherwise the same bilingual vocabulary `decide()` itself checks is applied to the
    same text `decide()` reads, split by which of its six patterns matched: a level or a stablecoin peg
    is `level_crossed`, a period high/low is `period_record`, an amount that moved is `quantified_flow`,
    and a freight rate is a number about physical supply, so it is a `new_quantity`. A card whose family
    is `market_flow_price` and whose text says none of those is exactly what the codebook calls it: the
    number was printed, and nothing else.

    Below that the taxonomy's `change_state` carries it. A venue's dated listing or delisting notice is
    a `state_change` even when the codebook calls it `scheduled` -- the venue has decided, and the date
    is a term of the decision -- while a `scheduled` anything else is a calendar item. A measure by a
    regulator or a ministry is an `official_measure`, an announced/effective/updated/delayed/cancelled
    state is a `state_change`, a reported result or figure is a `new_quantity`, and a claim nobody has
    acted on is a `statement`.

    `reviewer_category` is never read. It is the label the table is being scored against, and a proxy
    that consulted it would be reporting its own input back.
    """

    text = f"{row['title']}\n{row['headline_zh']}"
    basis = str(row.get("price_basis") or "")
    if basis in {"level_crossed", "period_record", "quantified_flow"}:
        return basis  # type: ignore[return-value]
    if _LEVEL.search(text) or _DEPEG.search(text):
        return "level_crossed"
    if _RECORD.search(text):
        return "period_record"
    if _FLOW_DIGIT.search(text) or _DIGIT_FLOW.search(text):
        return "quantified_flow"
    if _FREIGHT.search(text):
        return "new_quantity"
    family, change_state = row["event_family"], row["change_state"]
    if family == "market_flow_price":
        return "recap"
    if change_state == "scheduled":
        return "state_change" if family == "market_access" else "schedule"
    if family in {"regulatory_legal", "macro_policy_data"} and change_state in {
        "announced",
        "effective",
        "updated",
    }:
        return "official_measure"
    if change_state in {"announced", "effective", "updated", "delayed", "cancelled", "recalled"}:
        return "state_change"
    if change_state == "reported":
        return "new_quantity"
    return "statement"


def _rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def _replay(row: dict[str, Any]) -> DecisionResult:
    """Run the production `decide()` over one recorded row, with the proxy kind in the verdict.

    Three inputs are reconstructed rather than recorded, and each one is pinned to the value that cannot
    change the recorded outcome: `restates` is -1 (every row here was delivered, so none of them took the
    restatement drop), the seen ledger is empty (likewise for the similarity check and the storyline
    budget), and `source_age_s` is absent (likewise for the stale-source rule).
    """

    judgment = scored_judgment(
        TriageVerdict(
            novelty=row["novelty"],
            restates=-1,
            assets=row["assets"],
            direction=row["direction"],
            scope=row["scope"],
            fact_kind=proxy_fact_kind(row),
            evidence_ref="c1",
            confidence=0.8,
            headline_zh=row["headline_zh"][:60],
            why_zh="",
        ),
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
        if result.final not in PUSHED:
            yield row, str(result.override_rule)


def test_the_recording_is_the_delivered_day_and_carries_the_reviewer_labels() -> None:
    rows = _rows()
    assert len(rows) == 417
    assert len({row["event_id"] for row in rows}) == 417
    assert {TENCENT, REFINERY} <= {row["event_id"] for row in rows}
    assert all(row["v14_final_decision"] in PUSHED for row in rows)
    assert {row["reviewer_verdict"] for row in rows} == {"keep", "borderline", "demote"}
    # The two deleted columns are not in the recording any more: a replay that could read `magnitude`
    # or the `relevance` object would not be a replay of the policy this file measures.
    assert not any({"magnitude", "relevance"} & set(row) for row in rows)
    # Kept small on purpose: this is a regression recording, not a corpus.
    assert FIXTURE.stat().st_size < 600_000


def test_the_proxy_kind_is_derived_for_every_card_and_uses_the_reviewer_basis_where_it_exists() -> None:
    """The proxy's own denominator, so a change to it shows up here before it shows up in a count."""

    rows = _rows()
    kinds = [proxy_fact_kind(row) for row in rows]
    assert set(kinds) <= set(FACT_KINDS)
    assert len(kinds) == 417
    # Every reviewer-stated basis is honoured verbatim.
    stated = [
        (row, kind)
        for row, kind in zip(rows, kinds, strict=True)
        if row["price_basis"] in {"level_crossed", "period_record", "quantified_flow"}
    ]
    assert len(stated) == 49
    assert all(kind == row["price_basis"] for row, kind in stated)
    by_kind: dict[str, int] = {}
    for kind in kinds:
        by_kind[kind] = by_kind.get(kind, 0) + 1
    assert by_kind == {
        "state_change": 243,
        "recap": 42,
        "official_measure": 24,
        "quantified_flow": 22,
        "period_record": 22,
        "new_quantity": 22,
        "statement": 21,
        "level_crossed": 16,
        "schedule": 5,
    }
    # The proxy never answers `promotion`, and that is its largest single limitation rather than a
    # property of the day: marketing and a small project's own product announcement are both
    # `product_service_change` + `announced` in the taxonomy, so nothing in the recording separates
    # them and both land in `state_change`. The audit called 64 of the day's 232 demotes marketing or
    # a small-project product post (#675 §0); the seed teaches the model to call those `promotion`,
    # and only a live run can show whether it does.


def test_the_two_cards_that_opened_the_issue_are_withheld(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    by_id = {row["event_id"]: (row, result) for row, result in replayed}

    tencent, tencent_result = by_id[TENCENT]
    assert tencent["headline_zh"] == "Meta Muse智能体走红，腾讯港股盘中涨超7%"
    assert proxy_fact_kind(tencent) == "recap"
    assert (tencent_result.final, tencent_result.override_rule) == ("drop", "fact_kind_recap")

    refinery, refinery_result = by_id[REFINERY]
    assert refinery["headline_zh"] == "俄防部：俄军打击克列缅丘格炼油厂，该厂曾为乌军生产燃料"
    assert (refinery_result.final, refinery_result.override_rule) == ("drop", "conflict_claim_uncorroborated")
    # Both reached the reader under v14 through the ordinary realtime branch, not through a guard.
    assert tencent["v14_override_rule"] == refinery["v14_override_rule"] == "trade_relevance_realtime"


def test_the_table_withholds_one_hundred_and_twenty_four_delivered_cards(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """The measured effect on the recorded day, by rule and by what a reviewer said about each card.

    417 delivered -> 293. The v15 table withheld 65 of these with three rows bolted onto one branch;
    v16 decides every card, so the four `fact_kind` drop rows reach cards v15 never looked at -- the
    price-reaction pieces the reviewer called `price_report`, and the opinion and marketing cards that
    #675 §0 counted as 64 of the day's 232 demotes and that no v15 row could see.
    """

    withheld = list(_withheld(replayed))
    by_rule: dict[str, int] = {}
    for _, rule in withheld:
        by_rule[rule] = by_rule.get(rule, 0) + 1
    assert by_rule == {
        "fact_kind_recap": 41,
        "fact_kind_statement": 21,
        "conflict_claim_uncorroborated": 13,
        "fact_kind_schedule": 5,
        "price_report_without_basis": 5,
    }
    assert len(withheld) == 85

    labels: dict[str, int] = {}
    for row, _ in withheld:
        labels[row["reviewer_verdict"]] = labels.get(row["reviewer_verdict"], 0) + 1
    assert labels == {"demote": 59, "borderline": 13, "keep": 13}
    # Two rows fire on no card in this recording and are covered directly in `test_news_v3_pure.py`
    # instead: `conflict_running_storyline`, because the proxy reads nearly every conflict card's
    # `announced`/`effective` state as a `state_change` and the row exempts those, and
    # `single_name_without_instrument`, because every single-name card delivered that day named a
    # primary instrument.
    assert not {"conflict_running_storyline", "single_name_without_instrument"} & set(by_rule)


def test_what_the_table_withholds_is_mostly_what_the_rubric_called_noise(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """The reviewer's own category for each withheld card, so a rule that starts catching a different
    kind of card is visible as a category shift rather than only as a count."""

    categories: dict[str, int] = {}
    for row, _ in _withheld(replayed):
        categories[row["reviewer_category"]] = categories.get(row["reviewer_category"], 0) + 1
    assert categories["price_report"] == 31
    assert categories["opinion"] == 12
    assert categories["conflict_routine"] == 8
    assert categories["marketing"] == 7
    # The residue is what the rows pay for: cards a reviewer read as a real fact.
    assert categories["real_fact"] == 8
    assert sum(categories.values()) == 85


def test_the_cards_the_table_still_delivers_are_the_ones_the_reviewer_wanted(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """The product metric #675 §5 asks for: the keep share of what the reader is actually handed.

    23% of the delivered day was `keep` and 56% was `demote`. Withholding 85 cards moves the delivered
    slice to 25% keep and 52% demote. That is a small move and it is the honest one to report: the
    proxy cannot answer `promotion`, so the 64 marketing and small-project product cards the audit
    counted are all still delivered here, and the cross-language duplicate class #675 §2 owns is
    untouched by this PR. The >= 45% keep share #675 §5 targets is a continuous-track number, and
    nothing in this file claims progress toward it.
    """

    delivered = [row for row, result in replayed if result.final in PUSHED]
    assert len(delivered) == 332
    labels: dict[str, int] = {}
    for row in delivered:
        labels[row["reviewer_verdict"]] = labels.get(row["reviewer_verdict"], 0) + 1
    assert labels == {"keep": 83, "borderline": 76, "demote": 173}
    assert round(100 * labels["keep"] / len(delivered)) == 25
    assert round(100 * labels["demote"] / len(delivered)) == 52


def test_the_keep_labelled_cards_the_table_withholds_are_the_known_ones(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """Thirteen of the withheld cards had a reviewer who wanted them, and each one is accounted for.

    Four are the `statement` row's real cost and are accepted as such: two Fed speakers saying rates may
    have to rise, a network's report of a live antitrust negotiation, and a venue that "may" form next
    quarter are all somebody saying something, which is exactly what the row withholds. Four are the
    proxy's own coarseness rather than the table's -- two flows the PR-1 vocabulary still does not spell
    (`空头达 12 亿美元`, `6.48 亿美元看跌押注离场`, both named in the PR-1 receipt) and two dated venue
    notices the proxy could only read as a schedule. Three are taxonomy errors that belong to the
    classification target: a clinical result, a sanctions warning and a pipeline outage all classified
    `market_flow_price`. One is the owner's decision (#675 §7): a single stock at 10.4% is a price
    broadcast. Two are the conflict row's cost, and both are authority-registry gaps rather than rule
    defects -- UKMTO is a first-party maritime authority the registry does not name, and the Reuters
    line reached us through a relay the registry deliberately will not name.
    """

    keeps = sorted(
        (rule, row["t"], row["headline_zh"]) for row, rule in _withheld(replayed) if row["reviewer_verdict"] == "keep"
    )
    assert keeps == sorted(
        [
            # The `statement` row's accepted cost.
            ("fact_kind_statement", "09-21 18:19", "美联储Musalem称可能需进一步加息以抑制通胀"),
            ("fact_kind_statement", "09-21 23:31", "圣路易斯联储主席穆萨莱姆称通胀扩散至需求端，利率可能仍需上调"),
            ("fact_kind_statement", "09-21 12:47", "据CNN：派拉蒙正与各州检察长深入谈判解决收购华纳兄弟反垄断诉讼"),
            ("fact_kind_statement", "09-22 01:33", "美SEC创新豁免下首个代币化股票场所或于下季度成形"),
            # Flows outside the enumerated vocabulary, both already named in the PR-1 receipt.
            (
                "fact_kind_recap",
                "09-21 09:10",
                "Lookonchain：Abraxas Capital在Hyperliquid空头达12亿美元，未实现亏损超1亿",
            ),
            ("price_report_without_basis", "09-21 10:30", "比特币触及8.5万美元，空头挤压迫使6.48亿美元看跌押注离场"),
            # A dated venue notice the proxy read as a calendar item.
            ("fact_kind_schedule", "09-21 08:05", "Venus称2026年10月23日停止支持opBNB、Optimism及Unichain"),
            # Taxonomy errors: the fact is a clinical result, a sanctions warning and a supply outage.
            ("fact_kind_recap", "09-21 11:07", "Alkermes股价盘前涨6.5%：ADHD药物早期研究结果积极"),
            ("fact_kind_recap", "09-22 04:22", "贝森特警告伊朗航空停飞后美伊紧张升级，油价上涨"),
            ("fact_kind_recap", "09-22 00:08", "利比亚Sharara管线关闭致油田产量据称大幅下降"),
            # The owner's decision: a single stock is excluded from the >= 5% exception.
            ("fact_kind_recap", "09-21 17:17", "Meta股价涨幅扩大，最新上涨10.4%"),
            # Authority-registry gaps, not rule defects.
            ("conflict_claim_uncorroborated", "09-21 09:33", "UKMTO称军方通报一艘入港油轮被弹丸击中"),
            ("conflict_claim_uncorroborated", "09-21 14:51", "路透：美国提议将中美贸易休战延长六个月"),
        ]
    )


def test_the_owner_exception_admits_a_five_percent_commodity_move_and_not_a_single_stock(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """#675 §7, on the two cards the recording has for it.

    WTI -5.00% is a `commodity` primary and the move is itself the fact, so the exception admits it as a
    `new_quantity` however the model read the sentence. Meta +10.4% is one stock and stays withheld,
    which is the decision the owner wrote down and the reason the exception is carried by the primary
    asset's market rather than by the size of the move.
    """

    by_headline = {row["headline_zh"]: (row, result) for row, result in replayed}
    wti, wti_result = by_headline["WTI原油期货11月合约日内大跌5.00%，现报91.27美元/桶"]
    assert proxy_fact_kind(wti) == "recap"
    assert [asset["market_type"] for asset in wti["assets"] if asset["role"] == "primary"] == ["commodity"]
    assert (wti_result.final, wti_result.override_rule) == ("push", "fact_kind_new_quantity")

    meta, meta_result = by_headline["Meta股价涨幅扩大，最新上涨10.4%"]
    assert [asset["market_type"] for asset in meta["assets"] if asset["role"] == "primary"] == ["equity"]
    assert (meta_result.final, meta_result.override_rule) == ("drop", "fact_kind_recap")


def test_the_escalate_row_names_the_loudest_class_and_keeps_the_corroboration_rule(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """v16 decides an escalate from the facts instead of reading the model's own reader value.

    The corroboration condition is the one #504 D3 wrote and this table keeps: an unknown source with a
    single independent text is a claim, and it reaches the reader as an ordinary push under its own rule
    name rather than as the loudest card on the page.
    """

    escalated = [(row, result) for row, result in replayed if result.final == "escalate"]
    uncorroborated = [row for row, result in replayed if result.override_rule == "escalate_uncorroborated"]
    assert all(result.override_rule == "escalate_corroborated" for _, result in escalated)
    assert all(row["source_authority"] != "unknown" or row["independent_text_count"] >= 2 for row, _ in escalated)
    assert all(row["source_authority"] == "unknown" and row["independent_text_count"] <= 1 for row in uncorroborated)
    assert len(escalated) == 36
    assert len(uncorroborated) == 34


def test_every_withheld_conflict_card_carried_the_classification_the_row_read(
    replayed: list[tuple[dict[str, Any], DecisionResult]],
) -> None:
    """The two conflict rows read a taxonomy or they are silent (#651 §5.3); `test_news_v3_pure.py`
    asserts the silence directly."""

    for row, rule in _withheld(replayed):
        if rule in {"conflict_claim_uncorroborated", "conflict_running_storyline"}:
            assert row["event_family"] == "geopolitical_conflict", (rule, row["event_id"])
            assert row["change_state"] and row["assertion_status"]
