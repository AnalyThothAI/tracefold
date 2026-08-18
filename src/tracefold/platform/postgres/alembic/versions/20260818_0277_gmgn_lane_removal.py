"""GMGN lane removal (#50): drop the social evidence, token identity, DEX/CEX market, live broadcast, and
News market-mark tables. The system is News V3 + Macro; Macro's general market facts (market_instruments,
market_observations, market_settlements, market_position_facts) stay.

Revision ID: 20260818_0277
Revises: 20260818_0276
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0277"
down_revision = "20260818_0276"
branch_labels = None
depends_on = None

_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS = (0x54524644, 0)

# Children before parents; every table exists in baseline 0275 + 0276 and none is referenced by a retained table.
_DROPPED_TABLES = (
    "news_event_market_marks",
    "asset_identity_current",
    "asset_identity_evidence",
    "enriched_events",
    "event_anchor_backfill_jobs",
    "market_tick_current",
    "market_ticks",  # partitioned: drops market_ticks_default with it
    "price_feeds",
    "cex_tokens",
    "token_intent_lookup_keys",
    "token_intent_evidence",
    "token_intent_resolutions",
    "token_intents",
    "token_evidence",
    "event_entities",
    "events",
    "raw_frames",
    "registry_assets",
    "collector_pending_items",
    "persisted_live_events",
    "us_equity_symbols",
    "provider_circuit_state",
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
            RAISE EXCEPTION 'gmgn_lane_removal_workers_active' USING ERRCODE = '55006';
          END IF;
        END
        $migration$;
        """
    )
    for table in _DROPPED_TABLES:
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION forbid_market_fact_update()")
    op.execute(
        """
        DELETE FROM queue_terminal_events
         WHERE source_table IN ('event_anchor_backfill_jobs', 'collector_pending_items')
        """
    )


def downgrade() -> None:
    raise RuntimeError("gmgn_lane_removal_is_irreversible")
