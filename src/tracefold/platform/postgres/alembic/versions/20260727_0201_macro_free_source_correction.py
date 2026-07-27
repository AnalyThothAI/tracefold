"""Remove the unusable Stooq lane and invalidate derived Macro state."""

from __future__ import annotations

from alembic import op

revision = "20260727_0201"
down_revision = "20260727_0200"
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
    op.execute(
        """
        DELETE FROM macro_acquisition_targets
         WHERE dataset_id LIKE 'stooq.%'
        """
    )
    op.execute("DROP TRIGGER IF EXISTS macro_source_receipts_append_only ON macro_source_receipts")
    op.execute(
        """
        DELETE FROM macro_source_receipts
         WHERE dataset_id LIKE 'stooq.%'
        """
    )
    op.execute(
        """
        CREATE TRIGGER macro_source_receipts_append_only
        BEFORE UPDATE OR DELETE ON macro_source_receipts
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation()
        """
    )
    op.execute("DROP TRIGGER IF EXISTS market_observations_append_only ON market_observations")
    op.execute(
        """
        DELETE FROM market_observations
         WHERE dataset_id LIKE 'stooq.%'
            OR source_id = 'stooq'
        """
    )
    op.execute(
        """
        CREATE TRIGGER market_observations_append_only
        BEFORE UPDATE OR DELETE ON market_observations
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation()
        """
    )
    op.execute(
        """
        DELETE FROM checkpoints
         WHERE thread_id LIKE 'macro-research:%'
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260727_0201 is irreversible and removes the unsupported Stooq source lane")
