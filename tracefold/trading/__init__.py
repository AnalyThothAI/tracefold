"""Public engine-neutral Trading values.

The App composition root imports the one business action from `signal_lane`; this package root exports
only durable facts shared with application seams.
"""

from __future__ import annotations

from .contracts import (
    EXECUTION_STRATEGY_ID,
    AlphaDecision,
    Bar,
    CaseState,
    DecisionRuntimeV1,
    OiTradeCandidate,
    TradingCaseManifest,
    canonical_sha256,
)
from .demo_receipt import (
    BinanceDemoReceipt,
    DemoReceiptError,
    DemoReceiptObservation,
    verify_binance_demo_receipt,
)
from .execution_contracts import (
    IDENTITY_PATTERN,
    MARKET_KEY_PATTERN,
    MAX_OBSERVATION_APPEND_BATCH,
    MAX_OBSERVATION_APPEND_BYTES,
    SHA256_PATTERN,
    ExecutionObservationV1,
    OperatorIntentV1,
    TradeSignalV1,
    postgres_text_valid,
)
from .operator_control import (
    OperatorCommandError,
    ParsedOperatorCommand,
    parse_operator_command,
    prepare_parsed_operator_intent,
)
from .storage.execution_stream import (
    ExecutionAccountOrder,
    ExecutionAccountPosition,
    ExecutionAccountSnapshot,
    PreparedOperatorIntent,
    prepare_execution_observations,
)

__all__ = [
    "EXECUTION_STRATEGY_ID",
    "IDENTITY_PATTERN",
    "MARKET_KEY_PATTERN",
    "MAX_OBSERVATION_APPEND_BATCH",
    "MAX_OBSERVATION_APPEND_BYTES",
    "SHA256_PATTERN",
    "AlphaDecision",
    "Bar",
    "BinanceDemoReceipt",
    "CaseState",
    "DecisionRuntimeV1",
    "DemoReceiptError",
    "DemoReceiptObservation",
    "ExecutionAccountOrder",
    "ExecutionAccountPosition",
    "ExecutionAccountSnapshot",
    "ExecutionObservationV1",
    "OiTradeCandidate",
    "OperatorCommandError",
    "OperatorIntentV1",
    "ParsedOperatorCommand",
    "PreparedOperatorIntent",
    "TradeSignalV1",
    "TradingCaseManifest",
    "canonical_sha256",
    "parse_operator_command",
    "postgres_text_valid",
    "prepare_execution_observations",
    "prepare_parsed_operator_intent",
    "verify_binance_demo_receipt",
]
