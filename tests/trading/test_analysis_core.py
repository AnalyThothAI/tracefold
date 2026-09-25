"""Regression cases for target identity and recorded Agent decision compilation."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tracefold.app.trading_analysis import AnalysisRunner, _registry_from_rows
from tracefold.platform.config.models import TradingVerifiedRouteSettings
from tracefold.platform.market_identity import (
    DEFAULT_UNIVERSE,
    AssetId,
    AssetRegistry,
    InstrumentRef,
    VerifiedAlias,
)
from tracefold.trading.engine.brief import build_brief
from tracefold.trading.engine.contracts import (
    AgentAssessment,
    Candidate,
)
from tracefold.trading.engine.policy import InvalidAssessment, compile_assessment
from tracefold.trading.engine.target import SourceAsset, select_target


def test_paper_signal_rejects_a_frozen_mainnet_case() -> None:
    runner = SimpleNamespace(settings=SimpleNamespace(trading=SimpleNamespace(execution=SimpleNamespace(mode="paper"))))
    case = {"target_selection": {"instrument": {"native_symbol": "SOLUSDT", "environment": "live"}}}
    decision = {"side": "long", "exit_plan": {"stop_distance_bps": 200}}

    with pytest.raises(ValueError, match="analysis_execution_environment_mismatch"):
        AnalysisRunner._prepare_signal(runner, case, None, decision)


def _route(symbol: str, asset: str, *, environment: str = "live") -> InstrumentRef:
    return InstrumentRef(
        venue="binance.usdm",
        environment=environment,
        product="perpetual",
        native_symbol=symbol,
        asset_id=AssetId("crypto", asset),
        quote_asset="USDT",
        settlement_asset="USDT",
        units_per_contract=Decimal(1),
        price_unit="USDT/coin",
        quantity_unit="coin",
    )


def _registry() -> AssetRegistry:
    return AssetRegistry(
        snapshot_ref="catalogue-1",
        instruments=(
            _route("BTCUSDT", "BTC"),
            _route("SOLUSDT", "SOL"),
            _route("BCHUSDT", "BCH"),
            _route("UNIUSDT", "UNI"),
        ),
        aliases=(
            VerifiedAlias("crypto", "1000BTC", AssetId("crypto", "BTC"), Decimal(1000), "reviewed:BTC-multiplier"),
        ),
    )


def test_news_selects_only_one_eligible_primary_after_exclusion() -> None:
    selected = select_target(
        kind="catalyst",
        registry=_registry(),
        universe=DEFAULT_UNIVERSE,
        assets=(
            SourceAsset("BTC", "crypto", "primary"),
            SourceAsset("SOL", "crypto", "primary"),
            SourceAsset("BCH", "crypto", "mentioned"),
        ),
    )
    assert selected.reason == "selected"
    assert selected.asset_id == AssetId("crypto", "SOL")
    assert selected.instrument is not None and selected.instrument.native_symbol == "SOLUSDT"


def test_two_eligible_primaries_do_not_pick_array_first() -> None:
    selected = select_target(
        kind="catalyst",
        registry=_registry(),
        universe=DEFAULT_UNIVERSE,
        assets=(SourceAsset("BCH", "crypto", "primary"), SourceAsset("UNI", "crypto", "primary")),
    )
    assert selected.reason == "focus_ambiguous"
    assert selected.instrument is None


def test_verified_alias_and_quote_asset_do_not_change_exclusion() -> None:
    registry = _registry()
    assert registry.resolve("1000BTC").asset_id == AssetId("crypto", "BTC")
    assert (
        select_target(
            kind="oi",
            registry=registry,
            universe=DEFAULT_UNIVERSE,
            assets=(SourceAsset("1000BTC", "crypto", "primary"),),
        ).reason
        == "excluded_asset"
    )
    assert (
        select_target(
            kind="oi",
            registry=registry,
            universe=DEFAULT_UNIVERSE,
            assets=(SourceAsset("SOLUSDT", "crypto", "primary"),),
        ).reason
        == "selected"
    )


def test_multiplier_contract_requires_reviewed_asset_and_units() -> None:
    rows = [
        {
            "venue": "binance.perp",
            "instrument_class": "crypto",
            "venue_symbol": "1000PEPEUSDT",
            "base_symbol": "1000PEPE",
            "quote_asset": "USDT",
            "observed_at_ms": 1,
        }
    ]
    unreviewed = _registry_from_rows(rows, environment="demo", universe=DEFAULT_UNIVERSE, verified_routes=[])
    assert unreviewed.resolve("PEPE", execution_environment="demo").status == "unknown"
    reviewed = _registry_from_rows(
        rows,
        environment="demo",
        universe=DEFAULT_UNIVERSE,
        verified_routes=[
            TradingVerifiedRouteSettings(
                source_symbol="PEPE",
                asset_id="crypto:PEPE",
                native_symbol="1000PEPEUSDT",
                units_per_contract=Decimal(1000),
                evidence_ref="reviewed:binance-native-contract",
            )
        ],
    )
    result = reviewed.resolve("PEPE", execution_environment="demo")
    assert result.asset_id == AssetId("crypto", "PEPE")
    assert result.instrument is not None
    assert result.instrument.units_per_contract == Decimal(1000)


def _assessment(*, evidence: str = "source", action: str = "TRADE", candidate_id: str | None = None) -> AgentAssessment:
    return AgentAssessment(
        action=action,
        entry_candidate_id=candidate_id
        if candidate_id is not None
        else "crypto:SOL:long:event_price_confirmation_v1"
        if action == "TRADE"
        else None,
        hypothesis_side="short",
        supporting_evidence=(evidence,),
        public_rationale="Frozen facts support the proposal.",
        research_notes="Observe the next closed bar.",
    )


def _candidate(*, close: str = "102") -> tuple[Candidate, ...]:
    from tracefold.trading.engine.strategy import build_event_price_candidates

    bars = (
        *({"event_at_ms": (index + 1) * 60_000, "close": "100", "high": "101", "low": "99"} for index in range(15)),
        {"event_at_ms": 960_000, "close": close, "high": "103", "low": "98"},
    )
    return build_event_price_candidates(
        asset_id="crypto:SOL",
        instrument_semantics_digest="c" * 64,
        source_fact={"kind": "catalyst", "headline": "A visible event"},
        source_first_visible_at_ms=930_000,
        perp_rows=bars,
    )


def _catalog() -> dict[str, dict[str, object]]:
    return {
        ref: {
            "status": "ok",
            "values": {"close": "102"},
            "unit_definition": "USDT/base_asset",
            "event_at_ms": 960_000,
            "received_at_ms": 960_001,
            "knowledge_cutoff_ms": 970_000,
        }
        for ref in ("source", "market:perp_bars")
    }


def test_frozen_brief_lists_exactly_compiler_citable_evidence() -> None:
    catalog = _catalog()
    catalog["feature:source_oi_value_usd"] = {**catalog["source"], "status": "missing"}
    catalog["feature:future"] = {**catalog["source"], "received_at_ms": 970_001}
    brief = build_brief(
        target_asset_id="crypto:SOL",
        instrument_semantics_digest="c" * 64,
        source_fact={"kind": "catalyst"},
        source_history=(),
        evidence=catalog,
        features={},
        candidates=_candidate(),
    )
    payload = json.loads(brief.text)
    assert payload["brief_version"] == "trade_brief_v3"
    assert payload["citable_evidence_ids"] == ["market:perp_bars", "source"]
    for ref in payload["citable_evidence_ids"]:
        compile_assessment(assessment=_assessment(evidence=ref), candidates=_candidate(), evidence_catalog=catalog)
    for ref in ("feature:source_oi_value_usd", "feature:future"):
        with pytest.raises(InvalidAssessment, match="assessment_evidence_unavailable"):
            compile_assessment(assessment=_assessment(evidence=ref), candidates=_candidate(), evidence_catalog=catalog)


def test_compiler_accepts_only_frozen_ready_candidate() -> None:
    decision = compile_assessment(assessment=_assessment(), candidates=_candidate(), evidence_catalog=_catalog())
    assert decision.action == "TRADE" and decision.side == "long"
    assert decision.reason_code == "confirmed_entry"


@pytest.mark.parametrize("evidence", ["invented", "market:spot_bars"])
def test_unknown_or_unavailable_evidence_reference_is_invalid(evidence: str) -> None:
    with pytest.raises(InvalidAssessment, match="assessment_evidence"):
        compile_assessment(
            assessment=_assessment(evidence=evidence),
            candidates=_candidate(),
            evidence_catalog=_catalog(),
        )


def test_unready_candidate_is_a_recorded_proposal_with_code_refusal() -> None:
    decision = compile_assessment(
        assessment=_assessment(), candidates=_candidate(close="100"), evidence_catalog=_catalog()
    )
    assert decision.action == "NO_TRADE"
    assert decision.reason_code == "entry_condition_unmet"


def test_candidate_outside_menu_is_invalid_identity() -> None:
    with pytest.raises(InvalidAssessment, match="trade_candidate_outside_menu"):
        compile_assessment(
            assessment=_assessment(candidate_id="wrong"),
            candidates=_candidate(),
            evidence_catalog=_catalog(),
        )


def test_watch_tracks_both_directions_independent_of_model_hypothesis() -> None:
    decision = compile_assessment(
        assessment=_assessment(action="WATCH"),
        candidates=_candidate(close="100"),
        evidence_catalog=_catalog(),
        watch_expires_at_ms=1_100_000,
    )
    assert decision.action == "WATCH"
    assert decision.hypothesis_side == "short"
    assert decision.watch_condition is not None
    assert decision.watch_condition.upper_level == Decimal(101)
    assert decision.watch_condition.lower_level == Decimal(99)
    assert decision.watch_condition.kind == "closed_1m_range_cross"


def test_nontrade_hypothesis_and_notes_are_preserved() -> None:
    decision = compile_assessment(
        assessment=_assessment(action="NO_TRADE"),
        candidates=_candidate(),
        evidence_catalog=_catalog(),
    )
    assert decision.action == "NO_TRADE"
    assert decision.hypothesis_side == "short"
    assert decision.research_notes == "Observe the next closed bar."
