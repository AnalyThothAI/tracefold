"""Fail-closed bridge for the one private 0317/0318 revision collision.

The local Telegram branch used revision identifiers that upstream later used for
the Nautilus authority and Program-v8 hard cuts.  Alembic can only see the
identifier, so an affected database needs a schema fingerprint before it is
safe to continue at upstream 0319.  This module recognizes exactly those two
known lineages, repairs only the private lineage, and rejects every mixed or
unknown shape.
"""

from __future__ import annotations

from typing import Any, Final

import psycopg

_COLLIDING_REVISION: Final = "20260828_0318"
_PROGRAM_V7_EPOCH_BASELINE_SHA: Final = "7a460f8d3812c64c6ee38158871eb9f060811e5ffe87f399f7bc2e506b4e28ad"
_PROGRAM_V8_SHA: Final = "c9bd53421b8c5c41c183cda5ef69150f241d467fee7699a6c087e2f71b27f3e9"

_REMOTE_0317_SQL: Final = """
DO $cutover$
BEGIN
  LOCK TABLE trading_runtime_state, trading_cases, trading_intents, trading_orders,
             trading_order_observations IN SHARE ROW EXCLUSIVE MODE;
  IF NOT EXISTS (
    SELECT 1 FROM trading_runtime_state WHERE id = 1 AND control = 'PAUSED'
  ) THEN
    RAISE EXCEPTION 'trading_hard_cut_not_paused';
  END IF;
  IF EXISTS (SELECT 1 FROM trading_cases WHERE state IN ('PENDING', 'RUNNING')) THEN
    RAISE EXCEPTION 'trading_hard_cut_pending_case';
  END IF;
  IF EXISTS (
    SELECT 1 FROM trading_intents
     WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
  ) THEN
    RAISE EXCEPTION 'trading_hard_cut_nonterminal_intent';
  END IF;
  IF EXISTS (
    SELECT 1 FROM trading_orders
     WHERE state IN (
       'PREPARED', 'AWAITING_APPROVAL', 'APPROVED', 'SUBMITTING', 'AMBIGUOUS',
       'RECONCILING', 'MANUAL_REVIEW_REQUIRED', 'ACKNOWLEDGED', 'PARTIAL',
       'OPEN', 'UNPROTECTED', 'SAFETY_CLOSING'
     )
  ) THEN
    RAISE EXCEPTION 'trading_hard_cut_active_legacy_order';
  END IF;
END
$cutover$
"""

_REMOTE_0318_SQL: Final = f"""
DO $$
DECLARE
  prior_start_ms bigint;
  deployed_at_ms bigint;
  prior_sha text;
BEGIN
  SELECT starts_at_ms, baseline_program_sha256
    INTO STRICT prior_start_ms, prior_sha
    FROM news_learning_epochs
   WHERE epoch_id = 'program_v7';
  IF prior_sha <> '{_PROGRAM_V7_EPOCH_BASELINE_SHA}' THEN
    RAISE EXCEPTION 'news_learning_program_v7_baseline_mismatch';
  END IF;

  deployed_at_ms := greatest(
    floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint,
    prior_start_ms + 1
  );
  INSERT INTO news_learning_epochs (
    epoch_id, starts_at_ms, source_issue, program_factory_id, artifact_schema_version,
    baseline_program_version, baseline_program_sha256, prior_evidence_disposition,
    reset_reason, created_at_ms
  ) VALUES (
    'program_v8', deployed_at_ms,
    'https://github.com/AnalyThothAI/tracefold/issues/306',
    'tracefold.news.program.factory_v8',
    'news_program_strategy_artifact_v1',
    'news_semantic_program_v5',
    '{_PROGRAM_V8_SHA}',
    'audit_only',
    'single_instruction_seed_and_self_owned_transport_identity_migration',
    deployed_at_ms
  );

  UPDATE news_canary_activations
     SET state = 'tripped',
         revision = revision + 1,
         trip_reason = 'program_v8_hard_cut',
         tripped_at_ms = deployed_at_ms
   WHERE state IN ('armed', 'active');
END
$$
"""


def reconcile_colliding_telegram_lineage(database_url: str) -> bool:
    """Repair the known private 0318 lineage and return whether work was done."""

    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn:
        if not _table_exists(conn, "alembic_version"):
            return False
        version = _scalar(conn, "SELECT version_num FROM alembic_version LIMIT 1")
        if version != _COLLIDING_REVISION:
            return False

        conn.execute("SELECT pg_advisory_xact_lock(hashtext('tracefold:legacy-telegram-lineage'))")
        _set_owner_role_if_available(conn)
        lineage = _lineage_fingerprint(conn)
        if lineage == "remote":
            return False
        if lineage != "local_telegram":
            raise RuntimeError("legacy_migration_lineage_unrecognized:20260828_0318")

        conn.execute(_REMOTE_0317_SQL)
        conn.execute("ALTER TABLE trading_cases DROP CONSTRAINT trading_cases_state_check")
        conn.execute(
            """
            ALTER TABLE trading_cases
              ADD CONSTRAINT trading_cases_state_check CHECK (
                state IN (
                  'PENDING', 'RUNNING', 'NO_TRADE', 'POLICY_REJECTED',
                  'INTENT_EMITTED', 'ORDER_PREPARED', 'BLOCKED'
                )
              )
            """
        )
        conn.execute(
            "REVOKE INSERT, UPDATE, DELETE ON trading_orders, trading_order_observations FROM tracefold_workers"
        )
        conn.execute("REVOKE INSERT, UPDATE, DELETE ON trading_runtime_state FROM tracefold_workers")
        conn.execute(
            """
            GRANT UPDATE (control, day_key, dspy_calls_today, funnel, updated_at_ms)
              ON trading_runtime_state TO tracefold_workers
            """
        )
        conn.execute(_REMOTE_0318_SQL)
        return True


def _lineage_fingerprint(conn: Any) -> str:
    local_columns = int(
        _scalar(
            conn,
            """
            SELECT count(*)
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'news_deliveries'
               AND (column_name, data_type) IN (
                 ('edit_state', 'text'),
                 ('pending_card', 'jsonb'),
                 ('edit_error_code', 'text'),
                 ('edit_attempted_at_ms', 'bigint'),
                 ('edit_settled_at_ms', 'bigint'),
                 ('delete_state', 'text'),
                 ('delete_evidence', 'jsonb'),
                 ('delete_reason', 'text'),
                 ('delete_error_code', 'text'),
                 ('delete_attempted_at_ms', 'bigint'),
                 ('delete_settled_at_ms', 'bigint')
               )
            """,
        )
        or 0
    )
    local_constraints = int(
        _scalar(
            conn,
            """
            SELECT count(*)
              FROM pg_constraint
             WHERE conrelid = 'news_deliveries'::regclass
               AND conname IN (
                 'news_deliveries_edit_state_check', 'news_deliveries_edit_shape_check',
                 'news_deliveries_delete_state_check', 'news_deliveries_delete_shape_check'
               )
            """,
        )
        or 0
    )
    local_indexes = int(
        _scalar(
            conn,
            """
            SELECT count(*)
              FROM pg_indexes
             WHERE schemaname = 'public' AND tablename = 'news_deliveries'
               AND indexname IN ('ix_news_deliveries_editing', 'ix_news_deliveries_deleting')
            """,
        )
        or 0
    )
    state_constraint = str(
        _scalar(
            conn,
            """
            SELECT pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conrelid = 'trading_cases'::regclass
               AND conname = 'trading_cases_state_check'
            """,
        )
        or ""
    )
    program_v8 = bool(_scalar(conn, "SELECT EXISTS (SELECT 1 FROM news_learning_epochs WHERE epoch_id = 'program_v8')"))
    has_local_telegram = (local_columns, local_constraints, local_indexes) == (11, 4, 2)
    has_any_local_telegram = any((local_columns, local_constraints, local_indexes))
    has_remote_authority = "INTENT_EMITTED" in state_constraint

    if has_remote_authority and program_v8 and not has_any_local_telegram:
        return "remote"
    if has_local_telegram and not has_remote_authority and not program_v8:
        return "local_telegram"
    return "unknown"


def _set_owner_role_if_available(conn: Any) -> None:
    owns_through_role = bool(
        _scalar(
            conn,
            """
            SELECT CASE
              WHEN current_user = 'tracefold_migrate'
                AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_owner')
              THEN pg_has_role(current_user, 'tracefold_owner', 'MEMBER')
              ELSE false
            END
            """,
        )
    )
    if owns_through_role:
        conn.execute("SET ROLE tracefold_owner")


def _table_exists(conn: Any, table_name: str) -> bool:
    return bool(_scalar(conn, "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table_name}",)))


def _scalar(conn: Any, query: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(query, params).fetchone()
    return None if row is None else row[0]


__all__ = ["reconcile_colliding_telegram_lineage"]
