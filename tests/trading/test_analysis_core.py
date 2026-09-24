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
        entry_candidate_id="SOL-long",
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


def _catalog() -> dict[str, dict[str, object]]:
    return {
        "e:price": {
            "status": "ok",
            "values": {"close": "100"},
            "unit_definition": "quote_per_base_v1",
            "event_at_ms": 1,
            "received_at_ms": 1,
            "knowledge_cutoff_ms": 2,
        }
    }


def test_agent_score_is_recomputed_and_not_called_ev() -> None:
    decision = compile_assessment(
        assessment=_assessment(),
        brief_sha="a" * 64,
        candidate_menu_sha="b" * 64,
        candidates=(_candidate(),),
        evidence_catalog=_catalog(),
    )
    assert decision.action == "TRADE" and decision.side == "long"
    assert decision.scores[0].value == Decimal(40)
    assert decision.scores[0].kind == "agent_support"


@pytest.mark.parametrize(
    "assessment,reason",
    [
        (_assessment(evidence="invented"), "assessment_evidence_ref_unknown"),
        (
            _assessment(unknown=True).model_copy(update={"entry_candidate_id": "outside"}),
            "trade_candidate_not_assessed",
        ),
    ],
)
def test_invalid_or_partial_agent_output_cannot_trade(assessment: AgentAssessment, reason: str) -> None:
    with pytest.raises(InvalidAssessment, match=reason):
        compile_assessment(
            assessment=assessment,
            brief_sha="a" * 64,
            candidate_menu_sha="b" * 64,
            candidates=(_candidate(),),
            evidence_catalog=_catalog(),
        )


def test_watch_uses_a_frozen_candidate_level_not_a_model_delay() -> None:
    assessment = _assessment().model_copy(
        update={
            "action": "WATCH",
            "entry_candidate_id": None,
            "hypothesis_side": "long",
            "watch_intent": "closed_1m_price_crosses",
            "observation_note": "wait for a fresh close",
        }
    )
    decision = compile_assessment(
        assessment=assessment,
        brief_sha="a" * 64,
        candidate_menu_sha="b" * 64,
        candidates=(
            _candidate().model_copy(update={"entry_ready": False, "entry_level": Decimal(101), "watch_eligible": True}),
        ),
        evidence_catalog=_catalog(),
        watch_expires_at_ms=100_000,
    )
    assert decision.action == "WATCH"
    assert decision.watch_condition is not None
    assert decision.watch_condition.level == Decimal(101)
    assert decision.watch_condition.kind == "closed_1m_price_crosses"


def test_watch_requires_cited_available_entry_evidence() -> None:
    assessment = _assessment().model_copy(
        update={
            "action": "WATCH",
            "entry_candidate_id": None,
            "hypothesis_side": "long",
            "watch_intent": "closed_1m_price_crosses",
        }
    )
    candidate = _candidate().model_copy(
        update={"entry_ready": False, "watch_eligible": True, "required_evidence_refs": ("e:price", "e:oi")}
    )
    with pytest.raises(InvalidAssessment, match="watch_required_evidence_missing"):
        compile_assessment(
            assessment=assessment,
            brief_sha="a" * 64,
            candidate_menu_sha="b" * 64,
            candidates=(candidate,),
            evidence_catalog=_catalog(),
            watch_expires_at_ms=100_000,
        )


def test_known_missing_optional_frame_cannot_authorize_entry() -> None:
    assessment = _assessment(evidence="market:spot_bars")
    catalog = _catalog() | {
        "market:spot_bars": {
            "status": "missing",
            "values": {},
            "unit_definition": "quote_per_base_v1",
            "event_at_ms": None,
            "received_at_ms": None,
            "knowledge_cutoff_ms": 2,
        }
    }
    with pytest.raises(InvalidAssessment, match="known_factor_evidence_unavailable"):
        compile_assessment(
            assessment=assessment,
            brief_sha="a" * 64,
            candidate_menu_sha="b" * 64,
            candidates=(_candidate(),),
            evidence_catalog=catalog,
        )


def test_nontrade_observation_and_direction_hypothesis_are_valid() -> None:
    assessment = _assessment().model_copy(
        update={
            "action": "NO_TRADE",
            "entry_candidate_id": None,
            "hypothesis_side": "long",
            "observation_note": "Watch resistance manually.",
        }
    )
    decision = compile_assessment(
        assessment=assessment,
        brief_sha="a" * 64,
        candidate_menu_sha="b" * 64,
        candidates=(_candidate(),),
        evidence_catalog=_catalog(),
    )
    assert decision.action == "NO_TRADE"
    assert decision.hypothesis_side == "long"
    assert decision.observation_note == "Watch resistance manually."
