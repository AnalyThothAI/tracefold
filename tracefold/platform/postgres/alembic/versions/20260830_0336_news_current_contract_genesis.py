"""Start an empty current-contract News evidence epoch (#398).

Revision ID: 20260830_0336
Revises: 20260830_0335
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260830_0336"
down_revision = "20260830_0335"
branch_labels = None
depends_on = None

_ISSUE = "https://github.com/AnalyThothAI/tracefold/issues/398"
_PREFLIGHT_ENV = "TRACEFOLD_NEWS_GENESIS_PREFLIGHT_JSON"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

# Deliberately explicit: adding a new Event/evidence owner must fail the migration
# review instead of being pulled into an accidental CASCADE.
_CLEARED_TABLES = (
    "news_agent_assignments",
    "news_agent_runtime_manifests",
    "news_canary_activations",
    "news_deliveries",
    "news_event_assets",
    "news_event_bands",
    "news_event_evidence_snapshots",
    "news_event_members",
    "news_event_reactions",
    "news_events",
    "news_external_miss_snapshots",
    "news_ingest_state",
    "news_items",
    "news_learning_artifacts",
    "news_learning_cases",
    "news_learning_epochs",
    "news_learning_retention_state",
    "news_model_recordings",
    "news_oi_signals",
    "news_opennews_incidents",
    "news_reviews",
    "news_verdicts",
)
_PRESERVED_TABLES = (
    "news_market_instrument_listing_events",
    "news_market_instruments",
    "news_market_liquidations",
    "news_quote_snapshots",
    "news_symbol_aliases",
)
_EXPECTED_TABLES = frozenset((*_CLEARED_TABLES, *_PRESERVED_TABLES))

_SCHEMA_DIGEST_SQL = """
WITH objects AS (
  SELECT 'relation'::text AS kind,
         c.relname::text AS owner,
         c.relname::text AS name,
         concat_ws('|', c.relkind::text, COALESCE(c.relacl::text, ''),
                   CASE WHEN c.relkind = 'v' THEN pg_get_viewdef(c.oid, true) ELSE '' END) AS definition
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND left(c.relname, 5) = 'news_'
     AND c.relkind IN ('r', 'v', 'S')
  UNION ALL
  SELECT 'column', c.relname, a.attname,
         concat_ws('|', a.attnum::text, format_type(a.atttypid, a.atttypmod),
                   a.attnotnull::text, COALESCE(pg_get_expr(d.adbin, d.adrelid), ''))
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
   WHERE n.nspname = 'public' AND left(c.relname, 5) = 'news_'
     AND c.relkind IN ('r', 'v', 'S') AND a.attnum > 0 AND NOT a.attisdropped
  UNION ALL
  SELECT 'constraint', con.conrelid::regclass::text, con.conname, pg_get_constraintdef(con.oid, true)
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND left(c.relname, 5) = 'news_'
  UNION ALL
  SELECT 'index', tbl.relname, idx.relname, pg_get_indexdef(idx.oid)
    FROM pg_index i
    JOIN pg_class idx ON idx.oid = i.indexrelid
    JOIN pg_class tbl ON tbl.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = tbl.relnamespace
   WHERE n.nspname = 'public' AND left(tbl.relname, 5) = 'news_'
  UNION ALL
  SELECT 'trigger', t.tgrelid::regclass::text, t.tgname, pg_get_triggerdef(t.oid, true)
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND left(c.relname, 5) = 'news_' AND NOT t.tgisinternal
  UNION ALL
  SELECT 'function', '', p.proname,
         concat_ws('|', pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid),
                   COALESCE(p.proacl::text, ''))
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = 'public' AND left(p.proname, 5) = 'news_' AND p.prokind = 'f'
), document AS (
  SELECT COALESCE(
           jsonb_agg(jsonb_build_array(kind, owner, name, definition)
                     ORDER BY kind COLLATE "C", owner COLLATE "C", name COLLATE "C", definition COLLATE "C"),
           '[]'::jsonb
         ) AS value
    FROM objects
)
SELECT encode(sha256(convert_to(value::text, 'UTF8')), 'hex') AS digest
  FROM document
"""


def _counts(bind: Any, tables: tuple[str, ...]) -> dict[str, int]:
    return {table: int(bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()) for table in tables}


def _schema_digest(bind: Any) -> str:
    return str(bind.execute(sa.text(_SCHEMA_DIGEST_SQL)).scalar_one())


def _assert_news_table_inventory(bind: Any) -> None:
    actual = frozenset(
        bind.execute(
            sa.text(
                "SELECT rel.relname FROM pg_class rel "
                "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
                "WHERE ns.nspname = 'public' AND rel.relkind IN ('r', 'p') "
                "AND left(rel.relname, 5) = 'news_'"
            )
        ).scalars()
    )
    if actual != _EXPECTED_TABLES:
        missing = sorted(_EXPECTED_TABLES - actual)
        unexpected = sorted(actual - _EXPECTED_TABLES)
        raise RuntimeError(f"20260830_0336 News table disposition drift: missing={missing}, unexpected={unexpected}")


def _assert_current_only_schema(bind: Any) -> None:
    _assert_news_table_inventory(bind)
    legacy_count = int(
        bind.execute(
            sa.text(
                """
                SELECT
                  (SELECT count(*) FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name IN ('news_events', 'news_reviews')
                      AND column_name = 'current_contract_archive_only')
                  + (SELECT count(*) FROM pg_class rel
                       JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                      WHERE ns.nspname = 'public'
                        AND rel.relname IN ('news_current_events_v1', 'ix_news_events_current_opened'))
                  + (SELECT count(*) FROM pg_proc fn
                       JOIN pg_namespace ns ON ns.oid = fn.pronamespace
                      WHERE ns.nspname = 'public'
                        AND fn.proname = 'news_current_event_archive_guard')
                  + (SELECT count(*) FROM pg_trigger
                      WHERE NOT tgisinternal
                        AND tgname IN (
                          'news_events_current_archive_only_check',
                          'news_reviews_current_archive_only_check',
                          'news_event_evidence_current_archive_only_check'
                        ))
                """
            )
        ).scalar_one()
    )
    if legacy_count:
        raise RuntimeError("20260830_0336 left a retired News archive compatibility object")


def _requires_preflight(bind: Any, counts: dict[str, int], preserved_counts: dict[str, int]) -> bool:
    migration_seeded = {
        "news_ingest_state",
        "news_learning_artifacts",
        "news_learning_epochs",
        "news_learning_retention_state",
    }
    if any(count for table, count in counts.items() if table not in migration_seeded) or any(preserved_counts.values()):
        return True
    if bool(
        bind.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM news_learning_artifacts WHERE created_by NOT LIKE 'migration_%')")
        ).scalar_one()
    ):
        return True
    seeded_epochs = tuple(f"program_v{version}" for version in range(1, 10))
    if bool(
        bind.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM news_learning_epochs WHERE epoch_id != ALL(:seeded))"),
            {"seeded": list(seeded_epochs)},
        ).scalar_one()
    ):
        return True
    if bool(
        bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM news_learning_retention_state "
                "WHERE last_run_at_ms IS NOT NULL OR eligible_recordings <> 0 "
                "OR eligible_cases <> 0 OR eligible_artifacts <> 0 "
                "OR deleted_recordings <> 0 OR deleted_cases <> 0 OR deleted_artifacts <> 0 "
                "OR last_error_code IS NOT NULL)"
            )
        ).scalar_one()
    ):
        return True
    return bool(
        bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM news_ingest_state "
                "WHERE updated_at_ms <> 0 OR connected OR last_frame_at_ms IS NOT NULL "
                "OR last_publish_at_ms IS NOT NULL OR last_error_code IS NOT NULL)"
            )
        ).scalar_one()
    )


def _preflight(bind: Any, counts: dict[str, int], preserved_counts: dict[str, int]) -> dict[str, Any]:
    if not _requires_preflight(bind, counts, preserved_counts):
        return {
            "mode": "empty_install",
            "tested_git_sha": "empty_install",
            "deployed_git_sha": "empty_install",
            "image_digest": "empty_install",
            "runtime_revision": "empty_install",
            "runtime_manifest_sha": "empty_install",
            "snapshot_sha256": hashlib.sha256(b"").hexdigest(),
            "snapshot_verified": True,
            "queue_ready": 0,
            "queue_unacked": 0,
            "queue_dead_letter": 0,
            "queue_stale_reference_count": 0,
        }

    raw = os.environ.get(_PREFLIGHT_ENV, "").strip()
    if not raw:
        raise RuntimeError(f"20260830_0336 requires {_PREFLIGHT_ENV} for a non-empty News evidence plane")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{_PREFLIGHT_ENV} must be valid JSON") from exc
    expected = {
        "mode",
        "tested_git_sha",
        "deployed_git_sha",
        "image_digest",
        "runtime_revision",
        "runtime_manifest_sha",
        "snapshot_sha256",
        "snapshot_verified",
        "queue_ready",
        "queue_unacked",
        "queue_dead_letter",
        "queue_stale_reference_count",
    }
    if not isinstance(value, dict) or set(value) != expected or value.get("mode") != "maintenance_window":
        raise RuntimeError(f"{_PREFLIGHT_ENV} has an invalid field set or mode")
    if not _GIT_SHA.fullmatch(str(value["tested_git_sha"])) or value["tested_git_sha"] != value["deployed_git_sha"]:
        raise RuntimeError("news genesis tested/deployed git identity is invalid")
    if not _IMAGE_DIGEST.fullmatch(str(value["image_digest"])):
        raise RuntimeError("news genesis image digest is invalid")
    if value["runtime_revision"] != value["deployed_git_sha"]:
        raise RuntimeError("news genesis runtime revision does not match the deployed git SHA")
    if not _SHA256.fullmatch(str(value["runtime_manifest_sha"])):
        raise RuntimeError("news genesis runtime manifest SHA is invalid")
    if not _SHA256.fullmatch(str(value["snapshot_sha256"])) or value["snapshot_verified"] is not True:
        raise RuntimeError("news genesis snapshot is not verified")
    for field in ("queue_ready", "queue_unacked", "queue_dead_letter", "queue_stale_reference_count"):
        if type(value[field]) is not int or value[field] != 0:
            raise RuntimeError(f"news genesis requires {field}=0")
    if os.environ.get("TRACEFOLD_RUNTIME_REVISION", "").strip() != value["runtime_revision"]:
        raise RuntimeError("news genesis preflight does not match the migration runtime revision")
    if os.environ.get("TRACEFOLD_IMAGE_DIGEST", "").strip() != value["image_digest"]:
        raise RuntimeError("news genesis preflight does not match the migration image digest")
    return value


def _replace_current_views_and_guards() -> None:
    op.execute("DROP VIEW news_review_task_source_v1")
    op.execute("DROP VIEW news_review_records_v1")
    op.execute("DROP VIEW news_current_events_v1")
    op.execute("DROP INDEX ix_news_events_current_opened")

    op.execute("DROP TRIGGER news_events_current_archive_only_check ON news_events")
    op.execute("DROP TRIGGER news_reviews_current_archive_only_check ON news_reviews")
    op.execute("DROP TRIGGER news_event_evidence_current_archive_only_check ON news_event_evidence_snapshots")
    op.execute("DROP FUNCTION news_current_event_archive_guard()")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION news_current_verdict_evidence_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.judgment_contract_version = 'news_judgment_v2'
             AND NOT EXISTS (
               SELECT 1 FROM news_event_evidence_snapshots evidence
                WHERE evidence.event_id = NEW.event_id
                  AND evidence.evidence_version = NEW.evidence_version
                  AND evidence.evidence_sha256 = NEW.evidence_sha256
                  AND evidence.focus_fact_id = NEW.focus_fact_id
             ) THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              CONSTRAINT = 'news_verdicts_current_evidence_check',
              MESSAGE = 'news_current_verdict_evidence_not_exact';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )

    # TRUNCATE is intentionally inside the same transaction and before the
    # archive exemptions disappear: old rows are allowed to exist only until
    # this statement.  The complete FK closure is named, so a new external
    # dependency fails closed instead of widening through CASCADE.
    op.execute("TRUNCATE TABLE " + ", ".join(_CLEARED_TABLES) + " RESTART IDENTITY")

    op.execute("ALTER TABLE news_events DROP CONSTRAINT news_events_source_contract_reason_check")
    op.execute("ALTER TABLE news_events DROP CONSTRAINT news_events_source_contract_consistency_check")
    op.execute("ALTER TABLE news_events DROP COLUMN current_contract_archive_only")
    op.execute("ALTER TABLE news_reviews DROP COLUMN current_contract_archive_only")
    op.execute(
        """
        ALTER TABLE news_events
        ADD CONSTRAINT news_events_source_contract_reason_check CHECK ((
          source_contract_reason IS NULL
          OR source_contract_reason IN ('source_contract_drift', 'unsupported_market_contract')
        ) IS TRUE)
        """
    )
    op.execute(
        """
        ALTER TABLE news_events
        ADD CONSTRAINT news_events_source_contract_consistency_check CHECK ((
          (event_kind IN ('news', 'listing') AND source_contract_reason IS NULL)
          OR (
            event_kind IN ('oi', 'liquidation')
            AND (source_contract_reason IS NULL OR source_contract_reason = 'source_contract_drift')
          )
          OR (
            event_kind = 'unsupported_market'
            AND source_contract_reason IN ('source_contract_drift', 'unsupported_market_contract')
          )
        ) IS TRUE)
        """
    )
    op.execute(
        """
        CREATE VIEW news_review_task_source_v1 WITH (security_barrier = true) AS
        SELECT e.event_id,
               s.evidence_version,
               s.evidence_sha256,
               s.release_eligible AS evidence_release_eligible,
               s.snapshot AS evidence_snapshot,
               e.opened_at_ms,
               e.admission,
               e.queue_priority,
               e.storyline_key,
               e.ingest_mode,
               v.created_at_ms AS verdict_created_at_ms,
               v.evidence_version AS verdict_evidence_version,
               v.final_decision,
               v.degraded,
               v.error_code AS verdict_error_code,
               v.override_rule,
               v.throttled_by,
               v.verdict,
               v.trace,
               v.policy_version,
               v.model,
               d.state AS delivery_state,
               d.card AS delivery_card,
               d.settled_at_ms,
               d.error_code AS delivery_error_code,
               reaction.max_abs_return_1h_bps,
               v.program_version,
               v.program_sha256,
               v.judgment_contract_version,
               v.judgment_origin,
               v.editorial AS model_editorial,
               v.scored_judgment_sha256 AS judgment_sha256,
               v.runtime_manifest_sha,
               e.event_kind
          FROM news_events e
          JOIN LATERAL (
            SELECT x.* FROM news_verdicts x
             WHERE x.event_id = e.event_id AND x.stage = 'triage'
               AND x.judgment_contract_version = 'news_judgment_v2'
               AND x.judgment_origin = 'model'
             ORDER BY x.created_at_ms DESC LIMIT 1
          ) v ON true
          JOIN LATERAL (
            SELECT x.* FROM news_event_evidence_snapshots x
             WHERE x.event_id = e.event_id
             ORDER BY x.evidence_version DESC LIMIT 1
          ) s ON s.provenance = 'observed'
             AND s.release_eligible
             AND s.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
             AND s.evidence_version = v.evidence_version
             AND s.evidence_sha256 = v.evidence_sha256
             AND s.focus_fact_id = v.focus_fact_id
          LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
          LEFT JOIN LATERAL (
            SELECT max(abs(x.return_1h_bps)) AS max_abs_return_1h_bps
              FROM news_event_reactions x
             WHERE x.event_id = e.event_id
               AND x.metric_version = 'reaction_v1'
               AND x.is_primary
          ) reaction ON true
         WHERE e.event_kind = 'news'
        """
    )
    op.execute("GRANT SELECT ON news_review_task_source_v1 TO tracefold_serve, tracefold_workers")
    op.execute(
        """
        CREATE VIEW news_review_records_v1 WITH (security_barrier = true) AS
        SELECT review_id, idempotency_key, idempotency_request_sha, review_kind,
               subject_kind, task_id, task_version, event_id, evidence_version,
               external_snapshot_id, pairwise_case_id, rubric_version,
               reader_contract_version, reviewer, should_push, dimensions,
               novelty, first_bad_owner, evidence_refs, expected_correction,
               note, selection, payload, supersedes_review_id,
               accepts_review_id, release_eligible, created_at_ms
          FROM news_reviews
         WHERE news_current_review_valid(
                 review_kind, subject_kind, rubric_version, reader_contract_version,
                 event_id, evidence_version, external_snapshot_id, pairwise_case_id,
                 should_push, dimensions, novelty, first_bad_owner, evidence_refs,
                 expected_correction, note, selection, payload, accepts_review_id
               ) IS TRUE
        """
    )
    op.execute("GRANT SELECT ON news_review_records_v1 TO tracefold_serve, tracefold_workers")


def _validate_news_constraints() -> None:
    op.execute(
        """
        DO $$
        DECLARE item record;
        BEGIN
          FOR item IN
            SELECT con.conrelid::regclass AS owner, con.conname
              FROM pg_constraint con
              JOIN pg_class rel ON rel.oid = con.conrelid
              JOIN pg_namespace ns ON ns.oid = rel.relnamespace
             WHERE ns.nspname = 'public' AND left(rel.relname, 5) = 'news_'
               AND NOT con.convalidated
             ORDER BY rel.relname, con.conname
          LOOP
            EXECUTE format('ALTER TABLE %s VALIDATE CONSTRAINT %I', item.owner, item.conname);
          END LOOP;
        END
        $$
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    bind = op.get_bind()
    _assert_news_table_inventory(bind)
    pre_counts = _counts(bind, _CLEARED_TABLES)
    preserved_before = _counts(bind, _PRESERVED_TABLES)
    schema_digest_before = _schema_digest(bind)
    preflight = _preflight(bind, pre_counts, preserved_before)
    genesis_at_ms = int(
        bind.execute(sa.text("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint")).scalar_one()
    )
    prior_manifest = (
        bind.execute(
            sa.text(
                "SELECT manifest_sha, stable_bundle_sha, image_digest, runtime_revision "
                "FROM news_agent_runtime_manifests "
                "ORDER BY registered_at_ms DESC, manifest_sha DESC LIMIT 1"
            )
        )
        .mappings()
        .one_or_none()
    )
    active_stale_before = int(
        bind.execute(
            sa.text("SELECT count(*) FROM news_canary_activations WHERE state IN ('armed', 'active')")
        ).scalar_one()
    )

    _replace_current_views_and_guards()
    bind.execute(
        sa.text("INSERT INTO news_ingest_state (singleton_key, updated_at_ms) VALUES ('opennews', :now_ms)"),
        {"now_ms": genesis_at_ms},
    )
    bind.execute(
        sa.text("INSERT INTO news_learning_retention_state (singleton, updated_at_ms) VALUES (true, :now_ms)"),
        {"now_ms": genesis_at_ms},
    )
    _validate_news_constraints()
    _assert_current_only_schema(bind)

    preserved_after = _counts(bind, _PRESERVED_TABLES)
    if preserved_after != preserved_before:
        raise RuntimeError("20260830_0336 changed a preserved News market/instrument owner")
    post_counts = {table: 0 for table in _CLEARED_TABLES}
    post_counts["news_learning_artifacts"] = 1
    post_counts["news_ingest_state"] = 1
    post_counts["news_learning_retention_state"] = 1
    schema_digest_after = _schema_digest(bind)
    identity_suffix = str(preflight["deployed_git_sha"])[:8]
    genesis_epoch_id = f"news_genesis_{genesis_at_ms}_{identity_suffix}"
    preflight_sha256 = hashlib.sha256(
        json.dumps(preflight, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {
        "kind": "news_current_contract_genesis",
        "source_issue": _ISSUE,
        "migration_identity": revision,
        "genesis_epoch_id": genesis_epoch_id,
        "genesis_at_ms": genesis_at_ms,
        "tested_git_sha": preflight["tested_git_sha"],
        "deployed_git_sha": preflight["deployed_git_sha"],
        "image_digest": preflight["image_digest"],
        "runtime_revision": preflight["runtime_revision"],
        "runtime_manifest_sha": preflight["runtime_manifest_sha"],
        "prior_runtime_manifest": dict(prior_manifest) if prior_manifest else None,
        "schema_digest_before": schema_digest_before,
        "schema_digest_after": schema_digest_after,
        "pre_counts": pre_counts,
        "post_counts": post_counts,
        "preserved_counts": preserved_after,
        "disposition": {
            "cleared_tables": list(_CLEARED_TABLES),
            "preserved_tables": list(_PRESERVED_TABLES),
            "retired_compatibility_objects": [
                "news_events.current_contract_archive_only",
                "news_reviews.current_contract_archive_only",
                "news_current_events_v1",
                "ix_news_events_current_opened",
                "news_current_event_archive_guard",
                "news_events_current_archive_only_check",
                "news_reviews_current_archive_only_check",
                "news_event_evidence_current_archive_only_check",
            ],
        },
        "archive_only_row_count": 0,
        "active_stale_candidate_canary_count_before": active_stale_before,
        "active_stale_candidate_canary_count_after": 0,
        "queue_ready": preflight["queue_ready"],
        "queue_unacked": preflight["queue_unacked"],
        "queue_dead_letter": preflight["queue_dead_letter"],
        "queue_stale_reference_count": preflight["queue_stale_reference_count"],
        "snapshot_sha256": preflight["snapshot_sha256"],
        "snapshot_verified": preflight["snapshot_verified"],
        "preflight_mode": preflight["mode"],
        "preflight_sha256": preflight_sha256,
        "rollback": "verified_snapshot_restore_only",
    }
    receipt = bind.execute(
        sa.text(
            """
            WITH receipt AS (
              SELECT CAST(:payload AS jsonb) AS payload
            ), addressed AS (
              SELECT payload,
                     encode(sha256(convert_to(news_canonical_jsonb(jsonb_build_object(
                       'kind', 'epoch_reset', 'payload', payload
                     )), 'UTF8')), 'hex') AS artifact_sha
                FROM receipt
            )
            INSERT INTO news_learning_artifacts (
              artifact_sha, kind, parent_sha, payload, created_by, created_at_ms
            )
            SELECT artifact_sha, 'epoch_reset', NULL, payload,
                   'migration_20260830_0336', :created_at_ms
              FROM addressed
            RETURNING artifact_sha
            """
        ),
        {
            "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "created_at_ms": genesis_at_ms,
        },
    ).scalar_one()
    if not _SHA256.fullmatch(str(receipt)):
        raise RuntimeError("20260830_0336 failed to write the content-addressed genesis receipt")
    if _counts(bind, _CLEARED_TABLES) != post_counts:
        raise RuntimeError("20260830_0336 post-genesis counts do not match the receipt")


def downgrade() -> None:
    raise RuntimeError("20260830_0336 is an irreversible News current-contract genesis")
