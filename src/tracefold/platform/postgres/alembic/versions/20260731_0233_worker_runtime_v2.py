"""Worker Runtime V2 hard cut.

Revision ID: 20260731_0233
Revises: 20260731_0232
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from zoneinfo import ZoneInfo

from alembic import op
from psycopg.types.json import Jsonb

from tracefold.macro.market_calendar import is_us_market_session

revision = "20260731_0233"
down_revision = "20260731_0232"
branch_labels = None
depends_on = None

_CANONICAL_OWNERS = frozenset(
    {
        "event_anchor_backfill",
        "resolution_refresh",
        "asset_profile_refresh",
        "token_image_mirror",
        "radar_projection",
        "profile_projection",
        "macro_projection",
        "news_projection",
        "news_brief",
        "macro_thesis",
        "macro_document_analysis",
    }
)
_PROJECTION_OWNER_BY_SOURCE = {
    "radar_projection_frontiers": "radar_projection",
    "token_profile_projection_frontiers": "profile_projection",
    "macro_module_frontiers": "macro_projection",
    "news_projection_frontiers": "news_projection",
}
_MODEL_OWNER_BY_KIND = {
    "news_brief": "news_brief",
    "macro_thesis": "macro_thesis",
    "macro_document_analysis": "macro_document_analysis",
}
_LEGACY_TERMINAL_OWNER_MAPPINGS = {
    "news_page_projection": "news_projection",
    "news_source_quality_projection": "news_projection",
    "token_radar_projection": "radar_projection",
}
_NEW_YORK = ZoneInfo("America/New_York")
_THESIS_PUBLICATION_TIME = clock_time(8, 50)


def upgrade() -> None:
    conn = op.get_bind()
    now_ms = int(time.time() * 1_000)
    _validate_terminal_owners(conn)
    _add_resolution_reprocess_continuation()
    _add_news_source_claims()
    _add_native_news_state()
    model_snapshot = _model_migration_snapshot(conn, now_ms=now_ms)
    _migrate_terminal_evidence(conn, now_ms=now_ms)
    op.execute("DROP TRIGGER macro_thesis_runs_lifecycle ON macro_thesis_runs")
    _migrate_model_frontiers(conn, now_ms=now_ms)
    pending_snapshot = _pending_recompute_snapshot(conn, now_ms=now_ms)
    _recompute_native_eligibility(conn, now_ms=now_ms)
    _verify_model_migration(
        conn,
        snapshot=model_snapshot,
        pending_snapshot=pending_snapshot,
    )
    _create_thesis_lifecycle_trigger()
    _rename_terminal_surface()
    _create_workers_runtime()
    op.execute("DROP TABLE model_generation_frontiers")
    op.execute("DROP TABLE worker_runtime_status")
    _apply_runtime_grants()


def downgrade() -> None:
    raise RuntimeError("20260731_0233 is an irreversible Worker Runtime V2 hard cut")


def _create_thesis_lifecycle_trigger() -> None:
    op.execute(
        """
        CREATE FUNCTION enforce_macro_thesis_run_lifecycle_v2()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
          operator_retry boolean;
          prework_release boolean;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'macro_thesis_run_delete_forbidden';
          END IF;
          IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'pending' OR NEW.attempt_count <> 0 THEN
              RAISE EXCEPTION 'macro_thesis_run_initial_state_invalid';
            END IF;
            RETURN NEW;
          END IF;
          operator_retry := (
            OLD.status IN ('failed', 'config_error', 'not_published')
            AND NEW.status = 'retryable'
            AND NEW.attempt_count = 0
            AND NEW.lease_owner IS NULL
            AND NEW.leased_until_ms IS NULL
          );
          prework_release := (
            OLD.status = 'running'
            AND NEW.status IN ('pending', 'retryable')
            AND NEW.attempt_count = OLD.attempt_count - 1
            AND NEW.attempt_count >= 0
            AND NEW.lease_owner IS NULL
            AND NEW.leased_until_ms IS NULL
            AND NEW.due_at_ms = OLD.due_at_ms
            AND NEW.last_error_code IS NOT DISTINCT FROM OLD.last_error_code
            AND NEW.last_error_message IS NOT DISTINCT FROM OLD.last_error_message
            AND NEW.publication_id IS NOT DISTINCT FROM OLD.publication_id
          );
          IF (
            NEW.session_date,
            NEW.cutoff_ms,
            NEW.evidence_pack_id,
            NEW.evidence_pack_hash,
            NEW.max_attempts,
            NEW.created_at_ms
          ) IS DISTINCT FROM (
            OLD.session_date,
            OLD.cutoff_ms,
            OLD.evidence_pack_id,
            OLD.evidence_pack_hash,
            OLD.max_attempts,
            OLD.created_at_ms
          ) THEN
            RAISE EXCEPTION 'macro_thesis_run_frozen_fields_immutable';
          END IF;
          IF OLD.research_input_id IS NOT NULL AND (
            NEW.research_input_id,
            NEW.research_input_hash
          ) IS DISTINCT FROM (
            OLD.research_input_id,
            OLD.research_input_hash
          ) THEN
            RAISE EXCEPTION 'macro_thesis_run_research_input_immutable';
          END IF;
          IF OLD.status IN (
            'failed', 'config_error', 'not_published', 'published'
          ) AND NOT operator_retry THEN
            RAISE EXCEPTION 'macro_thesis_run_terminal';
          END IF;
          IF NOT (
            operator_retry
            OR prework_release
            OR (
              OLD.status = 'pending'
              AND OLD.attempt_count = 0
              AND NEW.status = 'pending'
              AND NEW.attempt_count = 0
              AND OLD.research_input_id IS NULL
              AND NEW.research_input_id IS NOT NULL
            )
            OR (
              OLD.status = 'pending'
              AND OLD.attempt_count = 0
              AND NEW.status IN ('config_error', 'failed')
              AND NEW.attempt_count = 0
            )
            OR (
              OLD.status IN ('pending', 'retryable')
              AND NEW.status = 'running'
              AND NEW.research_input_id IS NOT NULL
            )
            OR (
              OLD.status = 'running'
              AND NEW.status IN (
                'running', 'retryable', 'failed', 'config_error',
                'not_published', 'published'
              )
            )
          ) THEN
            RAISE EXCEPTION 'macro_thesis_run_transition_invalid:%->%',
              OLD.status, NEW.status;
          END IF;
          IF NEW.attempt_count < OLD.attempt_count AND NOT (operator_retry OR prework_release) THEN
            RAISE EXCEPTION 'macro_thesis_run_attempt_count_decrease';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE TRIGGER macro_thesis_runs_lifecycle
        BEFORE INSERT OR UPDATE OR DELETE ON macro_thesis_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_macro_thesis_run_lifecycle_v2();
        """
    )


def _validate_terminal_owners(conn: Any) -> None:
    owners = {
        str(row[0])
        for row in conn.exec_driver_sql("SELECT DISTINCT worker_name FROM worker_queue_terminal_events").fetchall()
    }
    unknown = sorted(
        owners
        - _CANONICAL_OWNERS
        - {"steady_projection_coordinator", "model_projection"}
        - set(_LEGACY_TERMINAL_OWNER_MAPPINGS)
    )
    if unknown:
        raise RuntimeError("worker_runtime_v2_unknown_terminal_owners:" + ",".join(unknown))
    bad_projection_sources = conn.exec_driver_sql(
        """
        SELECT DISTINCT source_table
          FROM worker_queue_terminal_events
         WHERE worker_name = 'steady_projection_coordinator'
           AND source_table <> ALL(%s)
        """,
        (list(_PROJECTION_OWNER_BY_SOURCE),),
    ).fetchall()
    if bad_projection_sources:
        raise RuntimeError(
            "worker_runtime_v2_unknown_projection_terminal_sources:"
            + ",".join(sorted(str(row[0]) for row in bad_projection_sources))
        )
    model_rows = conn.exec_driver_sql(
        """
        SELECT terminal_id, source_row_json->>'candidate_kind' AS candidate_kind
          FROM worker_queue_terminal_events
         WHERE worker_name = 'model_projection'
        """
    ).fetchall()
    invalid = [str(row[0]) for row in model_rows if str(row[1]) not in _MODEL_OWNER_BY_KIND]
    if invalid:
        raise RuntimeError("worker_runtime_v2_unknown_model_terminal_candidates:" + ",".join(sorted(invalid)))


def _add_native_news_state() -> None:
    op.execute(
        """
        ALTER TABLE news_brief_current
          ADD COLUMN pending_first_dirty_at_ms bigint,
          ADD COLUMN pending_due_at_ms bigint,
          ADD CONSTRAINT news_brief_current_pending_clock_check CHECK (
            (pending_first_dirty_at_ms IS NULL AND pending_due_at_ms IS NULL)
            OR (
              pending_first_dirty_at_ms IS NOT NULL
              AND pending_first_dirty_at_ms >= 0
              AND pending_due_at_ms IS NOT NULL
              AND pending_due_at_ms >= pending_first_dirty_at_ms
            )
          );

        ALTER TABLE news_brief_runs
          DROP CONSTRAINT news_brief_runs_status_check,
          ADD COLUMN next_due_at_ms bigint,
          ADD CONSTRAINT news_brief_runs_status_check CHECK (
            status IN ('running', 'retryable', 'ready', 'insufficient_material', 'failed')
          ),
          ADD CONSTRAINT news_brief_runs_retry_clock_check CHECK (
            (status = 'retryable' AND next_due_at_ms IS NOT NULL AND next_due_at_ms >= 0)
            OR (status <> 'retryable' AND next_due_at_ms IS NULL)
          );

        DROP INDEX ix_news_brief_runs_status;
        CREATE INDEX ix_news_brief_runs_due
          ON news_brief_runs(status, next_due_at_ms, lease_expires_at_ms, updated_at_ms);
        """
    )


def _add_resolution_reprocess_continuation() -> None:
    op.execute(
        """
        ALTER TABLE token_discovery_dirty_lookup_keys
          ADD COLUMN reprocess_lookup_keys text[],
          ADD COLUMN reprocess_after_intent_id text,
          ADD COLUMN reprocess_resolved boolean NOT NULL DEFAULT false,
          ADD COLUMN reprocess_queue_due_at_ms bigint,
          ADD CONSTRAINT token_discovery_reprocess_continuation_check CHECK (
            (
              reprocess_lookup_keys IS NULL
              AND reprocess_after_intent_id IS NULL
              AND reprocess_resolved = false
              AND reprocess_queue_due_at_ms IS NULL
            )
            OR (
              cardinality(reprocess_lookup_keys) > 0
              AND reprocess_after_intent_id IS NOT NULL
              AND length(reprocess_after_intent_id) > 0
              AND reprocess_queue_due_at_ms IS NOT NULL
              AND reprocess_queue_due_at_ms >= 0
            )
          );
        """
    )


def _add_news_source_claims() -> None:
    op.execute(
        """
        ALTER TABLE news_sources
          ADD COLUMN claim_token uuid,
          ADD COLUMN claim_lease_expires_at_ms bigint,
          ADD CONSTRAINT news_sources_claim_check CHECK (
            (claim_token IS NULL AND claim_lease_expires_at_ms IS NULL)
            OR (
              claim_token IS NOT NULL
              AND claim_lease_expires_at_ms IS NOT NULL
              AND claim_lease_expires_at_ms >= 0
            )
          );
        CREATE INDEX ix_news_sources_due_claim
          ON news_sources(next_fetch_at_ms, source_id, claim_lease_expires_at_ms)
          WHERE enabled;
        """
    )


def _migrate_model_frontiers(conn: Any, *, now_ms: int) -> None:
    rows = (
        conn.exec_driver_sql(
            """
        SELECT candidate_kind, shard_key, status, first_dirty_at_ms,
               deadline_at_ms, next_attempt_at_ms, attempt_count,
               transient_failure_count, input_fingerprint, workflow_version,
               claimed_by::text AS claimed_by, claimed_until_ms,
               last_error_code, updated_at_ms
          FROM model_generation_frontiers
         ORDER BY candidate_kind, shard_key
        """
        )
        .mappings()
        .all()
    )
    for raw in rows:
        row = dict(raw)
        kind = str(row["candidate_kind"])
        if kind == "news_brief":
            _migrate_news_frontier(conn, row=row, now_ms=now_ms)
        elif kind == "macro_document_analysis":
            _migrate_document_frontier(conn, row=row, now_ms=now_ms)
        elif kind == "macro_thesis":
            _migrate_thesis_frontier(conn, row=row, now_ms=now_ms)
        else:
            raise RuntimeError(f"worker_runtime_v2_unknown_model_frontier:{kind}")


def _model_migration_snapshot(conn: Any, *, now_ms: int) -> dict[str, Any]:
    rows = (
        conn.exec_driver_sql(
            """
            SELECT candidate_kind, shard_key, status, input_fingerprint
              FROM model_generation_frontiers
             ORDER BY candidate_kind, shard_key
            """
        )
        .mappings()
        .all()
    )
    pre_native = _native_model_candidate_keys(conn)
    outer_candidates: set[str] = set()
    for raw in rows:
        row = dict(raw)
        if str(row["status"]) == "clean":
            continue
        kind = str(row["candidate_kind"])
        if kind == "news_brief":
            outer_candidates.add(f"news_brief:{row['input_fingerprint']}")
        elif kind == "macro_thesis":
            outer_candidates.add(f"macro_thesis:{row['shard_key']}")
    document_targets = {
        str(row[0])
        for row in conn.exec_driver_sql(
            """
            SELECT analysis_job_id
              FROM macro_document_analysis_jobs
             WHERE status <> 'completed'
             ORDER BY analysis_job_id
            """
        ).fetchall()
    }
    if any(str(row["candidate_kind"]) == "macro_document_analysis" and str(row["status"]) != "clean" for row in rows):
        outer_candidates.update(f"macro_document_analysis:{target}" for target in document_targets)
    recomputed_candidates = _recomputed_model_candidate_keys(conn, now_ms=now_ms)
    expected = pre_native | outer_candidates | recomputed_candidates
    return {
        "pre_native_candidate_keys": sorted(pre_native),
        "pre_native_candidate_hash": _sha256_json(sorted(pre_native)),
        "outer_candidate_keys": sorted(outer_candidates),
        "outer_candidate_hash": _sha256_json(sorted(outer_candidates)),
        "recomputed_candidate_keys": sorted(recomputed_candidates),
        "recomputed_candidate_hash": _sha256_json(sorted(recomputed_candidates)),
        "expected_candidate_keys": sorted(expected),
        "expected_candidate_hash": _sha256_json(sorted(expected)),
        "document_targets": sorted(document_targets),
        "document_target_hash": _sha256_json(sorted(document_targets)),
    }


def _verify_model_migration(
    conn: Any,
    *,
    snapshot: Mapping[str, Any],
    pending_snapshot: Mapping[str, Any],
) -> None:
    actual = _native_model_candidate_keys(conn)
    expected = {str(value) for value in snapshot["expected_candidate_keys"]}
    observed = sorted(actual)
    observed_hash = _sha256_json(observed)
    if observed_hash != str(snapshot["expected_candidate_hash"]):
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise RuntimeError(
            "worker_runtime_v2_candidate_hash_mismatch:"
            f"expected={snapshot['expected_candidate_hash']}:actual={observed_hash}:"
            f"missing={','.join(missing)}:unexpected={','.join(unexpected)}"
        )
    document_targets = sorted(
        str(row[0])
        for row in conn.exec_driver_sql(
            """
            SELECT analysis_job_id
              FROM macro_document_analysis_jobs
             WHERE status <> 'completed'
             ORDER BY analysis_job_id
            """
        ).fetchall()
    )
    document_hash = _sha256_json(document_targets)
    if document_hash != str(snapshot["document_target_hash"]):
        raise RuntimeError(
            "worker_runtime_v2_unfinished_target_hash_mismatch:"
            f"expected={snapshot['document_target_hash']}:actual={document_hash}"
        )
    pending = _pending_model_candidate_keys(conn)
    expected_pending = {str(value) for value in pending_snapshot["expected_candidate_keys"]}
    pending_hash = _sha256_json(sorted(pending))
    if pending_hash != str(pending_snapshot["expected_candidate_hash"]):
        missing = sorted(expected_pending - pending)
        unexpected = sorted(pending - expected_pending)
        raise RuntimeError(
            "worker_runtime_v2_pending_candidate_hash_mismatch:"
            f"expected={pending_snapshot['expected_candidate_hash']}:actual={pending_hash}:"
            f"missing={','.join(missing)}:unexpected={','.join(unexpected)}"
        )


def _native_model_candidate_keys(conn: Any) -> set[str]:
    candidates: set[str] = {
        f"news_brief:{row[0]}"
        for row in conn.exec_driver_sql("SELECT fingerprint FROM news_brief_runs ORDER BY fingerprint").fetchall()
    }
    candidates.update(
        f"news_brief:{row[0]}"
        for row in conn.exec_driver_sql(
            """
            SELECT target_fingerprint
              FROM news_brief_current
             WHERE singleton_key
               AND pending_first_dirty_at_ms IS NOT NULL
               AND pending_due_at_ms IS NOT NULL
            """
        ).fetchall()
    )
    candidates.update(
        f"macro_thesis:{row[0]}"
        for row in conn.exec_driver_sql(
            "SELECT session_date::text FROM macro_thesis_runs ORDER BY session_date"
        ).fetchall()
    )
    candidates.update(
        f"macro_document_analysis:{row[0]}"
        for row in conn.exec_driver_sql(
            """
            SELECT analysis_job_id
              FROM macro_document_analysis_jobs
             WHERE status <> 'completed'
             ORDER BY analysis_job_id
            """
        ).fetchall()
    )
    return candidates


def _pending_model_candidate_keys(conn: Any) -> set[str]:
    candidates = {
        f"news_brief:{row[0]}"
        for row in conn.exec_driver_sql(
            """
            SELECT current.target_fingerprint
              FROM news_brief_current AS current
              LEFT JOIN news_brief_runs AS run
                ON run.fingerprint = current.target_fingerprint
             WHERE current.singleton_key
               AND current.target_fingerprint IS NOT NULL
               AND (
                 run.status IN ('running', 'retryable')
                 OR (
                   current.pending_first_dirty_at_ms IS NOT NULL
                   AND current.pending_due_at_ms IS NOT NULL
                   AND (run.run_id IS NULL OR run.status NOT IN ('ready', 'insufficient_material', 'failed'))
                 )
               )
            """
        ).fetchall()
    }
    candidates.update(
        f"macro_document_analysis:{row[0]}"
        for row in conn.exec_driver_sql(
            """
            SELECT analysis_job_id
              FROM macro_document_analysis_jobs
             WHERE status IN ('pending', 'claimed', 'retryable')
             ORDER BY analysis_job_id
            """
        ).fetchall()
    )
    candidates.update(
        f"macro_thesis:{row[0]}"
        for row in conn.exec_driver_sql(
            """
            SELECT session_date::text
              FROM macro_thesis_runs
             WHERE status IN ('pending', 'running', 'retryable')
             ORDER BY session_date
            """
        ).fetchall()
    )
    return candidates


def _pending_recompute_snapshot(conn: Any, *, now_ms: int) -> dict[str, Any]:
    before = _pending_model_candidate_keys(conn)
    expected = set(before)
    fingerprint = _brief_selection_fingerprint(conn)
    if fingerprint is not None:
        expected = {key for key in expected if not key.startswith("news_brief:")}
        if not bool(_brief_native_owner(conn, fingerprint=fingerprint)["owned"]):
            expected.add(f"news_brief:{fingerprint}")

    session_date = _resolve_thesis_session(now_ms=int(now_ms))
    cutoff_ms = _thesis_cutoff_ms(session_date)
    if int(now_ms) >= cutoff_ms:
        pack = conn.exec_driver_sql(
            "SELECT 1 FROM macro_evidence_packs WHERE session_date = %s::date LIMIT 1",
            (session_date.isoformat(),),
        ).first()
        if pack is not None:
            key = f"macro_thesis:{session_date.isoformat()}"
            state = conn.exec_driver_sql(
                "SELECT status FROM macro_thesis_runs WHERE session_date = %s::date",
                (session_date.isoformat(),),
            ).first()
            if state is None or str(state[0]) in {"pending", "running", "retryable"}:
                expected.add(key)
            else:
                expected.discard(key)
    return {
        "pre_recompute_candidate_keys": sorted(before),
        "pre_recompute_candidate_hash": _sha256_json(sorted(before)),
        "expected_candidate_keys": sorted(expected),
        "expected_candidate_hash": _sha256_json(sorted(expected)),
    }


def _brief_selection_fingerprint(conn: Any) -> str | None:
    stories = (
        conn.exec_driver_sql(
            """
            SELECT stories.story_id, stories.state_fingerprint
              FROM news_brief_selection_current AS selection
              JOIN news_stories AS stories USING(story_id)
             ORDER BY selection.rank
            """
        )
        .mappings()
        .all()
    )
    if not stories:
        return None
    payload = {
        "contract": {
            "prompt": "worldmonitor_top8_zh_v1",
            "workflow": "worldmonitor_world_brief_v1",
            "schema": "worldmonitor_world_brief_schema_v1",
            "locale": "zh-CN",
        },
        "stories": [
            {
                "story_id": str(story["story_id"]),
                "state_fingerprint": str(story["state_fingerprint"]),
                "rank": index + 1,
            }
            for index, story in enumerate(stories)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _recomputed_model_candidate_keys(conn: Any, *, now_ms: int) -> set[str]:
    candidates: set[str] = set()
    fingerprint = _brief_selection_fingerprint(conn)
    if fingerprint is not None:
        owner = _brief_native_owner(conn, fingerprint=fingerprint)
        if not bool(owner["owned"]):
            candidates.add(f"news_brief:{fingerprint}")

    session_date = _resolve_thesis_session(now_ms=int(now_ms))
    cutoff_ms = _thesis_cutoff_ms(session_date)
    if int(now_ms) >= cutoff_ms:
        pack = conn.exec_driver_sql(
            "SELECT 1 FROM macro_evidence_packs WHERE session_date = %s::date LIMIT 1",
            (session_date.isoformat(),),
        ).first()
        if pack is not None:
            candidates.add(f"macro_thesis:{session_date.isoformat()}")
    return candidates


def _brief_native_owner(conn: Any, *, fingerprint: str) -> dict[str, Any]:
    publication = (
        conn.exec_driver_sql(
            """
            SELECT publication_id
              FROM news_brief_publications
             WHERE fingerprint = %s
             LIMIT 1
            """,
            (fingerprint,),
        )
        .mappings()
        .first()
    )
    run = (
        conn.exec_driver_sql(
            """
            SELECT run_id, status
              FROM news_brief_runs
             WHERE fingerprint = %s
             LIMIT 1
            """,
            (fingerprint,),
        )
        .mappings()
        .first()
    )
    terminal_run = run is not None and str(run["status"]) in {
        "ready",
        "insufficient_material",
        "failed",
    }
    publication_id = str(publication["publication_id"]) if publication is not None else None
    run_id = str(run["run_id"]) if terminal_run else None
    return {
        "owned": publication_id is not None or terminal_run,
        "publication_id": publication_id,
        "run_id": run_id,
    }


def _recompute_native_eligibility(conn: Any, *, now_ms: int) -> None:
    _recompute_brief_eligibility(conn, now_ms=now_ms)
    _recompute_thesis_eligibility(conn, now_ms=now_ms)


def _recompute_brief_eligibility(conn: Any, *, now_ms: int) -> None:
    fingerprint = _brief_selection_fingerprint(conn)
    if fingerprint is None:
        return
    outer_fingerprints = {
        str(row[0])
        for row in conn.exec_driver_sql(
            """
            SELECT input_fingerprint
              FROM model_generation_frontiers
             WHERE candidate_kind = 'news_brief'
               AND status <> 'clean'
            """
        ).fetchall()
    }
    if outer_fingerprints and fingerprint not in outer_fingerprints:
        raise RuntimeError(
            "worker_runtime_v2_brief_recompute_mismatch:"
            f"computed={fingerprint}:outer={_sha256_json(sorted(outer_fingerprints))}"
        )
    owner = _brief_native_owner(conn, fingerprint=fingerprint)
    owned = bool(owner["owned"])
    conn.exec_driver_sql(
        """
        UPDATE news_brief_current
           SET target_fingerprint = %s,
               publication_id = COALESCE(%s, publication_id),
               latest_run_id = COALESCE(%s, latest_run_id),
               pending_first_dirty_at_ms = CASE
                 WHEN %s THEN NULL ELSE COALESCE(pending_first_dirty_at_ms, %s) END,
               pending_due_at_ms = CASE
                 WHEN %s THEN NULL ELSE COALESCE(pending_due_at_ms, %s) END,
               updated_at_ms = GREATEST(updated_at_ms, %s)
         WHERE singleton_key
        """,
        (
            fingerprint,
            owner["publication_id"],
            owner["run_id"],
            owned,
            now_ms,
            owned,
            now_ms + 600_000,
            now_ms,
        ),
    )


def _recompute_thesis_eligibility(conn: Any, *, now_ms: int) -> None:
    session_date = _resolve_thesis_session(now_ms=int(now_ms))
    cutoff_ms = _thesis_cutoff_ms(session_date)
    if int(now_ms) < cutoff_ms:
        return
    pack = conn.exec_driver_sql(
        "SELECT 1 FROM macro_evidence_packs WHERE session_date = %s::date LIMIT 1",
        (session_date.isoformat(),),
    ).first()
    if pack is None:
        return
    _ensure_thesis_run(
        conn,
        row={
            "shard_key": session_date.isoformat(),
            "deadline_at_ms": cutoff_ms,
            "first_dirty_at_ms": cutoff_ms,
            "updated_at_ms": now_ms,
            "attempt_count": 0,
            "transient_failure_count": 0,
        },
        now_ms=now_ms,
    )


def _migrate_news_frontier(conn: Any, *, row: Mapping[str, Any], now_ms: int) -> None:
    fingerprint = str(row["input_fingerprint"])
    status = str(row["status"])
    if status == "clean":
        return
    first_dirty = int(row["first_dirty_at_ms"])
    due_at = int(row["deadline_at_ms"])
    conn.exec_driver_sql(
        """
        UPDATE news_brief_current
           SET target_fingerprint = %s,
               pending_first_dirty_at_ms = COALESCE(pending_first_dirty_at_ms, %s),
               pending_due_at_ms = COALESCE(pending_due_at_ms, %s),
               updated_at_ms = GREATEST(updated_at_ms, %s)
         WHERE singleton_key
        """,
        (fingerprint, first_dirty, due_at, int(row["updated_at_ms"])),
    )
    run_id = _stable_id("brief_run", fingerprint)
    attempt_count = _outer_attempt_floor(row)
    last_error = _optional_text(row.get("last_error_code"))
    existing_row = (
        conn.exec_driver_sql(
            "SELECT * FROM news_brief_runs WHERE fingerprint = %s FOR UPDATE",
            (fingerprint,),
        )
        .mappings()
        .first()
    )
    existing = dict(existing_row) if existing_row is not None else None
    if existing is not None and str(existing["status"]) in {"ready", "insufficient_material"}:
        return
    if status == "running" and _matching_valid_lease(
        existing,
        outer_owner=row.get("claimed_by"),
        native_owner_key="lease_owner",
        native_until_key="lease_expires_at_ms",
        now_ms=now_ms,
    ):
        conn.exec_driver_sql(
            """
            UPDATE news_brief_runs
               SET attempt_count = GREATEST(attempt_count, %s),
                   last_error = COALESCE(%s, last_error),
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE fingerprint = %s
            """,
            (attempt_count, last_error, int(row["updated_at_ms"]), fingerprint),
        )
        return
    native_status = "failed" if status == "quarantined" else "retryable"
    next_due_at_ms = (
        None
        if native_status == "failed"
        else _later_due(
            now_ms,
            row.get("deadline_at_ms") if status == "dirty" else None,
            row.get("next_attempt_at_ms"),
            row.get("claimed_until_ms"),
            (existing or {}).get("next_due_at_ms"),
            (existing or {}).get("lease_expires_at_ms"),
        )
    )
    conn.exec_driver_sql(
        """
        INSERT INTO news_brief_runs(
          run_id, fingerprint, status, attempt_count,
          candidate_story_count, candidate_source_count,
          last_error, created_at_ms, updated_at_ms, completed_at_ms,
          next_due_at_ms
        )
        VALUES (%s, %s, %s, %s, 0, 0, %s, %s, %s, %s, %s)
        ON CONFLICT(fingerprint) DO UPDATE SET
          status = excluded.status,
          attempt_count = GREATEST(news_brief_runs.attempt_count, excluded.attempt_count),
          lease_owner = NULL,
          lease_expires_at_ms = NULL,
          heartbeat_at_ms = NULL,
          last_error = COALESCE(excluded.last_error, news_brief_runs.last_error),
          next_due_at_ms = excluded.next_due_at_ms,
          completed_at_ms = CASE
            WHEN excluded.status = 'failed' THEN excluded.completed_at_ms
            ELSE NULL
          END,
          updated_at_ms = GREATEST(news_brief_runs.updated_at_ms, excluded.updated_at_ms)
        """,
        (
            run_id,
            fingerprint,
            native_status,
            attempt_count,
            last_error,
            first_dirty,
            int(row["updated_at_ms"]),
            now_ms if native_status == "failed" else None,
            next_due_at_ms,
        ),
    )


def _migrate_document_frontier(conn: Any, *, row: Mapping[str, Any], now_ms: int) -> None:
    status = str(row["status"])
    if status == "clean":
        return
    attempt_floor = _outer_attempt_floor(row)
    last_error = _optional_text(row.get("last_error_code"))
    if status == "dirty":
        conn.exec_driver_sql(
            """
            UPDATE macro_document_analysis_jobs
               SET status = CASE WHEN status = 'failed' THEN 'retryable' ELSE status END,
                   next_due_at_ms = LEAST(next_due_at_ms, %s),
                   attempt_count = GREATEST(attempt_count, %s),
                   max_attempts = GREATEST(max_attempts, attempt_count, %s) +
                     CASE WHEN status = 'failed' THEN 1 ELSE 0 END,
                   last_error_code = COALESCE(%s, last_error_code),
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE status IN ('pending', 'retryable', 'failed')
            """,
            (
                int(row["deadline_at_ms"]),
                attempt_floor,
                attempt_floor,
                last_error,
                int(row["updated_at_ms"]),
            ),
        )
        return
    if status == "running":
        owner = _optional_text(row.get("claimed_by"))
        due = _later_due(now_ms, row.get("claimed_until_ms"), row.get("next_attempt_at_ms"))
        conn.exec_driver_sql(
            """
            UPDATE macro_document_analysis_jobs
               SET status = CASE
                     WHEN status = 'claimed'
                      AND lease_owner = %s
                      AND leased_until_ms > %s
                     THEN status ELSE 'retryable' END,
                   next_due_at_ms = CASE
                     WHEN status = 'claimed'
                      AND lease_owner = %s
                      AND leased_until_ms > %s
                     THEN next_due_at_ms ELSE GREATEST(next_due_at_ms, %s) END,
                   leased_until_ms = CASE
                     WHEN status = 'claimed'
                      AND lease_owner = %s
                      AND leased_until_ms > %s
                     THEN leased_until_ms ELSE NULL END,
                   lease_owner = CASE
                     WHEN status = 'claimed'
                      AND lease_owner = %s
                      AND leased_until_ms > %s
                     THEN lease_owner ELSE NULL END,
                   attempt_count = GREATEST(attempt_count, %s),
                   max_attempts = GREATEST(max_attempts, attempt_count, %s) +
                     CASE WHEN status = 'failed' THEN 1 ELSE 0 END,
                   last_error_code = COALESCE(%s, last_error_code),
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE status IN ('pending', 'claimed', 'retryable', 'failed')
            """,
            (
                owner,
                now_ms,
                owner,
                now_ms,
                due,
                owner,
                now_ms,
                owner,
                now_ms,
                attempt_floor,
                attempt_floor,
                last_error,
                int(row["updated_at_ms"]),
            ),
        )
        return
    terminal = status == "quarantined"
    next_due_at_ms = _later_due(now_ms, row.get("next_attempt_at_ms"))
    conn.exec_driver_sql(
        """
        UPDATE macro_document_analysis_jobs
           SET status = CASE WHEN %s THEN 'failed' ELSE 'retryable' END,
               next_due_at_ms = CASE WHEN %s THEN next_due_at_ms ELSE GREATEST(next_due_at_ms, %s) END,
               leased_until_ms = NULL,
               lease_owner = NULL,
               attempt_count = GREATEST(attempt_count, %s),
               max_attempts = CASE
                 WHEN %s THEN max_attempts
                 ELSE GREATEST(max_attempts, attempt_count, %s) +
                   CASE WHEN status = 'failed' THEN 1 ELSE 0 END
               END,
               last_error_code = COALESCE(%s, last_error_code),
               updated_at_ms = GREATEST(updated_at_ms, %s)
         WHERE status IN ('pending', 'claimed', 'retryable', 'failed')
        """,
        (
            terminal,
            terminal,
            next_due_at_ms,
            attempt_floor,
            terminal,
            attempt_floor,
            last_error,
            int(row["updated_at_ms"]),
        ),
    )


def _migrate_thesis_frontier(conn: Any, *, row: Mapping[str, Any], now_ms: int) -> None:
    status = str(row["status"])
    if status == "clean":
        return
    session_date = str(row["shard_key"])
    _ensure_thesis_run(conn, row=row, now_ms=now_ms)
    existing_row = (
        conn.exec_driver_sql(
            "SELECT * FROM macro_thesis_runs WHERE session_date = %s::date FOR UPDATE",
            (session_date,),
        )
        .mappings()
        .first()
    )
    if existing_row is None:
        raise RuntimeError(f"worker_runtime_v2_thesis_native_intent_missing:{session_date}")
    existing = dict(existing_row)
    if str(existing["status"]) in {"published", "not_published", "config_error"}:
        return
    attempt_floor = _outer_attempt_floor(row)
    last_error = _optional_text(row.get("last_error_code"))
    if status == "dirty":
        conn.exec_driver_sql(
            """
            UPDATE macro_thesis_runs
               SET status = CASE WHEN status = 'retryable' THEN status ELSE 'pending' END,
                   due_at_ms = LEAST(due_at_ms, %s),
                   attempt_count = GREATEST(attempt_count, %s),
                   max_attempts = GREATEST(max_attempts, attempt_count, %s) +
                     CASE WHEN status = 'failed' THEN 1 ELSE 0 END,
                   leased_until_ms = NULL,
                   lease_owner = NULL,
                   last_error_code = COALESCE(%s, last_error_code),
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE session_date = %s::date
               AND status IN ('pending', 'retryable', 'failed')
            """,
            (
                int(row["deadline_at_ms"]),
                attempt_floor,
                attempt_floor,
                last_error,
                int(row["updated_at_ms"]),
                session_date,
            ),
        )
        return
    if status == "running" and _matching_valid_lease(
        existing,
        outer_owner=row.get("claimed_by"),
        native_owner_key="lease_owner",
        native_until_key="leased_until_ms",
        now_ms=now_ms,
    ):
        conn.exec_driver_sql(
            """
            UPDATE macro_thesis_runs
               SET attempt_count = GREATEST(attempt_count, %s),
                   last_error_code = COALESCE(%s, last_error_code),
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE session_date = %s::date
            """,
            (attempt_floor, last_error, int(row["updated_at_ms"]), session_date),
        )
        return
    terminal = status == "quarantined"
    next_due_at_ms = _later_due(
        now_ms,
        row.get("next_attempt_at_ms"),
        row.get("claimed_until_ms"),
        existing.get("due_at_ms"),
        existing.get("leased_until_ms"),
    )
    conn.exec_driver_sql(
        """
        UPDATE macro_thesis_runs
           SET status = CASE WHEN %s THEN 'failed' ELSE 'retryable' END,
               due_at_ms = CASE WHEN %s THEN due_at_ms ELSE GREATEST(due_at_ms, %s) END,
               leased_until_ms = NULL,
               lease_owner = NULL,
               attempt_count = GREATEST(attempt_count, %s),
               max_attempts = CASE
                 WHEN %s THEN max_attempts
                 ELSE GREATEST(max_attempts, attempt_count, %s) +
                   CASE WHEN status = 'failed' THEN 1 ELSE 0 END
               END,
               last_error_code = COALESCE(%s, last_error_code),
               updated_at_ms = GREATEST(updated_at_ms, %s)
         WHERE session_date = %s::date
           AND status NOT IN ('published', 'not_published', 'config_error')
        """,
        (
            terminal,
            terminal,
            next_due_at_ms,
            attempt_floor,
            terminal,
            attempt_floor,
            last_error,
            int(row["updated_at_ms"]),
            session_date,
        ),
    )


def _ensure_thesis_run(conn: Any, *, row: Mapping[str, Any], now_ms: int) -> None:
    session_date = str(row["shard_key"])
    inserted = conn.exec_driver_sql(
        """
        INSERT INTO macro_thesis_runs(
          session_date, cutoff_ms, evidence_pack_id, evidence_pack_hash,
          status, attempt_count, max_attempts, due_at_ms,
          created_at_ms, updated_at_ms
        )
        SELECT packs.session_date, packs.cutoff_ms, packs.evidence_pack_id,
               packs.payload_hash, 'pending', 0, %s, %s, %s, %s
          FROM macro_evidence_packs AS packs
         WHERE packs.session_date = %s::date
         ORDER BY packs.sealed_at_ms DESC, packs.evidence_pack_id DESC
         LIMIT 1
        ON CONFLICT(session_date) DO NOTHING
        """,
        (
            max(3, _outer_attempt_floor(row) + 1),
            int(row["deadline_at_ms"]),
            min(int(row["first_dirty_at_ms"]), now_ms),
            int(row["updated_at_ms"]),
            session_date,
        ),
    )
    if int(inserted.rowcount or 0) == 0:
        exists = conn.exec_driver_sql(
            "SELECT 1 FROM macro_thesis_runs WHERE session_date = %s::date",
            (session_date,),
        ).first()
        if exists is None:
            raise RuntimeError(f"worker_runtime_v2_thesis_evidence_pack_missing:{session_date}")


def _outer_attempt_floor(row: Mapping[str, Any]) -> int:
    return max(
        0,
        int(row.get("attempt_count") or 0),
        int(row.get("transient_failure_count") or 0),
    )


def _later_due(now_ms: int, *values: object) -> int:
    return max(int(now_ms), *(int(str(value)) for value in values if value is not None))


def _matching_valid_lease(
    native: Mapping[str, Any] | None,
    *,
    outer_owner: object,
    native_owner_key: str,
    native_until_key: str,
    now_ms: int,
) -> bool:
    if native is None:
        return False
    outer_owner_text = str(outer_owner or "").strip()
    native_owner_text = str(native.get(native_owner_key) or "").strip()
    native_until = int(native.get(native_until_key) or 0)
    return bool(
        outer_owner_text
        and native_owner_text == outer_owner_text
        and native_until > int(now_ms)
        and str(native.get("status")) in {"running", "claimed"}
    )


def _migrate_terminal_evidence(conn: Any, *, now_ms: int) -> None:
    preservation_snapshot = _terminal_preservation_snapshot(conn)
    for legacy_owner, owner in _LEGACY_TERMINAL_OWNER_MAPPINGS.items():
        conn.exec_driver_sql(
            """
            UPDATE worker_queue_terminal_events
               SET worker_name = %s
             WHERE worker_name = %s
            """,
            (owner, legacy_owner),
        )
    for source_table, owner in _PROJECTION_OWNER_BY_SOURCE.items():
        conn.exec_driver_sql(
            """
            UPDATE worker_queue_terminal_events
               SET worker_name = %s
             WHERE worker_name = 'steady_projection_coordinator'
               AND source_table = %s
            """,
            (owner, source_table),
        )
    model_rows = (
        conn.exec_driver_sql(
            """
        SELECT *
          FROM worker_queue_terminal_events
         WHERE worker_name = 'model_projection'
         ORDER BY terminal_id
        """
        )
        .mappings()
        .all()
    )
    for raw in model_rows:
        row = dict(raw)
        source = dict(row["source_row_json"] or {})
        kind = str(source["candidate_kind"])
        owner = _MODEL_OWNER_BY_KIND[kind]
        targets = _native_terminal_targets(conn, kind=kind, row=row, source=source)
        target_set_hash = _sha256_json(sorted(targets))
        was_unresolved = row.get("operator_action") is None
        conn.exec_driver_sql(
            """
            UPDATE worker_queue_terminal_events
               SET worker_name = %s,
                   operator_action = CASE WHEN %s THEN 'archive' ELSE operator_action END,
                   operator_reason = CASE WHEN %s THEN %s ELSE operator_reason END,
                   operator_action_at_ms = CASE WHEN %s THEN %s ELSE operator_action_at_ms END
             WHERE terminal_id = %s
            """,
            (
                owner,
                was_unresolved,
                was_unresolved,
                f"migrated_to_native:{target_set_hash}",
                was_unresolved,
                now_ms,
                str(row["terminal_id"]),
            ),
        )
        if not was_unresolved:
            continue
        for target_key in targets:
            child_source = {
                **source,
                "migrated_from_terminal_id": str(row["terminal_id"]),
                "native_target_key": target_key,
                "target_set_hash": target_set_hash,
            }
            child_hash = _sha256_json(child_source)
            terminal_id = "term_" + _sha256_text(f"{row['terminal_id']}|{owner}|{target_key}")
            conn.exec_driver_sql(
                """
                INSERT INTO worker_queue_terminal_events(
                  terminal_id, worker_name, source_table, target_key,
                  source_row_json, source_row_hash, final_status, final_reason,
                  attempt_count, payload_hash, first_seen_at_ms,
                  last_attempted_at_ms, terminalized_at_ms, terminal_generation,
                  operator_action, operator_reason, operator_action_at_ms,
                  final_reason_bucket
                )
                VALUES (
                  %s, %s, %s, %s, %s, %s, 'quarantined', %s,
                  %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, %s
                )
                ON CONFLICT(terminal_id) DO NOTHING
                """,
                (
                    terminal_id,
                    owner,
                    _native_source_table(kind),
                    target_key,
                    Jsonb(child_source),
                    child_hash,
                    f"migrated_from_terminal:{row['terminal_id']}",
                    int(row["attempt_count"]),
                    str(row["payload_hash"]),
                    int(row["first_seen_at_ms"]),
                    int(row["last_attempted_at_ms"]),
                    int(row["terminalized_at_ms"]),
                    int(row["terminal_generation"]),
                    str(row["final_reason_bucket"]),
                ),
            )
        parent = (
            conn.exec_driver_sql(
                """
            SELECT worker_name, operator_action, operator_reason
              FROM worker_queue_terminal_events
             WHERE terminal_id = %s
            """,
                (str(row["terminal_id"]),),
            )
            .mappings()
            .first()
        )
        if (
            parent is None
            or str(parent["worker_name"]) != owner
            or str(parent["operator_action"]) != "archive"
            or str(parent["operator_reason"]) != f"migrated_to_native:{target_set_hash}"
        ):
            raise RuntimeError(f"worker_runtime_v2_terminal_parent_closure_invalid:{row['terminal_id']}")
        children = (
            conn.exec_driver_sql(
                """
                SELECT target_key, source_row_json, source_row_hash
                  FROM worker_queue_terminal_events
                 WHERE source_row_json->>'migrated_from_terminal_id' = %s
                 ORDER BY target_key
                """,
                (str(row["terminal_id"]),),
            )
            .mappings()
            .all()
        )
        actual_targets = [str(child["target_key"]) for child in children]
        if actual_targets != sorted(targets):
            raise RuntimeError(f"worker_runtime_v2_terminal_target_set_invalid:{row['terminal_id']}")
        for child in children:
            source_row = dict(child["source_row_json"] or {})
            if str(source_row.get("target_set_hash")) != target_set_hash or str(
                child["source_row_hash"]
            ) != _sha256_json(source_row):
                raise RuntimeError(f"worker_runtime_v2_terminal_child_hash_invalid:{row['terminal_id']}")
    _verify_terminal_preservation(conn, preservation_snapshot)


def _terminal_preservation_snapshot(conn: Any) -> dict[str, str]:
    rows = (
        conn.exec_driver_sql(
            """
            SELECT *
              FROM worker_queue_terminal_events
             WHERE worker_name <> 'model_projection'
             ORDER BY terminal_id
            """
        )
        .mappings()
        .all()
    )
    snapshot: dict[str, str] = {}
    for raw in rows:
        row = dict(raw)
        terminal_id = str(row.pop("terminal_id"))
        row.pop("worker_name", None)
        snapshot[terminal_id] = _sha256_json(row)
    return snapshot


def _verify_terminal_preservation(conn: Any, snapshot: Mapping[str, str]) -> None:
    for terminal_id, expected_hash in snapshot.items():
        raw = (
            conn.exec_driver_sql(
                "SELECT * FROM worker_queue_terminal_events WHERE terminal_id = %s",
                (terminal_id,),
            )
            .mappings()
            .first()
        )
        if raw is None:
            raise RuntimeError(f"worker_runtime_v2_terminal_row_missing:{terminal_id}")
        row = dict(raw)
        row.pop("terminal_id", None)
        row.pop("worker_name", None)
        actual_hash = _sha256_json(row)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"worker_runtime_v2_terminal_preservation_hash_mismatch:{terminal_id}:"
                f"expected={expected_hash}:actual={actual_hash}"
            )


def _native_terminal_targets(
    conn: Any,
    *,
    kind: str,
    row: Mapping[str, Any],
    source: Mapping[str, Any],
) -> list[str]:
    if kind == "news_brief":
        value = source.get("input_fingerprint") or source.get("shard_key") or row["target_key"]
        return [str(value)]
    if kind == "macro_thesis":
        value = source.get("shard_key") or row["target_key"]
        return [str(value)]
    targets = [
        str(item[0])
        for item in conn.exec_driver_sql(
            """
            SELECT analysis_job_id
              FROM macro_document_analysis_jobs
             WHERE status IN ('pending', 'claimed', 'retryable')
             ORDER BY analysis_job_id
            """
        ).fetchall()
    ]
    if not targets:
        raise RuntimeError(f"worker_runtime_v2_document_terminal_target_set_empty:{row['terminal_id']}")
    return targets


def _native_source_table(kind: str) -> str:
    return {
        "news_brief": "news_brief_runs",
        "macro_thesis": "macro_thesis_runs",
        "macro_document_analysis": "macro_document_analysis_jobs",
    }[kind]


def _rename_terminal_surface() -> None:
    op.execute(
        """
        ALTER TABLE worker_queue_terminal_events RENAME TO queue_terminal_events;
        ALTER TABLE queue_terminal_events RENAME COLUMN worker_name TO owner_key;
        ALTER TABLE queue_terminal_events
          ADD CONSTRAINT queue_terminal_events_owner_key_check CHECK (
            owner_key IN (
              'event_anchor_backfill', 'resolution_refresh',
              'asset_profile_refresh', 'token_image_mirror',
              'radar_projection', 'profile_projection', 'macro_projection',
              'news_projection', 'news_brief', 'macro_thesis',
              'macro_document_analysis'
            )
          );
        ALTER TABLE queue_terminal_events
          RENAME CONSTRAINT worker_queue_terminal_events_pkey TO queue_terminal_events_pkey;
        ALTER INDEX idx_worker_queue_terminal_reason_bucket_unresolved
          RENAME TO idx_queue_terminal_reason_bucket_unresolved;
        ALTER INDEX idx_worker_queue_terminal_resolved_retention
          RENAME TO idx_queue_terminal_resolved_retention;
        ALTER INDEX idx_worker_queue_terminal_source
          RENAME TO idx_queue_terminal_source;
        ALTER INDEX idx_worker_queue_terminal_unresolved
          RENAME TO idx_queue_terminal_unresolved;
        ALTER INDEX uq_worker_queue_terminal_one_unresolved
          RENAME TO uq_queue_terminal_one_unresolved;
        ALTER INDEX uq_worker_queue_terminal_source_snapshot
          RENAME TO uq_queue_terminal_source_snapshot;
        """
    )


def _create_workers_runtime() -> None:
    op.execute(
        """
        CREATE TABLE workers_runtime (
          singleton_key boolean PRIMARY KEY DEFAULT true CHECK (singleton_key),
          runtime_id uuid NOT NULL,
          runtime_version text NOT NULL CHECK (btrim(runtime_version) <> ''),
          lifecycle_state text NOT NULL CHECK (
            lifecycle_state IN ('starting', 'running', 'stopping', 'stopped', 'failed')
          ),
          started_at_ms bigint NOT NULL CHECK (started_at_ms >= 0),
          heartbeat_at_ms bigint NOT NULL CHECK (heartbeat_at_ms >= started_at_ms),
          fatal_code text CHECK (
            fatal_code IS NULL OR fatal_code IN (
              'startup_failed', 'child_failed', 'control_failed',
              'singleton_lost', 'runtime_invariant_failed',
              'resource_operation_overrun', 'graceful_deadline_exceeded',
              'cleanup_failed'
            )
          ),
          CHECK (
            (lifecycle_state = 'failed' AND fatal_code IS NOT NULL)
            OR (lifecycle_state <> 'failed' AND fatal_code IS NULL)
          )
        );
        """
    )


def _apply_runtime_grants() -> None:
    op.execute(
        """
        ALTER TABLE workers_runtime OWNER TO tracefold_owner;
        ALTER TABLE queue_terminal_events OWNER TO tracefold_owner;
        GRANT SELECT ON workers_runtime, queue_terminal_events TO tracefold_serve;
        GRANT SELECT, INSERT, UPDATE, DELETE
          ON workers_runtime, queue_terminal_events TO tracefold_workers;
        """
    )


def _resolve_thesis_session(*, now_ms: int) -> date:
    instant = datetime.fromtimestamp(int(now_ms) / 1_000, tz=_NEW_YORK)
    candidate = instant.date()
    if is_us_market_session(candidate) and instant.timetz().replace(tzinfo=None) >= _THESIS_PUBLICATION_TIME:
        return candidate
    candidate -= timedelta(days=1)
    while not is_us_market_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _thesis_cutoff_ms(session_date: date) -> int:
    if not is_us_market_session(session_date):
        raise ValueError(f"macro_thesis_market_session_required:{session_date.isoformat()}")
    return int(datetime.combine(session_date, _THESIS_PUBLICATION_TIME, tzinfo=_NEW_YORK).timestamp() * 1_000)


def _stable_id(namespace: str, value: str) -> str:
    payload = "\x1f".join((namespace, value))
    return f"{namespace}_{_sha256_text(payload)[:32]}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
