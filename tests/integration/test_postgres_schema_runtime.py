from __future__ import annotations

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.news import (
    STORY_IDENTITY_VERSION,
    NewsFeedEntry,
    NewsRepository,
    NewsSourceDefinition,
)
from tracefold.platform.postgres.postgres_migrations import alembic_config, latest_migration_version

RETIRED_BACKEND_TABLES = {
    "projection_runs",
    "projection_offsets",
    "pulse_agent_eval_results",
    "pulse_agent_eval_cases",
    "pulse_evidence_packets",
    "pulse_agent_run_steps",
    "pulse_playbook_snapshots",
    "pulse_candidates",
    "pulse_agent_runs",
    "pulse_agent_jobs",
    "pulse_agent_runtime_versions",
    "pulse_candidate_edge_state",
    "pulse_candidate_run_budget",
    "pulse_target_run_budget",
    "pulse_trigger_dirty_targets",
    "narrative_admissions",
    "narrative_admission_dirty_targets",
    "macro_daily_briefs",
    "macro_import_runs",
    "macro_observations",
    "macro_projection_dirty_targets",
    "macro_observation_series_rows",
    "macro_observation_series_publication_state",
    "macro_view_snapshots",
    "macro_judgment_jobs",
    "macro_judgment_publications",
    "macro_judgment_outcomes",
    "macro_sync_runs",
    "macro_sync_state",
    "macro_sync_windows",
    "cex_oi_radar_publication_state",
    "cex_oi_radar_rows",
    "cex_detail_snapshots",
    "account_profiles",
    "account_token_call_stats",
    "account_quality_snapshots",
    "news_item_agent_briefs",
    "news_item_agent_runs",
    "news_source_quality_rows",
    "token_radar_source_dirty_events",
    "market_tick_current_dirty_targets",
    "token_capture_tier_dirty_targets",
    "token_capture_tier",
    "news_story_agent_briefs",
    "news_story_agent_runs",
}
PROFESSIONAL_NEWS_TABLES = {
    "news_sources",
    "news_source_memberships",
    "news_source_fetches",
    "news_feed_observations",
    "news_items",
    "news_stories",
    "news_story_members",
    "news_story_aliases",
    "news_brief_runs",
    "news_brief_publications",
    "news_brief_current",
}
LEGACY_NEWS_TABLES = {
    "news_fetch_runs",
    "news_provider_items",
    "news_item_entities",
    "news_token_mentions",
    "news_fact_candidates",
    "news_item_observation_edges",
    "news_projection_dirty_targets",
    "news_page_rows",
    "news_story_articles",
    "news_story_analyses",
    "news_story_analysis_attempts",
    "news_brief_selection_snapshots",
    "news_articles",
    "news_article_revisions",
    "news_story_memberships",
    "news_story_profiles",
    "news_story_identity_decisions",
    "news_story_material_events",
    "news_narrative_grouping_snapshots",
    "news_brief_selections",
    "news_brief_proposals",
    "news_brief_activations",
    "news_brief_active",
    "news_story_analysis_requests",
    "news_ai_attempts",
    "news_ai_current_targets",
    "news_brief_activation_analysis",
    "news_story_analysis_publications",
    "news_story_analysis_current",
}


def test_current_postgres_schema_has_one_kappa_truth_and_durable_macro_research(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        tables = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        macro_research_run_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'macro_research_runs'
                """
            ).fetchall()
        }
        news_source_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'news_sources'
                """
            ).fetchall()
        }
        macro_research_publication_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'macro_research_publications'
                """
            ).fetchall()
        }
        market_current_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'market_tick_current'
                """
            ).fetchall()
        }
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
    finally:
        conn.close()

    assert {
        "raw_frames",
        "events",
        "token_intents",
        "token_intent_resolutions",
        "market_ticks",
        "enriched_events",
        "token_radar_current_rows",
        "token_radar_publication_state",
        "account_token_alerts",
        "market_instruments",
        "market_observations",
        "market_settlements",
        "market_position_facts",
        "macro_series_facts",
        "macro_release_facts",
        "macro_documents",
        "macro_source_receipts",
        "macro_acquisition_targets",
        "macro_feature_series",
        "macro_module_current",
        "macro_evidence_packs",
        "macro_daily_judgments",
        "macro_event_updates",
        "macro_research_runs",
        "macro_research_publications",
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    } <= tables
    assert RETIRED_BACKEND_TABLES.isdisjoint(tables)
    assert tables >= PROFESSIONAL_NEWS_TABLES
    assert LEGACY_NEWS_TABLES.isdisjoint(tables)
    assert macro_research_run_columns == {
        "session_date",
        "market_cutoff_ms",
        "evidence_pack_id",
        "status",
        "sealed_at_ms",
        "attempt_count",
        "max_attempts",
        "due_at_ms",
        "leased_until_ms",
        "lease_owner",
        "reviewer_disposition",
        "last_error_code",
        "last_error_message",
        "created_at_ms",
        "updated_at_ms",
    }
    assert news_source_columns == {
        "source_id",
        "name",
        "feed_url",
        "tier",
        "lang",
        "enabled",
        "refresh_interval_seconds",
        "etag",
        "last_modified",
        "last_fetch_started_at_ms",
        "last_fetch_finished_at_ms",
        "last_success_at_ms",
        "last_http_status",
        "consecutive_failures",
        "last_error",
        "next_fetch_at_ms",
        "created_at_ms",
        "updated_at_ms",
    }
    assert macro_research_publication_columns == {
        "session_date",
        "market_cutoff_ms",
        "evidence_pack_id",
        "artifact_json",
        "report_markdown",
        "audit_json",
        "reviewer_disposition",
        "model_name",
        "prompt_version",
        "workflow_version",
        "artifact_hash",
        "published_at_ms",
    }
    assert {"raw_payload_json", "payload_hash"}.isdisjoint(market_current_columns)
    assert version == latest_migration_version() == "20260727_0206"


@pytest.mark.skip(reason="superseded historical migration contract")
def test_news_professional_hard_cut_preserves_source_config_and_resets_old_facts(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = _test_postgres_dsn()
        command.upgrade(config, "20260726_0197")
        conn.execute(
            """
            INSERT INTO news_sources (
              source_id, name, feed_url, source_domain, source_role, trust_tier,
              source_chain_id, coverage_tags, default_language, enabled,
              refresh_interval_seconds, etag, last_modified,
              last_fetch_started_at_ms, last_fetch_finished_at_ms,
              last_success_at_ms, last_http_status, consecutive_failures,
              last_error, next_fetch_at_ms, created_at_ms, updated_at_ms
            )
            VALUES (
              'configured-source', 'Configured Source',
              'https://source.example/feed.xml', 'source.example',
              'original_publisher', 'trusted', 'publisher-org',
              '["macro", "policy"]'::jsonb, 'en', false, 123,
              'old-etag', 'old-last-modified', 10, 11, 11, 200, 2,
              'old-runtime-error', 999, 1, 2
            );
            INSERT INTO news_articles (
              article_id, source_id, identity_version, identity_method,
              identity_key, source_guid, canonical_url, title, snippet,
              published_at_ms, first_seen_at_ms, last_seen_at_ms, language,
              origin_url, origin_domain, origin_name, provenance_status,
              content_hash, source_entry
            )
            VALUES (
              'old-article', 'configured-source', 'old-identity',
              'canonical_url', 'old-key', 'old-guid',
              'https://source.example/old', 'Old fact', '', 10, 10, 10, 'en',
              NULL, NULL, NULL, 'verified', 'old-content-hash', '{}'::jsonb
            )
            """
        )
        conn.commit()

        command.upgrade(config, "head")

        source_row = conn.execute("SELECT * FROM news_sources WHERE source_id = 'configured-source'").fetchone()
        article_count = conn.execute("SELECT count(*) AS count FROM news_articles").fetchone()["count"]
        legacy_tables = {
            row["table_name"]
            for row in conn.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema = 'public'
                """
            ).fetchall()
        } & {
            "news_story_articles",
            "news_story_analyses",
            "news_story_analysis_attempts",
        }
    finally:
        conn.close()

    assert {
        "name": source_row["name"],
        "feed_url": source_row["feed_url"],
        "source_domain": source_row["source_domain"],
        "source_role": source_row["source_role"],
        "trust_tier": source_row["trust_tier"],
        "source_chain_id": source_row["source_chain_id"],
        "coverage_tags": source_row["coverage_tags"],
        "default_language": source_row["default_language"],
        "enabled": source_row["enabled"],
        "refresh_interval_seconds": source_row["refresh_interval_seconds"],
    } == {
        "name": "Configured Source",
        "feed_url": "https://source.example/feed.xml",
        "source_domain": "source.example",
        "source_role": "original_publisher",
        "trust_tier": "trusted",
        "source_chain_id": "publisher-org",
        "coverage_tags": ["macro", "policy"],
        "default_language": "en",
        "enabled": False,
        "refresh_interval_seconds": 123,
    }
    assert source_row["publisher_organization_id"] == "publisher-org"
    assert source_row["canonical_domains"] == ["source.example"]
    assert source_row["registry_version"] == "news_source_registry_v2"
    assert source_row["etag"] is None
    assert source_row["last_error"] is None
    assert source_row["consecutive_failures"] == 0
    assert source_row["next_fetch_at_ms"] == 0
    assert article_count == 0
    assert legacy_tables == set()


@pytest.mark.skip(reason="superseded historical migration contract")
def test_news_correctness_hard_cut_preserves_material_facts_and_rebuilds_identity_v2(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = _test_postgres_dsn()
        command.upgrade(config, "20260726_0198")

        repository = NewsRepository(conn)
        source = NewsSourceDefinition(
            source_id="preserved-source",
            name="Preserved Source",
            feed_url="https://preserved.example/feed.xml",
            source_domain="preserved.example",
            source_role="original_publisher",
            trust_tier="trusted",
            source_chain_id="preserved-publisher",
            publisher_organization_id="preserved-publisher",
            default_language="en",
            refresh_interval_seconds=60,
        )
        repository.sync_sources((source,), now_ms=100)
        repository.record_fetch_success(
            source=source,
            entries=(
                NewsFeedEntry(
                    guid="preserved-guid",
                    link="https://preserved.example/policy-decision",
                    title="Government implements a preserved policy decision",
                    summary="The material fact must survive the derived-state hard cut.",
                    published_at_ms=100,
                    language="en",
                ),
            ),
            started_at_ms=100,
            finished_at_ms=101,
            status_code=200,
            etag="preserved-etag",
            last_modified="preserved-last-modified",
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=200, limit=100)
        projected_story_id = str(conn.execute("SELECT story_id FROM news_stories").fetchone()["story_id"])
        columns = [
            str(row["column_name"])
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'news_stories'
                 ORDER BY ordinal_position
                """
            ).fetchall()
        ]
        insert_columns = ", ".join(f'"{column}"' for column in columns)
        select_columns = ", ".join(
            "'legacy-story-id' AS story_id"
            if column == "story_id"
            else "'news_story_identity_v1' AS identity_version"
            if column == "identity_version"
            else f'"{column}"'
            for column in columns
        )
        conn.execute(
            f"""
            INSERT INTO news_stories ({insert_columns})
            SELECT {select_columns}
              FROM news_stories
             WHERE story_id = %s
            """,
            (projected_story_id,),
        )
        for table, column in (
            ("news_story_memberships", "story_id"),
            ("news_story_profiles", "story_id"),
            ("news_story_identity_decisions", "selected_story_id"),
            ("news_story_material_events", "story_id"),
            ("news_story_analysis_requests", "story_id"),
            ("news_story_analysis_publications", "story_id"),
            ("news_story_analysis_current", "story_id"),
        ):
            conn.execute(
                f"UPDATE {table} SET {column} = 'legacy-story-id' WHERE {column} = %s",
                (projected_story_id,),
            )
        conn.execute(
            "DELETE FROM news_stories WHERE story_id = %s",
            (projected_story_id,),
        )
        conn.execute(
            """
            INSERT INTO news_narrative_grouping_snapshots (
              grouping_snapshot_id, input_hash, policy_version, embedding_model,
              fallback_used, groups, receipt, cutoff_at_ms, created_at_ms
            )
            VALUES (
              'legacy-grouping', 'legacy-input', 'legacy-grouping-policy', NULL,
              true, '[]'::jsonb, '{}'::jsonb, 200, 200
            );
            INSERT INTO news_brief_selection_snapshots (
              selection_snapshot_id, selection_fingerprint, grouping_snapshot_id,
              policy_version, cutoff_at_ms, selected_story_ids, decisions,
              critical, evidence_bundle_hash, evidence_bundle, status,
              publish_after_ms, created_at_ms, updated_at_ms
            )
            VALUES (
              'legacy-selection', 'legacy-fingerprint', 'legacy-grouping',
              'legacy-selection-policy', 200, '["legacy-story-id"]'::jsonb,
              '[]'::jsonb, false, 'legacy-bundle-hash', '{}'::jsonb,
              'published', 200, 200, 200
            );
            INSERT INTO news_brief_publications (
              publication_id, selection_snapshot_id, selection_fingerprint,
              evidence_bundle_hash, model, prompt_version, workflow_version,
              schema_version, locale, payload, evidence_references, receipt,
              published_at_ms
            )
            VALUES (
              'legacy-publication', 'legacy-selection', 'legacy-fingerprint',
              'legacy-bundle-hash', 'legacy-model', 'legacy-prompt',
              'legacy-workflow', 'legacy-schema', 'zh-CN', '{}'::jsonb,
              '[]'::jsonb, '{}'::jsonb, 200
            );
            INSERT INTO news_brief_current (
              singleton_key, publication_id, updated_at_ms
            )
            VALUES (true, 'legacy-publication', 200)
            """
        )
        material_counts_before = {
            table: int(conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"])
            for table in (
                "news_sources",
                "news_fetch_receipts",
                "news_feed_observations",
                "news_articles",
                "news_article_revisions",
                "news_article_content_snapshots",
            )
        }
        conn.commit()

        command.upgrade(config, "head")

        material_counts_after = {
            table: int(conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"])
            for table in material_counts_before
        }
        tables = {
            str(row["table_name"])
            for row in conn.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema = 'public'
                """
            ).fetchall()
        }
        assert material_counts_after == material_counts_before
        assert {"news_brief_selection_snapshots", "news_brief_current"}.isdisjoint(tables)
        assert {
            "news_brief_selections",
            "news_brief_proposals",
            "news_brief_activations",
            "news_brief_active",
            "news_brief_activation_analysis",
            "news_ai_current_targets",
        } <= tables
        assert "schema_migration_backup_receipts" not in tables
        attachment_columns = {
            str(row["column_name"]): str(row["is_nullable"])
            for row in conn.execute(
                """
                SELECT column_name, is_nullable
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'news_brief_activation_analysis'
                """
            ).fetchall()
        }
        assert attachment_columns["superseded_at_ms"] == "YES"
        current_attachment_index = conn.execute(
            """
            SELECT indexdef
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND tablename = 'news_brief_activation_analysis'
               AND indexname = 'ux_news_brief_activation_analysis_current'
            """
        ).fetchone()
        assert current_attachment_index is not None
        assert "WHERE (superseded_at_ms IS NULL)" in str(current_attachment_index["indexdef"])
        assert conn.execute("SELECT count(*) AS count FROM news_stories").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM news_brief_publications").fetchone()["count"] == 0

        rebuilt = NewsRepository(conn).project_pending_revisions(now_ms=300, limit=100)
        rebuilt_story = conn.execute("SELECT story_id, identity_version FROM news_stories").fetchone()
        assert rebuilt["processed"] == 1
        assert rebuilt_story["story_id"] != "legacy-story-id"
        assert rebuilt_story["identity_version"] == STORY_IDENTITY_VERSION
        assert (
            conn.execute(
                """
                SELECT count(*) AS count
                  FROM news_stories
                 WHERE story_id = 'legacy-story-id'
                """
            ).fetchone()["count"]
            == 0
        )
        conn.commit()

        command.upgrade(config, "head")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260727_0203"
    finally:
        conn.close()


def test_backend_kiss_hard_cut_migrates_nonempty_0184_state(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = _test_postgres_dsn()
        command.upgrade(config, "20260721_0184")

        conn.execute(
            """
            INSERT INTO projection_runs(
              run_id, projection_name, projection_version, mode, status, started_at_ms
            ) VALUES ('run-old', 'token_radar', 'v1', 'incremental', 'ready', 1);
            INSERT INTO projection_offsets(
              projection_name, projection_version, source_table, status, created_at_ms, updated_at_ms
            ) VALUES ('token_radar', 'v1', 'events', 'ready', 1, 1);

            INSERT INTO news_sources(
              source_id, provider_type, feed_url, source_domain, source_name,
              created_at_ms, updated_at_ms
            ) VALUES (
              'source-1', 'rss', 'https://example.test/feed', 'example.test', 'Example', 1, 1
            );
            INSERT INTO news_fetch_runs(
              fetch_run_id, source_id, started_at_ms, finished_at_ms, status
            ) VALUES ('fetch-old', 'source-1', 1, 2, 'success');
            INSERT INTO news_provider_items(
              provider_item_id, source_id, fetch_run_id, source_item_key, canonical_url,
              payload_hash, raw_payload_json, fetched_at_ms
            ) VALUES (
              'provider-item-old', 'source-1', 'fetch-old', 'source-item-old',
              'https://example.test/item-old', 'payload-old', '{}'::jsonb, 2
            );

            INSERT INTO token_radar_dirty_targets(
              target_type_key, identity_id, dirty_reason, payload_hash, due_at_ms,
              first_dirty_at_ms, updated_at_ms, market_dirty, repair_dirty
            ) VALUES (
              'Asset', 'asset-1', 'market_tick_written', 'target-hash', 200,
              100, 200, true, false
            );
            INSERT INTO token_radar_source_dirty_events(
              projection_version, source_event_id, target_type_key, identity_id,
              dirty_reason, payload_hash, due_at_ms, first_dirty_at_ms, updated_at_ms
            ) VALUES (
              'token-radar-v13-social-attention', 'event-1', 'Asset', 'asset-1',
              'resolution_updated', 'source-hash', 150, 50, 250
            );
            INSERT INTO worker_queue_terminal_events(
              terminal_id, worker_name, source_table, target_key, source_row_json,
              source_row_hash, final_status, final_reason, terminalized_at_ms
            ) VALUES (
              'terminal-source-queue', 'token_radar_projection',
              'token_radar_source_dirty_events', 'event-1:Asset:asset-1',
              '{}'::jsonb, 'source-row-hash', 'failed', 'retry_budget_exhausted', 1
            );

            """
        )
        conn.commit()

        command.upgrade(config, "head")

        tables = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        queue_row = conn.execute(
            """
            SELECT dirty_reason, due_at_ms, first_dirty_at_ms, updated_at_ms,
                   leased_until_ms, attempt_count, last_error, market_dirty, repair_dirty
            FROM token_radar_dirty_targets
            WHERE target_type_key = 'Asset' AND identity_id = 'asset-1'
            """
        ).fetchone()
        terminal_row = conn.execute(
            """
            SELECT operator_action, operator_reason
            FROM worker_queue_terminal_events
            WHERE terminal_id = 'terminal-source-queue'
            """
        ).fetchone()
        news_source_count = conn.execute("SELECT COUNT(*) AS count FROM news_sources").fetchone()["count"]
    finally:
        conn.close()

    assert RETIRED_BACKEND_TABLES.isdisjoint(tables)
    assert queue_row["dirty_reason"] == "mixed"
    assert tuple(queue_row[key] for key in ("due_at_ms", "first_dirty_at_ms", "updated_at_ms")) == (150, 50, 250)
    assert queue_row["leased_until_ms"] is None
    assert queue_row["attempt_count"] == 0
    assert queue_row["last_error"] is None
    assert queue_row["market_dirty"] is True
    assert queue_row["repair_dirty"] is False
    assert tuple(terminal_row.values()) == ("archive", "queue_retired_by_0185")
    assert tables >= PROFESSIONAL_NEWS_TABLES
    assert LEGACY_NEWS_TABLES.isdisjoint(tables)
    assert news_source_count == 0


def test_runtime_hard_cut_reconciles_nonempty_0185_backlog(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = _test_postgres_dsn()
        command.upgrade(config, "20260721_0185")

        conn.execute(
            """
            INSERT INTO registry_assets(
              asset_id, chain_id, token_standard, address, status, first_seen_at_ms, updated_at_ms
            ) VALUES
              (
                'asset-market-backlog', 'eip155:1', 'erc20', '0xabc',
                'canonical', 100, 100
              ),
              (
                'asset-terminal-backlog', 'eip155:1', 'erc20', '0xdef',
                'canonical', 100, 100
              ),
              (
                'asset-quarantined-backlog', 'eip155:1', 'erc20', '0xbad',
                'canonical', 100, 100
              );

            INSERT INTO market_ticks(
              observed_at_ms, tick_id, target_type, target_id, chain, token_address,
              source_tier, source_provider, received_at_ms, price_usd, raw_payload_json,
              payload_hash, created_at_ms
            ) VALUES
              (
                100, 'tick-old', 'chain_token', 'eip155:1:0xabc', 'eip155:1', '0xabc',
                'tier2_poll', 'okx_dex_rest', 110, 1.00, '{"version":"old"}'::jsonb,
                'hash-old', 111
              ),
              (
                200, 'tick-new', 'chain_token', 'eip155:1:0xabc', 'eip155:1', '0xabc',
                'tier2_poll', 'okx_dex_rest', 210, 2.00, '{"version":"new"}'::jsonb,
                'hash-new', 211
              ),
              (
                300, 'tick-terminal-only', 'chain_token', 'eip155:1:0xdef', 'eip155:1', '0xdef',
                'tier2_poll', 'okx_dex_rest', 310, 3.00, '{"version":"terminal"}'::jsonb,
                'hash-terminal', 311
              ),
              (
                400, 'tick-quarantined', 'chain_token', 'eip155:1:0xbad', 'eip155:1', '0xbad',
                'tier2_poll', 'okx_dex_rest', 410, 4.00, '{"version":"quarantined"}'::jsonb,
                'hash-quarantined', 411
              );

            INSERT INTO market_tick_current(
              target_type, target_id, tick_observed_at_ms, tick_id, source_tier,
              source_provider, chain, token_address, price_usd, raw_payload_json,
              payload_hash, updated_at_ms, created_at_ms
            ) VALUES (
              'chain_token', 'eip155:1:0xabc', 200, 'tick-new', 'tier2_poll',
              'okx_dex_rest', 'eip155:1', '0xabc', 999.00, '{"version":"corrupt"}'::jsonb,
              'hash-corrupt', 210, 211
            );

            INSERT INTO market_tick_current_dirty_targets(
              target_type, target_id, dirty_reason, payload_hash, due_at_ms,
              source_watermark_ms, priority, first_dirty_at_ms, updated_at_ms
            ) VALUES (
              'chain_token', 'eip155:1:0xabc', 'market_tick_written', 'dirty-new',
              200, 210, 0, 200, 210
            );

            INSERT INTO token_radar_dirty_targets(
              target_type_key, identity_id, dirty_reason, market_dirty, repair_dirty,
              payload_hash, due_at_ms, first_dirty_at_ms, updated_at_ms
            ) VALUES (
              'Asset', 'asset-market-backlog', 'ingest_resolution', false, false,
              'social-dirty-before-market', 150, 150, 150
            );

            INSERT INTO worker_queue_terminal_events(
              terminal_id, worker_name, source_table, target_key, source_row_json,
              source_row_hash, final_status, final_reason, attempt_count,
              payload_hash, terminalized_at_ms
            ) VALUES (
              'terminal-market-current-only', 'market_tick_current_projection',
              'market_tick_current_dirty_targets', 'chain_token:eip155:1:0xdef',
              '{"target_type":"chain_token","target_id":"eip155:1:0xdef"}'::jsonb,
              'terminal-source-hash', 'terminal', 'retry_budget_exhausted', 5,
              'dirty-terminal', 320
            );

            INSERT INTO worker_queue_terminal_events(
              terminal_id, worker_name, source_table, target_key, source_row_json,
              source_row_hash, final_status, final_reason, attempt_count,
              payload_hash, terminalized_at_ms, operator_action, operator_reason,
              operator_action_at_ms
            ) VALUES (
              'terminal-market-current-quarantined', 'market_tick_current_projection',
              'market_tick_current_dirty_targets', 'chain_token:eip155:1:0xbad',
              '{"target_type":"chain_token","target_id":"eip155:1:0xbad"}'::jsonb,
              'quarantined-source-hash', 'terminal', 'malformed_source', 5,
              'dirty-quarantined', 420, 'quarantine', 'operator_known_bad', 421
            );

            """
        )
        conn.commit()

        command.upgrade(config, "head")

        current = conn.execute(
            """
            SELECT tick_id, tick_observed_at_ms, updated_at_ms, price_usd
            FROM market_tick_current
            WHERE target_type = 'chain_token' AND target_id = 'eip155:1:0xabc'
            """
        ).fetchone()
        radar_dirty = conn.execute(
            """
            SELECT dirty_reason, market_dirty, repair_dirty, leased_until_ms, attempt_count
            FROM token_radar_dirty_targets
            WHERE target_type_key = 'Asset' AND identity_id = 'asset-market-backlog'
            """
        ).fetchone()
        terminal_current = conn.execute(
            """
            SELECT tick_id, tick_observed_at_ms, updated_at_ms, price_usd
            FROM market_tick_current
            WHERE target_type = 'chain_token' AND target_id = 'eip155:1:0xdef'
            """
        ).fetchone()
        terminal_radar_dirty = conn.execute(
            """
            SELECT dirty_reason, market_dirty, repair_dirty, leased_until_ms, attempt_count
            FROM token_radar_dirty_targets
            WHERE target_type_key = 'Asset' AND identity_id = 'asset-terminal-backlog'
            """
        ).fetchone()
        terminal_evidence = conn.execute(
            """
            SELECT operator_action, operator_reason
            FROM worker_queue_terminal_events
            WHERE terminal_id = 'terminal-market-current-only'
            """
        ).fetchone()
        quarantined_current = conn.execute(
            """
            SELECT tick_id
            FROM market_tick_current
            WHERE target_type = 'chain_token' AND target_id = 'eip155:1:0xbad'
            """
        ).fetchone()
        quarantined_radar_dirty = conn.execute(
            """
            SELECT identity_id
            FROM token_radar_dirty_targets
            WHERE target_type_key = 'Asset' AND identity_id = 'asset-quarantined-backlog'
            """
        ).fetchone()
        quarantined_evidence = conn.execute(
            """
            SELECT operator_action, operator_reason
            FROM worker_queue_terminal_events
            WHERE terminal_id = 'terminal-market-current-quarantined'
            """
        ).fetchone()
        retired_queue = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'market_tick_current_dirty_targets'
            """
        ).fetchone()
    finally:
        conn.close()

    assert current == {
        "tick_id": "tick-new",
        "tick_observed_at_ms": 200,
        "updated_at_ms": 210,
        "price_usd": 2,
    }
    assert radar_dirty == {
        "dirty_reason": "mixed",
        "market_dirty": True,
        "repair_dirty": False,
        "leased_until_ms": None,
        "attempt_count": 0,
    }
    assert terminal_current == {
        "tick_id": "tick-terminal-only",
        "tick_observed_at_ms": 300,
        "updated_at_ms": 310,
        "price_usd": 3,
    }
    assert terminal_radar_dirty == {
        "dirty_reason": "market_tick_current_changed",
        "market_dirty": True,
        "repair_dirty": False,
        "leased_until_ms": None,
        "attempt_count": 0,
    }
    assert terminal_evidence == {
        "operator_action": "archive",
        "operator_reason": "queue_retired_by_0186",
    }
    assert quarantined_current is None
    assert quarantined_radar_dirty is None
    assert quarantined_evidence == {
        "operator_action": "quarantine",
        "operator_reason": "operator_known_bad",
    }
    assert retired_queue is None


def test_postgres_migrations_are_idempotent_at_current_head(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        migrate(conn)
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()

    assert [row["version_num"] for row in rows] == [latest_migration_version()]


def test_token_radar_factor_cache_hard_cut_requeues_and_clears_private_cache(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = _test_postgres_dsn()
        command.upgrade(config, "20260722_0187")

        conn.execute(
            """
            INSERT INTO token_radar_target_features(
              projection_version, "window", scope, lane, target_type_key, identity_id,
              target_type, target_id, latest_event_received_at_ms, factor_snapshot_json,
              source_event_ids_json, source_intent_ids_json, source_resolution_ids_json,
              payload_hash, last_scored_at_ms, created_at_ms, updated_at_ms,
              intent_json, resolution_json
            ) VALUES (
              'token-radar-v13-social-attention', '24h', 'all', 'resolved',
              'Asset', 'asset:feature', 'Asset', 'asset:feature', 100, '{}'::jsonb,
              '["event-feature"]'::jsonb, '["intent-feature"]'::jsonb,
              '["resolution-feature"]'::jsonb, 'old-feature-hash', 100, 100, 100,
              '{"intent_id":"intent-feature","event_id":"event-feature"}'::jsonb,
              '{"status":"EXACT","reason_codes":[],"candidate_ids":[],"lookup_keys":[]}'::jsonb
            );
            INSERT INTO token_radar_rank_source_events(
              projection_version, target_type_key, identity_id, source_kind, source_id,
              event_received_at_ms, projected_at_ms, source_payload_hash
            ) VALUES (
              'token-radar-v13-social-attention', 'CexToken', 'cex_token:EDGE',
              'resolution', 'resolution-edge', 100, 100, 'edge-hash'
            );
            INSERT INTO token_radar_dirty_targets(
              target_type_key, identity_id, dirty_reason, market_dirty, repair_dirty,
              payload_hash, due_at_ms, leased_until_ms, lease_owner, attempt_count,
              last_error, first_dirty_at_ms, updated_at_ms
            ) VALUES (
              'Asset', 'asset:feature', 'market_tick_written', true, false,
              'old-dirty-hash', 50, 999, 'old-worker', 7, 'old-error', 10, 20
            );
            """
        )
        conn.commit()

        command.upgrade(config, "20260722_0188")

        feature_count = conn.execute("SELECT count(*) AS count FROM token_radar_target_features").fetchone()
        dirty_rows = conn.execute(
            """
            SELECT target_type_key, identity_id, dirty_reason, market_dirty, repair_dirty,
                   due_at_ms, leased_until_ms, lease_owner, attempt_count, last_error
            FROM token_radar_dirty_targets
            WHERE (target_type_key, identity_id) IN (
              ('Asset', 'asset:feature'), ('CexToken', 'cex_token:EDGE')
            )
            ORDER BY target_type_key, identity_id
            """
        ).fetchall()
        rank_source_count = conn.execute("SELECT count(*) AS count FROM token_radar_rank_source_events").fetchone()
    finally:
        conn.close()

    assert feature_count == {"count": 0}
    assert dirty_rows[0] == {
        "target_type_key": "Asset",
        "identity_id": "asset:feature",
        "dirty_reason": "mixed",
        "market_dirty": True,
        "repair_dirty": True,
        "due_at_ms": 50,
        "leased_until_ms": None,
        "lease_owner": None,
        "attempt_count": 0,
        "last_error": None,
    }
    edge_due_at_ms = dirty_rows[1].pop("due_at_ms")
    assert edge_due_at_ms > 100
    assert dirty_rows[1] == {
        "target_type_key": "CexToken",
        "identity_id": "cex_token:EDGE",
        "dirty_reason": "schema_hard_cut_0188",
        "market_dirty": False,
        "repair_dirty": True,
        "leased_until_ms": None,
        "lease_owner": None,
        "attempt_count": 0,
        "last_error": None,
    }
    assert rank_source_count == {"count": 1}
