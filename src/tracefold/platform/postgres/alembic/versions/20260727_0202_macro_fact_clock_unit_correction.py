"""Remove open Binance candles and Macro facts carrying incorrect source units."""

from __future__ import annotations

from alembic import op

revision = "20260727_0202"
down_revision = "20260727_0201"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5min'")
    op.execute(
        """
        TRUNCATE TABLE
          macro_research_publications,
          macro_research_runs,
          macro_daily_judgments,
          macro_event_updates,
          macro_evidence_packs,
          macro_module_current,
          macro_feature_series
        CASCADE
        """
    )
    _delete_invalid_facts()
    _reset_acquisition_targets()
    op.execute(
        """
        DELETE FROM checkpoints
         WHERE thread_id LIKE 'macro-research:%'
            OR thread_id LIKE 'macro_research:%'
        """
    )


def _delete_invalid_facts() -> None:
    op.execute("DROP TRIGGER IF EXISTS market_observations_append_only ON market_observations")
    op.execute(
        """
        DELETE FROM market_observations
         WHERE dataset_id = 'binance.btcusdt.spot'
           AND (
             observed_at_ms > (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint
             OR received_at_ms = observed_at_ms
           )
        """
    )
    op.execute(
        """
        CREATE TRIGGER market_observations_append_only
        BEFORE UPDATE OR DELETE ON market_observations
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation()
        """
    )
    op.execute("DROP TRIGGER IF EXISTS macro_series_facts_append_only ON macro_series_facts")
    op.execute(
        """
        DELETE FROM macro_series_facts
         WHERE dataset_id IN ('fred.wrbwfrbl', 'fred.wtregen')
        """
    )
    op.execute(
        """
        CREATE TRIGGER macro_series_facts_append_only
        BEFORE UPDATE OR DELETE ON macro_series_facts
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation()
        """
    )
    op.execute("DROP TRIGGER IF EXISTS macro_source_receipts_append_only ON macro_source_receipts")
    op.execute(
        """
        DELETE FROM macro_source_receipts
         WHERE dataset_id IN (
           'binance.btcusdt.spot',
           'fred.wrbwfrbl',
           'fred.wtregen'
         )
        """
    )
    op.execute(
        """
        CREATE TRIGGER macro_source_receipts_append_only
        BEFORE UPDATE OR DELETE ON macro_source_receipts
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation()
        """
    )


def _reset_acquisition_targets() -> None:
    op.execute(
        """
        UPDATE macro_acquisition_targets
           SET cursor_json = '{}'::jsonb,
               status = 'pending',
               next_due_at_ms = 0,
               leased_until_ms = NULL,
               lease_owner = NULL,
               attempt_count = 0,
               last_receipt_id = NULL,
               last_success_at_ms = NULL,
               last_error_code = NULL,
               updated_at_ms = GREATEST(
                 updated_at_ms,
                 (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint
               )
         WHERE dataset_id IN (
           'binance.btcusdt.spot',
           'fred.wrbwfrbl',
           'fred.wtregen'
         )
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260727_0202 is irreversible and removes invalid Macro material facts")
