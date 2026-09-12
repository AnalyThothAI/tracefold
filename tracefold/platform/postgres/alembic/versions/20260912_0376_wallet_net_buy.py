"""Replace retired wallet research with token net-buy episodes (#641).

Migration evidence:
- category: stopped-writer wallet hard cut with lossless JSON evidence archive.
- why_database_must_change: episode identity, immutable first trigger, current two-window
  facts and coverage cannot be represented by single-wallet research columns.
- current_source_revision: 20260908_0375
- minimum_supported_source_revision: 20260908_0375
- lock_level_and_order: ACCESS EXCLUSIVE wallet outcomes, checks, events, tape state,
  roster; ROW EXCLUSIVE wallet delivery/item rows. No Trading tables are changed.
- statement_timeout: 60s
- lock_timeout: 5s
- estimated_rows: bounded by retained wallet history; tens of thousands expected.
- estimated_bytes: archive duplicates existing JSON/rows during the transaction;
  operator measures relation sizes and free space before the maintenance window.
- rewrite_or_index_build: archived rows are copied once; new episode indexes are empty;
  roster metadata and pending-fill progress are updated once.
- preflight_and_maintenance_boundary: stop Serve and Workers and coordinate every
  shared-config reader before applying; verified external backup is required by the
  release procedure. Never restart the order runtime implicitly.
- archive_current_compatibility: all retired event/check/outcome/state rows survive as
  exact to_jsonb payloads. Frozen deliveries and attempted/sent/unknown evidence stay
  unchanged. Old pending unattempted intents are terminated, never converted.
- role_and_grant_impact: unchanged single application login.
- failure_state: transaction rollback restores the entire pre-cut schema and data.
- roll_forward_or_verified_backup_restore: downgrade refused; roll forward or restore
  the verified backup under the stopped-writer maintenance gate.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296
"""

from alembic import op

revision = "20260912_0376"
down_revision = "20260908_0375"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("""
        CREATE TABLE news_market_wallet_archive (
            record_type text NOT NULL,
            record_key text NOT NULL,
            payload jsonb NOT NULL,
            archived_at_ms bigint NOT NULL,
            PRIMARY KEY (record_type, record_key)
        )
    """)
    for table, key in (
        ("events", "item_id"),
        ("outcomes", "item_id || '|' || horizon"),
        ("checks", "chain_id::text || '|' || tx_hash || '|' || log_index::text"),
        ("tape_state", "state_id"),
    ):
        op.execute(f"""
            INSERT INTO news_market_wallet_archive
            SELECT '{table}', {key}, to_jsonb(t), (extract(epoch FROM now()) * 1000)::bigint
              FROM news_market_wallet_{table} t
        """)  # noqa: S608 -- interpolates only code-owned SQL identifiers.
    op.execute("""
        UPDATE news_market_deliveries SET state = 'failed', error = 'wallet_net_buy_cutover',
               settled_at_ms = (extract(epoch FROM now()) * 1000)::bigint,
               updated_at_ms = (extract(epoch FROM now()) * 1000)::bigint
         WHERE market_kind = 'wallet' AND state IN ('pending', 'unavailable') AND attempts = 0
    """)
    op.execute("""
        UPDATE news_items SET market_notify_state = 'processed'
         WHERE market_kind = 'wallet' AND market_notify_state = 'pending'
    """)
    op.execute("DROP TABLE news_market_wallet_outcomes")
    op.execute("DROP TABLE news_market_wallet_checks")
    op.execute("DROP TABLE news_market_wallet_events")
    op.execute("""
        CREATE TABLE news_market_wallet_events (
            item_id text PRIMARY KEY REFERENCES news_items(item_id) ON DELETE CASCADE,
            chain_id bigint NOT NULL CHECK (chain_id > 0),
            token text NOT NULL CHECK (token ~ '^0x[0-9a-f]{40}$'),
            token_symbol text,
            trigger_tx_hash text NOT NULL CHECK (trigger_tx_hash ~ '^0x[0-9a-f]{64}$'),
            event_at_ms bigint NOT NULL,
            received_at_ms bigint NOT NULL,
            detected_at_ms bigint NOT NULL,
            last_effective_buy_at_ms bigint NOT NULL,
            ended_at_ms bigint,
            initial_snapshot jsonb NOT NULL,
            latest_snapshot jsonb NOT NULL,
            latest_matched boolean NOT NULL,
            change_reason text NOT NULL,
            updated_at_ms bigint NOT NULL,
            trigger_max_age_s integer NOT NULL CHECK (trigger_max_age_s > 0),
            notification_eligible boolean NOT NULL,
            notification_reason text,
            send_snapshot jsonb,
            reference_price numeric,
            reference_at_ms bigint,
            reference_source text,
            outcome_attempted_at_ms bigint,
            UNIQUE (chain_id, token, trigger_tx_hash),
            CHECK ((reference_price IS NULL) = (reference_at_ms IS NULL)),
            CHECK ((reference_price IS NULL) = (reference_source IS NULL)),
            CHECK (reference_price IS NULL OR reference_price > 0)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX ix_news_market_wallet_events_active
        ON news_market_wallet_events(chain_id, token) WHERE ended_at_ms IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_news_market_wallet_events_triggered
        ON news_market_wallet_events(event_at_ms DESC, item_id DESC)
    """)
    op.execute("""
        CREATE TABLE news_market_wallet_outcomes (
            item_id text NOT NULL REFERENCES news_market_wallet_events(item_id) ON DELETE CASCADE,
            horizon text NOT NULL CHECK (horizon IN ('15m','1h','4h')),
            delivery_key text REFERENCES news_market_deliveries(delivery_key) ON DELETE SET NULL,
            target_at_ms bigint NOT NULL,
            at_ms bigint NOT NULL,
            price numeric,
            source text NOT NULL,
            reference_price numeric,
            reference_at_ms bigint,
            status text NOT NULL CHECK (status IN ('comparable','missing_reference','unavailable','late')),
            PRIMARY KEY (item_id, horizon),
            CHECK (price IS NULL OR price > 0),
            CHECK (status <> 'comparable' OR
              (price IS NOT NULL AND reference_price > 0 AND reference_at_ms IS NOT NULL))
        )
    """)
    op.execute("""
        ALTER TABLE news_market_wallet_tape_state
          DROP COLUMN digest_attempted_at_ms,
          ADD COLUMN detection_cutover_at_ms bigint NOT NULL DEFAULT ((extract(epoch FROM now()) * 1000)::bigint),
          ADD COLUMN coverage_from_ms bigint,
          ADD COLUMN scanned_at_ms bigint,
          ADD COLUMN scanned_block bigint,
          ADD COLUMN scanned_log integer,
          ADD COLUMN gap_at_ms bigint
    """)
    op.execute("""
        ALTER TABLE news_market_wallet_roster
          ADD COLUMN known_at_ms bigint,
          ADD COLUMN monitoring_from_ms bigint
    """)
    # Historical refresh time cannot prove when membership was first known or coverage began.
    # Existing members establish support afresh after the cut, without replaying old candidates.
    op.execute("""
        UPDATE news_market_wallet_roster
           SET known_at_ms = (extract(epoch FROM now()) * 1000)::bigint
    """)
    op.execute("ALTER TABLE news_market_wallet_roster ALTER COLUMN known_at_ms SET NOT NULL")
    op.execute("DROP INDEX ix_news_market_wallet_fills_pending")
    op.execute("ALTER TABLE news_market_wallet_fills ADD COLUMN derived_reason text")
    op.execute("""
        UPDATE news_market_wallet_fills SET derived_at_ms = classified_at_ms,
               derived_reason = 'wallet_net_buy_cutover' WHERE derived_at_ms IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_news_market_wallet_fills_pending_order
          ON news_market_wallet_fills(block_number, log_index, chain_id, tx_hash)
          WHERE derived_at_ms IS NULL
    """)

    op.execute("""
        CREATE INDEX ix_news_market_wallet_fills_block
          ON news_market_wallet_fills(chain_id, block_number, log_index)
          INCLUDE (tx_hash, block_hash, wallet)
    """)


def downgrade() -> None:
    raise RuntimeError("wallet_net_buy_downgrade_requires_verified_backup")
