"""Index bounded story browsing and receipt lookup for the Event reader.

Migration evidence:
- category: additive, reversible read indexes.
- why_database_must_change: 48-hour storyline reads and frozen known-claim receipt lookup need indexed bounds.
- current_source_revision: 20261002_0428
- minimum_supported_source_revision: 20261002_0428
- lock_level_and_order: SHARE on news_events, then news_notifications; News writers stopped.
- statement_timeout: 600s; lock_timeout: 5s.
- estimated_rows: one key per Event and one GIN posting per sent claim; scales with retained history.
- estimated_bytes: one B-tree and one partial JSONB GIN index; no fact rewrite.
- rewrite_or_index_build: two read indexes; earlier intent identity uses the existing unique index.
- preflight_and_maintenance_boundary: normal verified backup and stop-write migration window.
- archive_current_compatibility: frozen cards, receipts, plans and Event heads are unchanged.
- role_and_grant_impact: none; no new grants or extensions.
- failure_state: transactional DDL rollback keeps revision 0428 with existing facts and indexes.
- roll_forward_or_verified_backup_restore: downgrade only these indexes to 0428 before its matching image.
- production_postgres_image: postgres:18-bookworm; isolated PostgreSQL 18 verification.
"""

from alembic import op

revision = "20261002_0429"
down_revision = "20261002_0428"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.execute("CREATE INDEX news_events_story_window ON news_events (storyline_key, opened_at_ms, event_id)")
    op.execute(
        "CREATE INDEX news_notifications_sent_claims ON news_notifications USING gin (claim_refs jsonb_path_ops) "
        "WHERE kind='update' AND state='sent'"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.execute("DROP INDEX news_notifications_sent_claims")
    op.execute("DROP INDEX news_events_story_window")
