from __future__ import annotations

from tests.integration.test_news_brief_state_machine import (
    NOW_MS,
    ORDINARY_DEBOUNCE_MS,
    _entry,
    _plan,
    _seed_story,
    _source,
)
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news import NewsFeedEntry, NewsRepository


def test_news_health_is_running_for_closed_material_facts_and_due_source(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        _seed_story(
            repository,
            source_id="healthy-wire",
            title="Government implements a new semiconductor export policy",
            observed_at_ms=NOW_MS,
            impact_score=80,
        )

        health = repository.health_snapshot(now_ms=NOW_MS + 2_000)

        assert health["status"] == "running"
        assert health["reasons"] == []
        assert {name: layer["status"] for name, layer in health["layers"].items()} == {
            "source": "running",
            "material": "running",
            "brief": "running",
            "public": "running",
            "ai": "running",
        }
        conn.commit()
    finally:
        conn.close()


def test_news_health_treats_repeat_observation_and_multiple_revisions_as_closed(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source = _source("revision-closure-wire", "wire_service")
        repository.sync_sources((source,), now_ms=NOW_MS)
        initial = NewsFeedEntry(
            guid="policy-v1",
            link="https://revision-closure-wire.example/policy",
            title="Government implements a new semiconductor export policy",
            summary="The policy takes effect immediately.",
            published_at_ms=NOW_MS,
            language="en",
        )
        repeat_observation = initial.model_copy(update={"guid": "policy-v1-repeat"})
        changed_revision = initial.model_copy(
            update={
                "guid": "policy-v2",
                "summary": "The policy takes effect immediately and adds licensing details.",
            }
        )
        for offset_ms, item in enumerate((initial, repeat_observation, changed_revision)):
            observed_at_ms = NOW_MS + offset_ms * 1_000
            repository.record_fetch_success(
                source=source,
                entries=(item,),
                started_at_ms=observed_at_ms,
                finished_at_ms=observed_at_ms,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
        repository.project_pending_revisions(now_ms=NOW_MS + 3_000, limit=100)

        health = repository.health_snapshot(now_ms=NOW_MS + 4_000)

        assert conn.execute("SELECT count(*) AS count FROM news_feed_observations").fetchone()["count"] == 3
        assert conn.execute("SELECT count(*) AS count FROM news_article_revisions").fetchone()["count"] == 2
        assert conn.execute("SELECT count(*) AS count FROM news_story_memberships").fetchone()["count"] == 1
        assert health["layers"]["material"]["status"] == "running"
        assert _reason_or_none(health, "observation_revision_orphan") is None
        assert _reason_or_none(health, "primary_membership_invariant") is None
        conn.commit()
    finally:
        conn.close()


def test_news_health_material_event_tie_break_matches_projector_order(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source = _source("financial-times", "original_publisher")
        observed_at_ms = 1_785_080_085_523
        repository.sync_sources((source,), now_ms=observed_at_ms)
        repository.record_fetch_success(
            source=source,
            entries=(
                NewsFeedEntry(
                    guid=(
                        "CBMihAFBVV95cUxObWtITTNCY3VxMEQxQXBZZ3BOd0h0NUN1dTA5UlI0aUdfeFFL"
                        "QVU4V1d4b2ZlamltNmZlNkM5NU9WZzNFakRxSDJMeGtGaXlWRmNYcGhtdnQ2YVYy"
                        "RVA1SjdSZWxRQTU3alcxVUYyVXhXdFEzeDlVNjBKejV6a01TYk1KYXY"
                    ),
                    link=(
                        "https://news.google.com/rss/articles/"
                        "CBMihAFBVV95cUxObWtITTNCY3VxMEQxQXBZZ3BOd0h0NUN1dTA5UlI0aUdfeFFL"
                        "QVU4V1d4b2ZlamltNmZlNkM5NU9WZzNFakRxSDJMeGtGaXlWRmNYcGhtdnQ2YVYy"
                        "RVA1SjdSZWxRQTU3alcxVUYyVXhXdFEzeDlVNjBKejV6a01TYk1KYXY?oc=5"
                    ),
                    title="Oil hits $100 and drives global bond sell-off - Financial Times",
                    summary="Oil hits $100 and drives global bond sell-off Financial Times",
                    published_at_ms=1_784_865_681_000,
                    language="en",
                ),
                NewsFeedEntry(
                    guid=(
                        "CBMihAFBVV95cUxOclF2RTc2cFc2ajlnUjN2blRGYS1vbTNGeTRnNkFKNGduZGpS"
                        "S05BMnVkY21vcFRkR3BvMDhGZkFuTzFvaUxtc1NDVEJyb180TGcyUkROc2QwYkJR"
                        "aEJ5eTlOTEdNbVdMbGNhdC1ETnYyUno1dldEeU1Yb3JpVEkxZml4dEM"
                    ),
                    link=(
                        "https://news.google.com/rss/articles/"
                        "CBMihAFBVV95cUxOclF2RTc2cFc2ajlnUjN2blRGYS1vbTNGeTRnNkFKNGduZGpS"
                        "S05BMnVkY21vcFRkR3BvMDhGZkFuTzFvaUxtc1NDVEJyb180TGcyUkROc2QwYkJR"
                        "aEJ5eTlOTEdNbVdMbGNhdC1ETnYyUno1dldEeU1Yb3JpVEkxZml4dEM?oc=5"
                    ),
                    title="Oil price surge drives global bond sell-off - Financial Times",
                    summary="Oil price surge drives global bond sell-off Financial Times",
                    published_at_ms=1_784_794_970_000,
                    language="en",
                ),
            ),
            started_at_ms=observed_at_ms,
            finished_at_ms=observed_at_ms,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=observed_at_ms + 1_000, limit=100)

        health = repository.health_snapshot(now_ms=observed_at_ms + 2_000)

        assert conn.execute("SELECT count(*) AS count FROM news_stories").fetchone()["count"] == 1
        assert conn.execute("SELECT count(*) AS count FROM news_story_material_events").fetchone()["count"] == 2
        assert health["layers"]["material"]["status"] == "running"
        assert _reason_or_none(health, "story_material_hash_closure") is None
        conn.commit()
    finally:
        conn.close()


def test_news_health_applies_source_refresh_interval_thresholds(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source = _source("interval-wire", "wire_service")
        repository.sync_sources((source,), now_ms=NOW_MS)
        conn.execute(
            """
            UPDATE news_sources
               SET next_fetch_at_ms = %s, consecutive_failures = 0
             WHERE source_id = %s
            """,
            (NOW_MS, source.source_id),
        )

        degraded = repository.health_snapshot(now_ms=NOW_MS + 120_000)
        failed = repository.health_snapshot(now_ms=NOW_MS + 300_000)

        degraded_reason = _reason(degraded, "source_fetch_overdue")
        failed_reason = _reason(failed, "source_fetch_overdue")
        assert degraded["layers"]["source"]["status"] == "degraded"
        assert degraded_reason["measured_ms"] == 120_000
        assert degraded_reason["threshold_ms"] == 120_000
        assert degraded_reason["details"]["source_id"] == source.source_id
        assert failed["layers"]["source"]["status"] == "failed"
        assert failed_reason["measured_ms"] == 300_000
        assert failed_reason["threshold_ms"] == 300_000
        conn.commit()
    finally:
        conn.close()


def test_news_health_reports_projection_lag_at_degraded_and_failed_boundaries(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source = _source("lag-wire", "wire_service")
        repository.sync_sources((source,), now_ms=NOW_MS)
        repository.record_fetch_success(
            source=source,
            entries=(
                _entry(
                    source_id=source.source_id,
                    title="Government announces a new trade policy",
                    summary="",
                    published_at_ms=NOW_MS,
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )

        degraded = repository.health_snapshot(now_ms=NOW_MS + 30_001)
        failed = repository.health_snapshot(now_ms=NOW_MS + 120_001)

        degraded_reason = _reason(degraded, "story_projection_lag")
        failed_reason = _reason(failed, "story_projection_lag")
        assert degraded["layers"]["material"]["status"] == "degraded"
        assert degraded_reason["measured_ms"] == 30_001
        assert degraded_reason["threshold_ms"] == 30_000
        assert degraded_reason["details"]["backlog"] == 1
        assert failed["layers"]["material"]["status"] == "failed"
        assert failed_reason["measured_ms"] == 120_001
        assert failed_reason["threshold_ms"] == 120_000
        conn.commit()
    finally:
        conn.close()


def test_news_health_fails_story_counter_and_active_public_contract_corruption(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        story_id = _seed_story(
            repository,
            source_id="invariant-wire",
            title="Government implements a strategic border policy",
            observed_at_ms=NOW_MS,
            impact_score=80,
        )
        _plan(repository, now_ms=NOW_MS + 2_000, ordinary_ms=0)
        conn.execute(
            """
            UPDATE news_stories
               SET primary_member_count = primary_member_count + 1,
                   material_evidence_hash = 'corrupt-material-hash'
             WHERE story_id = %s
            """,
            (story_id,),
        )
        conn.execute(
            """
            UPDATE news_brief_selections AS selections
               SET evidence_bundle = jsonb_set(
                     selections.evidence_bundle,
                     '{selection_id}',
                     '"corrupt-selection-id"'::jsonb
                   )
              FROM news_brief_activations AS activations
              JOIN news_brief_active AS active_rows
                ON active_rows.activation_id = activations.activation_id
             WHERE selections.selection_id = activations.selection_id
               AND active_rows.singleton_key
            """
        )

        health = repository.health_snapshot(now_ms=NOW_MS + 3_000)

        assert health["layers"]["material"]["status"] == "failed"
        assert _reason(health, "story_counter_invariant")["measured"] == 1
        assert _reason(health, "story_material_hash_closure")["measured"] == 1
        assert health["layers"]["public"]["status"] == "failed"
        assert _reason(health, "public_active_contract_mismatch")["measured"] == 1
        conn.commit()
    finally:
        conn.close()


def test_news_health_detects_matured_proposal_immediately_and_after_two_cycles(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        _seed_story(
            repository,
            source_id="proposal-wire",
            title="Government implements a new semiconductor export policy",
            observed_at_ms=NOW_MS,
            impact_score=80,
        )
        proposed_at_ms = NOW_MS + 2_000
        proposal = _plan(repository, now_ms=proposed_at_ms)
        due_at_ms = proposed_at_ms + ORDINARY_DEBOUNCE_MS

        degraded = repository.health_snapshot(now_ms=due_at_ms)
        failed = repository.health_snapshot(now_ms=due_at_ms + 60_001)

        degraded_reason = _reason(degraded, "planner_active_mismatch")
        failed_reason = _reason(failed, "planner_active_mismatch")
        assert degraded["layers"]["brief"]["status"] == "degraded"
        assert degraded_reason["measured_ms"] == 0
        assert degraded_reason["threshold_ms"] == 0
        assert degraded_reason["details"]["proposal_id"] == proposal["proposal_id"]
        assert degraded_reason["details"]["lane"] == "ordinary"
        assert failed["layers"]["brief"]["status"] == "failed"
        assert failed_reason["measured_ms"] == 60_001
        assert failed_reason["threshold_ms"] == 60_000
        conn.commit()
    finally:
        conn.close()


def test_news_health_reports_lane_specific_proposal_activation_slos(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        _seed_story(
            repository,
            source_id="proposal-slo-wire",
            title="Government implements a new semiconductor export policy",
            observed_at_ms=NOW_MS,
            impact_score=80,
        )
        proposal = _plan(repository, now_ms=NOW_MS + 2_000)
        proposal_id = proposal["proposal_id"]

        for lane, degraded_after_ms, failed_after_ms in (
            ("ordinary", 180_000, 300_000),
            ("verified_critical", 60_000, 120_000),
            ("rectification", 45_000, 90_000),
        ):
            conn.execute(
                """
                UPDATE news_brief_proposals
                   SET lane = %s,
                       first_proposed_at_ms = %s,
                       activation_due_at_ms = %s
                 WHERE proposal_id = %s
                """,
                (lane, NOW_MS, NOW_MS, proposal_id),
            )
            degraded = repository.health_snapshot(now_ms=NOW_MS + degraded_after_ms + 1)
            failed = repository.health_snapshot(now_ms=NOW_MS + failed_after_ms + 1)

            degraded_reason = _reason(degraded, "proposal_activation_lag")
            failed_reason = _reason(failed, "proposal_activation_lag")
            assert degraded_reason["status"] == "degraded"
            assert degraded_reason["threshold_ms"] == degraded_after_ms
            assert degraded_reason["details"]["lane"] == lane
            assert failed_reason["status"] == "failed"
            assert failed_reason["threshold_ms"] == failed_after_ms
        conn.commit()
    finally:
        conn.close()


def test_news_health_reports_unattached_ai_queue_terminal_failure_and_expired_lease(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        _seed_story(
            repository,
            source_id="ai-official",
            title="Central bank implements emergency liquidity support",
            observed_at_ms=NOW_MS,
            impact_score=95,
            source_role="official_authority",
        )
        activated_at_ms = NOW_MS + 2_000
        active = _plan(
            repository,
            now_ms=activated_at_ms,
            ordinary_ms=0,
            critical_ms=0,
        )

        queued = repository.health_snapshot(now_ms=activated_at_ms + 300_001)
        assert queued["layers"]["ai"]["status"] == "degraded"
        queue_reason = _reason(queued, "ai_queue_age")
        assert queue_reason["measured_ms"] == 300_001
        assert queue_reason["threshold_ms"] == 300_000

        conn.execute(
            """
            INSERT INTO news_ai_attempts (
              attempt_key, publication_kind, target_id, evidence_hash,
              model, prompt_version, workflow_version, schema_version, locale,
              status, attempt_count, lease_token, lease_expires_at_ms,
              next_attempt_at_ms, requested_at_ms, updated_at_ms
            )
            SELECT
              'expired-attempt', 'brief', activations.activation_id,
              selections.synthesis_input_hash, 'model', 'prompt', 'workflow',
              'schema', 'zh-CN', 'running', 1, 'expired-lease',
              %s, 0, %s, %s
              FROM news_brief_activations AS activations
              JOIN news_brief_selections AS selections
                ON selections.selection_id = activations.selection_id
             WHERE activations.activation_id = %s
            """,
            (
                activated_at_ms + 1_000,
                activated_at_ms,
                activated_at_ms + 1_000,
                active["activation_id"],
            ),
        )
        conn.execute(
            """
            INSERT INTO news_ai_current_targets (
              publication_kind, target_id, evidence_hash, model,
              prompt_version, workflow_version, schema_version, locale,
              desired_at_ms
            )
            SELECT
              'brief', activations.activation_id,
              selections.synthesis_input_hash, 'model', 'prompt', 'workflow',
              'schema', 'zh-CN', %s
              FROM news_brief_activations AS activations
              JOIN news_brief_selections AS selections
                ON selections.selection_id = activations.selection_id
             WHERE activations.activation_id = %s
            """,
            (activated_at_ms, active["activation_id"]),
        )
        expired = repository.health_snapshot(now_ms=activated_at_ms + 2_000)
        assert expired["layers"]["ai"]["status"] == "degraded"
        assert _reason(expired, "ai_lease_expired")["measured_ms"] == 1_000

        conn.execute(
            """
            UPDATE news_ai_attempts
               SET status = 'failed',
                   attempt_count = 3,
                   lease_expires_at_ms = 0,
                   next_attempt_at_ms = 9223372036854775000,
                   last_error = 'terminal_validation',
                   updated_at_ms = %s
             WHERE attempt_key = 'expired-attempt'
            """,
            (activated_at_ms + 3_000,),
        )
        terminal = repository.health_snapshot(now_ms=activated_at_ms + 3_000)
        assert terminal["layers"]["ai"]["status"] == "failed"
        assert _reason(terminal, "ai_terminal_failure")["measured"] == 3
        conn.commit()
    finally:
        conn.close()


def test_news_health_rejects_active_publication_with_wrong_synthesis_identity(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        _seed_story(
            repository,
            source_id="publication-wire",
            title="Government implements a new strategic trade policy",
            observed_at_ms=NOW_MS,
            impact_score=80,
        )
        active = _plan(repository, now_ms=NOW_MS + 2_000, ordinary_ms=0)
        conn.execute(
            """
            INSERT INTO news_brief_publications (
              publication_id, selection_id, synthesis_input_hash,
              evidence_cutoff_at_ms, model, prompt_version, workflow_version,
              schema_version, locale, payload, evidence_references, receipt,
              published_at_ms
            )
            SELECT
              'mismatched-publication', activations.selection_id, 'wrong-hash',
              selections.evidence_cutoff_at_ms, 'model', 'prompt', 'workflow',
              'schema', 'zh-CN', '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, %s
              FROM news_brief_activations AS activations
              JOIN news_brief_selections AS selections
                ON selections.selection_id = activations.selection_id
             WHERE activations.activation_id = %s
            """,
            (
                NOW_MS + 3_000,
                active["activation_id"],
            ),
        )
        conn.execute(
            """
            INSERT INTO news_brief_activation_analysis (
              activation_id, publication_id, attachment_kind, attached_at_ms
            )
            VALUES (%s, 'mismatched-publication', 'generated', %s)
            """,
            (
                active["activation_id"],
                NOW_MS + 3_000,
            ),
        )

        health = repository.health_snapshot(now_ms=NOW_MS + 4_000)

        assert health["layers"]["brief"]["status"] == "failed"
        reason = _reason(health, "active_publication_mismatch")
        assert reason["measured"] == 1
        assert reason["threshold"] == 0
        assert reason["details"]["activation_id"] == active["activation_id"]
        conn.commit()
    finally:
        conn.close()


def _reason(health: dict[str, object], code: str) -> dict[str, object]:
    reasons = health["reasons"]
    assert isinstance(reasons, list)
    matches = [reason for reason in reasons if isinstance(reason, dict) and reason.get("code") == code]
    assert len(matches) == 1, (code, reasons)
    return matches[0]


def _reason_or_none(health: dict[str, object], code: str) -> dict[str, object] | None:
    reasons = health["reasons"]
    assert isinstance(reasons, list)
    return next(
        (reason for reason in reasons if isinstance(reason, dict) and reason.get("code") == code),
        None,
    )
