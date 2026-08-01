"""Keep Macro facts and six current modules; remove research audit history.

Revision ID: 20260801_0236
Revises: 20260801_0235
"""

from __future__ import annotations

from alembic import op

revision = "20260801_0236"
down_revision = "20260801_0235"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        DELETE FROM queue_terminal_events
         WHERE owner_key IN ('macro_thesis', 'news_projection')
            OR source_table IN (
              'macro_thesis_runs',
              'macro_thesis_reviews',
              'macro_thesis_publications',
              'macro_research_inputs',
              'macro_evidence_packs',
              'macro_live_deltas',
              'macro_outcome_replays',
              'news_projection_dirty_targets',
              'news_projection_frontiers'
            );

        ALTER TABLE queue_terminal_events
          DROP CONSTRAINT queue_terminal_events_owner_key_check,
          ADD CONSTRAINT queue_terminal_events_owner_key_check CHECK (
            owner_key IN (
              'event_anchor_backfill', 'resolution_refresh',
              'asset_profile_refresh', 'token_image_mirror',
              'radar_projection', 'profile_projection', 'macro_projection',
              'news_brief', 'macro_document_analysis'
            )
          );

        ALTER TABLE macro_acquisition_targets
          DROP CONSTRAINT macro_acquisition_targets_last_receipt_id_fkey,
          DROP COLUMN last_receipt_id;

        DROP TRIGGER macro_thesis_runs_lifecycle ON macro_thesis_runs;
        DROP TRIGGER macro_evidence_packs_append_only ON macro_evidence_packs;
        DROP TRIGGER macro_research_inputs_append_only ON macro_research_inputs;
        DROP TRIGGER macro_thesis_reviews_append_only ON macro_thesis_reviews;
        DROP TRIGGER macro_thesis_publications_append_only ON macro_thesis_publications;
        DROP TRIGGER macro_outcome_replays_append_only ON macro_outcome_replays;
        DROP TRIGGER macro_source_receipts_append_only ON macro_source_receipts;

        ALTER TABLE macro_thesis_runs
          DROP CONSTRAINT macro_thesis_runs_publication_fk;

        DROP TABLE macro_live_deltas;
        DROP TABLE macro_outcome_replays;
        DROP TABLE macro_thesis_publications;
        DROP TABLE macro_thesis_reviews;
        DROP TABLE macro_thesis_runs;
        DROP TABLE macro_research_inputs;
        DROP TABLE macro_evidence_packs;
        DROP TABLE macro_source_receipts;
        DROP TABLE macro_feature_series;
        DROP TABLE macro_projection_state;

        DROP FUNCTION enforce_macro_thesis_run_lifecycle_v2();
        DROP FUNCTION enforce_macro_thesis_run_lifecycle();
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260801_0236 is an irreversible Macro current-module hard cut")
