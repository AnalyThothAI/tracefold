"""Operator label plane (#81): correctable labels, several labellers, and a miss with no Event to hang on.

`news_event_labels` was PK `(event_id, label_version)` with `ON CONFLICT DO NOTHING`, `event_id` NOT NULL and a
cascading FK. Three consequences: relabelling failed silently, two people could not disagree, and the one label
that measures recall — "the reader should have got this and did not" — could only be recorded against an Event
that already existed, which is exactly the case a real miss does not have. The table also died with its Event.

The PK becomes a deterministic `label_id` so a write stays idempotent by key, `event_id` becomes nullable with
`ON DELETE SET NULL` (the denormalised `subject` keeps the row readable after the Event is purged), and a unique
index keeps one label per (subject, version, labeller).

Also indexes `news_items.observed_at_ms`, which the retention purge scans every minute and never had an index for.

Revision ID: 20260820_0281
Revises: 20260820_0280
"""

from __future__ import annotations

from alembic import op

revision = "20260820_0281"
down_revision = "20260820_0280"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE news_event_labels ADD COLUMN labeled_by text NOT NULL DEFAULT 'operator'")
    op.execute("ALTER TABLE news_event_labels ADD COLUMN subject text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE news_event_labels ADD COLUMN label_id text")
    # Same identity the repository computes for an Event-anchored label: sha256("<event_id>:<version>:<who>").
    op.execute(
        "UPDATE news_event_labels SET label_id = "
        "encode(sha256((event_id || ':' || label_version || ':' || 'operator')::bytea), 'hex') "
        "WHERE label_id IS NULL"
    )
    op.execute("ALTER TABLE news_event_labels ALTER COLUMN label_id SET NOT NULL")
    op.execute("ALTER TABLE news_event_labels DROP CONSTRAINT news_event_labels_pkey")
    op.execute("ALTER TABLE news_event_labels ADD CONSTRAINT news_event_labels_pkey PRIMARY KEY (label_id)")
    op.execute("ALTER TABLE news_event_labels ALTER COLUMN event_id DROP NOT NULL")
    op.execute("ALTER TABLE news_event_labels DROP CONSTRAINT news_event_labels_event_id_fkey")
    op.execute(
        "ALTER TABLE news_event_labels ADD CONSTRAINT news_event_labels_event_id_fkey "
        "FOREIGN KEY (event_id) REFERENCES news_events(event_id) ON DELETE SET NULL"
    )
    # One label per (Event, version, labeller); for a miss with no Event, one per (subject, version, labeller).
    op.execute(
        "CREATE UNIQUE INDEX ux_news_event_labels_event ON news_event_labels "
        "(event_id, label_version, labeled_by) WHERE event_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_news_event_labels_subject ON news_event_labels "
        "(subject, label_version, labeled_by) WHERE event_id IS NULL"
    )
    op.execute("CREATE INDEX ix_news_event_labels_created ON news_event_labels (created_at_ms DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_news_items_observed ON news_items (observed_at_ms)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_news_items_observed")
    op.execute("DROP INDEX IF EXISTS ix_news_event_labels_created")
    op.execute("DROP INDEX IF EXISTS ux_news_event_labels_subject")
    op.execute("DROP INDEX IF EXISTS ux_news_event_labels_event")
    op.execute("DELETE FROM news_event_labels WHERE event_id IS NULL")
    op.execute("ALTER TABLE news_event_labels DROP CONSTRAINT news_event_labels_event_id_fkey")
    op.execute("ALTER TABLE news_event_labels ALTER COLUMN event_id SET NOT NULL")
    op.execute(
        "ALTER TABLE news_event_labels ADD CONSTRAINT news_event_labels_event_id_fkey "
        "FOREIGN KEY (event_id) REFERENCES news_events(event_id) ON DELETE CASCADE"
    )
    op.execute("ALTER TABLE news_event_labels DROP CONSTRAINT news_event_labels_pkey")
    op.execute(
        "DELETE FROM news_event_labels a USING news_event_labels b "
        "WHERE a.ctid > b.ctid AND a.event_id = b.event_id AND a.label_version = b.label_version"
    )
    op.execute(
        "ALTER TABLE news_event_labels ADD CONSTRAINT news_event_labels_pkey PRIMARY KEY (event_id, label_version)"
    )
    op.execute("ALTER TABLE news_event_labels DROP COLUMN label_id")
    op.execute("ALTER TABLE news_event_labels DROP COLUMN subject")
    op.execute("ALTER TABLE news_event_labels DROP COLUMN labeled_by")
