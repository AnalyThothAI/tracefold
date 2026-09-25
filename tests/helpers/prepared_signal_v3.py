"""Compact V3 Signal factory for storage and migration tests."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from tracefold.trading.execution_contracts import (
    SignalEntryEnvelopeV3,
    SignalExitPlanV1,
    TradeSignalV3,
)
from tracefold.trading.storage.execution_stream import PreparedTradeSignal, prepare_trade_signal_v3


def prepared_v3_signal(
    *,
    signal_id: str,
    case_id: str,
    market_key: str,
    direction: Literal["long", "short"],
    observed_at_ns: int,
    expires_at_ns: int,
) -> PreparedTradeSignal:
    return prepare_trade_signal_v3(
        TradeSignalV3(
            seq=1,
            signal_id=signal_id,
            case_id=case_id,
            decision_id="b" * 64,
            account_slot="demo-v1",
            runtime_mode="paper",
            entry_scope_id="c" * 64,
            asset_id="crypto:BTC",
            market_key=market_key,
            native_symbol="BTCUSDT",
            mapping_semantics_digest="d" * 64,
            direction=direction,
            observed_at_ns=observed_at_ns,
            expires_at_ns=expires_at_ns,
            exit_plan=SignalExitPlanV1(stop_distance_bps=100, take_profit_bps=200, max_holding_ns=1_000),
            entry_envelope=SignalEntryEnvelopeV3(
                plan_id="e" * 64,
                entry_kind="immediate_entry_v1",
                root_expires_at_ns=expires_at_ns,
                reference_price=Decimal("100"),
                max_price_drift_bps=100,
                universe_version="test-v1",
            ),
        )
    )
