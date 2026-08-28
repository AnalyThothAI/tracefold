from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import (
    connect_postgres_test,
    reset_postgres_schema,
)
from tests.postgres_test_utils import (
    test_postgres_dsn as postgres_test_dsn,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]


def _upgrade(revision: str) -> None:
    config = alembic_config()
    config.attributes["database_url"] = postgres_test_dsn()
    command.upgrade(config, revision)


def _fresh_schema_at(revision: str) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    _upgrade(revision)


def test_0283_to_head_preserves_eventless_legacy_label_byte_for_byte() -> None:
    """The production upgrade hard-cuts Label v1 only after a lossless copy.

    Event-less misses are the most important legacy shape because they are the
    only evidence that the old pipeline failed before creating an Event.
    """

    label_id = "a" * 64
    label = {"label": "missed", "note": "operator observed an upstream miss"}
    subject = "DRAM export unit price continued to rise"
    conn: Any | None = None
    try:
        _fresh_schema_at("20260820_0283")
        conn = connect_postgres_test(read_only=False)
        conn.execute(
            """
            INSERT INTO news_event_labels (
              event_id, label_version, source, label, created_at_ms,
              labeled_by, subject, label_id
            ) VALUES (NULL, %s, 'human', %s::jsonb, %s, %s, %s, %s)
            """,
            ("news_label_v1", json.dumps(label), 1_787_279_400_000, "massis", subject, label_id),
        )
        conn.commit()
        conn.close()
        conn = None

        _upgrade("head")

        conn = connect_postgres_test(read_only=False)
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision["version_num"] == "20260828_0317"
        assert conn.execute("SELECT to_regclass('public.news_event_labels') AS name").fetchone()["name"] is None

        migrated = conn.execute(
            """
            SELECT review_kind, subject_kind, task_id, task_version, event_id,
                   rubric_version, reader_contract_version, reviewer,
                   release_eligible, created_at_ms, payload
              FROM news_reviews
             WHERE review_kind = 'legacy'
            """
        ).fetchall()
        assert len(migrated) == 1
        row = migrated[0]
        assert row["review_kind"] == "legacy"
        assert row["subject_kind"] == "legacy_label"
        assert row["task_id"] == f"legacy:{label_id}"
        assert row["task_version"] == f"legacy:{label_id}"
        assert row["event_id"] is None
        assert row["rubric_version"] == "news_label_v1_legacy"
        assert row["reader_contract_version"] == "unknown"
        assert row["reviewer"] == "massis"
        assert row["release_eligible"] is False
        assert row["created_at_ms"] == 1_787_279_400_000
        assert row["payload"] == {
            "event_id": None,
            "label_version": "news_label_v1",
            "source": "human",
            "label": label,
            "created_at_ms": 1_787_279_400_000,
            "labeled_by": "massis",
            "subject": subject,
            "label_id": label_id,
        }

        privileges = conn.execute(
            """
            SELECT
              has_table_privilege('tracefold_serve', 'news_reviews', 'SELECT') AS review_select,
              has_table_privilege('tracefold_serve', 'news_reviews', 'INSERT') AS review_insert,
              has_table_privilege('tracefold_serve', 'news_reviews', 'UPDATE,DELETE') AS review_rewrite,
              has_table_privilege('tracefold_serve', 'news_events', 'INSERT') AS news_insert,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'SELECT')
                AS workers_evidence_select,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'INSERT')
                AS workers_evidence_insert,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'UPDATE')
                AS workers_evidence_update,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'DELETE')
                AS workers_evidence_delete
            """
        ).fetchone()
        assert privileges == {
            "review_select": True,
            "review_insert": True,
            "review_rewrite": False,
            "news_insert": False,
            "workers_evidence_select": True,
            "workers_evidence_insert": True,
            "workers_evidence_update": False,
            "workers_evidence_delete": False,
        }
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0288_to_head_repairs_the_worker_evidence_grant() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260821_0288")
        conn = connect_postgres_test(read_only=False)
        conn.execute("REVOKE ALL ON news_event_evidence_snapshots FROM tracefold_workers")
        conn.commit()
        conn.close()
        conn = None

        _upgrade("head")

        conn = connect_postgres_test(read_only=False)
        privileges = conn.execute(
            """
            SELECT
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'SELECT')
                AS select_allowed,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'INSERT')
                AS insert_allowed,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'UPDATE')
                AS update_allowed,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'DELETE')
                AS delete_allowed
            """
        ).fetchone()
        assert privileges == {
            "select_allowed": True,
            "insert_allowed": True,
            "update_allowed": False,
            "delete_allowed": False,
        }
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260828_0317"
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0291_to_head_preserves_prompt_recordings_as_audit_and_starts_program_epoch() -> None:
    """The hard cut adds call-path identity without rewriting append-only history."""

    conn: Any | None = None
    try:
        _fresh_schema_at("20260821_0291")
        conn = connect_postgres_test(read_only=False)
        conn.execute(
            """
            INSERT INTO news_model_recordings (
              recording_sha, run_sha, case_id, arm, trial, request_sha256,
              response_sha256, request, response, provider, model, model_sha,
              execution_contract_sha, latency_ms, input_tokens, output_tokens,
              finish_reason, error_code, created_at_ms
            ) VALUES (
              %s, %s, 'legacy-case', 'stable', 1, %s,
              %s, '{}'::jsonb, '{}'::jsonb, 'litellm', 'legacy-model', %s,
              %s, 10, 11, 7, 'stop', NULL, 1787329286999
            )
            """,
            ("1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, "6" * 64),
        )
        conn.commit()
        conn.close()
        conn = None

        deployed_after_ms = int(time.time() * 1000) - 5_000
        _upgrade("head")
        deployed_before_ms = int(time.time() * 1000) + 5_000

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260828_0317"
        epoch = conn.execute("SELECT * FROM news_learning_epochs WHERE epoch_id = 'program_v1'").fetchone()
        assert epoch is not None
        assert deployed_after_ms <= epoch["starts_at_ms"] <= deployed_before_ms
        assert epoch["created_at_ms"] == epoch["starts_at_ms"]
        assert epoch["prior_evidence_disposition"] == "audit_only"
        assert epoch["reset_reason"] == "zero_compatibility_reset_reaccrue_evidence"
        assert epoch["program_factory_id"] == "tracefold.news.semantic_program.factory_v1"
        assert epoch["artifact_schema_version"] == "news_semantic_program_artifact_v1"
        assert epoch["baseline_program_version"] == "news_semantic_program_v1"
        assert epoch["baseline_program_sha256"] == ("87c62ed2b3a89eadddbdc90bdab03405c11da30ee259da49e11b0bd973094119")
        legacy = conn.execute(
            "SELECT predictor_name, call_index, attempt, route, cached_tokens, total_tokens, "
            "provider_cost_microusd FROM news_model_recordings WHERE recording_sha = %s",
            ("1" * 64,),
        ).fetchone()
        assert legacy == {
            "predictor_name": "legacy_prompt",
            "call_index": 0,
            "attempt": 1,
            "route": "legacy",
            "cached_tokens": None,
            "total_tokens": None,
            "provider_cost_microusd": None,
        }
        verdict_columns = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'news_verdicts'"
            ).fetchall()
        }
        assert {"program_version", "program_sha256"} <= verdict_columns
        privileges = conn.execute(
            """
            SELECT
              has_table_privilege('tracefold_serve', 'news_learning_epochs', 'SELECT') AS serve_select,
              has_table_privilege('tracefold_serve', 'news_learning_epochs', 'INSERT') AS serve_insert,
              has_table_privilege('tracefold_workers', 'news_learning_epochs', 'SELECT') AS workers_select,
              has_table_privilege('tracefold_workers', 'news_learning_epochs', 'INSERT') AS workers_insert
            """
        ).fetchone()
        assert privileges == {
            "serve_select": True,
            "serve_insert": False,
            "workers_select": True,
            "workers_insert": False,
        }
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0292_to_0293_appends_program_v2_epoch_without_rewriting_program_v1() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260822_0292")
        conn = connect_postgres_test(read_only=False)
        program_v1 = dict(conn.execute("SELECT * FROM news_learning_epochs WHERE epoch_id = 'program_v1'").fetchone())
        conn.close()
        conn = None

        deployed_after_ms = int(time.time() * 1000) - 5_000
        _upgrade("20260822_0293")
        deployed_before_ms = int(time.time() * 1000) + 5_000

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260822_0293"
        epochs = conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        assert len(epochs) == 2
        assert dict(next(row for row in epochs if row["epoch_id"] == "program_v1")) == program_v1
        program_v2 = next(row for row in epochs if row["epoch_id"] == "program_v2")
        assert deployed_after_ms <= program_v2["starts_at_ms"] <= deployed_before_ms
        assert program_v2["created_at_ms"] == program_v2["starts_at_ms"]
        assert program_v2["starts_at_ms"] >= program_v1["starts_at_ms"]
        assert program_v2["prior_evidence_disposition"] == "audit_only"
        assert program_v2["reset_reason"] == "semantic_retry_and_restatement_contract_reidentity"
        assert program_v2["program_factory_id"] == "tracefold.news.semantic_program.factory_v1"
        assert program_v2["artifact_schema_version"] == "news_semantic_program_artifact_v1"
        assert program_v2["baseline_program_version"] == "news_semantic_program_v1"
        assert program_v2["baseline_program_sha256"] == (
            "ad8720a8f70c9210440f7c9eaeaad182b261359dcbe77a0fb7c6d2063815da3c"
        )
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0293_to_0294_appends_program_v3_epoch_without_rewriting_prior_epochs() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260822_0293")
        conn = connect_postgres_test(read_only=False)
        prior_epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        conn.close()
        conn = None

        deployed_after_ms = int(time.time() * 1000) - 5_000
        _upgrade("20260822_0294")
        deployed_before_ms = int(time.time() * 1000) + 5_000

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260822_0294"
        epochs = conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        assert len(epochs) == 3
        assert dict(next(row for row in epochs if row["epoch_id"] == "program_v1")) == prior_epochs["program_v1"]
        assert dict(next(row for row in epochs if row["epoch_id"] == "program_v2")) == prior_epochs["program_v2"]
        program_v3 = next(row for row in epochs if row["epoch_id"] == "program_v3")
        assert deployed_after_ms <= program_v3["starts_at_ms"] <= deployed_before_ms
        assert program_v3["created_at_ms"] == program_v3["starts_at_ms"]
        assert program_v3["starts_at_ms"] >= prior_epochs["program_v2"]["starts_at_ms"]
        assert program_v3["source_issue"] == "https://github.com/AnalyThothAI/tracefold/issues/132"
        assert program_v3["prior_evidence_disposition"] == "audit_only"
        assert program_v3["reset_reason"] == "expert_quality_baseline_and_semantic_normalization"
        assert program_v3["program_factory_id"] == "tracefold.news.semantic_program.factory_v1"
        assert program_v3["artifact_schema_version"] == "news_semantic_program_artifact_v1"
        assert program_v3["baseline_program_version"] == "news_semantic_program_v1"
        assert program_v3["baseline_program_sha256"] == (
            "49643db931211aee7f1d4f5b7124345d45e18132b10628b85843c55e05dff8d5"
        )
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0297_to_0298_appends_program_v5_epoch_without_rewriting_prior_epochs() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260822_0297")
        conn = connect_postgres_test(read_only=False)
        prior_epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        conn.execute(
            """
            INSERT INTO news_canary_activations (
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
              state, revision, created_at_ms, activated_at_ms
            ) VALUES (%s, %s, %s, %s, 'news_canary_selector_v1', 1000, %s, %s, 'active', 1, %s, %s)
            """,
            (
                "a" * 32,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                prior_epochs["program_v4"]["starts_at_ms"],
                prior_epochs["program_v4"]["starts_at_ms"],
            ),
        )
        conn.commit()
        conn.close()
        conn = None

        deployed_after_ms = int(time.time() * 1000) - 5_000
        _upgrade("20260822_0298")
        deployed_before_ms = int(time.time() * 1000) + 5_000

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260822_0298"
        epochs = conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        assert len(epochs) == 5
        for epoch_id, prior in prior_epochs.items():
            assert dict(next(row for row in epochs if row["epoch_id"] == epoch_id)) == prior
        program_v5 = next(row for row in epochs if row["epoch_id"] == "program_v5")
        assert deployed_after_ms <= program_v5["starts_at_ms"] <= deployed_before_ms
        assert program_v5["created_at_ms"] == program_v5["starts_at_ms"]
        assert program_v5["starts_at_ms"] > prior_epochs["program_v4"]["starts_at_ms"]
        assert program_v5["source_issue"] == "https://github.com/AnalyThothAI/tracefold/issues/138"
        assert program_v5["prior_evidence_disposition"] == "audit_only"
        assert program_v5["reset_reason"] == "candidate_conditioned_told_context_and_predictor_input_partition"
        assert program_v5["program_factory_id"] == "tracefold.news.semantic_program.factory_v3"
        assert program_v5["artifact_schema_version"] == "news_semantic_program_artifact_v2"
        assert program_v5["baseline_program_version"] == "news_semantic_program_v3"
        assert program_v5["baseline_program_sha256"] == (
            "c62e0d69bf6c1901b3e8a1a716ca153acaf92793421d5af2701030c0477cac3b"
        )
        canary = conn.execute(
            "SELECT state, revision, trip_reason, tripped_at_ms FROM news_canary_activations WHERE activation_id = %s",
            ("a" * 32,),
        ).fetchone()
        assert canary["state"] == "tripped"
        assert canary["revision"] == 2
        assert canary["trip_reason"] == "program_v5_hard_cut"
        assert canary["tripped_at_ms"] == program_v5["starts_at_ms"]
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0300_to_head_hard_cuts_queue_priority_editorial_and_program_v6() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260823_0300")
        conn = connect_postgres_test(read_only=False)
        prior_epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        assert "priority" in {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'news_events'"
            ).fetchall()
        }
        conn.execute(
            """
            INSERT INTO news_canary_activations (
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
              state, revision, created_at_ms
            ) VALUES (%s, %s, %s, %s, 'news_canary_selector_v1', 1000, %s, %s, 'armed', 1, %s)
            """,
            ("1" * 32, "2" * 64, "3" * 64, "4" * 64, "5" * 64, "6" * 64, int(time.time() * 1000)),
        )
        old_opened_at_ms = int(time.time() * 1000) - 1_000
        old_expires_at_ms = old_opened_at_ms + 7 * 24 * 3_600_000
        conn.execute(
            """
            INSERT INTO news_items(
              item_id, source_id, source_item_key, title, raw_first_line, description,
              canonical_url, reporting_origin, published_at_ms, observed_at_ms,
              provider_metadata, provenance, first_ingest_mode, trace_id,
              created_at_ms, updated_at_ms, source_artifact_id
            ) VALUES (
              'old-item', 'opennews', 'old-source-key', 'Same post-cut fact', '', '',
              'https://x.com/source/status/12345', 'opennews', %s, %s,
              '{}'::jsonb, '[]'::jsonb, 'live', 'old-trace', %s, %s, 'x:12345'
            )
            """,
            (old_opened_at_ms, old_opened_at_ms, old_opened_at_ms, old_opened_at_ms),
        )
        conn.execute(
            """
            INSERT INTO news_events(
              event_id, leader_item_id, family, comparison_fingerprint, comparison_title,
              leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, member_count,
              admission, priority, provider_score_max, engine_type, asset_class,
              grounded_assets, watchlist_hits, macro_lexicon, storyline_key, context_line,
              published_at_ms, ingest_mode, trace_id, created_at_ms, updated_at_ms,
              focus_fact_id, focus_fact_text, focus_fact_context, focus_fact_method,
              focus_span_start, focus_span_end
            ) VALUES (
              'old-event', 'old-item', 'general', 'same-fingerprint', 'same post cut fact',
              'Same post-cut fact', %s, %s, %s, 1,
              'candidate', 'normal', 80, 'news', 'none',
              '[]'::jsonb, '[]'::jsonb, false, 'theme:test', '',
              %s, 'live', 'old-trace', %s, %s,
              'old-fact', 'Same post-cut fact', '', 'whole_title', 0, 18
            )
            """,
            (
                old_opened_at_ms,
                old_opened_at_ms,
                old_expires_at_ms,
                old_opened_at_ms,
                old_opened_at_ms,
                old_opened_at_ms,
            ),
        )
        conn.execute(
            """
            INSERT INTO news_event_members(
              event_id, item_id, joined_at_ms, match_kind, jaccard_estimate, fact_id, fact_text
            ) VALUES ('old-event', 'old-item', %s, 'leader', NULL, 'old-fact', 'Same post-cut fact')
            """,
            (old_opened_at_ms,),
        )
        conn.execute(
            """
            INSERT INTO news_event_evidence_snapshots(
              event_id, evidence_version, focus_fact_id, evidence_sha256,
              provenance, release_eligible, snapshot, created_at_ms
            ) VALUES (
              'old-event', 1, 'old-fact', %s, 'observed', true,
              '{"schema_version":"news_event_evidence_v1"}'::jsonb, %s
            )
            """,
            ("a" * 64, old_opened_at_ms),
        )
        conn.execute(
            """
            INSERT INTO news_event_bands(band_index, band_key, event_id, family, expires_at_ms)
            VALUES (0, 'same-band', 'old-event', 'general', %s)
            """,
            (old_expires_at_ms,),
        )
        conn.commit()
        conn.close()
        conn = None

        deployed_after_ms = int(time.time() * 1000) - 5_000
        _upgrade("head")
        deployed_before_ms = int(time.time() * 1000) + 5_000

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260828_0317"
        event_columns = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'news_events'"
            ).fetchall()
        }
        assert "queue_priority" in event_columns
        assert "priority" not in event_columns
        verdict_columns = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'news_verdicts'"
            ).fetchall()
        }
        assert {"editorial", "scored_judgment_sha256", "runtime_manifest_sha"} <= verdict_columns
        view_columns = [
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'news_review_task_source_v1' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        assert "queue_priority" in view_columns
        assert "priority" not in view_columns
        assert view_columns[-3:] == ["editorial", "scored_judgment_sha256", "runtime_manifest_sha"]

        epochs = conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        assert len(epochs) == 7
        for epoch_id, prior in prior_epochs.items():
            assert dict(next(row for row in epochs if row["epoch_id"] == epoch_id)) == prior
        program_v6 = next(row for row in epochs if row["epoch_id"] == "program_v6")
        assert deployed_after_ms <= program_v6["starts_at_ms"] <= deployed_before_ms
        assert program_v6["starts_at_ms"] > prior_epochs["program_v5"]["starts_at_ms"]
        assert program_v6["source_issue"] == "https://github.com/AnalyThothAI/tracefold/issues/160"
        assert program_v6["program_factory_id"] == "tracefold.news.semantic_program.factory_v4"
        assert program_v6["artifact_schema_version"] == "news_semantic_program_artifact_v2"
        assert program_v6["baseline_program_version"] == "news_semantic_program_v4"
        # The epoch row is immutable audit history, pinned by literal like every epoch above it. It records the
        # root that was stable when #160 opened the epoch, not whatever ships today, and #173 re-issues the
        # code-owned root inside the same epoch rather than opening a new one.
        #
        # That does *not* mean accepted evidence survives the re-issue: `CandidateEvaluator` selects its cohort
        # with `source.program_sha256 = self._stable.program_sha256` (plus the bundle sha), so every review
        # accrued under 648e696d leaves the release-evidence denominators the moment the root changes. What
        # keeping the epoch preserves is the `starts_at_ms` floor that bounds the learning window and the
        # release-cohort query — the only column any runtime caller reads from this table.
        assert program_v6["starts_at_ms"] > 0
        assert program_v6["baseline_program_sha256"] == (
            "648e696df5a8f251085a0749795a8d9e9227d05fb7e976fd1b5b538a7b8e87e7"
        )
        assert program_v6["baseline_program_sha256"] != load_stable_program_artifact().program_sha256
        assert program_v6["prior_evidence_disposition"] == "audit_only"
        assert program_v6["reset_reason"] == "trade_relevance_editorial_authority_hard_cut"

        # #162 PR8-B: the package split re-issued the Program root, so the epoch after v6 is v7. Its
        # guard reads v6's *recorded* baseline (648e696d), not today's runtime root — the two diverged
        # when #173 re-issued inside the v6 epoch, and asserting the wrong one fails the migration.
        program_v7 = next(row for row in epochs if row["epoch_id"] == "program_v7")
        assert program_v7["starts_at_ms"] > program_v6["starts_at_ms"]
        assert program_v7["source_issue"] == "https://github.com/AnalyThothAI/tracefold/issues/162"
        assert program_v7["program_factory_id"] == "tracefold.news.program.factory_v5"
        assert program_v7["artifact_schema_version"] == "news_semantic_program_artifact_v2"
        assert program_v7["baseline_program_version"] == "news_semantic_program_v5"
        # The v7 row is the immutable baseline recorded when #162 opened the epoch. #175 re-issues the
        # sole stable Program inside v7 because retrieval evidence changed; it must not rewrite migration
        # history or open another learning epoch.
        assert program_v7["baseline_program_sha256"] == (
            "7a460f8d3812c64c6ee38158871eb9f060811e5ffe87f399f7bc2e506b4e28ad"
        )
        assert program_v7["baseline_program_sha256"] != load_stable_program_artifact().program_sha256
        assert program_v7["prior_evidence_disposition"] == "audit_only"
        assert program_v7["reset_reason"] == "program_learning_package_split_identity_migration"
        assert (
            conn.execute("SELECT expires_at_ms FROM news_events WHERE event_id = 'old-event'").fetchone()[
                "expires_at_ms"
            ]
            == program_v6["starts_at_ms"]
        )
        assert (
            conn.execute("SELECT expires_at_ms FROM news_event_bands WHERE event_id = 'old-event'").fetchone()[
                "expires_at_ms"
            ]
            == program_v6["starts_at_ms"]
        )
        news = repositories_for_connection(conn).news
        assert (
            news.find_exact_event(
                family="general",
                event_kind="news",
                fingerprint="same-fingerprint",
                now_ms=program_v6["starts_at_ms"],
            )
            is None
        )
        assert (
            news.find_artifact_event(
                source_artifact_id="x:12345",
                family="general",
                event_kind="news",
                fingerprint="same-fingerprint",
                item_id="new-item",
                opened_after_ms=program_v6["starts_at_ms"] - 7 * 24 * 3_600_000,
            )
            is None
        )
        assert (
            news.find_band_candidates(
                family="general",
                event_kind="news",
                band_keys=("same-band",),
                now_ms=program_v6["starts_at_ms"],
            )
            == []
        )
        canary = conn.execute(
            "SELECT state, revision, trip_reason, tripped_at_ms FROM news_canary_activations WHERE activation_id = %s",
            ("1" * 32,),
        ).fetchone()
        assert canary == {
            "state": "tripped",
            "revision": 2,
            "trip_reason": "program_v6_hard_cut",
            "tripped_at_ms": program_v6["starts_at_ms"],
        }
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


# #193 replaces the two-file `news_semantic_program_artifact_v2` manifest/state document with the single
# `news_program_strategy_artifact_v1` document, and `factory_v5` with `factory_v6`. Pinned as literals,
# like every epoch row above: this is the receipt the migration wrote once, not whatever the module
# constant says today.
_HARD_CUT_RECEIPT = {
    "kind": "program_strategy_artifact_hard_cut",
    "source_issue": "https://github.com/AnalyThothAI/tracefold/issues/193",
    "epoch_id": "program_v7",
    "from_artifact_schema_version": "news_semantic_program_artifact_v2",
    "to_artifact_schema_version": "news_program_strategy_artifact_v1",
    "from_program_factory_id": "tracefold.news.program.factory_v5",
    "to_program_factory_id": "tracefold.news.program.factory_v6",
    "program_version": "news_semantic_program_v5",
    "prior_evidence_disposition": "accepted_review_v4_remains_eligible",
    "activation_disposition": "open_activations_tripped",
}
_HARD_CUT_TRIP_REASON = "program_strategy_artifact_v1_hard_cut"
# #193 PR-B replaces the seven content-addressed compile receipts, their chain root, the runner receipt,
# the optimizer provenance record and the machine diff with one `news_program_compile_record_v1`
# document. Pinned as a literal for the same reason as everything above.
_COMPILE_RECORD_TRIP_REASON = "compile_record_v1_hard_cut"
_RUN_SPEND_TRIP_REASON = "compile_record_run_spend_embed"
# #202: one candidate lifecycle. Release eligibility stops coming from where a candidate was
# produced, so a candidate registered under the compile chain can no longer be armed.
_PROMPT_CANDIDATE_TRIP_REASON = "prompt_candidate_v1_hard_cut"


def _ledger_artifact_sha(kind: str, payload: dict[str, Any]) -> str:
    """Re-derive the append-only ledger's content address independently of the migration."""

    encoded = json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def test_0303_to_0304_trips_the_open_activation_and_records_the_hard_cut_without_reopening_v7() -> None:
    """#193 changes Program identity, not which evidence is eligible.

    Exactly two things are database facts here. An armed or active canary points at a candidate this
    image can no longer execute, so it is closed with a durable reason instead of being re-tripped by
    every worker start. And the migration is recorded once in the append-only learning ledger.

    The `program_v7` epoch must survive byte for byte: re-opening it would discard accepted
    `news_review_v4` truth that a serialization change never invalidated, and would move the
    `starts_at_ms` floor that bounds every learning window and release cohort.
    """

    conn: Any | None = None
    try:
        _fresh_schema_at("20260824_0303")
        conn = connect_postgres_test(read_only=False)
        prior_epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        assert set(prior_epochs) >= {"program_v7"}
        settled_at_ms = int(time.time() * 1000) - 60_000
        # `ux_news_canary_one_open` permits a single armed-or-active row, so the open activation and the
        # already-closed one it must not touch are the whole surface of this migration.
        conn.execute(
            """
            INSERT INTO news_canary_activations (
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
              state, revision, trip_reason, tripped_at_ms, created_at_ms
            ) VALUES (%s, %s, %s, %s, 'news_canary_selector_v1', 1000, %s, %s, 'tripped', 5, %s, %s, %s)
            """,
            (
                "a" * 32,
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "4" * 64,
                "5" * 64,
                "earlier_operator_trip",
                settled_at_ms,
                settled_at_ms,
            ),
        )
        conn.execute(
            """
            INSERT INTO news_canary_activations (
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
              state, revision, created_at_ms, activated_at_ms
            ) VALUES (%s, %s, %s, %s, 'news_canary_selector_v1', 1000, %s, %s, 'active', 2, %s, %s)
            """,
            (
                "b" * 32,
                "6" * 64,
                "7" * 64,
                "8" * 64,
                "9" * 64,
                "e" * 64,
                settled_at_ms,
                settled_at_ms,
            ),
        )
        conn.commit()
        conn.close()
        conn = None

        deployed_after_ms = int(time.time() * 1000) - 5_000
        _upgrade("head")
        deployed_before_ms = int(time.time() * 1000) + 5_000

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260828_0317"

        epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        assert epochs == prior_epochs
        assert epochs["program_v7"]["program_factory_id"] == "tracefold.news.program.factory_v5"
        assert epochs["program_v7"]["artifact_schema_version"] == "news_semantic_program_artifact_v2"
        assert epochs["program_v7"]["baseline_program_sha256"] == (
            "7a460f8d3812c64c6ee38158871eb9f060811e5ffe87f399f7bc2e506b4e28ad"
        )

        activations = {
            row["activation_id"]: dict(row)
            for row in conn.execute(
                "SELECT activation_id, state, revision, trip_reason, tripped_at_ms FROM news_canary_activations"
            ).fetchall()
        }
        assert activations["a" * 32] == {
            "activation_id": "a" * 32,
            "state": "tripped",
            "revision": 5,
            "trip_reason": "earlier_operator_trip",
            "tripped_at_ms": settled_at_ms,
        }
        open_activation = activations["b" * 32]
        assert open_activation["state"] == "tripped"
        assert open_activation["revision"] == 3
        assert open_activation["trip_reason"] == _HARD_CUT_TRIP_REASON
        assert deployed_after_ms <= open_activation["tripped_at_ms"] <= deployed_before_ms
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM news_canary_activations WHERE state IN ('armed', 'active')"
            ).fetchone()["n"]
            == 0
        )

        receipts = conn.execute(
            "SELECT artifact_sha, parent_sha, payload, created_by, created_at_ms "
            "FROM news_learning_artifacts WHERE kind = 'epoch_reset' "
            "AND created_by = 'migration_20260824_0304'"
        ).fetchall()
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt["payload"] == _HARD_CUT_RECEIPT
        assert receipt["artifact_sha"] == _ledger_artifact_sha("epoch_reset", _HARD_CUT_RECEIPT)
        assert receipt["parent_sha"] is None
        assert receipt["created_by"] == "migration_20260824_0304"
        assert deployed_after_ms <= receipt["created_at_ms"] <= deployed_before_ms
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0304_to_0305_admits_the_compile_record_and_closes_the_old_chain_without_reopening_v7() -> None:
    """#193 PR-B changes how one compile is serialized, not which evidence is eligible.

    Two things are database facts here. `news_learning_artifacts` gains `compile_record` as a kind while
    keeping the retired `compile_receipt`, because those rows are audit history and have to stay
    readable. And any activation still open points at a candidate registered against the old chain,
    whose `compile_receipt` row no longer validates — so it is closed once, durably, with a legible
    reason, rather than being re-tripped by every worker start.

    The `program_v7` epoch must survive byte for byte, `baseline_program_sha256` included: re-opening it
    would discard accepted `news_review_v4` truth and move the `starts_at_ms` floor that bounds every
    learning window and release cohort.
    """

    conn: Any | None = None
    try:
        _fresh_schema_at("20260824_0304")
        conn = connect_postgres_test(read_only=False)
        prior_epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        assert set(prior_epochs) >= {"program_v7"}

        # The kind does not exist yet, which is the whole reason this migration is a schema change.
        with pytest.raises(CheckViolation, match="news_learning_artifact_kind"):
            conn.execute(
                "INSERT INTO news_learning_artifacts "
                "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
                "VALUES (%s, 'compile_record', NULL, %s::jsonb, 'test', %s)",
                ("1" * 64, json.dumps({"schema_version": "news_program_compile_record_v1"}), 1),
            )
        conn.rollback()

        # One row of the retired chain, written before the cut. It is audit history: the migration must
        # keep the kind admissible and must not rewrite the payload.
        retired_payload = {"schema_version": "tracefold.news.compile_receipt_chain.v3", "receipts": []}
        conn.execute(
            "INSERT INTO news_learning_artifacts "
            "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
            "VALUES (%s, 'compile_receipt', NULL, %s::jsonb, 'test', %s)",
            ("2" * 64, json.dumps(retired_payload, sort_keys=True), 1),
        )

        settled_at_ms = int(time.time() * 1000) - 60_000
        # `ux_news_canary_one_open` permits a single armed-or-active row, so the open activation and the
        # already-closed one it must not touch are the whole surface of this migration.
        conn.execute(
            """
            INSERT INTO news_canary_activations (
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
              state, revision, trip_reason, tripped_at_ms, created_at_ms
            ) VALUES (%s, %s, %s, %s, 'news_canary_selector_v1', 1000, %s, %s, 'tripped', 5, %s, %s, %s)
            """,
            (
                "c" * 32,
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "4" * 64,
                "5" * 64,
                "earlier_operator_trip",
                settled_at_ms,
                settled_at_ms,
            ),
        )
        conn.execute(
            """
            INSERT INTO news_canary_activations (
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
              state, revision, created_at_ms
            ) VALUES (%s, %s, %s, %s, 'news_canary_selector_v1', 1000, %s, %s, 'armed', 7, %s)
            """,
            (
                "d" * 32,
                "6" * 64,
                "7" * 64,
                "8" * 64,
                "9" * 64,
                "e" * 64,
                settled_at_ms,
            ),
        )
        conn.commit()
        conn.close()
        conn = None

        deployed_after_ms = int(time.time() * 1000) - 5_000
        _upgrade("head")
        deployed_before_ms = int(time.time() * 1000) + 5_000

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260828_0317"

        epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        assert epochs == prior_epochs
        assert epochs["program_v7"]["baseline_program_sha256"] == (
            "7a460f8d3812c64c6ee38158871eb9f060811e5ffe87f399f7bc2e506b4e28ad"
        )
        assert epochs["program_v7"]["program_factory_id"] == "tracefold.news.program.factory_v5"
        assert epochs["program_v7"]["artifact_schema_version"] == "news_semantic_program_artifact_v2"

        retired = conn.execute(
            "SELECT kind, payload FROM news_learning_artifacts WHERE artifact_sha = %s",
            ("2" * 64,),
        ).fetchone()
        assert (retired["kind"], retired["payload"]) == ("compile_receipt", retired_payload)

        # The new kind is admitted; nothing else is.
        record_payload = {"schema_version": "news_program_compile_record_v1"}
        conn.execute(
            "INSERT INTO news_learning_artifacts "
            "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
            "VALUES (%s, 'compile_record', NULL, %s::jsonb, 'test', %s)",
            ("3" * 64, json.dumps(record_payload), 1),
        )
        assert (
            conn.execute(
                "SELECT kind FROM news_learning_artifacts WHERE artifact_sha = %s",
                ("3" * 64,),
            ).fetchone()["kind"]
            == "compile_record"
        )
        with pytest.raises(CheckViolation, match="news_learning_artifact_kind"):
            conn.execute(
                "INSERT INTO news_learning_artifacts "
                "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
                "VALUES (%s, 'compile_receipt_chain', NULL, %s::jsonb, 'test', %s)",
                ("4" * 64, json.dumps(record_payload), 1),
            )
        conn.rollback()

        activations = {
            row["activation_id"]: dict(row)
            for row in conn.execute(
                "SELECT activation_id, state, revision, trip_reason, tripped_at_ms FROM news_canary_activations"
            ).fetchall()
        }
        assert activations["c" * 32] == {
            "activation_id": "c" * 32,
            "state": "tripped",
            "revision": 5,
            "trip_reason": "earlier_operator_trip",
            "tripped_at_ms": settled_at_ms,
        }
        open_activation = activations["d" * 32]
        assert open_activation["state"] == "tripped"
        assert open_activation["revision"] == 8
        assert open_activation["trip_reason"] == _COMPILE_RECORD_TRIP_REASON
        assert deployed_after_ms <= open_activation["tripped_at_ms"] <= deployed_before_ms
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM news_canary_activations WHERE state IN ('armed', 'active')"
            ).fetchone()["n"]
            == 0
        )

        # A serialization change is not an epoch reset, so this migration writes no ledger receipt of its
        # own. Later hard cuts may add receipts, so scope this historical assertion to the two migrations
        # whose boundary this test owns.
        receipts = conn.execute(
            "SELECT created_by FROM news_learning_artifacts WHERE kind = 'epoch_reset' "
            "AND created_by IN ('migration_20260824_0304', 'migration_20260825_0305') "
            "ORDER BY created_by"
        ).fetchall()
        assert [row["created_by"] for row in receipts] == ["migration_20260824_0304"]
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0305_to_0306_closes_an_activation_written_against_the_flat_compile_record() -> None:
    """#193 PR-C embeds the optimization and the spend, so a record written between `0305` and it is dead.

    `0305` shipped one day earlier with eleven flat fields; the record carries `run` and `spend` now, and
    `extra="forbid"` refuses the old shape outright. Without this migration a candidate registered in that
    window surfaces at evaluate time as `news_learning_program_compile_record_invalid` — corruption, rather
    than the hard cut it actually is. `schema_version` deliberately does not move: the document is still one
    trusted compile, and a bump would imply two readable shapes when there is one.
    """

    _fresh_schema_at("20260825_0305")
    conn = connect_postgres_test(read_only=False)
    try:
        settled_at_ms = int(time.time() * 1000) - 60_000
        # Armed *after* `0305` ran, so it is `0306`'s to close and nothing else's.
        conn.execute(
            """
            INSERT INTO news_canary_activations (
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
              state, revision, created_at_ms
            ) VALUES (%s, %s, %s, %s, 'news_canary_selector_v1', 1000, %s, %s, 'armed', 3, %s)
            """,
            ("f" * 32, "1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, settled_at_ms),
        )
        prior_epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        conn.commit()
        conn.close()
        conn = None

        deployed_after_ms = int(time.time() * 1000) - 5_000
        _upgrade("head")
        deployed_before_ms = int(time.time() * 1000) + 5_000

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260828_0317"

        activation = dict(
            conn.execute(
                "SELECT state, revision, trip_reason, tripped_at_ms FROM news_canary_activations "
                "WHERE activation_id = %s",
                ("f" * 32,),
            ).fetchone()
        )
        assert activation["state"] == "tripped"
        assert activation["revision"] == 4
        # `_upgrade("head")` runs 0307 after this, and it finds nothing armed to trip: each hard cut closes
        # what it is about, once. An activation cannot be tripped twice.
        assert activation["trip_reason"] == _RUN_SPEND_TRIP_REASON
        assert deployed_after_ms <= activation["tripped_at_ms"] <= deployed_before_ms

        # Accepted `news_review_v4` truth does not depend on how a compile is serialized.
        epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        assert epochs == prior_epochs
    finally:
        if conn is not None:
            conn.close()


def test_0306_to_0307_admits_the_prompt_candidate_and_closes_the_compile_chain_registrations() -> None:
    """#202: one candidate lifecycle, so the two-lifecycle rows become audit and nothing else.

    Two facts are database facts here. `news_learning_artifacts` gains `prompt_candidate` while keeping
    `compile_receipt` and `compile_record` in the constraint — old rows are append-only history and must
    stay readable (§10.3) — and any activation still open is closed, because the manifest it points at no
    longer parses: `target: program | policy` is gone.
    """

    _fresh_schema_at("20260825_0306")
    conn = connect_postgres_test(read_only=False)
    try:
        settled_at_ms = int(time.time() * 1000) - 60_000
        conn.execute(
            """
            INSERT INTO news_canary_activations (
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
              state, revision, created_at_ms
            ) VALUES (%s, %s, %s, %s, 'news_canary_selector_v1', 1000, %s, %s, 'active', 2, %s)
            """,
            ("e" * 32, "1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, settled_at_ms),
        )
        # One row of the retired kind, so the constraint's backward readability is a fact rather than a claim.
        conn.execute(
            "INSERT INTO news_learning_artifacts (artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
            "VALUES (%s, 'compile_record', NULL, %s::jsonb, 'test', %s)",
            ("9" * 64, json.dumps({"schema_version": "news_program_compile_record_v1"}), 1),
        )
        prior_epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        conn.commit()
        conn.close()
        conn = None

        deployed_after_ms = int(time.time() * 1000) - 5_000
        _upgrade("head")
        deployed_before_ms = int(time.time() * 1000) + 5_000

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260828_0317"

        activation = dict(
            conn.execute(
                "SELECT state, revision, trip_reason, tripped_at_ms FROM news_canary_activations "
                "WHERE activation_id = %s",
                ("e" * 32,),
            ).fetchone()
        )
        assert activation["state"] == "tripped"
        assert activation["revision"] == 3
        assert activation["trip_reason"] == _PROMPT_CANDIDATE_TRIP_REASON
        assert deployed_after_ms <= activation["tripped_at_ms"] <= deployed_before_ms

        # The new kind is writable; the retired one is still readable and still refuses to be re-armed by
        # anything, because the manifest that names it no longer parses.
        conn.execute(
            "INSERT INTO news_learning_artifacts (artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
            "VALUES (%s, 'prompt_candidate', NULL, %s::jsonb, 'test', %s)",
            ("a" * 64, json.dumps({"schema_version": "news_prompt_candidate_v1"}), 1),
        )
        assert (
            conn.execute("SELECT kind FROM news_learning_artifacts WHERE artifact_sha = %s", ("9" * 64,)).fetchone()[
                "kind"
            ]
            == "compile_record"
        )

        # Accepted `news_review_v4` truth does not depend on how a candidate is serialized, and #199's
        # frozen bundle keeps its start.
        epochs = {
            row["epoch_id"]: dict(row)
            for row in conn.execute("SELECT * FROM news_learning_epochs ORDER BY starts_at_ms, epoch_id").fetchall()
        }
        assert epochs == prior_epochs
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()
