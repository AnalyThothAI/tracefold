"""Regression cases for target identity and recorded Agent decision compilation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.app.trading_analysis import _registry_from_rows
from tracefold.platform.config.models import TradingVerifiedRouteSettings
from tracefold.platform.market_identity import (
    DEFAULT_UNIVERSE,
    AssetId,
    AssetRegistry,
    InstrumentRef,
    VerifiedAlias,
)
from tracefold.trading.engine.contracts import (
    AgentAssessment,
    Candidate,
    CandidateAssessment,
    ExitPlan,
    FactorAssessment,
    WatchCondition,
)
from tracefold.trading.engine.policy import InvalidAssessment, compile_assessment
from tracefold.trading.engine.target import SourceAsset, select_target


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


def _assessment(*, evidence: str = "e:price", unknown: bool = False) -> AgentAssessment:
    factors = tuple(
        FactorAssessment(
            factor_id=factor,
            weight_bps=5_000 if index < 2 else 0,
            support_score=None if unknown and index == 0 else 40,
            status="unknown" if unknown and index == 0 else "known",
            evidence_refs=() if unknown and index == 0 else (evidence,),
        )
        for index, factor in enumerate(
            (
                "catalyst",
                "price_structure",
                "volume_and_oi",
                "crowding",
                "entry_timing",
                "trading_cost",
            )
        )
    )
    return AgentAssessment(
        brief_sha="a" * 64,
        candidate_menu_sha="b" * 64,
        action="TRADE",
        selected_candidate_id="SOL-long",
        candidate_assessments=(CandidateAssessment(candidate_id="SOL-long", factors=factors),),
        supporting_evidence=(evidence,),
        public_rationale="Evidence supports this candidate.",
    )


def _candidate() -> Candidate:
    return Candidate(
        candidate_id="SOL-long",
        asset_id="crypto:SOL",
        instrument_semantics_digest="c" * 64,
        side="long",
        exit_plan=ExitPlan(stop_distance_bps=200, take_profit_bps=300, max_holding_seconds=14_400),
        required_evidence_refs=("e:price",),
    )


def test_agent_score_is_recomputed_and_not_called_ev() -> None:
    decision = compile_assessment(
        assessment=_assessment(),
        brief_sha="a" * 64,
        candidate_menu_sha="b" * 64,
        candidates=(_candidate(),),
        evidence_refs=frozenset({"e:price"}),
    )
    assert decision.action == "TRADE" and decision.side == "long"
    assert decision.scores[0].value == Decimal(40)
    assert decision.scores[0].kind == "agent_support"


@pytest.mark.parametrize(
    "assessment,reason",
    [
        (_assessment(evidence="invented"), "assessment_evidence_ref_unknown"),
        (_assessment(unknown=True), "trade_assessment_partial"),
    ],
)
def test_invalid_or_partial_agent_output_cannot_trade(assessment: AgentAssessment, reason: str) -> None:
    with pytest.raises(InvalidAssessment, match=reason):
        compile_assessment(
            assessment=assessment,
            brief_sha="a" * 64,
            candidate_menu_sha="b" * 64,
            candidates=(_candidate(),),
            evidence_refs=frozenset({"e:price"}),
        )


def test_watch_requires_a_bounded_recheck_time_and_keeps_its_condition() -> None:
    assessment = _assessment().model_copy(
        update={
            "action": "WATCH",
            "selected_candidate_id": None,
            "watch_condition": WatchCondition(
                kind="price_retest", detail="wait for a fresh close", due_after_seconds=90
            ),
        }
    )
    decision = compile_assessment(
        assessment=assessment,
        brief_sha="a" * 64,
        candidate_menu_sha="b" * 64,
        candidates=(_candidate(),),
        evidence_refs=frozenset({"e:price"}),
    )
    assert decision.action == "WATCH"
    assert decision.watch_condition is not None
    assert decision.watch_condition.due_after_seconds == 90
