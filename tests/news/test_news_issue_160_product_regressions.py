"""Fixed product behavior cases for the Issue #160 editorial hard cut."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest

from tests.support.news_judgment import scored_judgment, trade_relevance
from tracefold.news.models import TriageAsset, TriageVerdict
from tracefold.news.semantic_contract import ScoredJudgment, TriageContext
from tracefold.news.triage_rules import DecisionResult, GateFacts, decide

_NO_OBJECTIVE_GUARD = GateFacts(
    grounded_assets=(),
    watchlist_symbols=frozenset(),
    admission="candidate",
)


def _verdict(**overrides: Any) -> TriageVerdict:
    values: dict[str, Any] = {
        "novelty": "new_fact",
        "event_type": "macro",
        "assets": [],
        "direction": "neutral",
        "scope": "macro",
        "magnitude": 2,
        "actionable": False,
        "confidence": 0.9,
        "decision": "drop",
        "audience": "macro",
        "headline_zh": "固定产品回归案例",
        "title_zh": "",
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
            "evidence_version": 1,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-160",
            "reporting_origin": "official",
            "provenance": ["1018"],
            "leader_title": "Local official repeats an in-line statement",
            "leader_description": "No new priced transmission was reported.",
            "opened_at_ms": 1_000_000,
            "family": "general",
            "provider_score_max": provider_score,
            "queue_priority": queue_priority,
            "asset_class": "macro",
            "macro_lexicon": True,
            "grounded_assets": [],
            "storyline_key": "theme:local_official",
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
    context = _context(provider_score=95)
    relevance = trade_relevance(
        impact_breadth="single_instrument",
        tradability="contextual",
        surprise="unknown",
        development_delta="color_only",
        channels=[],
        affected_markets=[],
        reader_value="background",
    )
    judgment = scored_judgment(
        _verdict(
            event_type="regulation",
            scope="single_name",
            magnitude=3,
            headline_zh="地方官员重复既有表态",
        ),
        relevance=relevance,
    )

    assert context.evidence.provider_score == 95
    _exact_decision(
        judgment,
        DecisionResult(
            final="drop",
            override_rule="reader_value_background",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_queue_high_or_macro_scope_does_not_create_reader_urgency() -> None:
    context = _context(provider_score=95, queue_priority="high")
    relevance = trade_relevance(
        impact_breadth="none",
        tradability="contextual",
        surprise="in_line",
        development_delta="color_only",
        channels=[],
        affected_markets=[],
        reader_value="background",
    )
    judgment = scored_judgment(
        _verdict(scope="macro", magnitude=3, headline_zh="宏观标签本身不构成打断理由"),
        relevance=relevance,
    )

    assert context.evidence.queue_priority == "high"
    assert {item.name for item in fields(GateFacts)}.isdisjoint(
        {"priority", "queue_priority", "provider_score", "provider_score_max", "macro_lexicon"}
    )
    _exact_decision(
        judgment,
        DecisionResult(
            final="drop",
            override_rule="reader_value_background",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_unexpected_fed_cut_with_rates_and_liquidity_escalates() -> None:
    judgment = scored_judgment(
        _verdict(
            event_type="rates",
            direction="bullish",
            magnitude=3,
            actionable=True,
            decision="escalate",
            headline_zh="美联储意外降息",
        ),
        relevance=trade_relevance(
            impact_breadth="global_systemic",
            tradability="direct",
            surprise="unscheduled",
            development_delta="state_change",
            channels=["rates", "liquidity"],
            affected_markets=["rates", "fx", "us_equity_broad", "crypto_broad"],
            reader_value="escalate",
        ),
    )

    _exact_decision(
        judgment,
        DecisionResult(
            final="escalate",
            override_rule="trade_relevance_escalate",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_official_hormuz_closure_with_energy_and_risk_escalates() -> None:
    judgment = scored_judgment(
        _verdict(
            direction="bearish",
            magnitude=3,
            actionable=True,
            decision="escalate",
            headline_zh="霍尔木兹海峡正式关闭",
        ),
        relevance=trade_relevance(
            impact_breadth="global_systemic",
            tradability="direct",
            surprise="unscheduled",
            development_delta="state_change",
            channels=["energy_supply", "risk_premium"],
            affected_markets=["energy", "us_equity_broad", "crypto_broad"],
            reader_value="escalate",
        ),
    )

    _exact_decision(
        judgment,
        DecisionResult(
            final="escalate",
            override_rule="trade_relevance_escalate",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_regional_port_supply_state_change_is_realtime() -> None:
    judgment = scored_judgment(
        _verdict(
            scope="sector",
            magnitude=2,
            actionable=True,
            decision="push",
            headline_zh="地区港口停运中断商品供应",
        ),
        relevance=trade_relevance(
            impact_breadth="regional",
            tradability="second_order",
            surprise="unscheduled",
            development_delta="state_change",
            channels=["commodity_supply", "risk_premium"],
            affected_markets=["energy", "single_asset"],
            reader_value="realtime",
        ),
    )

    _exact_decision(
        judgment,
        DecisionResult(
            final="push",
            override_rule="trade_relevance_realtime",
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_material_local_regulation_for_us_listed_single_name_is_realtime() -> None:
    facts = GateFacts(
        grounded_assets=("UWMC",),
        watchlist_symbols=frozenset(),
        admission="candidate",
    )
    judgment = scored_judgment(
        _verdict(
            event_type="regulation",
            assets=[TriageAsset(symbol="UWMC", role="primary", market_type="us_equity")],
            direction="bearish",
            scope="single_name",
            magnitude=2,
            actionable=True,
            decision="push",
            audience="us_equity",
            headline_zh="地方监管新规直接改变 UWMC 业务",
        ),
        relevance=trade_relevance(
            impact_breadth="single_instrument",
            tradability="direct",
            surprise="unknown",
            development_delta="material_detail",
            channels=["regulation", "earnings_cashflow"],
            affected_markets=["single_asset"],
            reader_value="realtime",
        ),
    )

    _exact_decision(
        judgment,
        DecisionResult(
            final="push",
            override_rule="trade_relevance_realtime",
            throttled_by=None,
            rule_baseline="drop",
        ),
        facts=facts,
    )


@pytest.mark.parametrize(
    ("surprise", "reader_value", "expected_rule"),
    [
        pytest.param("unknown", "background", "reader_value_background", id="repeated-local-official"),
        pytest.param("in_line", "none", "reader_value_none", id="in-line-local-data"),
    ],
)
def test_repeated_regional_statement_or_in_line_local_data_is_held(
    surprise: str,
    reader_value: str,
    expected_rule: str,
) -> None:
    judgment = scored_judgment(
        _verdict(scope="macro", magnitude=2, headline_zh="地区官员重复表态或数据符合预期"),
        relevance=trade_relevance(
            impact_breadth="regional",
            tradability="contextual",
            surprise=surprise,
            development_delta="color_only",
            channels=[],
            affected_markets=[],
            reader_value=reader_value,
        ),
    )

    _exact_decision(
        judgment,
        DecisionResult(
            final="drop",
            override_rule=expected_rule,
            throttled_by=None,
            rule_baseline="drop",
        ),
    )


def test_scheduled_calendar_has_no_reader_value() -> None:
    judgment = scored_judgment(
        _verdict(magnitude=1, headline_zh="明日公布计划内经济数据"),
        relevance=trade_relevance(
            impact_breadth="none",
            tradability="none",
            surprise="unknown",
            development_delta="scheduled",
            channels=[],
            affected_markets=[],
            reader_value="none",
        ),
    )

    _exact_decision(
        judgment,
        DecisionResult(
            final="drop",
            override_rule="reader_value_none",
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
