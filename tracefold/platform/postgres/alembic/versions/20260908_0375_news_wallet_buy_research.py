"""Buy observations, durable derivation and honest observation price receipts (#614).

Migration evidence:
- category: additive fill progress and event kind; outcome identity hard cut.
- why_database_must_change: unsent buy observations need receipts and derivation survives restart.
- current_source_revision: 20260907_0374
- minimum_supported_source_revision: 20260907_0374
- lock_level_and_order: ACCESS EXCLUSIVE fills, events, outcomes; SHARE for indexes.
- statement_timeout: 60s
- lock_timeout: 5s
- estimated_rows: tens of thousands of fills, thousands of events and receipts.
- estimated_bytes: one bigint per fill; outcome identity and reference metadata under 1 MB initially.
- rewrite_or_index_build: pending index on fills; outcome rows gain their existing trigger Item identity.
- preflight_and_maintenance_boundary: stop old workers before migration; deploy new source atomically.
- archive_current_compatibility: historical receipts remain but have unknown reference prices; no invented returns.
- role_and_grant_impact: unchanged single application login.
- failure_state: transactional rollback preserves the previous schema.
- roll_forward_or_verified_backup_restore: downgrade refused; roll forward or verified restore.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296
"""

from __future__ import annotations

from alembic import op

revision = "20260908_0375"
down_revision = "20260907_0374"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("ALTER TABLE news_market_wallet_fills ADD COLUMN derived_at_ms bigint")
    # A hard cut: old observations are preserved, never reclassified as new live opportunities.
    op.execute("UPDATE news_market_wallet_fills SET derived_at_ms = classified_at_ms")
    op.execute("""
        CREATE INDEX ix_news_market_wallet_fills_pending
          ON news_market_wallet_fills (block_number, log_index) WHERE derived_at_ms IS NULL
    """)
    op.execute("""
        ALTER TABLE news_market_wallet_events
          ADD COLUMN outcome_attempted_at_ms bigint,
          DROP CONSTRAINT news_market_wallet_events_kind_check,
          ADD CONSTRAINT news_market_wallet_events_kind_check
            CHECK (kind IN ('buy','exit','crowding','digest'))
    """)
    op.execute("""
        ALTER TABLE news_market_wallet_outcomes
          ADD COLUMN item_id text,
          ADD COLUMN reference_price numeric(38,18),
          ADD COLUMN reference_at_ms bigint,
          ADD COLUMN target_at_ms bigint,
          ADD COLUMN reference_kind text NOT NULL DEFAULT 'legacy_delivery'
    """)
    op.execute("""
        UPDATE news_market_wallet_outcomes o
           SET item_id = d.trigger_item_id,
               reference_at_ms = COALESCE(d.settled_at_ms, o.at_ms),
               target_at_ms = COALESCE(d.settled_at_ms, o.at_ms)
                   + CASE WHEN o.horizon = '1h' THEN 3600000 ELSE 14400000 END
          FROM news_market_deliveries d WHERE d.delivery_key = o.delivery_key
    """)
    op.execute("""
        ALTER TABLE news_market_wallet_outcomes
          DROP CONSTRAINT news_market_wallet_outcomes_pkey,
          DROP CONSTRAINT news_market_wallet_outcomes_horizon_check,
          ALTER COLUMN delivery_key DROP NOT NULL,
          ALTER COLUMN item_id SET NOT NULL,
          ALTER COLUMN reference_at_ms SET NOT NULL,
          ALTER COLUMN target_at_ms SET NOT NULL,
          ALTER COLUMN reference_kind DROP DEFAULT,
          ADD CONSTRAINT news_market_wallet_outcomes_pkey PRIMARY KEY (item_id, horizon),
          ADD CONSTRAINT news_market_wallet_outcomes_item_fkey FOREIGN KEY (item_id)
            REFERENCES news_market_wallet_events (item_id) ON DELETE CASCADE,
          ADD CONSTRAINT news_market_wallet_outcomes_horizon_check CHECK (horizon IN ('15m','1h','4h')),
          ADD CONSTRAINT news_market_wallet_outcomes_reference_check CHECK (
            reference_kind IN ('observed','legacy_delivery')
            AND (reference_price IS NULL OR reference_price > 0)
            AND reference_at_ms > 0 AND target_at_ms > reference_at_ms)
    """)
    op.execute("""
        CREATE INDEX ix_news_market_wallet_events_observed
          ON news_market_wallet_events (((evidence->>'observed_at_ms')::bigint))
          WHERE kind IN ('buy','crowding','exit') AND evidence ? 'observed_at_ms'
    """)


def downgrade() -> None:
    raise RuntimeError("news_wallet_buy_research_downgrade_unsupported")
