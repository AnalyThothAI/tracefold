from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from tests.postgres_test_utils import connect_postgres_test, repository_session_for_connection
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news import (
    NewsAiClassifyWorker,
    NewsBriefDraft,
    NewsClassification,
    NewsFeedEntry,
    NewsFeedFetch,
    NewsInterface,
    NewsPipelineWorker,
    NewsRepository,
    NewsSourceDefinition,
    NewsWorldBriefWorker,
)

NOW_MS = 1_779_000_000_000


class SingleConnectionDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def worker_session(self, *_args: Any, **_kwargs: Any):
        return repository_session_for_connection(self.conn)


class TwoSourceReader:
    def fetch(
        self,
        *,
        source: NewsSourceDefinition,
        etag: str | None,
        last_modified: str | None,
    ) -> NewsFeedFetch:
        del etag, last_modified
        title = (
            "Iran threatens to close Strait of Hormuz if US blockade continues"
            if source.reporting_origin == "reuters"
            else "Iran threatens to close Strait of Hormuz — live updates"
        )
        return NewsFeedFetch(
            status_code=200,
            entries=(
                NewsFeedEntry(
                    guid="story-1",
                    link=f"https://{source.reporting_origin}.example/story-1",
                    title=title,
                    description="Officials issued a formal statement about the shipping route.",
                    published_at_ms=NOW_MS - 60_000,
                    language="en",
                    raw={"title": title, "source": source.name},
                ),
            ),
        )

    def close(self) -> None:
        return None


class FixedBriefPublisher:
    def publish(self, stories: list[Any]) -> NewsBriefDraft:
        return NewsBriefDraft(
            lead="霍尔木兹海峡相关表态成为当前重点 [1]",
            lines=tuple(
                f"第{index}条保持输入标题所述事实 [${index}]".replace("$", "") for index in range(1, len(stories) + 1)
            ),
            provider="test",
            model="test-model",
            raw_response="{}",
        )

    def close(self) -> None:
        return None


class InvalidBriefPublisher:
    def publish(self, stories: list[Any]) -> NewsBriefDraft:
        return NewsBriefDraft(
            lead="Fabricated Acme development without a citation",
            lines=tuple("Fabricated Acme claim [1]" for _story in stories),
            provider="test",
            model="invalid-model",
            raw_response="{}",
        )

    def close(self) -> None:
        return None


class RaisingBriefPublisher:
    def publish(self, stories: list[Any]) -> NewsBriefDraft:
        del stories
        raise RuntimeError("provider unavailable")

    def close(self) -> None:
        return None


class FixedClassificationPublisher:
    last_model = "classifier-model"
    last_raw_response = "{}"

    def classify(self, titles: list[str]) -> tuple[NewsClassification, ...]:
        return tuple(
            NewsClassification(
                level="critical",
                category="economic",
                confidence=0.95,
                source="llm",
            )
            for _title in titles
        )

    def close(self) -> None:
        return None


def source(source_id: str, name: str, origin: str) -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id=source_id,
        name=name,
        feed_url=f"https://{origin}.example/rss",
        reporting_origin=origin,
        tier=1,
        category_hint="politics",
        refresh_interval_seconds=120,
    )


def test_pipeline_persists_the_current_claim_time_for_each_fetch_cycle(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        clock = SimpleNamespace(now_ms=NOW_MS)
        pipeline = NewsPipelineWorker(
            settings=SimpleNamespace(
                batch_size=20,
                fetch_concurrency=1,
                statement_timeout_seconds=30.0,
            ),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            sources=(source("reuters", "Reuters", "reuters"),),
            feed_reader=TwoSourceReader(),
            clock_ms=lambda: clock.now_ms,
        )

        first = asyncio.run(pipeline.run_once())
        clock.now_ms = NOW_MS + 120_000
        second = asyncio.run(pipeline.run_once())

        assert first.processed == 1
        assert second.processed == 1
        fetches = conn.execute(
            """
            SELECT started_at_ms, finished_at_ms
              FROM news_source_fetches
             ORDER BY started_at_ms
            """
        ).fetchall()
        assert fetches == [
            {"started_at_ms": NOW_MS, "finished_at_ms": NOW_MS},
            {
                "started_at_ms": NOW_MS + 120_000,
                "finished_at_ms": NOW_MS + 120_000,
            },
        ]
    finally:
        conn.close()


def test_rss_to_postgres_to_story_to_brief_and_interface(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        pipeline = NewsPipelineWorker(
            settings=SimpleNamespace(
                batch_size=20,
                fetch_concurrency=2,
                statement_timeout_seconds=30.0,
            ),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            sources=(
                source("reuters", "Reuters", "reuters"),
                source("ap", "AP News", "ap"),
            ),
            feed_reader=TwoSourceReader(),
            clock_ms=lambda: NOW_MS,
        )
        result = asyncio.run(pipeline.run_once())
        assert result.processed == 2
        assert result.failed == 0

        assert conn.execute("SELECT count(*) AS n FROM news_feed_observations").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM news_items").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM news_stories WHERE active").fetchone()["n"] == 1
        story_row = conn.execute("SELECT * FROM news_stories WHERE active").fetchone()
        assert story_row["source_count"] == 2
        assert story_row["item_count"] == 2

        interface = NewsInterface(NewsRepository(conn))
        feed = interface.get_feed()
        assert feed["story_count"] == 1
        detail = interface.get_story(story_id=story_row["story_id"])
        assert detail is not None
        assert len(detail["members"]) == 2
        assert {member["reporting_origin"] for member in detail["members"]} == {"reuters", "ap"}

        brief_worker = NewsWorldBriefWorker(
            settings=SimpleNamespace(statement_timeout_seconds=30.0),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            publisher=FixedBriefPublisher(),
            clock_ms=lambda: NOW_MS + 120_000,
        )
        brief_result = asyncio.run(brief_worker.run_once())
        assert brief_result.processed == 1
        brief = interface.get_world_brief(now_ms=NOW_MS + 120_000)
        assert brief["state"] == "fresh"
        assert brief["publication"]["selected_story_ids"] == [story_row["story_id"]]
        assert brief["publication"]["sources"][0]["story_id"] == story_row["story_id"]
    finally:
        conn.close()


def test_brief_accepts_admitted_source_time_within_future_tolerance(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters", "reuters")
        with conn.transaction():
            repository.sync_sources((reuters,), now_ms=NOW_MS)
            repository.record_fetch_success(
                source=reuters,
                entries=(
                    NewsFeedEntry(
                        guid="future-within-tolerance",
                        link="https://reuters.example/future-tolerance",
                        title="Central bank announces emergency liquidity facility",
                        description="The central bank published the facility terms.",
                        published_at_ms=NOW_MS + 30 * 60_000,
                    ),
                ),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repository.rebuild_stories(now_ms=NOW_MS)

        worker = NewsWorldBriefWorker(
            settings=SimpleNamespace(statement_timeout_seconds=30.0),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            publisher=FixedBriefPublisher(),
            clock_ms=lambda: NOW_MS,
        )
        result = asyncio.run(worker.run_once())
        assert result.processed == 1
        assert NewsInterface(repository).get_world_brief(now_ms=NOW_MS)["state"] == "fresh"
    finally:
        conn.close()


def test_pubdate_only_drift_creates_observation_but_zero_item_or_story_write(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters", "reuters")
        with conn.transaction():
            repository.sync_sources((reuters,), now_ms=NOW_MS)
            first = repository.record_fetch_success(
                source=reuters,
                entries=(
                    NewsFeedEntry(
                        guid="same-guid",
                        link="https://reuters.example/story",
                        title="Iran threatens to close Strait of Hormuz",
                        description="Officials issued a formal statement about the shipping route.",
                        published_at_ms=NOW_MS - 60_000,
                        raw={"published": "first"},
                    ),
                ),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            projection = repository.rebuild_stories(now_ms=NOW_MS)
        story = conn.execute("SELECT story_id, state_fingerprint FROM news_stories").fetchone()

        with conn.transaction():
            second = repository.record_fetch_success(
                source=reuters,
                entries=(
                    NewsFeedEntry(
                        guid="same-guid",
                        link="https://reuters.example/story",
                        title="Iran threatens to close Strait of Hormuz",
                        description="Officials issued a formal statement about the shipping route.",
                        published_at_ms=NOW_MS + 30_000,
                        raw={"published": "drifted"},
                    ),
                ),
                started_at_ms=NOW_MS + 120_000,
                finished_at_ms=NOW_MS + 120_000,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            second_projection = repository.rebuild_stories(now_ms=NOW_MS + 120_000)

        assert first["items_inserted"] == 1
        assert second["items_inserted"] == 0
        assert second["items_updated"] == 0
        assert projection["stories"] == 1
        assert second_projection["story_writes"] == 0
        assert conn.execute("SELECT count(*) AS n FROM news_feed_observations").fetchone()["n"] == 2
        current_story = conn.execute("SELECT story_id, state_fingerprint FROM news_stories").fetchone()
        assert current_story == story
    finally:
        conn.close()


def test_structured_item_origin_prevents_aggregator_corroboration_inflation(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        google = source("google-world", "Google News World", "google-news")
        with conn.transaction():
            repository.sync_sources((google,), now_ms=NOW_MS)
            repository.record_fetch_success(
                source=google,
                entries=(
                    NewsFeedEntry(
                        guid="reuters-copy",
                        link="https://news.google.example/reuters-copy",
                        title="Iran threatens to close Strait of Hormuz",
                        published_at_ms=NOW_MS - 60_000,
                        reporting_origin="reuters",
                    ),
                    NewsFeedEntry(
                        guid="ap-copy",
                        link="https://news.google.example/ap-copy",
                        title="Iran threatens to close Strait of Hormuz — live updates",
                        published_at_ms=NOW_MS - 30_000,
                        reporting_origin="ap",
                    ),
                ),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repository.rebuild_stories(now_ms=NOW_MS)

        story_row = conn.execute("SELECT * FROM news_stories WHERE active").fetchone()
        assert story_row["item_count"] == 2
        assert story_row["source_count"] == 2
        origins = {
            row["reporting_origin"] for row in conn.execute("SELECT reporting_origin FROM news_items").fetchall()
        }
        assert origins == {"reuters", "ap"}
    finally:
        conn.close()


def test_acquisition_gates_preserve_raw_observations_and_historical_items(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source_definition = source("example", "Example", "example")
        with conn.transaction():
            repository.sync_sources((source_definition,), now_ms=NOW_MS)
            repository.record_fetch_success(
                source=source_definition,
                entries=(
                    NewsFeedEntry(
                        guid="missing-date",
                        link="https://example.example/missing-date",
                        title="Missing date remains replayable",
                    ),
                    NewsFeedEntry(
                        guid="future",
                        link="https://example.example/future",
                        title="Future item remains replayable",
                        published_at_ms=NOW_MS + 3_600_001,
                    ),
                    NewsFeedEntry(
                        guid="historical",
                        link="https://example.example/historical",
                        title="Historical item remains durable",
                        published_at_ms=NOW_MS - 96 * 60 * 60 * 1000 - 1,
                    ),
                ),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
                entries_seen=7,
                gate_counts={"per_feed_cap": 4},
            )
            projection = repository.rebuild_stories(now_ms=NOW_MS)

        assert conn.execute("SELECT count(*) AS n FROM news_feed_observations").fetchone()["n"] == 3
        historical = conn.execute("SELECT source_item_key, active FROM news_items").fetchone()
        assert dict(historical) == {"source_item_key": "historical", "active": False}
        fetch = conn.execute("SELECT entries_seen, rejection_counts FROM news_source_fetches").fetchone()
        assert fetch["entries_seen"] == 7
        assert dict(fetch["rejection_counts"]) == {
            "future_date": 1,
            "missing_date": 1,
            "per_feed_cap": 4,
            "stale_age": 1,
        }
        assert projection["stories"] == 0
    finally:
        conn.close()


def test_feed_importance_and_latest_are_two_orders_over_same_story_ids(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        authority = source("authority", "Authority", "authority")
        standard = NewsSourceDefinition(
            source_id="standard",
            name="Standard",
            feed_url="https://standard.example/rss",
            reporting_origin="standard",
            tier=4,
            refresh_interval_seconds=120,
        )
        with conn.transaction():
            repository.sync_sources((authority, standard), now_ms=NOW_MS)
            repository.record_fetch_success(
                source=authority,
                entries=(
                    NewsFeedEntry(
                        guid="older-authority",
                        link="https://authority.example/older",
                        title="Central bank warns recession may deepen",
                        published_at_ms=NOW_MS - 60_000,
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
                source=standard,
                entries=(
                    NewsFeedEntry(
                        guid="newer-standard",
                        link="https://standard.example/newer",
                        title="Government announces new tariff schedule",
                        published_at_ms=NOW_MS - 10_000,
                    ),
                ),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repository.rebuild_stories(now_ms=NOW_MS)

        importance = repository.list_feed(category="economic", sort="importance")
        latest = repository.list_feed(category="economic", sort="latest")
        importance_stories = importance["categories"][0]["stories"]
        latest_stories = latest["categories"][0]["stories"]
        assert {row["story_id"] for row in importance_stories} == {row["story_id"] for row in latest_stories}
        assert importance_stories[0]["source_id"] == "authority"
        assert latest_stories[0]["source_id"] == "standard"
    finally:
        conn.close()


def test_degraded_and_failed_brief_refreshes_preserve_last_known_good(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters", "reuters")
        with conn.transaction():
            repository.sync_sources((reuters,), now_ms=NOW_MS)
            repository.record_fetch_success(
                source=reuters,
                entries=(
                    NewsFeedEntry(
                        guid="first",
                        link="https://reuters.example/first",
                        title="Central bank raises interest rate after policy shock",
                        published_at_ms=NOW_MS - 60_000,
                    ),
                ),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        first_worker = NewsWorldBriefWorker(
            settings=SimpleNamespace(statement_timeout_seconds=30.0),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            publisher=FixedBriefPublisher(),
            clock_ms=lambda: NOW_MS + 60_000,
        )
        assert asyncio.run(first_worker.run_once()).processed == 1
        first_publication_id = repository.get_brief(now_ms=NOW_MS + 60_000)["publication"]["publication_id"]

        with conn.transaction():
            repository.record_fetch_success(
                source=reuters,
                entries=(
                    NewsFeedEntry(
                        guid="second",
                        link="https://reuters.example/second",
                        title="Major earthquake strikes coastal region",
                        published_at_ms=NOW_MS + 90_000,
                    ),
                ),
                started_at_ms=NOW_MS + 120_000,
                finished_at_ms=NOW_MS + 120_000,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repository.rebuild_stories(now_ms=NOW_MS + 120_000)
        invalid_worker = NewsWorldBriefWorker(
            settings=SimpleNamespace(statement_timeout_seconds=30.0),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            publisher=InvalidBriefPublisher(),
            clock_ms=lambda: NOW_MS + 180_000,
        )
        invalid_result = asyncio.run(invalid_worker.run_once())
        assert invalid_result.notes["degraded"] is True
        after_invalid = repository.get_brief(now_ms=NOW_MS + 180_000)
        assert after_invalid["publication"]["publication_id"] == first_publication_id
        assert after_invalid["history"][0]["status"] == "degraded"

        with conn.transaction():
            repository.record_fetch_success(
                source=reuters,
                entries=(
                    NewsFeedEntry(
                        guid="third",
                        link="https://reuters.example/third",
                        title="Cyber attack disrupts regional infrastructure",
                        published_at_ms=NOW_MS + 210_000,
                    ),
                ),
                started_at_ms=NOW_MS + 240_000,
                finished_at_ms=NOW_MS + 240_000,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repository.rebuild_stories(now_ms=NOW_MS + 240_000)
        failed_worker = NewsWorldBriefWorker(
            settings=SimpleNamespace(statement_timeout_seconds=30.0),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            publisher=RaisingBriefPublisher(),
            clock_ms=lambda: NOW_MS + 300_000,
        )
        failed_result = asyncio.run(failed_worker.run_once())
        assert failed_result.failed == 1
        assert repository.get_brief(now_ms=NOW_MS + 300_000)["publication"]["publication_id"] == first_publication_id
    finally:
        conn.close()


def test_optional_ai_cache_is_bounded_and_applied_only_by_pipeline_writer(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        source_definition = source("example", "Example", "example")
        with conn.transaction():
            repository.sync_sources((source_definition,), now_ms=NOW_MS)
            repository.record_fetch_success(
                source=source_definition,
                entries=(
                    NewsFeedEntry(
                        guid="plain",
                        link="https://example.example/plain",
                        title="Company publishes quarterly operations update",
                        published_at_ms=NOW_MS - 10_000,
                    ),
                ),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        assert conn.execute("SELECT level FROM news_items").fetchone()["level"] == "info"

        classifier = NewsAiClassifyWorker(
            settings=SimpleNamespace(
                batch_size=20,
                statement_timeout_seconds=30.0,
            ),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            publisher=FixedClassificationPublisher(),
            clock_ms=lambda: NOW_MS + 1_000,
        )
        assert asyncio.run(classifier.run_once()).processed == 1
        # The optional worker writes only its cache; the deterministic pipeline
        # remains the sole NewsItem/Story writer.
        assert conn.execute("SELECT level FROM news_items").fetchone()["level"] == "info"
        with conn.transaction():
            repository.rebuild_stories(now_ms=NOW_MS + 1_000)
        item = conn.execute("SELECT level, category, classification_source FROM news_items").fetchone()
        assert dict(item) == {
            "level": "medium",
            "category": "economic",
            "classification_source": "llm",
        }
    finally:
        conn.close()


def test_destructive_schema_contains_exactly_ten_news_tables(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        tables = {
            row["tablename"]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'news_%'"
            ).fetchall()
        }
        assert tables == {
            "news_sources",
            "news_source_fetches",
            "news_feed_observations",
            "news_items",
            "news_stories",
            "news_story_members",
            "news_story_aliases",
            "news_ai_classification_cache",
            "news_brief_publications",
            "news_brief_current",
        }
    finally:
        conn.close()
