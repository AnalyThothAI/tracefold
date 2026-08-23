from __future__ import annotations

import json
import time
from typing import Any

from alembic import command

from tests.postgres_test_utils import (
    connect_postgres_test,
    reset_postgres_schema,
)
from tests.postgres_test_utils import (
    test_postgres_dsn as postgres_test_dsn,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.agents.semantic_program import load_stable_program_artifact
from tracefold.platform.postgres.postgres_migrations import alembic_config


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
        assert revision["version_num"] == "20260823_0301"
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
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260823_0301"
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
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260823_0301"
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
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260823_0301"
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
        assert len(epochs) == 6
        for epoch_id, prior in prior_epochs.items():
            assert dict(next(row for row in epochs if row["epoch_id"] == epoch_id)) == prior
        program_v6 = next(row for row in epochs if row["epoch_id"] == "program_v6")
        assert deployed_after_ms <= program_v6["starts_at_ms"] <= deployed_before_ms
        assert program_v6["starts_at_ms"] > prior_epochs["program_v5"]["starts_at_ms"]
        assert program_v6["source_issue"] == "https://github.com/AnalyThothAI/tracefold/issues/160"
        assert program_v6["program_factory_id"] == "tracefold.news.semantic_program.factory_v4"
        assert program_v6["artifact_schema_version"] == "news_semantic_program_artifact_v2"
        assert program_v6["baseline_program_version"] == "news_semantic_program_v4"
        assert program_v6["baseline_program_sha256"] == load_stable_program_artifact().program_sha256
        assert program_v6["prior_evidence_disposition"] == "audit_only"
        assert program_v6["reset_reason"] == "trade_relevance_editorial_authority_hard_cut"
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
                fingerprint="same-fingerprint",
                now_ms=program_v6["starts_at_ms"],
            )
            is None
        )
        assert (
            news.find_artifact_event(
                source_artifact_id="x:12345",
                family="general",
                fingerprint="same-fingerprint",
                item_id="new-item",
                opened_after_ms=program_v6["starts_at_ms"] - 7 * 24 * 3_600_000,
            )
            is None
        )
        assert (
            news.find_band_candidates(
                family="general",
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
