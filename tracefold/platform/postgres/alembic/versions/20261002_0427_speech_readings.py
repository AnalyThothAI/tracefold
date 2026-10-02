"""#791: convert persisted speech readings to the sole v5 contract.

Migration evidence:
- category: forward News semantic reading conversion, without live compatibility aliases.
- why_database_must_change: strict v5 readers cannot parse the retired v4 mode values.
- production_postgres_image: postgres:18-bookworm; verified with PostgreSQL 18.6 fixture clones.
- current_source_revision: 20261002_0426
- minimum_supported_source_revision: 20261002_0426
- lock_level_and_order: ACCESS EXCLUSIVE on analyses, notifications; then cache, jobs, public outbox.
- statement_timeout: 600s; lock_timeout: 5s.
- estimated_rows: approximately 75k semantic claims plus their frozen receipts/checkpoints.
- estimated_bytes: bounded JSONB rewrite of documents containing retired speech modes.
- rewrite_or_index_build: no new index; rewrite derived mode readings only, preserving refs and source text.
- preflight_and_maintenance_boundary: verified backup; stop all writers before migrate-before-start.
- archive_current_compatibility: convert historical readings; preserve source quotes and fact identities.
- role_and_grant_impact: none; temporary function is removed before commit.
- failure_state: transactional rollback also restores both immutable-record triggers.
- roll_forward_or_verified_backup_restore: forward-only; restore verified backup with its previous image.
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "20261002_0427"
down_revision = "20261002_0426"
branch_labels = None
depends_on = None

_COLUMNS = {
    "news_analyses": ("document", "understanding", "input_manifest", "repair"),
    "news_notifications": ("input_snapshot", "plan", "history_context", "sent_claims", "card_copy_document"),
    "news_judgment_cache": ("answer",),
    "news_jobs": ("detail",),
}
_PREDICATE = '$.**.mode ? (@ == "commentary" || @ == "conditional_threat")'


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    # Record guards remain active in normal operation. Their transactional suspension is
    # limited to this reading conversion during the writer-stopped maintenance boundary.
    op.execute("ALTER TABLE news_analyses DISABLE TRIGGER news_analyses_immutable")
    op.execute("ALTER TABLE news_notifications DISABLE TRIGGER news_notifications_guard")
    op.execute("""
CREATE FUNCTION pg_temp.news_speech_v5(value jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE result jsonb;
BEGIN
 IF jsonb_typeof(value) = 'object' THEN
  SELECT coalesce(jsonb_object_agg(key,
    CASE WHEN key = 'mode' AND item = '"conditional_threat"'::jsonb THEN '"threat"'::jsonb
         WHEN key = 'mode' AND item = '"commentary"'::jsonb THEN '"unknown"'::jsonb
         ELSE pg_temp.news_speech_v5(item) END), '{}'::jsonb) INTO result
  FROM jsonb_each(value) AS entry(key,item);
  RETURN result;
 ELSIF jsonb_typeof(value) = 'array' THEN
  SELECT coalesce(jsonb_agg(pg_temp.news_speech_v5(item) ORDER BY position), '[]'::jsonb) INTO result
  FROM jsonb_array_elements(value) WITH ORDINALITY AS entry(item,position);
  RETURN result;
 END IF;
 RETURN value;
END $$;
""")
    for table, columns in _COLUMNS.items():
        for column in columns:
            op.execute(
                sa.text(
                    f"UPDATE {table} SET {column}=pg_temp.news_speech_v5({column}) "  # noqa: S608 -- static identifiers
                    f"WHERE {column} @? CAST(:predicate AS jsonpath)"
                ).bindparams(predicate=_PREDICATE)
            )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "UPDATE news_trade_events SET payload=pg_temp.news_speech_v5(payload) "
            "WHERE payload @? CAST(:predicate AS jsonpath) RETURNING event_id,payload"
        ),
        {"predicate": _PREDICATE},
    ).all()
    for event_id, payload in rows:
        # The public payload's byte digest follows its converted structured reading.
        # Source keys, revision, public update ID and original source evidence are retained.
        material = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        bind.execute(
            sa.text("UPDATE news_trade_events SET payload_sha256=:sha WHERE event_id=:event"),
            {"sha": hashlib.sha256(material.encode()).hexdigest(), "event": event_id},
        )
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("DROP FUNCTION pg_temp.news_speech_v5(jsonb)")
    op.execute("ALTER TABLE news_notifications ENABLE TRIGGER news_notifications_guard")
    op.execute("ALTER TABLE news_analyses ENABLE TRIGGER news_analyses_immutable")


def downgrade() -> None:
    raise RuntimeError("Speech readings are forward-only: restore a verified backup and its matching image")
