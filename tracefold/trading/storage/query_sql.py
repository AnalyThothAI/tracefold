"""Current Signal-boundary reads shared with the query-plan audit."""

from __future__ import annotations

from typing import Final

TRADING_STATUS_CASE_COUNTS_SQL: Final = """
    SELECT
      count(*) FILTER (WHERE created_at_ms >= %(since)s) AS cases_24h,
      count(*) FILTER (WHERE created_at_ms >= %(since)s AND state = 'SIGNAL_EMITTED') AS signals_24h,
      count(*) FILTER (WHERE created_at_ms >= %(since)s AND state = 'NO_TRADE') AS no_trade_24h,
      count(*) FILTER (WHERE created_at_ms >= %(since)s AND state = 'BLOCKED') AS blocked_24h,
      count(*) FILTER (WHERE state IN ('PENDING', 'RUNNING')) AS cases_open
    FROM trading_cases
    WHERE created_at_ms >= %(since)s OR state IN ('PENDING', 'RUNNING')
"""
TRADING_STATUS_SIGNAL_COUNTS_SQL: Final = """
    SELECT count(*) FILTER (WHERE observed_at_ns >= %(since)s) AS signals_24h,
           count(*) FILTER (WHERE expires_at_ns > %(now)s) AS signals_unexpired
      FROM trading_trade_signals
     WHERE observed_at_ns >= %(since)s OR expires_at_ns > %(now)s
"""
TRADING_CASE_COUNTS_SQL: Final = (
    "SELECT state, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY state"
)
TRADING_CASE_REASON_COUNTS_SQL: Final = (
    "SELECT coalesce(policy_reason, 'undecided') AS reason, count(*) AS n "
    "FROM trading_cases WHERE created_at_ms >= %s GROUP BY reason"
)
TRADING_CONSOLE_CASES_SQL: Final = """
    SELECT case_id, underlying_key, trigger_kind, primary_source_key, manifest,
           manifest_sha256, state, policy_decision, policy_reason, policy_checks,
           observed_at_ms, created_at_ms AS case_created_at_ms, decided_at_ms,
           strategy_id, strategy_version, strategy_config_digest
      FROM trading_cases
     WHERE created_at_ms >= %(since)s
     ORDER BY created_at_ms DESC, case_id DESC
     LIMIT %(limit)s
"""
TRADING_CONSOLE_SIGNALS_SQL: Final = """
    SELECT seq, signal_id, case_id, alpha_contract_sha256, market_key, direction,
           observed_at_ns, expires_at_ns, evidence_sha256, alpha_metadata
      FROM trading_trade_signals
     WHERE observed_at_ns >= %(since)s
     ORDER BY observed_at_ns DESC, signal_id DESC
     LIMIT %(limit)s
"""
TRADING_CONSOLE_OBSERVATIONS_SQL: Final = """
    SELECT seq, event_id, runtime_profile_id, runtime_release, execution_strategy,
           signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
           native_identity_references, summary, payload_digest
      FROM trading_execution_observations
     WHERE observed_at_ns >= %(since)s
     ORDER BY observed_at_ns DESC, event_id DESC
     LIMIT %(limit)s
"""
GATE_DECISION_FOR_SOURCE_KEY_SQL: Final = """
    SELECT source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
           source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
           first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
      FROM trading_candidate_gate_decisions
     WHERE source_key = %s
     ORDER BY (status = 'CASE_CREATED') DESC, last_evaluated_at_ms DESC, gate_config_digest
     LIMIT 1
"""
LATEST_GATE_DECISION_PER_SOURCE_SQL: Final = """
    SELECT DISTINCT ON (source_key)
           source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
           source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
           first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
      FROM trading_candidate_gate_decisions
     WHERE trigger_kind = %s AND source_observed_at_ms >= %s
     ORDER BY source_key, (status = 'CASE_CREATED') DESC, last_evaluated_at_ms DESC
"""


def gate_decisions_since_sql() -> str:
    return f"""
        SELECT source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
               source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
               first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
          FROM ({LATEST_GATE_DECISION_PER_SOURCE_SQL}) latest
         ORDER BY source_observed_at_ms DESC, source_key
         LIMIT %s
    """  # noqa: S608 -- the interpolated subquery is a module-owned constant


__all__ = [name for name in globals() if name.startswith("TRADING_") or name.startswith("GATE_")] + [
    "LATEST_GATE_DECISION_PER_SOURCE_SQL",
    "gate_decisions_since_sql",
]
