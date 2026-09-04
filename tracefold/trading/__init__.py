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
    OiTradeCandidate,
    TradingCaseManifest,
    canonical_sha256,
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
from .stages import (
    ACCEPTED_ENTRY_DISPOSITIONS,
    CommandStage,
    ExecutionStage,
    command_stage,
    execution_stage,
)
from .storage.execution_stream import (
    ExecutionAccountOrder,
    ExecutionAccountPosition,
    ExecutionAccountSnapshot,
    PreparedOperatorIntent,
    prepare_execution_observations,
)

__all__ = [
    "ACCEPTED_ENTRY_DISPOSITIONS",
    "EXECUTION_STRATEGY_ID",
    "IDENTITY_PATTERN",
    "MARKET_KEY_PATTERN",
    "MAX_OBSERVATION_APPEND_BATCH",
    "MAX_OBSERVATION_APPEND_BYTES",
    "SHA256_PATTERN",
    "AlphaDecision",
    "Bar",
    "CaseState",
    "CommandStage",
    "ExecutionAccountOrder",
    "ExecutionAccountPosition",
    "ExecutionAccountSnapshot",
    "ExecutionObservationV1",
    "ExecutionStage",
    "OiTradeCandidate",
    "OperatorCommandError",
    "OperatorIntentV1",
    "ParsedOperatorCommand",
    "PreparedOperatorIntent",
    "TradeSignalV1",
    "TradingCaseManifest",
    "canonical_sha256",
    "command_stage",
    "execution_stage",
    "parse_operator_command",
    "postgres_text_valid",
    "prepare_execution_observations",
    "prepare_parsed_operator_intent",
]
