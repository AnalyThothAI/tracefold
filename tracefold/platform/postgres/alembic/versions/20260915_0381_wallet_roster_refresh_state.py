"""Record what the roster refresh did, and drop an outcome status nothing writes (#649 §5.1, §9).

Migration evidence:
- category: three nullable observability columns on one single-row state table, and one CHECK
  restatement that removes a value no writer produces.
- why_database_must_change: the roster refresh became its own Workers task, and a refresh that
  failed must be able to say so without being confused with a collection turn that failed. The
  tape state row already carries `last_outcome`/`last_error` for the chain half; the roster half
  had nowhere to record an attempt that did not publish, so a rate-limited hour was indistinguishable
  from an hour in which the list genuinely did not change. Separately, the outcome status CHECK has
  admitted `unavailable` since `20260912_0376` and `WalletPriceSampler` has never written it -- the
  three values it produces are `comparable`, `missing_reference` and `late` -- so the column, the
  API `Literal` and the generated TS union all published a fourth state that cannot occur.
- current_source_revision: 20260915_0380
- minimum_supported_source_revision: 20260915_0380
- lock_level_and_order: ACCESS EXCLUSIVE on news_market_wallet_tape_state for the column catalog
  update, then ACCESS EXCLUSIVE on news_market_wallet_outcomes for the CHECK swap. No table is
  rewritten; the new CHECK is added NOT VALID and validated separately under SHARE UPDATE EXCLUSIVE.
- statement_timeout: 30s
- lock_timeout: 5s
- estimated_rows: one `chain_tape` row; the outcomes table is validated in full, and it holds at most
  three rows per episode over the retention window (low tens of thousands).
- estimated_bytes: negligible; three nullable columns add no stored bytes to the existing row.
- rewrite_or_index_build: none. `ADD COLUMN ... NULL` without a default is catalog-only in
  PostgreSQL 11+, and a CHECK swap rewrites nothing. The upgrade refuses if any row already holds
  `unavailable`, rather than deleting or rewriting a stored observation.
- preflight_and_maintenance_boundary: online. The columns are additive and nullable, so the running
  Serve and Workers images read the table unchanged until the new image is deployed.
- archive_current_compatibility: nothing is archived because nothing is removed or rewritten.
- role_and_grant_impact: unchanged single application login.
- failure_state: transaction rollback leaves the table exactly as it was.
- roll_forward_or_verified_backup_restore: downgrade drops the three columns and restores the wider
  status CHECK, losing only the last refresh attempt's stamp and reason.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296
"""

from alembic import op

revision = "20260915_0381"
down_revision = "20260915_0380"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("""
        ALTER TABLE news_market_wallet_tape_state
            ADD COLUMN roster_last_attempt_at_ms bigint,
            ADD COLUMN roster_last_success_at_ms bigint,
            ADD COLUMN roster_last_error text
    """)
    # Fail closed rather than rewrite an observation: if anything ever did write `unavailable`, an
    # operator decides what it meant, and this migration is not the place to guess.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM news_market_wallet_outcomes WHERE status = 'unavailable') THEN
                RAISE EXCEPTION 'news_market_wallet_outcomes still holds status=unavailable';
            END IF;
        END $$
    """)
    op.execute("ALTER TABLE news_market_wallet_outcomes DROP CONSTRAINT news_market_wallet_outcomes_status_check")
    op.execute("""
        ALTER TABLE news_market_wallet_outcomes
            ADD CONSTRAINT news_market_wallet_outcomes_status_check
            CHECK (status IN ('comparable','missing_reference','late'))
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("ALTER TABLE news_market_wallet_outcomes DROP CONSTRAINT news_market_wallet_outcomes_status_check")
    op.execute("""
        ALTER TABLE news_market_wallet_outcomes
            ADD CONSTRAINT news_market_wallet_outcomes_status_check
            CHECK (status IN ('comparable','missing_reference','unavailable','late'))
    """)
    op.execute("""
        ALTER TABLE news_market_wallet_tape_state
            DROP COLUMN roster_last_attempt_at_ms,
            DROP COLUMN roster_last_success_at_ms,
            DROP COLUMN roster_last_error
    """)
