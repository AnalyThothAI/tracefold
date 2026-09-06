"""Public engine-neutral Trading values.

The App composition root imports the one business action from `signal_lane`; this package root exports
only durable facts shared with application seams -- and only the ones an application seam actually
imports from here. Nine more names were re-exported for no caller; each of them still lives in the
module that owns it, which is where the Trading modules that use them already import them from
(#589 PR-2).
"""

from __future__ import annotations

from .contracts import (
    EXECUTION_STRATEGY_ID,
    OiTradeCandidate,
)
from .execution_contracts import (
    IDENTITY_PATTERN,
    MARKET_KEY_PATTERN,
    MAX_OBSERVATION_APPEND_BATCH,
    MAX_OBSERVATION_APPEND_BYTES,
    ExecutionObservationV1,
    OperatorIntentV1,
    TradeSignalV1,
)
from .operator_control import (
    OperatorCommandError,
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
)

__all__ = [
    "ACCEPTED_ENTRY_DISPOSITIONS",
    "EXECUTION_STRATEGY_ID",
    "IDENTITY_PATTERN",
    "MARKET_KEY_PATTERN",
    "MAX_OBSERVATION_APPEND_BATCH",
    "MAX_OBSERVATION_APPEND_BYTES",
    "CommandStage",
    "ExecutionAccountOrder",
    "ExecutionAccountPosition",
    "ExecutionAccountSnapshot",
    "ExecutionObservationV1",
    "ExecutionStage",
    "OiTradeCandidate",
    "OperatorCommandError",
    "OperatorIntentV1",
    "PreparedOperatorIntent",
    "TradeSignalV1",
    "command_stage",
    "execution_stage",
    "parse_operator_command",
    "prepare_parsed_operator_intent",
]
