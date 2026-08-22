"""Latest-only CoinGlass liquidation-level shadow read model (#144).

The identity is provider + exact venue pair + model contract + range. Rows retain only the strongest 64 raw
model levels; failed attempts update health metadata without erasing the last successful zones.

Revision ID: 20260822_0299
Revises: 20260822_0298
"""

from __future__ import annotations

from alembic import op

revision = "20260822_0299"
down_revision = "20260822_0298"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE news_liquidation_snapshots (
          provider           TEXT    NOT NULL,
          venue              TEXT    NOT NULL,
          venue_symbol       TEXT    NOT NULL,
          base_symbol        TEXT    NOT NULL,
          quote_asset        TEXT    NOT NULL,
          model_version      TEXT    NOT NULL,
          range_key          TEXT    NOT NULL,
          contract           TEXT    NOT NULL,
          authenticated      BOOLEAN NOT NULL,
          completeness       TEXT    NOT NULL,
          zones              JSONB   NOT NULL,
          source_at_ms       BIGINT,
          received_at_ms     BIGINT,
          last_success_at_ms BIGINT,
          last_attempt_at_ms BIGINT  NOT NULL,
          freshness          TEXT    NOT NULL,
          degraded           BOOLEAN NOT NULL,
          error_class        TEXT,
          payload_sha256     TEXT,
          raw_level_count    INTEGER NOT NULL,
          raw_price_count    INTEGER NOT NULL,
          PRIMARY KEY (provider, venue, venue_symbol, model_version, range_key),
          CONSTRAINT news_liquidation_snapshots_freshness_check
            CHECK (freshness IN ('fresh', 'stale', 'unavailable')),
          CONSTRAINT news_liquidation_snapshots_zones_check
            CHECK (jsonb_typeof(zones) = 'array' AND jsonb_array_length(zones) <= 64),
          CONSTRAINT news_liquidation_snapshots_counts_check
            CHECK (raw_level_count >= 0 AND raw_price_count >= 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_news_liquidation_snapshots_attempt "
        "ON news_liquidation_snapshots (provider, model_version, range_key, last_attempt_at_ms)"
    )
    op.execute("GRANT SELECT ON news_liquidation_snapshots TO tracefold_serve")
    op.execute("GRANT SELECT, INSERT, UPDATE ON news_liquidation_snapshots TO tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("20260822_0299 is an irreversible liquidation-shadow contract")
