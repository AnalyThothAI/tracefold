from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from tests.postgres_test_utils import connect_postgres_test, repository_session_for_connection
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news import (
    NewsFeedEntry,
    NewsFeedFetch,
    NewsIngestWorker,
    NewsInterface,
    NewsPageFetch,
    NewsRepository,
    NewsSourceDefinition,
)

NOW_MS = 1_779_000_000_000


class SingleConnectionDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def worker_session(self, *_args: Any, **_kwargs: Any):
        return repository_session_for_connection(self.conn)


class OneFailedFeedReader:
    def fetch(
        self,
        *,
        source: NewsSourceDefinition,
        etag: str | None,
        last_modified: str | None,
    ) -> NewsFeedFetch:
        del etag, last_modified
        if source.source_id == "failed-wire":
            raise RuntimeError("source_temporarily_unavailable")
        return NewsFeedFetch(
            status_code=200,
            entries=(
                entry(
                    "healthy-story",
                    "https://healthy.example/policy",
                    "Government implements a new trade policy",
                ),
            ),
        )

    def close(self) -> None:
        return None


def test_ingest_failure_is_isolated_per_source_and_other_source_commits(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        worker = NewsIngestWorker(
            settings=SimpleNamespace(
                enabled=True,
                interval_seconds=1,
                batch_size=10,
                statement_timeout_seconds=30.0,
            ),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            sources=(
                source(
                    "failed-wire",
                    "Failed Wire",
                    "failed.example",
                    "failed-wire",
                ),
                source(
                    "healthy-wire",
                    "Healthy Wire",
                    "healthy.example",
                    "healthy-wire",
                ),
            ),
            feed_reader=OneFailedFeedReader(),
            clock_ms=lambda: NOW_MS,
        )

        result = asyncio.run(worker.run_once())

        assert result.processed == 1
        assert result.failed == 1
        receipts = conn.execute(
            """
            SELECT source_id, error_code
              FROM news_fetch_receipts
             ORDER BY source_id
            """
        ).fetchall()
        assert [row["source_id"] for row in receipts] == ["failed-wire", "healthy-wire"]
        assert receipts[0]["error_code"] == "RuntimeError"
        assert receipts[1]["error_code"] is None
        assert conn.execute("SELECT count(*) AS count FROM news_articles").fetchone()["count"] == 1
        conn.commit()
    finally:
        conn.close()


def test_professional_news_pipeline_admits_facts_then_projects_event_stories(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters", "reuters.com", "reuters")
        ap = source("ap", "Associated Press", "apnews.com", "ap")
        repository.sync_sources((reuters, ap), now_ms=NOW_MS)

        first = repository.record_fetch_success(
            source=reuters,
            entries=(
                entry(
                    "r1",
                    "https://reuters.com/world/fed-cut",
                    "Federal Reserve cuts interest rates by 25 basis points",
                ),
                NewsFeedEntry(
                    guid="missing-time",
                    link="https://reuters.com/world/missing",
                    title="This row has no source time",
                ),
                entry(
                    "stale",
                    "https://reuters.com/world/stale",
                    "Old static page",
                    published_at_ms=NOW_MS - 97 * 60 * 60 * 1000,
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS + 100,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        duplicate = repository.record_fetch_success(
            source=reuters,
            entries=(
                entry(
                    "r1",
                    "https://reuters.com/world/fed-cut",
                    "Federal Reserve cuts interest rates by 25 basis points",
                ),
            ),
            started_at_ms=NOW_MS + 1_000,
            finished_at_ms=NOW_MS + 1_100,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.record_fetch_success(
            source=ap,
            entries=(
                entry(
                    "a1",
                    "https://apnews.com/article/fed-cut",
                    "Federal Reserve lowers rates 25 basis points",
                ),
            ),
            started_at_ms=NOW_MS + 2_000,
            finished_at_ms=NOW_MS + 2_100,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        assert first["entries_admitted"] == 1
        assert first["rejection_counts"] == {
            "missing_source_time": 1,
            "stale_source_time": 1,
        }
        assert duplicate["duplicate_seen_count"] == 1
        assert (
            repository.list_story_rows(
                limit=10,
                cursor=None,
                q=None,
                evidence_posture=None,
                source=None,
            )
            == []
        )

        projected = repository.project_pending_revisions(now_ms=NOW_MS + 3_000, limit=100)
        assert projected["processed"] == 2
        interface = NewsInterface(repository)
        stories = interface.list_stories(limit=10)
        assert len(stories["items"]) == 1
        story = stories["items"][0]
        assert story["primary_member_count"] == 2
        assert story["independent_origin_count"] == 2
        assert story["evidence_posture"] == "independently_corroborated"
        detail = interface.get_story(story_id=story["story_id"])
        assert detail is not None
        assert len(detail["identity_decisions"]) == 2
        assert len(detail["articles"]) == 2
        assert {row["epistemic_use"] for row in detail["memberships"]} == {"fact_evidence"}
        conn.commit()
    finally:
        conn.close()


def test_story_identity_rejects_different_action_and_stage_hard_negative(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source_row = source("wire", "Wire", "wire.example", "wire")
        repository.sync_sources((source_row,), now_ms=NOW_MS)
        repository.record_fetch_success(
            source=source_row,
            entries=(
                entry(
                    "proposal",
                    "https://wire.example/tariff-proposal",
                    "Government proposes new tariffs on steel imports",
                ),
                entry(
                    "implementation",
                    "https://wire.example/tariff-implementation",
                    "Government implements new tariffs on steel imports",
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS + 100,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=NOW_MS + 1_000, limit=100)
        stories = NewsInterface(repository).list_stories(limit=10)["items"]
        assert len(stories) == 2
        conn.commit()
    finally:
        conn.close()


def test_compatible_quantity_disagreement_stays_in_one_story_and_becomes_contested(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters", "reuters.example", "reuters")
        ap = source("ap", "Associated Press", "ap.example", "ap")
        repository.sync_sources((reuters, ap), now_ms=NOW_MS)
        repository.record_fetch_success(
            source=reuters,
            entries=(
                entry(
                    "fed-25",
                    "https://reuters.example/fed-cut",
                    "Federal Reserve cuts interest rates by 25 basis points",
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.record_fetch_success(
            source=ap,
            entries=(
                entry(
                    "fed-50",
                    "https://ap.example/fed-cut",
                    "Fed lowers interest rates by 50 basis points",
                ),
            ),
            started_at_ms=NOW_MS + 1_000,
            finished_at_ms=NOW_MS + 1_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )

        repository.project_pending_revisions(now_ms=NOW_MS + 2_000, limit=100)
        stories = NewsInterface(repository).list_stories(limit=10)["items"]
        assert len(stories) == 1
        assert stories[0]["evidence_posture"] == "contested"
        conflicts = stories[0]["evidence_factors"]["material_conflicts"]
        assert [conflict["kind"] for conflict in conflicts] == ["numeric_conflict"]
        assert conflicts[0]["quantity_kind"] == "basis_points"
        conn.commit()
    finally:
        conn.close()


def test_brief_planning_is_deterministic_and_does_not_call_ai_for_same_fingerprint(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source_row = source("official", "Central Bank", "bank.example", "bank", role="official_authority")
        repository.sync_sources((source_row,), now_ms=NOW_MS)
        repository.record_fetch_success(
            source=source_row,
            entries=(
                entry(
                    "decision",
                    "https://bank.example/policy/decision",
                    "Central bank cuts interest rates by 50 basis points",
                    summary="The central bank approved and implemented a 50 basis point rate cut.",
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS + 100,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=NOW_MS + 1_000, limit=100)
        conn.execute(
            """
            UPDATE news_stories
               SET brief_eligible = true, impact_score = 95, priority_score = 90
            """
        )
        first = repository.plan_global_brief(
            now_ms=NOW_MS + 2_000,
            candidate_limit=100,
            debounce_ms=0,
            critical_debounce_ms=0,
        )
        second = repository.plan_global_brief(
            now_ms=NOW_MS + 3_000,
            candidate_limit=100,
            debounce_ms=0,
            critical_debounce_ms=0,
        )
        assert first["changed"] is True
        assert first["selected_story_count"] == 1
        assert second["changed"] is False
        assert first["selection_fingerprint"] == second["selection_fingerprint"]
        brief = NewsInterface(repository).get_global_brief()
        assert brief["current"] is None
        assert brief["fallback"]["status"] == "publishable"
        assert len(brief["fallback"]["evidence_bundle"]["stories"]) == 1
        conn.commit()
    finally:
        conn.close()


def test_admission_contract_rejects_invalid_time_url_title_and_static_pages(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source_row = source("wire", "Wire", "wire.example", "wire")
        repository.sync_sources((source_row,), now_ms=NOW_MS)
        result = repository.record_fetch_success(
            source=source_row,
            entries=(
                NewsFeedEntry(
                    guid="missing-title",
                    link="https://wire.example/story",
                    title=" ",
                    published_at_ms=NOW_MS,
                ),
                NewsFeedEntry(
                    guid="unsafe-url",
                    link="ftp://wire.example/story",
                    title="Policy decision",
                    published_at_ms=NOW_MS,
                ),
                NewsFeedEntry(
                    guid="credential-url",
                    link="https://user:secret@wire.example/story",
                    title="Policy decision",
                    published_at_ms=NOW_MS,
                ),
                NewsFeedEntry(
                    guid="missing-time",
                    link="https://wire.example/missing-time",
                    title="Policy decision",
                ),
                entry(
                    "future",
                    "https://wire.example/future",
                    "Policy decision",
                    published_at_ms=NOW_MS + 61 * 60 * 1000,
                ),
                entry(
                    "stale",
                    "https://wire.example/stale",
                    "Policy decision",
                    published_at_ms=NOW_MS - 96 * 60 * 60 * 1000 - 1,
                ),
                entry(
                    "section",
                    "https://wire.example/world",
                    "World News",
                ),
                entry(
                    "static-title",
                    "https://wire.example/current",
                    "Breaking News & Views",
                ),
                entry(
                    "valid",
                    "https://wire.example/policy/decision",
                    "Government approves a new trade policy",
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        assert result["entries_admitted"] == 1
        assert result["rejection_counts"] == {
            "future_source_time": 1,
            "invalid_title": 1,
            "invalid_url": 2,
            "missing_source_time": 1,
            "stale_source_time": 1,
            "static_or_section_page": 2,
        }
        receipt = conn.execute(
            "SELECT * FROM news_fetch_receipts WHERE fetch_receipt_id = %s",
            (result["fetch_receipt_id"],),
        ).fetchone()
        assert receipt is not None
        assert receipt["entries_seen"] == 9
        assert receipt["rejection_counts"] == result["rejection_counts"]
        assert conn.execute("SELECT count(*) AS count FROM news_articles").fetchone()["count"] == 1
        conn.commit()
    finally:
        conn.close()


def test_confirmed_url_reuse_creates_a_new_article_incarnation_and_story(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source_row = source("publisher", "Publisher", "publisher.example", "publisher")
        repository.sync_sources((source_row,), now_ms=NOW_MS)
        shared_url = "https://publisher.example/latest/story"
        repository.record_fetch_success(
            source=source_row,
            entries=(
                entry(
                    "first-event",
                    shared_url,
                    "Federal Reserve cuts interest rates",
                    published_at_ms=NOW_MS - 13 * 60 * 60 * 1000,
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.record_fetch_success(
            source=source_row,
            entries=(
                entry(
                    "second-event",
                    shared_url,
                    "China rejects new trade tariffs",
                    published_at_ms=NOW_MS - 60_000,
                ),
            ),
            started_at_ms=NOW_MS + 1_000,
            finished_at_ms=NOW_MS + 1_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        articles = conn.execute(
            "SELECT article_id, identity_status FROM news_articles ORDER BY created_at_ms, article_id"
        ).fetchall()
        assert [row["identity_status"] for row in articles] == ["ended", "active"]
        revisions = conn.execute(
            "SELECT article_id, material_change_kind FROM news_article_revisions ORDER BY observed_at_ms"
        ).fetchall()
        assert [row["material_change_kind"] for row in revisions] == ["initial", "url_reuse"]
        assert len({row["article_id"] for row in revisions}) == 2

        repository.project_pending_revisions(now_ms=NOW_MS + 2_000, limit=100)
        assert len(NewsInterface(repository).list_stories(limit=10)["items"]) == 2
        conn.commit()
    finally:
        conn.close()


def test_ambiguous_revision_is_quarantined_without_reassigning_story_history(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source_row = source("publisher", "Publisher", "publisher.example", "publisher")
        repository.sync_sources((source_row,), now_ms=NOW_MS)
        shared_url = "https://publisher.example/policy/live"
        repository.record_fetch_success(
            source=source_row,
            entries=(
                entry(
                    "same-entry",
                    shared_url,
                    "Federal Reserve cuts interest rates",
                    published_at_ms=NOW_MS - 60 * 60 * 1000,
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.record_fetch_success(
            source=source_row,
            entries=(
                entry(
                    "same-entry",
                    shared_url,
                    "China rejects new trade tariffs",
                    published_at_ms=NOW_MS - 30 * 60 * 1000,
                ),
            ),
            started_at_ms=NOW_MS + 1_000,
            finished_at_ms=NOW_MS + 1_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        outcome = repository.project_pending_revisions(now_ms=NOW_MS + 2_000, limit=100)
        assert outcome == {
            "processed": 2,
            "created": 1,
            "joined": 0,
            "revised": 0,
            "ambiguous": 1,
        }
        article = conn.execute("SELECT * FROM news_articles").fetchone()
        assert article["identity_status"] == "revision_identity_ambiguous"
        stories = NewsInterface(repository).list_stories(limit=10)["items"]
        assert len(stories) == 1
        assert stories[0]["title"] == "Federal Reserve cuts interest rates"
        detail = NewsInterface(repository).get_story(story_id=stories[0]["story_id"])
        assert detail is not None
        assert detail["memberships"][0]["revision_id"] != detail["articles"][-1]["revision_id"]
        assert any(decision["verdict"] == "revision_identity_ambiguous" for decision in detail["identity_decisions"])
        conn.commit()
    finally:
        conn.close()


def test_story_rebuild_replays_the_same_runtime_seam_across_batch_sizes(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters", "reuters.com", "reuters")
        ap = source("ap", "Associated Press", "apnews.com", "ap")
        repository.sync_sources((reuters, ap), now_ms=NOW_MS)
        repository.record_fetch_success(
            source=reuters,
            entries=(
                entry(
                    "r1",
                    "https://reuters.com/world/fed-cut",
                    "Federal Reserve cuts interest rates by 25 basis points",
                ),
                entry(
                    "r2",
                    "https://reuters.com/world/tariff-proposal",
                    "US proposes new tariffs on China",
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS + 100,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.record_fetch_success(
            source=ap,
            entries=(
                entry(
                    "a1",
                    "https://apnews.com/article/fed-cut",
                    "Federal Reserve lowers rates 25 basis points",
                ),
            ),
            started_at_ms=NOW_MS + 200,
            finished_at_ms=NOW_MS + 300,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=NOW_MS + 1_000, limit=100)
        live = projection_snapshot(conn)

        repository.reset_story_projection()
        while True:
            batch = repository.project_pending_revisions(now_ms=NOW_MS + 1_000, limit=1)
            if batch["processed"] < 1:
                break
        rebuilt = projection_snapshot(conn)
        assert rebuilt == live
        conn.commit()
    finally:
        conn.close()


def test_material_hash_ignores_source_time_and_syndication_but_tracks_independent_evidence(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters", "reuters.example", "reuters")
        aggregator = source(
            "aggregator",
            "Aggregator",
            "aggregator.example",
            "aggregator",
            role="trusted_aggregator",
        )
        ap = source("ap", "Associated Press", "ap.example", "ap")
        repository.sync_sources((reuters, aggregator, ap), now_ms=NOW_MS)
        canonical_title = "Federal Reserve cuts interest rates by 25 basis points"
        repository.record_fetch_success(
            source=reuters,
            entries=(
                entry(
                    "reuters-decision",
                    "https://reuters.example/rate-cut",
                    canonical_title,
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=NOW_MS + 1_000, limit=100)
        initial = conn.execute("SELECT * FROM news_stories").fetchone()
        initial_hash = str(initial["material_evidence_hash"])
        initial_material_at_ms = int(initial["last_material_evidence_at_ms"])
        initial_event_count = conn.execute("SELECT count(*) AS count FROM news_story_material_events").fetchone()[
            "count"
        ]

        repository.record_fetch_success(
            source=reuters,
            entries=(
                entry(
                    "reuters-decision",
                    "https://reuters.example/rate-cut",
                    canonical_title,
                    published_at_ms=NOW_MS - 30_000,
                ),
            ),
            started_at_ms=NOW_MS + 2_000,
            finished_at_ms=NOW_MS + 2_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=NOW_MS + 3_000, limit=100)
        time_only = conn.execute("SELECT * FROM news_stories").fetchone()
        assert time_only["material_evidence_hash"] == initial_hash
        assert time_only["last_material_evidence_at_ms"] == initial_material_at_ms
        assert (
            conn.execute("SELECT count(*) AS count FROM news_story_material_events").fetchone()["count"]
            == initial_event_count
        )

        repository.record_fetch_success(
            source=aggregator,
            entries=(
                entry(
                    "aggregated-decision",
                    "https://aggregator.example/rate-cut",
                    f"According to Reuters, {canonical_title}",
                ),
            ),
            started_at_ms=NOW_MS + 4_000,
            finished_at_ms=NOW_MS + 4_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=NOW_MS + 5_000, limit=100)
        syndicated = conn.execute("SELECT * FROM news_stories").fetchone()
        assert syndicated["material_evidence_hash"] == initial_hash
        assert syndicated["syndicated_article_count"] == 1
        assert syndicated["independent_origin_count"] == 1

        repository.record_fetch_success(
            source=ap,
            entries=(
                entry(
                    "ap-decision",
                    "https://ap.example/rate-cut",
                    "Fed lowers rates by 25 basis points",
                ),
            ),
            started_at_ms=NOW_MS + 6_000,
            finished_at_ms=NOW_MS + 6_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=NOW_MS + 7_000, limit=100)
        corroborated = conn.execute("SELECT * FROM news_stories").fetchone()
        assert corroborated["material_evidence_hash"] != initial_hash
        assert corroborated["independent_origin_count"] == 2
        assert corroborated["evidence_posture"] == "independently_corroborated"
        conn.commit()
    finally:
        conn.close()


def test_page_enrichment_is_lease_fenced_and_can_unlock_deep_analysis(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        publisher = source(
            "publisher",
            "Publisher",
            "publisher.example",
            "publisher",
        )
        repository.sync_sources((publisher,), now_ms=NOW_MS)
        repository.record_fetch_success(
            source=publisher,
            entries=(
                entry(
                    "policy",
                    "https://publisher.example/policy",
                    "Government implements emergency financial stability policy",
                ),
            ),
            started_at_ms=NOW_MS,
            finished_at_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=NOW_MS + 1_000, limit=100)
        conn.execute("UPDATE news_stories SET impact_score = 90, priority_score = 90")
        claims = repository.claim_page_enrichment(
            now_ms=NOW_MS + 2_000,
            limit=1,
            minimum_impact_score=75,
            extractor_version="reader-v1",
            lease_ms=30_000,
            max_attempts=3,
        )
        assert len(claims) == 1
        claim = claims[0]
        available = NewsPageFetch(
            status="available",
            fetched_at_ms=NOW_MS + 2_500,
            http_status=200,
            content_type="text/html",
            content_hash="content-hash",
            extracted_text="政策正文。" * 800,
            byte_count=4_800,
            final_url="https://publisher.example/policy",
        )

        repository.complete_page_enrichment(
            content_snapshot_id=claim["content_snapshot_id"],
            lease_token="stale-owner",
            result=available,
            retry_ms=60_000,
            now_ms=NOW_MS + 2_500,
        )
        snapshot = conn.execute(
            """
            SELECT status
              FROM news_article_content_snapshots
             WHERE content_snapshot_id = %s
            """,
            (claim["content_snapshot_id"],),
        ).fetchone()
        assert snapshot["status"] == "pending"

        repository.complete_page_enrichment(
            content_snapshot_id=claim["content_snapshot_id"],
            lease_token=claim["lease_token"],
            result=available,
            retry_ms=60_000,
            now_ms=NOW_MS + 2_600,
        )

        snapshot = conn.execute(
            """
            SELECT status
              FROM news_article_content_snapshots
             WHERE content_snapshot_id = %s
            """,
            (claim["content_snapshot_id"],),
        ).fetchone()
        assert snapshot["status"] == "available"
        request = conn.execute(
            """
            SELECT request_kind, status, reason
              FROM news_story_analysis_requests
             WHERE request_kind = 'automatic'
            """
        ).fetchone()
        assert request["status"] == "pending"
        assert request["reason"] == {"content_snapshot_available": claim["content_snapshot_id"]}
        conn.commit()
    finally:
        conn.close()


def source(
    source_id: str,
    name: str,
    domain: str,
    chain: str,
    *,
    role: str = "wire_service",
) -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id=source_id,
        name=name,
        feed_url=f"https://{domain}/feed.xml",
        source_domain=domain,
        source_role=role,  # type: ignore[arg-type]
        trust_tier="authoritative" if role == "official_authority" else "trusted",
        source_chain_id=chain,
        default_language="en",
        refresh_interval_seconds=60,
    )


def entry(
    guid: str,
    link: str,
    title: str,
    *,
    summary: str = "",
    published_at_ms: int = NOW_MS - 60_000,
) -> NewsFeedEntry:
    return NewsFeedEntry(
        guid=guid,
        link=link,
        title=title,
        summary=summary,
        published_at_ms=published_at_ms,
        language="en",
    )


def projection_snapshot(conn: object) -> dict[str, list[dict[str, object]]]:
    return {
        table: [
            dict(row)
            for row in conn.execute(  # type: ignore[attr-defined]
                f"SELECT * FROM {table} ORDER BY 1, 2"
            ).fetchall()
        ]
        for table in (
            "news_stories",
            "news_story_memberships",
            "news_story_profiles",
            "news_story_identity_decisions",
            "news_story_material_events",
            "news_article_identity_features",
            "news_story_projection_checkpoints",
        )
    }
