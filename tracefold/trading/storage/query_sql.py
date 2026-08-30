"""Production Trading console statements shared with the query-plan audit."""

from __future__ import annotations

from typing import Final

from ..intent import ACTIVE_INTENT_STATES

DEFAULT_CONSOLE_INTENT_STATES: Final = ACTIVE_INTENT_STATES
TRADING_STATUS_COUNTS_SQL: Final = (
    "SELECT execution_state, count(*) AS n FROM trading_intents "
    "WHERE created_at_ms >= %s GROUP BY execution_state"
)
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
BINDING_RUNTIME_ROWS_SQL: Final = """
    SELECT runtime.binding, runtime.credential_state, runtime.credential_fingerprint,
           runtime.runtime_state, runtime.account_state, runtime.account_generation,
           CASE
             WHEN runtime.catalog_state = 'ready'
              AND snapshot.stale_after_ms IS NOT NULL
              AND runtime.catalog_captured_at_ms + snapshot.stale_after_ms <= %(now)s
             THEN 'stale'
             ELSE runtime.catalog_state
           END AS catalog_state,
           runtime.catalog_snapshot_sha256, runtime.catalog_captured_at_ms,
           runtime.capability_state, runtime.capability_snapshot_sha256,
           runtime.capability_compiled_at_ms, runtime.capability_compile_error,
           runtime.execution_binding_sha256, runtime.active_arm_receipt_sha256,
           runtime.heartbeat_at_ms, runtime.reason, runtime.updated_at_ms
      FROM trading_binding_runtime runtime
      LEFT JOIN trading_venue_catalog_snapshots snapshot
        ON snapshot.snapshot_sha256 = runtime.catalog_snapshot_sha256
     ORDER BY runtime.binding
"""
EXECUTION_CAPABILITY_SNAPSHOT_SQL: Final = """
    SELECT payload
      FROM trading_execution_capability_snapshots
     WHERE snapshot_sha256 = %s
       AND payload ->> 'snapshot_version' = 'execution_capability_snapshot_v2'
"""
AUTHORITY_PROJECTION_SQL: Final = """
    SELECT runtime.binding, runtime.active_arm_receipt_sha256,
           arm.payload AS arm_payload,
           promotion.payload AS grant_payload,
           policy.payload AS policy_payload,
           revocation.payload AS revocation_payload
      FROM trading_binding_runtime runtime
      LEFT JOIN trading_operator_arm_receipts arm
        ON arm.arm_receipt_sha256 = runtime.active_arm_receipt_sha256
      LEFT JOIN trading_production_promotion_grants promotion
        ON promotion.grant_sha256 = arm.grant_sha256
      LEFT JOIN trading_daily_risk_policies policy
        ON policy.risk_policy_sha256 = arm.risk_policy_sha256
      LEFT JOIN trading_promotion_grant_revocations revocation
        ON revocation.grant_sha256 = promotion.grant_sha256
     ORDER BY runtime.binding
"""
CAPITAL_AUTHORITY_SNAPSHOT_SQL: Final = """
    WITH capital_runtime AS (
        SELECT control FROM trading_runtime_state WHERE id = 1
    )
    SELECT capital_runtime.control AS capital_control,
           ARRAY(
               SELECT DISTINCT COALESCE(intent.underlying_key, trading_case.underlying_key)
                 FROM trading_intents intent
                 JOIN trading_cases trading_case ON trading_case.case_id = intent.case_id
                WHERE intent.execution_state = ANY(%(active_states)s)
                ORDER BY 1
           ) AS active_underlyings,
           ARRAY(
               SELECT DISTINCT underlying_key
                 FROM trading_cases
                WHERE state IN ('PENDING', 'RUNNING')
                ORDER BY 1
           ) AS underlyings_in_flight,
           ARRAY(
               SELECT DISTINCT primary_source_key
                 FROM trading_cases
                WHERE observed_at_ms >= %(since_ms)s
                ORDER BY 1
           ) AS cased_source_keys,
           COALESCE((
               SELECT jsonb_agg(
                   jsonb_build_object(
                       'base_symbol', base_symbol,
                       'reason', reason,
                       'created_at_ms', created_at_ms,
                       'expires_at_ms', expires_at_ms
                   ) ORDER BY base_symbol
               )
                 FROM trading_symbol_blacklist
           ), '[]'::jsonb)::text AS blacklist_rows_json,
           COALESCE((
               SELECT jsonb_object_agg(
                   binding_runtime.binding,
                   jsonb_build_object(
                       'credential_state', binding_runtime.credential_state,
                       'runtime_state', binding_runtime.runtime_state,
                       'account_state', binding_runtime.account_state,
                       'catalog_state', CASE
                           WHEN binding_runtime.catalog_state = 'ready'
                            AND catalog.stale_after_ms IS NOT NULL
                            AND binding_runtime.catalog_captured_at_ms + catalog.stale_after_ms <= %(now_ms)s
                           THEN 'stale'
                           ELSE binding_runtime.catalog_state
                       END,
                       'catalog_snapshot_sha256', binding_runtime.catalog_snapshot_sha256,
                       'catalog_payload', catalog.payload,
                       'reason', binding_runtime.reason
                   )
               )
                 FROM trading_binding_runtime binding_runtime
                 LEFT JOIN trading_venue_catalog_snapshots catalog
                   ON catalog.snapshot_sha256 = binding_runtime.catalog_snapshot_sha256
                WHERE binding_runtime.binding = ANY(%(bindings)s)
           ), '{}'::jsonb)::text AS binding_rows_json
      FROM capital_runtime
"""


def console_intents_sql(
    *,
    closed_window: bool = False,
    underlying: bool = False,
    states: bool = False,
    before: bool = False,
) -> str:
    recency = (
        "(i.execution_state = ANY(%s) OR "
        "(i.execution_state = 'TERMINAL' AND i.closed_at_ms >= %s AND i.closed_at_ms < %s))"
        if closed_window
        else "(i.execution_state = ANY(%s) OR "
        "coalesce(i.closed_at_ms, i.flat_verified_at_ms, i.updated_at_ms, i.created_at_ms) >= %s)"
    )
    predicates = [recency]
    if underlying:
        predicates.append("c.underlying_key = %s")
    if states:
        predicates.append("i.execution_state = ANY(%s)")
    if before:
        predicates.append("(i.created_at_ms, i.intent_id) < (%s, %s)")
    where_clause = " AND ".join(predicates)
    return f"""
        SELECT i.intent_id, i.intent_version, i.case_id, i.execution_environment,
               i.source_venue, i.source_identity, i.canonical_asset, i.binding,
               i.account_generation, i.execution_binding_sha256,
               i.venue_catalog_snapshot_sha256, i.execution_capability_snapshot_sha256,
               i.capability_entry_id, i.provider_instrument_id, i.settlement_asset,
               i.intent_policy_sha256, i.execution_policy_sha256, i.quote_contract_sha256,
               i.protection_contract_sha256, i.capital_authorization_receipt_sha256,
               i.blacklist_revision_at_emission, i.blacklist_snapshot_sha256_at_emission,
               i.instrument_id, i.side, i.leverage, i.risk_currency, i.economic_lifecycle_id,
               i.entry_leg_id, i.protection_leg_id, i.close_leg_id, i.valid_until_ms,
               i.execution_state, i.execution_phase, i.terminal_outcome, i.reason_code,
               i.entry_fenced_at_ms, i.opened_at_ms, i.protected_at_ms, i.closed_at_ms,
               i.flat_verified_at_ms, i.realized_pnl_currency, i.commissions_by_currency,
               i.funding_by_currency, i.created_at_ms, i.updated_at_ms,
               i.target_notional_usd, i.target_notional, i.max_risk_amount, i.reference_price,
               i.actual_quantity, i.protected_quantity, i.avg_entry_price, i.avg_exit_price,
               i.stop_price, i.realized_pnl_amount,
               c.underlying_key, c.primary_source_key, c.strategy_id, c.strategy_version
          FROM trading_intents i
          JOIN trading_cases c ON c.case_id = i.case_id
         WHERE {where_clause}
         ORDER BY i.created_at_ms DESC, i.intent_id DESC
         LIMIT %s
    """  # noqa: S608


def console_cases_sql(*, underlying: bool = False, states: bool = False, before: bool = False) -> str:
    predicates = ["c.created_at_ms >= %s"]
    if underlying:
        predicates.append("c.underlying_key = %s")
    if states:
        predicates.append("c.state = ANY(%s)")
    if before:
        predicates.append("(c.created_at_ms, c.case_id) < (%s, %s)")
    where_clause = " AND ".join(predicates)
    return f"""
        SELECT c.case_id, c.underlying_key, c.primary_source_key,
               c.trigger_kind, c.strategy_id, c.strategy_version, c.strategy_config_digest,
               c.state, c.policy_decision, c.policy_reason, c.policy_checks,
               c.capital_disposition, c.capital_reason,
               c.manifest -> 'policy_config' AS policy_config,
               (c.manifest -> 'contexts' -> 'market' ->> 'pre_move_bps')::bigint AS pre_move_bps,
               (c.manifest -> 'contexts' -> 'market' ->> 'mark_price') AS mark_price,
               (c.manifest -> 'contexts' -> 'oi' ->> 'oi_change_bps')::bigint AS oi_change_bps,
               (c.manifest -> 'contexts' -> 'oi' ->> 'oi_value_usd')::bigint AS oi_value_usd,
               (c.manifest -> 'contexts' -> 'oi' ->> 'whale_oi_ratio_bps')::bigint AS whale_oi_ratio_bps,
               (c.manifest -> 'contexts' -> 'oi' ->> 'whale_long_profit_bps')::bigint AS whale_long_profit_bps,
               (c.manifest -> 'instrument' ->> 'provider_symbol') AS provider_symbol,
               (c.manifest ->> 'manifest_version') AS manifest_version,
               c.observed_at_ms, c.created_at_ms AS case_created_at_ms, c.decided_at_ms,
               i.intent_id
          FROM trading_cases c
          LEFT JOIN trading_intents i ON i.case_id = c.case_id
         WHERE {where_clause}
         ORDER BY c.created_at_ms DESC, c.case_id DESC
         LIMIT %s
    """  # noqa: S608


def console_capital_evidence_sql(
    *, binding: bool = False, statuses: bool = False, before: bool = False
) -> str:
    predicates: list[str] = []
    if binding:
        predicates.append("reservation.binding = %s")
    if statuses:
        predicates.append("state.status = ANY(%s)")
    if before:
        predicates.append("(state.updated_at_ms, reservation.reservation_sha256) < (%s, %s)")
    where_clause = f"WHERE {' AND '.join(predicates)}" if predicates else ""
    return f"""
        SELECT reservation.reservation_sha256, reservation.case_id,
               reservation.economic_lifecycle_id, reservation.binding,
               reservation.settlement_asset, reservation.risk_policy_sha256,
               reservation.grant_sha256, reservation.arm_receipt_sha256,
               reservation.risk_day_start_ms, reservation.risk_day_end_ms,
               reservation.target_notional, reservation.planned_risk_amount,
               receipt.authorization_receipt_sha256,
               state.intent_id, state.status, state.current_planned_risk_amount,
               state.attempt_consumed, state.attempt_day_start_ms, state.attempt_day_end_ms,
               state.settlement_known, state.updated_at_ms,
               intent.execution_state, intent.execution_phase, intent.terminal_outcome,
               intent.reason_code, intent.flat_verified_at_ms
          FROM trading_capital_risk_reservations reservation
          JOIN trading_capital_authorization_receipts receipt
            ON receipt.reservation_sha256 = reservation.reservation_sha256
          JOIN trading_capital_risk_reservation_state state
            ON state.reservation_sha256 = reservation.reservation_sha256
          JOIN trading_intents intent ON intent.intent_id = state.intent_id
         {where_clause}
         ORDER BY state.updated_at_ms DESC, reservation.reservation_sha256 DESC
         LIMIT %s
    """  # noqa: S608


def gate_decisions_since_sql() -> str:
    return f"""
        SELECT source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
               source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
               first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
          FROM ({LATEST_GATE_DECISION_PER_SOURCE_SQL}) latest
         ORDER BY source_observed_at_ms DESC, source_key
         LIMIT %s
    """  # noqa: S608


__all__ = [
    "AUTHORITY_PROJECTION_SQL",
    "BINDING_RUNTIME_ROWS_SQL",
    "CAPITAL_AUTHORITY_SNAPSHOT_SQL",
    "DEFAULT_CONSOLE_INTENT_STATES",
    "EXECUTION_CAPABILITY_SNAPSHOT_SQL",
    "GATE_DECISION_FOR_SOURCE_KEY_SQL",
    "LATEST_GATE_DECISION_PER_SOURCE_SQL",
    "TRADING_STATUS_COUNTS_SQL",
    "console_capital_evidence_sql",
    "console_cases_sql",
    "console_intents_sql",
    "gate_decisions_since_sql",
]
