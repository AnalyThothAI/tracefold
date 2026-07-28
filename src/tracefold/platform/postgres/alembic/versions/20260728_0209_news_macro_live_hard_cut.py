"""Focus News and restore a best-effort intraday Macro market clock."""

from __future__ import annotations

from alembic import op

revision = "20260728_0209"
down_revision = "20260727_0208"
branch_labels = None
depends_on = None

_RETIRED_NASDAQ_DATASETS = (
    "nasdaq.spy.history",
    "nasdaq.qqq.history",
    "nasdaq.iwm.history",
    "nasdaq.tlt.history",
    "nasdaq.ief.history",
    "nasdaq.lqd.history",
    "nasdaq.hyg.history",
    "nasdaq.dxy.history",
    "nasdaq.gld.history",
    "nasdaq.uso.history",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5min'")
    op.execute(
        """
        ALTER TABLE macro_acquisition_targets
          DROP CONSTRAINT macro_acquisition_targets_clock_kind_check
        """
    )
    op.execute(
        """
        ALTER TABLE macro_acquisition_targets
          ADD CONSTRAINT macro_acquisition_targets_clock_kind_check
          CHECK (
            clock_kind IN (
              'intraday_market',
              'daily_settlement',
              'scheduled_release',
              'official_state',
              'official_document',
              'backfill'
            )
          )
        """
    )
    op.execute(
        f"""
        DELETE FROM macro_acquisition_targets
         WHERE dataset_id IN ({", ".join(repr(value) for value in _RETIRED_NASDAQ_DATASETS)})
        """
    )
    op.execute("DELETE FROM macro_module_current")
    op.execute(
        """
        CREATE TABLE macro_judgment_status (
          session_date DATE PRIMARY KEY,
          judgment_cutoff_ms BIGINT NOT NULL CHECK (judgment_cutoff_ms >= 0),
          state TEXT NOT NULL CHECK (state IN ('blocked', 'current')),
          reason_code TEXT NOT NULL CHECK (btrim(reason_code) <> ''),
          details_json JSONB NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(details_json) = 'object'),
          payload_hash TEXT NOT NULL CHECK (btrim(payload_hash) <> ''),
          attempted_at_ms BIGINT NOT NULL CHECK (attempted_at_ms >= judgment_cutoff_ms),
          updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= attempted_at_ms)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_judgment_status_updated
          ON macro_judgment_status(updated_at_ms DESC, session_date DESC)
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260728_0209 is an irreversible News and Macro live-market hard cut")
