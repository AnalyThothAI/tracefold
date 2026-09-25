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
from tracefold.trading.engine.plans import AnalysisProposal, compile_proposal
from tracefold.trading.engine.policy import InvalidAssessment
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


def test_frozen_brief_lists_exactly_compiler_citable_evidence() -> None:
    item = {
        "status": "ok",
        "values": {"close": "102"},
        "unit_definition": "USDT/base_asset",
        "event_at_ms": 960_000,
        "received_at_ms": 960_001,
        "knowledge_cutoff_ms": 970_000,
    }
    catalog = {
        "source": item,
        "feature:missing": {**item, "status": "missing"},
        "feature:future": {**item, "received_at_ms": 970_001},
    }
    brief = build_brief(
        target_asset_id="crypto:SOL",
        instrument_semantics_digest="c" * 64,
        source_fact={"kind": "catalyst"},
        source_history=(),
        evidence=catalog,
        features={},
        plans=(),
    )
    payload = json.loads(brief.text)
    assert payload["brief_version"] == "trade_brief_v4"
    assert payload["citable_evidence_ids"] == ["source"]
    compile_proposal(
        proposal=AnalysisProposal(action="NO_TRADE", supporting_evidence=("source",), public_rationale="No plan."),
        plans=(),
        evidence_catalog=catalog,
        judgment_refs=frozenset(),
        now_ms=970_000,
    )
    for ref in ("feature:missing", "feature:future"):
        with pytest.raises(InvalidAssessment, match="proposal_evidence_unavailable"):
            compile_proposal(
                proposal=AnalysisProposal(action="NO_TRADE", supporting_evidence=(ref,), public_rationale="No plan."),
                plans=(),
                evidence_catalog=catalog,
                judgment_refs=frozenset(),
                now_ms=970_000,
            )
