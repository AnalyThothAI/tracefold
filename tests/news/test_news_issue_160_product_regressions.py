"""Fixed product behavior cases for the Issue #160 editorial hard cut."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest

from tests.support.news_judgment import news_taxonomy, scored_judgment
from tracefold.news.models import TriageAsset, TriageVerdict
from tracefold.news.program.contracts import ScoredJudgment, TriageContext
from tracefold.news.triage_rules import DecisionResult, GateFacts, decide

_NO_OBJECTIVE_GUARD = GateFacts(
    grounded_assets=(),
    watchlist_symbols=frozenset(),
    admission="candidate",
)
# #504 D3, narrowed by #675 §3: an escalate from a source of unknown authority is corroborated by a second
# *independent member text*, not by a second arrival of the same wire line.
_CORROBORATED = GateFacts(
    grounded_assets=(),
    watchlist_symbols=frozenset(),
    admission="candidate",
    independent_text_count=2,
)


def _verdict(**overrides: Any) -> TriageVerdict:
    values: dict[str, Any] = {
        "novelty": "new_fact",
        "assets": [],
        "direction": "neutral",
        "scope": "macro",
        "fact_kind": "state_change",
        "evidence_ref": "c1",
        "confidence": 0.9,
        "headline_zh": "固定产品回归案例",
        "why_zh": "",
    }
    values.update(overrides)
    return TriageVerdict.model_validate(values)


def _exact_decision(
    judgment: ScoredJudgment,
    expected: DecisionResult,
    *,
    facts: GateFacts = _NO_OBJECTIVE_GUARD,
) -> None:
    assert decide(judgment, facts, None) == expected


def _context(*, provider_score: int = 0, queue_priority: str = "normal") -> TriageContext:
    return TriageContext.from_card(
        {
            "event_id": "issue-160-product-case",
            "evidence_version": 3,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-160",
            "reporting_origin": "official",
            "provenance": ["1018"],
            "leader_title": "Local official repeats an in-line statement",
            "leader_description": "No new priced transmission was reported.",
            "opened_at_ms": 1_000_000,
            "dedupe_family": "general",
            "provider_score_max": provider_score,
            "queue_priority": queue_priority,
            "asset_class": "macro",
            "macro_lexicon": True,
            "grounded_assets": [],
            "storyline_key": "none",
        },
        watchlist=("BTC",),
        told_rows=(),
        now_ms=1_010_000,
        queue_lag_ms=10_000,
    )


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value))
    return set()


def test_provider_score_95_local_contextual_color_only_is_held() -> None:
    """A loud provider score is still no reason to interrupt: the text is one official repeating himself."""

    context = _context(provider_score=95)
    judgment = scored_judgment(
        _verdict(
            scope="single_name",
            fact_kind="statement",
            headline_zh="地方官员重复既有表态",
        ),
    )

    assert context.evidence.provider_score == 95
    _exact_decision(
        judgment,
        DecisionResult(
            final="drop",
            override_rule="fact_kind_statement",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_queue_high_or_macro_scope_does_not_create_reader_urgency() -> None:
    context = _context(provider_score=95, queue_priority="high")
    judgment = scored_judgment(
        _verdict(scope="macro", fact_kind="statement", headline_zh="宏观标签本身不构成打断理由"),
    )

    assert context.evidence.queue_priority == "high"
    assert {item.name for item in fields(GateFacts)}.isdisjoint(
        {"priority", "queue_priority", "provider_score", "provider_score_max", "macro_lexicon"}
    )
    _exact_decision(
        judgment,
        DecisionResult(
            final="drop",
            override_rule="fact_kind_statement",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_unexpected_fed_cut_with_rates_and_liquidity_escalates() -> None:
    judgment = scored_judgment(
        _verdict(
            direction="bullish",
            fact_kind="official_measure",
            headline_zh="美联储意外降息",
        ),
        taxonomy=news_taxonomy(event_family="macro_policy_data", change_state="effective"),
    )

    _exact_decision(
        judgment,
        DecisionResult(
            final="escalate",
            override_rule="escalate_corroborated",
            throttled_by=None,
            rule_baseline="drop",
        ),
        facts=_CORROBORATED,
    )
    # The same judgment from one Item of unknown authority is a push, not a wake-up (#504 D3).
    _exact_decision(
        judgment,
        DecisionResult(
            final="push",
            override_rule="escalate_uncorroborated",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_official_hormuz_closure_with_energy_and_risk_escalates() -> None:
    judgment = scored_judgment(
        _verdict(
            direction="bearish",
            fact_kind="state_change",
            headline_zh="霍尔木兹海峡正式关闭",
        ),
        taxonomy=news_taxonomy(event_family="geopolitical_conflict", change_state="effective"),
    )

    _exact_decision(
        judgment,
        DecisionResult(
            final="escalate",
            override_rule="escalate_corroborated",
            throttled_by=None,
            rule_baseline="drop",
        ),
        facts=_CORROBORATED,
    )


def test_regional_port_supply_state_change_pushes_without_corroboration() -> None:
    """A regional operational change still reaches the reader — as an ordinary card, not a wake-up."""

    judgment = scored_judgment(
        _verdict(
            scope="sector",
            fact_kind="state_change",
            headline_zh="地区港口停运中断商品供应",
        ),
        taxonomy=news_taxonomy(event_family="security_operational_incident", change_state="effective"),
    )

    _exact_decision(
        judgment,
        DecisionResult(
            final="push",
            override_rule="escalate_uncorroborated",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_material_local_regulation_for_us_listed_single_name_is_pushed() -> None:
    """A measure an authority took is a fact; outside the four loud families it is an ordinary push."""

    facts = GateFacts(
        grounded_assets=("UWMC",),
        watchlist_symbols=frozenset(),
        admission="candidate",
    )
    judgment = scored_judgment(
        _verdict(
            assets=[TriageAsset(symbol="UWMC", role="primary", market_type="us_equity")],
            direction="bearish",
            scope="single_name",
            fact_kind="official_measure",
            headline_zh="地方监管新规直接改变 UWMC 业务",
        ),
        taxonomy=news_taxonomy(event_family="regulatory_legal", change_state="effective"),
    )

    _exact_decision(
        judgment,
        DecisionResult(
            final="push",
            override_rule="fact_kind_official_measure",
            throttled_by=None,
            rule_baseline="drop",
        ),
        facts=facts,
    )


@pytest.mark.parametrize(
    ("fact_kind", "headline_zh"),
    [
        pytest.param("statement", "地区官员重复既有表态", id="repeated-local-official"),
        pytest.param("recap", "重新讲述昨日已送达的地区数据", id="restated-local-data"),
    ],
)
def test_repeated_regional_statement_or_restated_local_data_is_held(fact_kind: str, headline_zh: str) -> None:
    judgment = scored_judgment(_verdict(scope="macro", fact_kind=fact_kind, headline_zh=headline_zh))

    _exact_decision(
        judgment,
        DecisionResult(
            final="drop",
            override_rule=f"fact_kind_{fact_kind}",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_scheduled_calendar_item_is_held() -> None:
    judgment = scored_judgment(_verdict(fact_kind="schedule", headline_zh="明日公布计划内经济数据"))

    _exact_decision(
        judgment,
        DecisionResult(
            final="drop",
            override_rule="fact_kind_schedule",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_both_model_payloads_exclude_queue_provider_and_other_editorial_hints() -> None:
    context = _context(provider_score=95, queue_priority="high")
    forbidden = {
        "priority",
        "queue_priority",
        "provider_score",
        "provider_score_max",
        "macro_lexicon",
        "queue_lag_ms",
        "queue_lag_s",
        "watchlist",
    }

    assert context.evidence.provider_score == 95
    assert context.evidence.queue_priority == "high"
    assert context.gate.macro_lexicon is True
    assert context.queue_lag_ms == 10_000
    for payload in (context.event_semantics_payload(), context.reader_card_payload()):
        assert forbidden.isdisjoint(_all_keys(payload))
