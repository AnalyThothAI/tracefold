"""Wallet membership hard cut and conservative continuous-prefix repair (#697).

Migration evidence:
- category: wallet contract hard cut and one-row cursor repair.
- why_database_must_change: membership is no longer a ranking; retries need durable
  due times; an old oversized scanned cutoff cannot be retained by monotonic writes.
- current_source_revision: 20260925_0398
- minimum_supported_source_revision: 20260925_0398
- lock_level_and_order: brief ACCESS EXCLUSIVE, roster then tape state; no network.
- statement_timeout: 60s; lock_timeout: 5s.
- estimated_rows: archive each existing roster row once and repair one state row.
- estimated_bytes: historical source statistics retained in one JSONB archive column.
- preflight_and_maintenance_boundary: stop wallet Workers, back up roster/state and
  episode/delivery tables, migrate, remove retired config keys, restart new image.
- archive_current_compatibility: original snapshot JSON and sent cards are untouched;
  retired roster fields are archived, known legacy JSON keys projected at reads.
- role_and_grant_impact: none, existing single tracefold login and table grants.
- failure_state: all DDL/data changes roll back transactionally.
- roll_forward_or_verified_backup_restore: restore verified pre-0399 backup on rollback;
  never clear derived markers, cutover, episodes, intents, or delivery identities.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260925_0399
Revises: 20260925_0398
"""

from __future__ import annotations

from alembic import op

revision = "20260925_0399"
down_revision = "20260925_0398"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("ALTER TABLE public.news_market_wallet_roster ADD COLUMN archived_source_statistics jsonb")
    op.execute("""
        UPDATE public.news_market_wallet_roster SET archived_source_statistics = jsonb_build_object(
            'followers', followers, 'realized_pnl', realized_pnl, 'closed_trades', closed_trades,
            'win_rate', win_rate, 'profit_factor', profit_factor, 'open_cost', open_cost,
            'rank_quality', rank_quality, 'rank_whale', rank_whale
        )
    """)
    op.execute("""
        ALTER TABLE public.news_market_wallet_roster
            DROP CONSTRAINT news_market_wallet_roster_rank_check,
            DROP COLUMN followers, DROP COLUMN realized_pnl, DROP COLUMN closed_trades,
            DROP COLUMN win_rate, DROP COLUMN profit_factor, DROP COLUMN open_cost,
            DROP COLUMN rank_quality, DROP COLUMN rank_whale
    """)
    op.execute("""
        ALTER TABLE public.news_market_wallet_tape_state
            ADD COLUMN next_attempt_at_ms bigint NOT NULL DEFAULT 0 CHECK (next_attempt_at_ms >= 0),
            ADD COLUMN consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
            ADD COLUMN blocked_tx_hash text,
            ADD COLUMN enrichment_error text,
            ADD COLUMN roster_next_attempt_at_ms bigint NOT NULL DEFAULT 0 CHECK (roster_next_attempt_at_ms >= 0),
            ADD COLUMN roster_consecutive_failures integer NOT NULL DEFAULT 0 CHECK (roster_consecutive_failures >= 0),
            ADD COLUMN pre_0399_cursor jsonb
    """)
    # A partial receipt cursor did not persist its safe log boundary. Re-read the
    # preceding complete block; never infer coverage from the greatest stored fill.
    # Complete but inconsistent legacy encodings also need a fresh header timestamp.
    op.execute("""
        UPDATE public.news_market_wallet_tape_state SET
            pre_0399_cursor = jsonb_build_object(
                'high_water_block', high_water_block, 'high_water_tx_index', high_water_tx_index,
                'scanned_block', scanned_block, 'scanned_log', scanned_log, 'scanned_at_ms', scanned_at_ms
            ),
            high_water_block = CASE WHEN high_water_tx_index = 2147483647 THEN high_water_block
                                    ELSE GREATEST(0, high_water_block - 1) END,
            high_water_tx_index = 2147483647,
            scanned_block = NULL, scanned_log = NULL, scanned_at_ms = NULL,
            last_outcome = 'error', last_error = 'wallet_cutoff_repair_pending'
        WHERE high_water_block > 0 AND (
            high_water_tx_index <> 2147483647 OR
            scanned_block IS DISTINCT FROM high_water_block OR scanned_log IS DISTINCT FROM 2147483647
        )
    """)


def downgrade() -> None:
    raise RuntimeError("wallet_complete_prefix_forward_only: restore a verified pre-0399 archive")
