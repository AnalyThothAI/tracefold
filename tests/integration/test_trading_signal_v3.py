"""The v4 plan is consumable through the real Signal SQL without a global breakout gate."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.integration.test_trading_analysis_storage import _selection
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.platform.market_identity import DEFAULT_UNIVERSE
from tracefold.trading.engine.policy import decision_identity
from tracefold.trading.execution_contracts import (
    SignalEntryEnvelopeV3,
    SignalExitPlanV1,
    TradeSignalV3,
    entry_condition_allows,
    market_key,
)
from tracefold.trading.storage.execution_stream import prepare_trade_signal_v3
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_immediate_plan_publishes_and_is_visible_to_runtime(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "signal-v3-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-v3",
                source_revision="oi-v1",
                payload_sha256="a" * 64,
                payload={
                    "kind": "oi",
                    "source_recorded_at_ms": 1_000,
                    "provider_event_at_ms": 900,
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                },
                selection=_selection(),
                now_ms=1_100,
                root_ttl_ms=10_000,
            )
            case = trading.claim_analysis_case(now_ms=1_500, lease_ms=5_000)
        assert case is not None
        decision = {
            "decision_version": "trade_decision_v4",
            "action": "TRADE",
            "side": "long",
            "selected_plan_id": "f" * 64,
            "reason": "fixture",
            "exit_plan": {"stop_distance_bps": 150, "take_profit_bps": 300, "max_holding_seconds": 3_600},
        }
        signal = TradeSignalV3(
            seq=1,
            signal_id="b" * 64,
            case_id=case_id,
            decision_id=decision_identity(case_id, decision),
            account_slot="binance_usdm_primary",
            runtime_mode="paper",
            entry_scope_id=case["entry_scope_id"],
            asset_id="crypto:SOL",
            market_key=market_key("SOL"),
            native_symbol="SOLUSDT",
            mapping_semantics_digest=case["mapping_semantics_digest"],
            direction="long",
            observed_at_ns=2_000_000_000,
            expires_at_ns=6_000_000_000,
            exit_plan=SignalExitPlanV1(
                stop_distance_bps=150,
                take_profit_bps=300,
                max_holding_ns=3_600_000_000_000,
            ),
            entry_envelope=SignalEntryEnvelopeV3(
                plan_id="f" * 64,
                entry_kind="immediate_entry_v1",
                root_expires_at_ns=11_000_000_000,
                reference_price=Decimal("100"),
                max_price_drift_bps=200,
                universe_version=DEFAULT_UNIVERSE.digest,
            ),
        )
        assert entry_condition_allows(
            direction="long",
            executable=Decimal("99"),
            envelope=signal.entry_envelope,
        )
        with conn.transaction():
            assert trading.finish_analysis_case(
                case_id=case_id,
                claim_token=case["claim_token"],
                now_ms=2_000,
                analysis_status="analyzed",
                evidence_ref="evidence",
                decision=decision,
                assessment_ref="assessment",
                prepared_signal=prepare_trade_signal_v3(signal),
            )
        assert trading.unresolved_trade_signals(
            account_slot="binance_usdm_primary",
            execution_strategy="oi_nautilus_v1",
            now_ns=2_100_000_000,
            limit=10,
            runtime_mode="paper",
        ) == ((1, signal.model_dump(mode="json", exclude={"seq"})),)
        assert trading.console_case(case_id=case_id)["analysis_decision"]["policy_version"] == "v4"
    finally:
        conn.close()


def test_parent_condition_remains_directional_in_runtime() -> None:
    envelope = SignalEntryEnvelopeV3(
        plan_id="a" * 64,
        entry_kind="closed_bar_cross_v1",
        parent_plan_id="b" * 64,
        root_expires_at_ns=10_000_000_000,
        reference_price=Decimal("100"),
        structure_level=Decimal("101"),
        max_price_drift_bps=200,
        universe_version=DEFAULT_UNIVERSE.digest,
    )
    assert not entry_condition_allows(direction="long", executable=Decimal("100"), envelope=envelope)
    assert entry_condition_allows(direction="long", executable=Decimal("102"), envelope=envelope)
