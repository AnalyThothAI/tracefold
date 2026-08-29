"""Macro lane removal (#68): drop the macro fact/derived tables, the macro document analysis queue,
the macro projection frontier, Macro's general market observation facts, and the durable queue terminal
evidence table whose only writers were the macro repository and the projection frontier. The system is
News V3 only; no retained table references any dropped table.

Revision ID: 20260819_0278
Revises: 20260818_0277
"""

from __future__ import annotations

from alembic import op

revision = "20260819_0278"
down_revision = "20260818_0277"
branch_labels = None
depends_on = None

_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS = (0x54524644, 0)

# Children before parents: the analyses/jobs reference macro_documents, and observations/settlements
# reference market_instruments. Every table exists at 0277 and none is referenced by a retained table.
_DROPPED_TABLES = (
    "macro_document_analysis_jobs",
    "macro_document_analyses",
    "macro_documents",
    "macro_fed_official_role_facts",
    "macro_release_facts",
    "macro_series_facts",
    "macro_module_current",
    "macro_module_frontiers",
    "macro_dataset_projection_states",
    "macro_acquisition_targets",
    "market_position_facts",
    "market_settlements",
    "market_observations",
    "market_instruments",
    # The only writers of queue_terminal_events were macro/repository.py and the projection frontier.
    "queue_terminal_events",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    op.execute("SET LOCAL transaction_timeout = '600s'")
    op.execute(
        f"""
        DO $migration$
        BEGIN
          IF NOT pg_try_advisory_xact_lock(
            {_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS[0]},
            {_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS[1]}
          ) THEN
            RAISE EXCEPTION 'macro_lane_removal_workers_active' USING ERRCODE = '55006';
          END IF;
        END
        $migration$;
        """
    )
    for table in _DROPPED_TABLES:
        op.execute(f"DROP TABLE {table}")
    # The append-only triggers go with their tables; the function they shared does not.
    op.execute("DROP FUNCTION reject_macro_fact_mutation()")


def downgrade() -> None:
    raise RuntimeError("macro_lane_removal_is_irreversible")
