"""Bound OpenNews REST recovery and retain one provider gap boundary.

Revision ID: 20260801_0237
Revises: 20260801_0236
"""

from __future__ import annotations

from alembic import op

revision = "20260801_0237"
down_revision = "20260801_0236"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE news_sources
          ADD COLUMN gap_boundary_provider_record_id text,
          ADD COLUMN gap_version bigint NOT NULL DEFAULT 0,
          ADD CONSTRAINT news_sources_gap_boundary_provider_record_id_check
            CHECK (
              gap_boundary_provider_record_id IS NULL
              OR (
                btrim(gap_boundary_provider_record_id) <> ''
                AND octet_length(gap_boundary_provider_record_id) <= 512
              )
            ),
          ADD CONSTRAINT news_sources_gap_version_check
            CHECK (gap_version >= 0);

        UPDATE news_sources source
           SET gap_boundary_provider_record_id = (
                 SELECT item.provider_record_id
                   FROM news_items item
                  WHERE item.source_id = source.source_id
                  ORDER BY item.last_observed_at_ms DESC, item.item_id DESC
                  LIMIT 1
               ),
               gap_version = 1
         WHERE source.source_kind = 'opennews'
           AND source.gap_unclosed;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260801_0237 is an irreversible OpenNews recovery hard cut")
