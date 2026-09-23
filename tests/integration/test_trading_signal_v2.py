"""V2 publication, scoped plan ownership and last Trading validity check."""

from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg.errors import UniqueViolation

from tests.integration.test_trading_analysis_storage import _selection
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.platform.market_identity import DEFAULT_UNIVERSE
from tracefold.trading.engine.policy import decision_identity
from tracefold.trading.execution_contracts import (
    SignalEntryEnvelopeV1,
    SignalExitPlanV1,
    TradeSignalV2,
    market_key,
)
from tracefold.trading.storage.execution_stream import prepare_trade_signal_v2
from tracefold.trading.storage.root import TradingRepository
from tracefold.trading.storage.trade_plans import prepare_trade_plan
from tracefold.trading.trade_plan import TradePlan

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_signal_v2_scope_and_pre_submit_check(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "signal-v2-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        payload = {
            "source_recorded_at_ms": 1_000,
            "provider_event_at_ms": 900,
            "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
        }
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-1",
                source_revision="oi-v1",
                payload_sha256="a" * 64,
                payload=payload,
                selection=_selection(),
                now_ms=1_100,
                root_ttl_ms=10_000,
            )
            case = trading.claim_analysis_case(now_ms=1_500, lease_ms=5_000)
        assert case is not None
        decision = {
            "action": "TRADE",
            "side": "short",
            "reason": "frozen fixture",
            "exit_plan": {"stop_distance_bps": 150, "take_profit_bps": 300, "max_holding_seconds": 3_600},
        }
        decision_id = decision_identity(case_id, decision)
        signal = TradeSignalV2(
            seq=1,
            signal_id="b" * 64,
            case_id=case_id,
            decision_id=decision_id,
            account_slot="binance_usdm_primary",
            runtime_mode="paper",
            entry_scope_id=case["entry_scope_id"],
            asset_id="crypto:SOL",
            market_key=market_key("SOL"),
            native_symbol="SOLUSDT",
            mapping_semantics_digest=case["mapping_semantics_digest"],
            direction="short",
            observed_at_ns=2_000_000_000,
            expires_at_ns=6_000_000_000,
            exit_plan=SignalExitPlanV1(stop_distance_bps=150, take_profit_bps=300, max_holding_ns=3_600_000_000_000),
            entry_envelope=SignalEntryEnvelopeV1(
                root_expires_at_ns=11_000_000_000,
                reference_price=Decimal("100"),
                max_price_drift_bps=200,
                universe_version=DEFAULT_UNIVERSE.digest,
            ),
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
                prepared_signal=prepare_trade_signal_v2(signal),
            )
        assert trading.unresolved_trade_signals(
            account_slot="binance_usdm_primary",
            execution_strategy="oi_nautilus_v1",
            now_ns=2_100_000_000,
            limit=10,
            runtime_mode="paper",
        ) == ((1, signal.model_dump(mode="json", exclude={"seq"})),)
        assert (
            trading.unresolved_trade_signals(
                account_slot="binance_usdm_primary",
                execution_strategy="oi_nautilus_v1",
                now_ns=2_100_000_000,
                limit=10,
                runtime_mode="live",
            )
            == ()
        )
        plan = TradePlan(
            entry_id=signal.signal_id,
            entry_scope_id=signal.entry_scope_id,
            source="signal",
            case_id=case_id,
            account_slot=signal.account_slot,
            runtime_mode_at_creation="paper",
            market_key=signal.market_key,
            instrument_id="SOLUSDT-PERP.BINANCE",
            direction="short",
            entry_client_order_id="tf" + "b" * 30,
            created_at_ns=2_200_000_000,
            entry_expires_at_ns=signal.expires_at_ns,
            entry_quantity=Decimal("1"),
            stop_distance_bps=150,
            risk_budget_usd=Decimal("10"),
            max_leverage_at_creation=2,
            exit_policy_id="analysis_dynamic_v1",
            take_profit_bps=300,
            max_holding_ns=3_600_000_000_000,
            updated_at_ns=2_200_000_000,
        )
        with conn.transaction():
            assert trading.insert_trade_plan(prepare_trade_plan(plan))
            assert trading.validate_signal_entry(entry_id=plan.entry_id, now_ns=2_500_000_000) == (True, "valid")
        rival = plan.model_copy(update={"entry_id": "c" * 64, "entry_client_order_id": "tf" + "c" * 30})
        with pytest.raises(UniqueViolation), conn.transaction():
            trading.insert_trade_plan(prepare_trade_plan(rival))
        with conn.transaction():
            _, revised_case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-1",
                source_revision="oi-v2",
                payload_sha256="d" * 64,
                payload=payload | {"source_recorded_at_ms": 2_550},
                selection=_selection(),
                now_ms=2_600,
                root_ttl_ms=10_000,
            )
            revised = conn.execute(
                "SELECT entry_scope_id FROM trading_cases WHERE case_id=%s",
                (revised_case_id,),
            ).fetchone()
            assert revised["entry_scope_id"] == signal.entry_scope_id
            assert trading.validate_signal_entry(entry_id=plan.entry_id, now_ns=2_700_000_000) == (
                False,
                "source_superseded",
            )
    finally:
        conn.close()
