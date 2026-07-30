"""Replace repeated hot-path scans with exact lookup indexes."""

from __future__ import annotations

from alembic import op

revision = "20260730_0221"
down_revision = "20260730_0220"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP INDEX ix_news_source_fetches_source_time;
        CREATE INDEX ix_news_source_fetches_source_time
          ON news_source_fetches(source_id, finished_at_ms DESC, fetch_id DESC)
          INCLUDE (status, fetch_path, direct_error_code, entries_seen);

        CREATE INDEX idx_asset_identity_evidence_profile_source
          ON asset_identity_evidence(
            provider, evidence_kind, asset_id, observed_at_ms DESC, evidence_id DESC
          );

        CREATE INDEX idx_asset_identity_evidence_asset_provider_lookup
          ON asset_identity_evidence(
            asset_id, provider, lookup_mode, observed_at_ms DESC, evidence_id DESC
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260730_0221 is an irreversible worker hot-path index hard cut")
