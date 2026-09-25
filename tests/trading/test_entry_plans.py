"""Plan identity and directed conditions are code facts, never model parameters."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.trading.engine.plans import AnalysisProposal, build_entry_plans, compile_proposal, directed_cross


def _rows() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "event_at_ms": (index + 1) * 60_000,
            "high": "102",
            "low": "98",
            "close": "100",
        }
        for index in range(16)
    )


def _plans(rows: tuple[dict[str, object], ...] | None = None):
    return build_entry_plans(
        asset_id="crypto:SOL",
        instrument_semantics_digest="a" * 64,
        source_revision="revision-1",
        source_fact={"kind": "oi", "oi_change_bps": 100, "measurement_definition": "USD"},
        source_first_visible_at_ms=10_000,
        root_expires_at_ms=1_500_000,
        perp_rows=_rows() if rows is None else rows,
    )


def _evidence():
    item = {
        "status": "ok",
        "values": {"value": "100"},
        "unit_definition": "USDT",
        "event_at_ms": 900_000,
        "received_at_ms": 950_000,
        "knowledge_cutoff_ms": 1_000_000,
    }
    return {"source": item, "market:perp_bars": item}


def test_same_inputs_reuse_plan_ids_and_missing_atr_is_no_plan() -> None:
    plans = _plans()
    assert len(plans) == 4
    assert {plan.kind for plan in plans} == {"immediate_entry_v1", "closed_bar_cross_v1"}
    assert [plan.plan_id for plan in plans] == [plan.plan_id for plan in _plans()]
    assert _plans(_rows()[:-1]) == ()
    changed = list(_rows())
    changed[-1] = {**changed[-1], "close": "101"}
    assert [plan.plan_id for plan in _plans(tuple(changed))] != [plan.plan_id for plan in plans]


def test_new_source_after_last_closed_bar_still_has_an_immediate_plan() -> None:
    plans = build_entry_plans(
        asset_id="crypto:SOL",
        instrument_semantics_digest="a" * 64,
        source_revision="revision-2",
        source_fact={"kind": "oi", "oi_change_bps": 100, "measurement_definition": "USD"},
        source_first_visible_at_ms=970_000,
        root_expires_at_ms=1_500_000,
        perp_rows=_rows(),
    )
    assert any(plan.kind == "immediate_entry_v1" for plan in plans)


def test_immediate_trade_and_one_direction_watch_compile() -> None:
    plans = _plans()
    immediate = next(plan for plan in plans if plan.kind == "immediate_entry_v1" and plan.side == "short")
    watch = next(plan for plan in plans if plan.kind == "closed_bar_cross_v1" and plan.side == "long")
    decision = compile_proposal(
        proposal=AnalysisProposal(selected_plan_id=immediate.plan_id, public_rationale="Sell now."),
        plans=plans,
        evidence_catalog=_evidence(),
        judgment_refs=frozenset(),
        now_ms=970_000,
    )
    assert decision.action == "TRADE" and decision.side == "short" and decision.watch_condition is None
    decision = compile_proposal(
        proposal=AnalysisProposal(selected_plan_id=watch.plan_id, public_rationale="Wait."),
        plans=plans,
        evidence_catalog=_evidence(),
        judgment_refs=frozenset(),
        now_ms=970_000,
    )
    assert decision.action == "WATCH" and decision.watch_condition is not None
    assert decision.watch_condition.side == "long"
    assert decision.watch_condition.plan_id == watch.plan_id
    assert directed_cross(side="long", previous=Decimal("100"), current=Decimal("103"), level=Decimal("102"))
    assert not directed_cross(side="long", previous=Decimal("100"), current=Decimal("97"), level=Decimal("102"))


def test_proposal_cannot_invent_plan_or_judgment() -> None:
    plans = _plans()
    with pytest.raises(ValueError, match="proposal_plan_outside_menu"):
        compile_proposal(
            proposal=AnalysisProposal(selected_plan_id="invented", public_rationale="Now."),
            plans=plans,
            evidence_catalog=_evidence(),
            judgment_refs=frozenset(),
            now_ms=970_000,
        )
    with pytest.raises(ValueError, match="proposal_judgment_ref_unknown"):
        compile_proposal(
            proposal=AnalysisProposal(selected_plan_id=None, judgment_refs=("invented",), public_rationale="Unknown."),
            plans=plans,
            evidence_catalog=_evidence(),
            judgment_refs=frozenset(),
            now_ms=970_000,
        )
