"""A published V2 fact for real PostgreSQL and Nautilus integration fixtures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal

from tests.nautilus_oi_runtime_fixtures import MARKET, SECOND_NS, oi_profile
from tracefold.integrations.nautilus.oi_runtime.config import OiRuntimeProfile
from tracefold.trading.execution_contracts import (
    SignalEntryEnvelopeV1,
    SignalExitPlanV1,
    TradeSignalV2,
)
from tracefold.trading.storage.execution_stream import prepare_trade_signal_v2
from tracefold.trading.storage.root import TradingRepository


def execution_fixture_profile() -> OiRuntimeProfile:
    # BTC is excluded by the online universe. This old backtest instrument is
    # intentionally eligible only inside this isolated execution fixture.
    return replace(oi_profile(), excluded_asset_ids=frozenset())


def append_published_v2_signal(
    repo: TradingRepository,
    *,
    signal_id: str,
    case_id: str,
    observed_at_ns: int,
    expires_at_ns: int,
    max_holding_ns: int = 4 * 3_600 * SECOND_NS,
) -> str:
    profile = execution_fixture_profile()
    asset_id, mapping_digest = profile.route_semantics(profile.routes[0]) or (None, None)
    if asset_id is None or mapping_digest is None:
        raise AssertionError("fixture_route_unmapped")
    scope = hashlib.sha256(f"scope:{case_id}".encode()).hexdigest()
    decision_id = hashlib.sha256(f"decision:{case_id}".encode()).hexdigest()
    exit_plan = SignalExitPlanV1(
        stop_distance_bps=200,
        take_profit_bps=200,
        max_holding_ns=max_holding_ns,
    )
    root_expires_at_ns = expires_at_ns + 60 * SECOND_NS
    signal = TradeSignalV2(
        seq=1,
        signal_id=signal_id,
        case_id=case_id,
        decision_id=decision_id,
        account_slot=profile.account_slot,
        runtime_mode=profile.mode,
        entry_scope_id=scope,
        asset_id=asset_id,
        market_key=MARKET,
        native_symbol="BTCUSDT",
        mapping_semantics_digest=mapping_digest,
        direction="long",
        observed_at_ns=observed_at_ns,
        expires_at_ns=expires_at_ns,
        exit_plan=exit_plan,
        entry_envelope=SignalEntryEnvelopeV1(
            root_expires_at_ns=root_expires_at_ns,
            reference_price=Decimal("10000"),
            max_price_drift_bps=200,
            universe_version=profile.universe_digest,
        ),
    )
    decision = {
        "action": "TRADE",
        "side": "long",
        "reason": "integration fixture",
        "exit_plan": {
            "stop_distance_bps": 200,
            "take_profit_bps": 200,
            "max_holding_seconds": max_holding_ns // SECOND_NS,
        },
    }
    at_ms = observed_at_ns // 1_000_000
    with repo.conn.transaction():
        repo.conn.execute(
            "INSERT INTO trading_cases "
            "(case_id,underlying_key,trigger_kind,primary_source_key,manifest,manifest_sha256,"
            "state,policy_decision,policy_reason,observed_at_ms,created_at_ms,decided_at_ms,updated_at_ms,"
            "target_asset_id,entry_scope_id,mapping_semantics_digest,root_expires_at_ms) "
            "VALUES (%s,%s,'oi',%s,'{}'::jsonb,%s,'SIGNAL_EMITTED','long','fixture',"
            "%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                case_id,
                asset_id,
                f"runtime-source:{case_id}",
                "4" * 64,
                at_ms,
                at_ms,
                at_ms,
                at_ms,
                asset_id,
                scope,
                mapping_digest,
                root_expires_at_ns // 1_000_000,
            ),
        )
        repo.conn.execute(
            "INSERT INTO trading_case_decisions "
            "(case_id,decision_id,policy_id,policy_version,input_ref,action,decision,"
            "publish_status,decided_at_ms,valid_until_ms) "
            "VALUES (%s,%s,'trade_assessment','v1','fixture','TRADE',%s::jsonb,'published',%s,%s)",
            (case_id, decision_id, json.dumps(decision), at_ms, root_expires_at_ns // 1_000_000),
        )
        repo.append_trade_signal(prepare_trade_signal_v2(signal))
    return signal_id
