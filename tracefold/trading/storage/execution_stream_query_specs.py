"""Bound execution-stream reads for PostgreSQL query-plan audit."""

from __future__ import annotations

from tracefold.platform.postgres.audit import ReadQuerySpec
from tracefold.trading.contracts import EXECUTION_STRATEGY_ID

from .execution_stream_sql import UNRESOLVED_OPERATOR_INTENTS_SQL, UNRESOLVED_TRADE_SIGNALS_SQL


def execution_stream_query_specs(
    *,
    runtime_profile_id: str = "query-audit-disabled",
    execution_strategy: str = EXECUTION_STRATEGY_ID,
) -> tuple[ReadQuerySpec, ...]:
    params = (execution_strategy, runtime_profile_id, 100)
    return (
        ReadQuerySpec(
            name="trading_unresolved_trade_signals",
            sql=UNRESOLVED_TRADE_SIGNALS_SQL,
            params=params,
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="trading_unresolved_operator_intents",
            sql=UNRESOLVED_OPERATOR_INTENTS_SQL,
            params=params,
            max_read_return_amplification=20.0,
        ),
    )


__all__ = ["execution_stream_query_specs"]
