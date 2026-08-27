"""Narrow public values and ports for the Trading bounded context.

App owns composition. Candidate rules, runners, persistence, and execution helpers stay under their
owning subpackages instead of becoming a second accidental API at this root.
"""

from __future__ import annotations

from .contracts import (
    Bar,
    ExecutionAdapter,
    ExecutionObservation,
    ExecutionObservationState,
    ExecutionReceipt,
    InstrumentRef,
    LiveExchangeId,
    LiveExecutionAdapter,
    LivePreflight,
    NativeProtection,
    OrderSide,
    PreparedOrder,
    RemoteExposure,
    StartupReconciliation,
    TradingMode,
)
from .intent import INTENT_POLICY_SHA256, IntentOutcome, IntentReasonCode, TradeIntent, deterministic_client_order_id

__all__ = [
    "INTENT_POLICY_SHA256",
    "Bar",
    "ExecutionAdapter",
    "ExecutionObservation",
    "ExecutionObservationState",
    "ExecutionReceipt",
    "InstrumentRef",
    "IntentOutcome",
    "IntentReasonCode",
    "LiveExchangeId",
    "LiveExecutionAdapter",
    "LivePreflight",
    "NativeProtection",
    "OrderSide",
    "PreparedOrder",
    "RemoteExposure",
    "StartupReconciliation",
    "TradeIntent",
    "TradingMode",
    "deterministic_client_order_id",
]
