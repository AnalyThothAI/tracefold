"""Production unresolved reads for the execution stream."""

from __future__ import annotations

from typing import Final

UNRESOLVED_TRADE_SIGNALS_SQL: Final = """
    SELECT signal.seq, signal.payload
      FROM trading_execution_profile_activations activation
      JOIN trading_trade_signals signal
        ON signal.seq > activation.activated_after_signal_seq
      LEFT JOIN trading_execution_observations disposition
        ON disposition.runtime_profile_id = activation.runtime_profile_id
       AND disposition.execution_strategy = %s
       AND disposition.signal_id = signal.signal_id
       AND disposition.normalized_kind = 'signal_disposition'
     WHERE activation.runtime_profile_id = %s
       AND disposition.event_id IS NULL
     ORDER BY signal.seq
     LIMIT %s
"""

UNRESOLVED_OPERATOR_INTENTS_SQL: Final = """
    SELECT command.seq, command.payload
      FROM trading_execution_profile_activations activation
      JOIN trading_operator_intents command
        ON command.target_profile_id = activation.runtime_profile_id
       AND command.seq > activation.activated_after_command_seq
      LEFT JOIN trading_execution_observations disposition
        ON disposition.runtime_profile_id = activation.runtime_profile_id
       AND disposition.execution_strategy = %s
       AND disposition.command_id = command.command_id
       AND disposition.normalized_kind = 'control_disposition'
     WHERE activation.runtime_profile_id = %s
       AND disposition.event_id IS NULL
     ORDER BY command.seq
     LIMIT %s
"""

__all__ = ["UNRESOLVED_OPERATOR_INTENTS_SQL", "UNRESOLVED_TRADE_SIGNALS_SQL"]
