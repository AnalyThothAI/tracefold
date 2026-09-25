"""V2 is frozen before a plan and gated again before an order."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tests.nautilus_oi_runtime_fixtures import NOW_NS, SECOND_NS, oi_profile, unit_runtime
from tracefold.integrations.nautilus.oi_runtime.journal import EntryValidityReceipt
from tracefold.trading.execution_contracts import (
    SignalEntryEnvelopeV2,
    SignalExitPlanV1,
    TradeSignalV2,
    entry_structure_allows,
)


def _signal() -> TradeSignalV2:
    profile = replace(oi_profile(), excluded_asset_ids=frozenset())
    semantics = profile.route_semantics(profile.routes[0])
    assert semantics is not None
    return TradeSignalV2(
        seq=1,
        signal_id="1" * 64,
        case_id="case-v2",
        decision_id="2" * 64,
        account_slot=profile.account_slot,
        runtime_mode=profile.mode,
        entry_scope_id="3" * 64,
        asset_id=semantics[0],
        market_key="crypto:perp:BTC:USDT",
        native_symbol="BTCUSDT",
        mapping_semantics_digest=semantics[1],
        direction="short",
        observed_at_ns=NOW_NS - SECOND_NS,
        expires_at_ns=NOW_NS + 30 * SECOND_NS,
        exit_plan=SignalExitPlanV1(stop_distance_bps=100, take_profit_bps=250, max_holding_ns=1_800 * SECOND_NS),
        entry_envelope=SignalEntryEnvelopeV2(
            root_expires_at_ns=NOW_NS + 60 * SECOND_NS,
            reference_price=Decimal("10000"),
            structure_level=Decimal("10100"),
            max_price_drift_bps=200,
            universe_version=profile.universe_digest,
        ),
    )


def test_dynamic_short_plan_waits_for_final_check_before_submitting() -> None:
    profile = replace(oi_profile(), excluded_asset_ids=frozenset())
    runtime = unit_runtime(signals=(_signal(),), profile=profile)
    runtime.pump()
    plan = runtime.settle()
    assert plan is not None
    assert plan.direction == "short"
    assert (
        plan.entry_scope_id,
        plan.stop_distance_bps,
        plan.take_profit_bps,
        plan.max_holding_ns,
        plan.exit_policy_id,
    ) == ("3" * 64, 100, 250, 1_800 * SECOND_NS, "analysis_dynamic_v1")
    assert runtime.strategy.submitted == []
    assert runtime.journal.pending_entry_validity() == plan
    runtime.journal.settle_entry_validity(
        EntryValidityReceipt(
            entry_id=plan.entry_id,
            allowed=True,
            reason="valid",
            checked_at_ns=NOW_NS,
        )
    )
    runtime.pump()
    assert len(runtime.strategy.submitted) == 1
    assert runtime.strategy.submitted[0][0].side.name == "SELL"


def test_failed_final_check_ends_plan_without_order() -> None:
    profile = replace(oi_profile(), excluded_asset_ids=frozenset())
    runtime = unit_runtime(signals=(_signal(),), profile=profile)
    runtime.pump()
    plan = runtime.settle()
    assert plan is not None
    runtime.journal.settle_entry_validity(
        EntryValidityReceipt(
            entry_id=plan.entry_id,
            allowed=False,
            reason="source_superseded",
            checked_at_ns=NOW_NS,
        )
    )
    runtime.pump()
    assert runtime.strategy.submitted == []
    assert {"disposition": "source_superseded"} in runtime.dispositions()
    assert runtime.plans()[0].status == "closed"


def test_changed_asset_mapping_is_refused_before_plan() -> None:
    profile = replace(oi_profile(), excluded_asset_ids=frozenset())
    signal = _signal().model_copy(update={"mapping_semantics_digest": "4" * 64})
    runtime = unit_runtime(signals=(signal,), profile=profile)
    runtime.pump()
    assert runtime.settle() is None
    assert {"disposition": "mapping_changed"} in runtime.dispositions()


def test_changed_asset_mapping_is_refused_again_before_order() -> None:
    profile = replace(oi_profile(), excluded_asset_ids=frozenset())
    runtime = unit_runtime(signals=(_signal(),), profile=profile)
    runtime.pump()
    plan = runtime.settle()
    assert plan is not None
    runtime.strategy._profile = replace(
        profile,
        verified_routes=(("BTCUSDT", "crypto:WBTC", Decimal(1)),),
    )
    runtime.journal.settle_entry_validity(
        EntryValidityReceipt(
            entry_id=plan.entry_id,
            allowed=True,
            reason="valid",
            checked_at_ns=NOW_NS,
        )
    )
    runtime.pump()
    assert runtime.strategy.submitted == []
    assert {"disposition": "mapping_changed"} in runtime.dispositions()


def test_structure_is_refused_before_plan_and_before_final_order() -> None:
    profile = replace(oi_profile(), excluded_asset_ids=frozenset())
    invalid = _signal().model_copy(
        update={"entry_envelope": _signal().entry_envelope.model_copy(update={"structure_level": Decimal("9998")})}
    )
    runtime = unit_runtime(signals=(invalid,), profile=profile)
    runtime.pump()
    assert runtime.settle() is None
    assert {"disposition": "entry_structure_lost"} in runtime.dispositions()

    runtime = unit_runtime(signals=(_signal(),), profile=profile)
    runtime.pump()
    plan = runtime.settle()
    assert plan is not None
    runtime.add_quote(10_101, 10_102)
    runtime.journal.settle_entry_validity(
        EntryValidityReceipt(entry_id=plan.entry_id, allowed=True, reason="valid", checked_at_ns=NOW_NS)
    )
    runtime.pump()
    assert runtime.strategy.submitted == []
    assert {"disposition": "entry_structure_lost"} in runtime.dispositions()


def test_long_and_short_structure_use_executable_side() -> None:
    assert entry_structure_allows(direction="long", executable=Decimal("101.01"), level=Decimal("101"))
    assert not entry_structure_allows(direction="long", executable=Decimal("101"), level=Decimal("101"))
    assert entry_structure_allows(direction="short", executable=Decimal("98.99"), level=Decimal("99"))
    assert not entry_structure_allows(direction="short", executable=Decimal("99"), level=Decimal("99"))
