from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from psycopg import pq

import tracefold.news.repository as news_repository_module
from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
)
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.runtime_resources import ProviderGovernor, RuntimeResources
from tracefold.integrations.news_feeds import RssFeedReader
from tracefold.news import (
    NewsBriefDraft,
    NewsFeedEntry,
    NewsFeedFetch,
    NewsIngestWorker,
    NewsInterface,
    NewsRepository,
    NewsSourceDefinition,
    NewsWorldBriefWorker,
    attach_pipeline_runtime_health,
    default_sources,
)
from tracefold.news.brief import brief_fingerprint
from tracefold.news.projection import (
    NewsProjectionService,
    compute_news_component_projection,
    compute_news_edge_block,
    compute_news_identity_feature,
    merge_final_edges,
    plan_news_edge_pairs,
    rebuild_all_news_for_maintenance,
)

NOW_MS = 1_779_000_000_000


class SingleConnectionDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def worker_session(self, *_args: Any, **_kwargs: Any):
        return repository_session_for_connection(self.conn)


class OneItemReader:
    def fetch(
        self,
        *,
        source: NewsSourceDefinition,
        etag: str | None,
        last_modified: str | None,
    ) -> NewsFeedFetch:
        del etag, last_modified
        return NewsFeedFetch(
            status_code=200,
            fetch_path="direct",
            entries=(
                NewsFeedEntry(
                    guid="story-1",
                    link=f"https://{source.source_id}.example/story-1",
                    title="Iran threatens to close Strait of Hormuz",
                    description="Officials issued a formal statement.",
                    published_at_ms=NOW_MS - 60_000,
                    raw={"source": source.name},
                ),
            ),
        )

    def close(self) -> None:
        return None


class FixedBriefPublisher:
    calls = 0

    def publish(self, stories: list[Any]) -> NewsBriefDraft:
        self.calls += 1
        return NewsBriefDraft(
            lead=f"今日重点：{stories[0].title} [1]",
            lines=tuple(f"第{index}条：{story.title} [{index}]" for index, story in enumerate(stories, 1)),
            provider="test",
            model="test-model",
            raw_response="{}",
        )

    def close(self) -> None:
        return None


class RaisingBriefPublisher:
    calls = 0

    def publish(self, stories: list[Any]) -> NewsBriefDraft:
        del stories
        self.calls += 1
        raise RuntimeError("provider unavailable")

    def close(self) -> None:
        return None


def source(
    source_id: str,
    name: str,
    *,
    tier: int = 1,
    memberships: tuple[str, ...] = ("politics",),
) -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id=source_id,
        name=name,
        feed_url=f"https://{source_id}.example/rss",
        tier=tier,
        memberships=memberships,
        refresh_interval_seconds=120,
    )


def record(
    repository: NewsRepository,
    definition: NewsSourceDefinition,
    *,
    guid: str,
    title: str,
    published_at_ms: int,
    started_at_ms: int = NOW_MS,
    reporting_origin: str | None = None,
    link: str | None = None,
    description: str = "Durable source description for this report.",
) -> dict[str, int]:
    return repository.record_fetch_success(
        source=definition,
        entries=(
            NewsFeedEntry(
                guid=guid,
                link=link or f"https://{definition.source_id}.example/{guid}",
                title=title,
                description=description,
                published_at_ms=published_at_ms,
                reporting_origin=reporting_origin,
                raw={"guid": guid},
            ),
        ),
        started_at_ms=started_at_ms,
        finished_at_ms=started_at_ms,
        status_code=200,
        fetch_path="direct",
        direct_error_code=None,
        etag=None,
        last_modified=None,
        not_modified=False,
    )


def brief_settings() -> SimpleNamespace:
    return SimpleNamespace(
        statement_timeout_seconds=30.0,
        max_attempts=3,
    )


def test_pipeline_persists_current_claim_time_for_each_fetch_cycle(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        clock = SimpleNamespace(now_ms=NOW_MS)
        pipeline = NewsIngestWorker(
            settings=SimpleNamespace(
                batch_size=20,
                fetch_concurrency=1,
                statement_timeout_seconds=30.0,
            ),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            sources=(source("reuters", "Reuters"),),
            feed_reader=OneItemReader(),
            clock_ms=lambda: clock.now_ms,
        )
        resources = RuntimeResources()
        pipeline.bind_runtime_resources(resources)
        pipeline.bind_provider_governor(ProviderGovernor())
        try:
            assert asyncio.run(pipeline.run_once()).processed == 1
            clock.now_ms = NOW_MS + 120_000
            assert asyncio.run(pipeline.run_once()).processed == 1
        finally:
            resources.close()
        assert conn.execute(
            """
            SELECT started_at_ms
              FROM news_source_fetches
             ORDER BY started_at_ms
            """
        ).fetchall() == [
            {"started_at_ms": NOW_MS},
            {"started_at_ms": NOW_MS + 120_000},
        ]
    finally:
        conn.close()


def test_incremental_news_projection_persists_edges_and_publishes_only_affected_closure(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters")
        ap = source("ap", "AP")
        with conn.transaction():
            repository.sync_sources((reuters, ap), now_ms=NOW_MS)
            record(
                repository,
                reuters,
                guid="iran-r",
                title="Iran threatens to close Strait of Hormuz",
                published_at_ms=NOW_MS - 60_000,
            )
            record(
                repository,
                ap,
                guid="iran-a",
                title="Iran threatens to close Strait of Hormuz — live updates",
                published_at_ms=NOW_MS - 30_000,
                started_at_ms=NOW_MS + 1,
            )

        service = NewsProjectionService(db=SingleConnectionDB(conn))
        runtime_id = str(uuid4())
        projection_now_ms = NOW_MS + 60_001
        processed = 0
        while row := conn.execute(
            """
            SELECT bucket_id
              FROM news_projection_frontiers
             WHERE status IN ('dirty', 'retry_wait')
               AND deadline_at_ms <= %s
             ORDER BY deadline_at_ms, bucket_id
             LIMIT 1
            """,
            (projection_now_ms,),
        ).fetchone():
            claim = service.claim(
                bucket_id=str(row["bucket_id"]),
                runtime_id=runtime_id,
                now_ms=projection_now_ms,
            )
            assert claim is not None
            target = service.load_target(claim, now_ms=projection_now_ms)
            feature = compute_news_identity_feature(target)
            context = service.load_context(
                claim,
                feature,
                now_ms=projection_now_ms,
            )
            edge_plan = plan_news_edge_pairs(context)
            new_edges = compute_news_edge_block(edge_plan["recompute_pairs"])
            edge_plan["new_edges"] = new_edges
            projection = compute_news_component_projection(
                {
                    **context,
                    "final_edges": merge_final_edges(
                        existing_edges=context["existing_edges"],
                        affected_pairs=edge_plan["affected_pairs"],
                        new_edges=new_edges,
                    ),
                }
            )
            result = service.publish(
                claim,
                feature=feature,
                context=context,
                edge_plan=edge_plan,
                projection=projection,
                now_ms=projection_now_ms,
            )
            assert result["projection_status"] == "published"
            processed += 1

        assert processed == 2
        assert conn.execute("SELECT count(*) AS n FROM news_identity_features").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM news_similarity_edges").fetchone()["n"] == 1
        assert conn.execute(
            """
            SELECT item_count, source_count
              FROM news_stories
             WHERE active
            """
        ).fetchall() == [{"item_count": 2, "source_count": 2}]
        assert conn.execute("SELECT count(*) AS n FROM news_story_members WHERE current").fetchone()["n"] == 2

        score_row = conn.execute(
            """
            SELECT bucket_id, deadline_at_ms
              FROM news_projection_frontiers
             WHERE bucket_id LIKE 'score:%'
             ORDER BY bucket_id
            """
        ).fetchone()
        assert score_row is not None
        score_now_ms = int(score_row["deadline_at_ms"])
        score_claim = service.claim(
            bucket_id=str(score_row["bucket_id"]),
            runtime_id=runtime_id,
            now_ms=score_now_ms,
        )
        assert score_claim is not None
        loaded_score = service.load_score(score_claim, now_ms=score_now_ms)
        score_context = loaded_score["context"]
        score_feature = loaded_score["feature"]
        score_projection = compute_news_component_projection(
            {
                **score_context,
                "final_edges": [],
            }
        )
        score_result = service.publish(
            score_claim,
            feature=score_feature,
            context=score_context,
            edge_plan={
                "affected_pairs": [],
                "recompute_pairs": [],
                "new_edges": [],
                "pair_blocks": 0,
            },
            projection=score_projection,
            now_ms=score_now_ms,
        )
        assert score_result["projection_status"] == "published"
        assert conn.execute(
            """
            SELECT status, deadline_at_ms
              FROM news_projection_frontiers
             WHERE bucket_id = %s
            """,
            (score_row["bucket_id"],),
        ).fetchone() == {
            "status": "dirty",
            "deadline_at_ms": score_now_ms + 60 * 60 * 1000,
        }
    finally:
        conn.close()


def test_wallstengine_rss_runs_reader_worker_receipts_and_duplicate_zero_writes(
    tmp_path,
    monkeypatch,
) -> None:
    rss_body = b"""
    <rss version="2.0"><channel>
      <item>
        <guid>wall-quote</guid>
        <link>https://x.com/wallstengine/status/301</link>
        <title>Fed pricing now implies two cuts before year end</title>
        <description><![CDATA[
          Fed pricing now implies two cuts before year end
          <hr>
          Federal Reserve: The committee will remain data dependent
          while monitoring inflation and employment risks.
        ]]></description>
        <pubDate>Sun, 17 May 2026 06:35:00 GMT</pubDate>
      </item>
      <item>
        <guid>wall-302</guid>
        <link>https://x.com/wallstengine/status/302</link>
        <title>Government announces emergency tariff package</title>
        <pubDate>Sun, 17 May 2026 06:34:00 GMT</pubDate>
      </item>
      <item>
        <guid>wall-303</guid>
        <link>https://x.com/wallstengine/status/303</link>
        <title>Oil markets brace for a volatile opening</title>
        <pubDate>Sun, 17 May 2026 06:33:00 GMT</pubDate>
      </item>
      <item>
        <guid>wall-304</guid>
        <link>https://x.com/wallstengine/status/304</link>
        <title>Equity futures hold near the overnight range</title>
        <pubDate>Sun, 17 May 2026 06:32:00 GMT</pubDate>
      </item>
      <item>
        <guid>wall-305</guid>
        <link>https://x.com/wallstengine/status/305</link>
        <title>Treasury yields edge lower before the open</title>
        <pubDate>Sun, 17 May 2026 06:31:00 GMT</pubDate>
      </item>
      <item>
        <guid>wall-older-than-cap</guid>
        <link>https://x.com/wallstengine/status/300</link>
        <title>This sixth entry must not enter the pipeline</title>
        <pubDate>Sun, 17 May 2026 06:30:00 GMT</pubDate>
      </item>
    </channel></rss>
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "rsshub"
        assert request.url.path.endswith(
            "/twitter/user/wallstengine/includeReplies=0&includeRts=0&showRetweetTextInTitle=1&showQuotedInTitle=0"
        )
        if len(requests) == 2:
            assert request.headers["if-none-match"] == '"wall-etag-1"'
            return httpx.Response(304, headers={"etag": '"wall-etag-1"'})
        assert len(requests) in {1, 3}
        if len(requests) == 3:
            assert request.headers["if-none-match"] == '"wall-etag-1"'
        return httpx.Response(
            200,
            content=rss_body,
            headers={"etag": f'"wall-etag-{1 if len(requests) == 1 else 2}"'},
        )

    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
    )
    cluster_transaction_states: list[pq.TransactionStatus] = []
    original_cluster_texts = news_repository_module.cluster_texts

    def observed_cluster_texts(titles):
        cluster_transaction_states.append(conn.info.transaction_status)
        return original_cluster_texts(titles)

    monkeypatch.setattr(news_repository_module, "cluster_texts", observed_cluster_texts)
    try:
        migrate(conn)
        wallstengine = next(definition for definition in default_sources() if definition.name == "WallStEngine")
        clock = SimpleNamespace(now_ms=NOW_MS)
        pipeline = NewsIngestWorker(
            settings=SimpleNamespace(
                batch_size=1,
                fetch_concurrency=1,
                statement_timeout_seconds=30.0,
            ),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            sources=(wallstengine,),
            feed_reader=reader,
            clock_ms=lambda: clock.now_ms,
        )
        resources = RuntimeResources()
        pipeline.bind_runtime_resources(resources)
        pipeline.bind_provider_governor(ProviderGovernor())

        try:
            first = asyncio.run(pipeline.run_once())
            clock.now_ms += wallstengine.refresh_interval_seconds * 1000
            not_modified = asyncio.run(pipeline.run_once())
            clock.now_ms += wallstengine.refresh_interval_seconds * 1000
            duplicate = asyncio.run(pipeline.run_once())
        finally:
            resources.close()

        assert first.notes["entries_seen"] == 6
        assert first.notes["observations_inserted"] == 5
        assert first.notes["items_inserted"] == 5
        assert first.notes["projection_frontiers_written"] == 5
        assert not_modified.notes["entries_seen"] == 0
        assert not_modified.notes["observations_inserted"] == 0
        assert not_modified.notes["items_inserted"] == 0
        assert not_modified.notes["projection_frontiers_written"] == 0
        assert duplicate.notes["entries_seen"] == 6
        assert duplicate.notes["observations_inserted"] == 5
        assert duplicate.notes["items_inserted"] == 0
        assert duplicate.notes["items_updated"] == 0
        assert duplicate.notes["projection_frontiers_written"] == 0
        assert cluster_transaction_states == []
        assert conn.execute("SELECT count(*) AS n FROM news_stories").fetchone()["n"] == 0
        assert (
            conn.execute(
                """
            SELECT count(*) AS n
              FROM news_projection_frontiers
             WHERE bucket_id LIKE 'identity:%'
               AND status = 'dirty'
            """
            ).fetchone()["n"]
            == 5
        )

        fetches = conn.execute(
            """
            SELECT status, entries_seen, observations_inserted,
                   items_inserted, items_updated, rejection_counts
              FROM news_source_fetches
             WHERE source_id = %s
             ORDER BY started_at_ms
            """,
            (wallstengine.source_id,),
        ).fetchall()
        assert [row["status"] for row in fetches] == [
            "success",
            "not_modified",
            "success",
        ]
        assert fetches[0]["rejection_counts"] == {"per_feed_cap": 1}
        assert fetches[2]["rejection_counts"] == {
            "duplicate": 5,
            "per_feed_cap": 1,
        }
        assert conn.execute(
            "SELECT count(*) AS count FROM news_feed_observations WHERE source_id = %s",
            (wallstengine.source_id,),
        ).fetchone() == {"count": 10}
        assert conn.execute(
            "SELECT count(*) AS count FROM news_items WHERE source_id = %s",
            (wallstengine.source_id,),
        ).fetchone() == {"count": 5}
        quote = conn.execute(
            """
            SELECT title, description
              FROM news_items
             WHERE source_id = %s AND source_item_key = 'wall-quote'
            """,
            (wallstengine.source_id,),
        ).fetchone()
        assert quote["title"] == "Fed pricing now implies two cuts before year end"
        assert "Federal Reserve" in quote["description"]
        assert all(
            candidate["representative_source_id"] == wallstengine.source_id
            for candidate in NewsRepository(conn).brief_candidates()
        )
    finally:
        reader.close()
        conn.close()


def test_item_story_feed_and_brief_form_one_persisted_chain(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters")
        ap = source("ap", "AP")
        with conn.transaction():
            repository.sync_sources((reuters, ap), now_ms=NOW_MS)
            record(
                repository,
                reuters,
                guid="iran-r",
                title="Iran threatens to close Strait of Hormuz",
                published_at_ms=NOW_MS - 60_000,
            )
            record(
                repository,
                ap,
                guid="iran-a",
                title="Iran threatens to close Strait of Hormuz — live updates",
                published_at_ms=NOW_MS - 30_000,
            )
            record(
                repository,
                reuters,
                guid="rates",
                title="Central bank raises interest rate after policy shock",
                published_at_ms=NOW_MS - 20_000,
                started_at_ms=NOW_MS + 1,
            )
            record(
                repository,
                ap,
                guid="quake",
                title="Major earthquake strikes coastal region",
                published_at_ms=NOW_MS - 10_000,
                started_at_ms=NOW_MS + 1,
            )
            projection = repository.rebuild_stories(now_ms=NOW_MS)
        assert projection["stories"] == 3
        assert conn.execute("SELECT count(*) AS n FROM news_feed_observations").fetchone()["n"] == 4
        assert conn.execute("SELECT count(*) AS n FROM news_items").fetchone()["n"] == 4

        interface = NewsInterface(repository)
        feed = interface.get_feed(sort="importance")
        assert len(feed["stories"]) == 3
        assert feed["has_more"] is False
        iran = next(row for row in feed["stories"] if row["item_count"] == 2)
        assert iran["source_count"] == 2
        detail = interface.get_story(story_id=iran["story_id"])
        assert detail is not None
        assert len(detail["members"]) == 2
        first_member_page = interface.get_story(
            story_id=iran["story_id"],
            members_limit=1,
        )
        assert first_member_page is not None
        assert first_member_page["members_page"]["has_more"] is True
        second_member_page = interface.get_story(
            story_id=iran["story_id"],
            members_limit=1,
            members_cursor=first_member_page["members_page"]["next_cursor"],
        )
        assert second_member_page is not None
        assert second_member_page["members_page"]["has_more"] is False
        assert {
            first_member_page["members"][0]["item_id"],
            second_member_page["members"][0]["item_id"],
        } == {member["item_id"] for member in detail["members"]}
        first_source_page = interface.get_sources(limit=1)
        assert first_source_page["page"]["has_more"] is True
        second_source_page = interface.get_sources(
            limit=1,
            cursor=first_source_page["page"]["next_cursor"],
        )
        assert second_source_page["items"][0]["source_id"] != first_source_page["items"][0]["source_id"]

        publisher = FixedBriefPublisher()
        worker = NewsWorldBriefWorker(
            settings=brief_settings(),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            publisher=publisher,
            clock_ms=lambda: NOW_MS + 120_000,
        )
        assert worker.run_once_sync().processed == 1
        brief = interface.get_world_brief(now_ms=NOW_MS + 120_000)
        assert brief["state"] == "ready"
        assert len(brief["publication"]["selected_story_ids"]) == 3
        assert publisher.calls == 1
        assert worker.run_once_sync().skipped == 1
        assert publisher.calls == 1
    finally:
        conn.close()


def test_source_inventory_hard_cut_disables_and_unserves_retired_sources(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        retained = source("reuters", "Reuters")
        retired = source("regional-general", "Regional General", tier=4, memberships=("asia",))
        with conn.transaction():
            repository.sync_sources((retained, retired), now_ms=NOW_MS)
            record(
                repository,
                retained,
                guid="retained",
                title="Federal Reserve signals policy decision",
                published_at_ms=NOW_MS - 60_000,
            )
            record(
                repository,
                retired,
                guid="retired",
                title="Local festival opens in regional capital",
                published_at_ms=NOW_MS - 30_000,
            )
            assert repository.rebuild_stories(now_ms=NOW_MS)["stories"] == 2

        with conn.transaction():
            repository.sync_sources((retained,), now_ms=NOW_MS + 1)
            projection = repository.rebuild_stories(now_ms=NOW_MS + 1)

        assert projection["stories"] == 1
        assert conn.execute(
            "SELECT enabled FROM news_sources WHERE source_id = %s",
            (retired.source_id,),
        ).fetchone() == {"enabled": False}
        assert conn.execute(
            "SELECT active FROM news_items WHERE source_id = %s",
            (retired.source_id,),
        ).fetchone() == {"active": False}
        assert [row["source_id"] for row in repository.list_sources()["items"]] == [retained.source_id]
        feed = repository.list_feed()
        assert len(feed["stories"]) == 1
        assert feed["stories"][0]["source_id"] == retained.source_id
        assert all(
            candidate["representative_source_id"] == retained.source_id for candidate in repository.brief_candidates()
        )
    finally:
        conn.close()


def test_wallstengine_uses_ordinary_item_story_classification_and_brief_rules(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        wallstengine = next(definition for definition in default_sources() if definition.name == "WallStEngine")
        reuters = source("reuters", "Reuters")
        with conn.transaction():
            repository.sync_sources((wallstengine, reuters), now_ms=NOW_MS)
            record(
                repository,
                wallstengine,
                guid="wall-only",
                title="Positioning looks stretched into the closing bell",
                description=(
                    "The quoted report says the central bank raised interest rates "
                    "after an emergency inflation meeting."
                ),
                published_at_ms=NOW_MS - 40_000,
                link="https://x.com/wallstengine/status/201",
            )
            record(
                repository,
                wallstengine,
                guid="wall-tariff",
                title="Government announces emergency tariff package",
                published_at_ms=NOW_MS - 30_000,
                started_at_ms=NOW_MS + 1,
                link="https://x.com/wallstengine/status/202",
            )
            record(
                repository,
                reuters,
                guid="reuters-tariff",
                title="Government announces emergency tariff package",
                published_at_ms=NOW_MS - 20_000,
                started_at_ms=NOW_MS + 2,
            )
            record(
                repository,
                reuters,
                guid="reuters-rate",
                title="Central bank raises interest rate after policy shock",
                published_at_ms=NOW_MS - 10_000,
                started_at_ms=NOW_MS + 3,
            )
            repository.rebuild_stories(now_ms=NOW_MS)

        wall_item = conn.execute(
            """
            SELECT title, description, category, source_id
              FROM news_items
             WHERE source_id = %s AND source_item_key = 'wall-only'
            """,
            (wallstengine.source_id,),
        ).fetchone()
        assert wall_item["category"] == "general"
        assert "central bank raised interest rates" in wall_item["description"]

        corroborated = conn.execute(
            """
            SELECT source_count
              FROM news_stories
             WHERE active
               AND representative_title = 'Government announces emergency tariff package'
            """
        ).fetchone()
        assert corroborated == {"source_count": 2}

        candidates = repository.brief_candidates()
        wall_only = next(
            candidate
            for candidate in candidates
            if candidate["representative_title"] == "Positioning looks stretched into the closing bell"
        )
        assert wall_only["representative_source_id"] == wallstengine.source_id
        assert wall_only["source_count"] == 1
        assert wall_only["category"] == "general"
        assert {
            key for key in wall_only if "wallstengine" in str(key).casefold() or "social" in str(key).casefold()
        } == set()
    finally:
        conn.close()


def test_pubdate_only_drift_writes_observation_but_not_item_or_story(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters")
        with conn.transaction():
            repository.sync_sources((reuters,), now_ms=NOW_MS)
            first = record(
                repository,
                reuters,
                guid="same-guid",
                title="Iran threatens to close Strait of Hormuz",
                published_at_ms=NOW_MS - 60_000,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        before_item = conn.execute(
            """
            SELECT published_at_ms, last_observed_at_ms, updated_at_ms
              FROM news_items
            """
        ).fetchone()
        before_story = conn.execute("SELECT story_id, state_fingerprint, updated_at_ms FROM news_stories").fetchone()
        with conn.transaction():
            second = record(
                repository,
                reuters,
                guid="same-guid",
                title="Iran threatens to close Strait of Hormuz",
                published_at_ms=NOW_MS + 30_000,
                started_at_ms=NOW_MS + 120_000,
            )
            projection = repository.rebuild_stories(now_ms=NOW_MS + 120_000)
        assert first["items_inserted"] == 1
        assert second["items_inserted"] == 0
        assert second["items_updated"] == 0
        assert projection["story_writes"] == 0
        assert projection["projection_status"] == "unchanged_input"
        assert projection["clustered"] == 0
        assert (
            conn.execute("SELECT published_at_ms, last_observed_at_ms, updated_at_ms FROM news_items").fetchone()
            == before_item
        )
        assert (
            conn.execute("SELECT story_id, state_fingerprint, updated_at_ms FROM news_stories").fetchone()
            == before_story
        )
        assert conn.execute("SELECT count(*) AS n FROM news_feed_observations").fetchone()["n"] == 2
        with conn.transaction():
            next_epoch = repository.rebuild_stories(now_ms=NOW_MS + 3_600_000)
        assert next_epoch["projection_status"] == "rebuilt"
        assert next_epoch["clustered"] == 1
    finally:
        conn.close()


def test_corroboration_counts_physical_sources_not_reporting_origin(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        aggregator = source("google-world", "Google World")
        direct = source("direct-feed", "Direct Feed")
        with conn.transaction():
            repository.sync_sources((aggregator, direct), now_ms=NOW_MS)
            record(
                repository,
                aggregator,
                guid="copy-1",
                title="Iran threatens to close Strait of Hormuz",
                published_at_ms=NOW_MS - 60_000,
                reporting_origin="reuters",
            )
            record(
                repository,
                aggregator,
                guid="copy-2",
                title="Iran threatens to close Strait of Hormuz — live updates",
                published_at_ms=NOW_MS - 50_000,
                reporting_origin="ap",
                started_at_ms=NOW_MS + 1,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        story = conn.execute("SELECT * FROM news_stories WHERE active").fetchone()
        assert story["item_count"] == 2
        assert story["source_count"] == 1
        assert dict(story["importance_factors"])["physical_source_count"] == 1

        with conn.transaction():
            record(
                repository,
                direct,
                guid="direct",
                title="Iran threatens to close Strait of Hormuz amid blockade",
                published_at_ms=NOW_MS - 40_000,
                reporting_origin="reuters",
                started_at_ms=NOW_MS + 2,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        story = conn.execute("SELECT * FROM news_stories WHERE active").fetchone()
        assert story["source_count"] == 2
        assert dict(story["importance_factors"])["physical_source_count"] == 2
    finally:
        conn.close()


def test_live_alias_unions_temporary_clusters_before_materialization(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters")
        with conn.transaction():
            repository.sync_sources((reuters,), now_ms=NOW_MS)
            record(
                repository,
                reuters,
                guid="a",
                title="Central bank raises interest rate",
                published_at_ms=NOW_MS - 60_000,
            )
            record(
                repository,
                reuters,
                guid="b",
                title="Central bank raises interest rate — live updates",
                published_at_ms=NOW_MS - 50_000,
                started_at_ms=NOW_MS + 1,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        original_story_id = conn.execute("SELECT story_id FROM news_stories WHERE active").fetchone()["story_id"]

        with conn.transaction():
            record(
                repository,
                reuters,
                guid="a",
                title="Wildfire forces evacuation of coastal town",
                published_at_ms=NOW_MS - 40_000,
                started_at_ms=NOW_MS + 120_000,
            )
            record(
                repository,
                reuters,
                guid="b",
                title="Technology company releases new processor",
                published_at_ms=NOW_MS - 30_000,
                started_at_ms=NOW_MS + 120_001,
            )
            projection = repository.rebuild_stories(now_ms=NOW_MS + 120_000)
        assert projection["temporary_clusters"] == 2
        assert projection["stories"] == 1
        story = conn.execute("SELECT story_id, item_count FROM news_stories WHERE active").fetchone()
        assert story == {"story_id": original_story_id, "item_count": 2}
        owners = conn.execute(
            """
            SELECT item_id, count(*) AS owners
              FROM news_story_members
             WHERE current
             GROUP BY item_id
            """
        ).fetchall()
        assert {row["owners"] for row in owners} == {1}
    finally:
        conn.close()


def test_flat_feed_uses_keyset_order_and_filter_before_pagination(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        authority = source("authority", "Authority", tier=1)
        standard = source("standard", "Standard", tier=4)
        with conn.transaction():
            repository.sync_sources((authority, standard), now_ms=NOW_MS)
            record(
                repository,
                authority,
                guid="older",
                title="Central bank warns recession may deepen",
                published_at_ms=NOW_MS - 60_000,
            )
            record(
                repository,
                standard,
                guid="newer",
                title="Government announces new tariff schedule",
                published_at_ms=NOW_MS - 10_000,
                started_at_ms=NOW_MS + 1,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        importance = repository.list_feed(
            category="economic",
            sort="importance",
            limit=1,
        )
        latest = repository.list_feed(
            category="economic",
            sort="latest",
            limit=1,
        )
        assert importance["stories"][0]["source_id"] == "authority"
        assert latest["stories"][0]["source_id"] == "standard"
        assert importance["has_more"] is True
        second = repository.list_feed(
            category="economic",
            sort="importance",
            limit=1,
            cursor=importance["next_cursor"],
        )
        assert second["stories"][0]["source_id"] == "standard"
        assert second["has_more"] is False
    finally:
        conn.close()


def test_brief_states_are_evidence_driven_and_keep_last_known_good(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters")
        ap = source("ap", "AP")
        with conn.transaction():
            repository.sync_sources((reuters, ap), now_ms=NOW_MS)
            record(
                repository,
                reuters,
                guid="one",
                title="Central bank raises interest rate after policy shock",
                published_at_ms=NOW_MS - 30_000,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        publisher = FixedBriefPublisher()
        worker = NewsWorldBriefWorker(
            settings=brief_settings(),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            publisher=publisher,
            clock_ms=lambda: NOW_MS + 60_000,
        )
        insufficient = worker.run_once_sync()
        assert insufficient.skipped == 1
        assert insufficient.notes["model_calls"] == 0
        assert publisher.calls == 0
        assert repository.get_brief(now_ms=NOW_MS + 60_000)["state"] == ("insufficient_material")
        first_updated_at_ms = conn.execute("SELECT updated_at_ms FROM news_brief_runs").fetchone()["updated_at_ms"]
        worker.clock_ms = lambda: NOW_MS + 90_000
        assert worker.run_once_sync().skipped == 1
        assert publisher.calls == 0
        assert (
            conn.execute("SELECT updated_at_ms FROM news_brief_runs").fetchone()["updated_at_ms"] == first_updated_at_ms
        )

        with conn.transaction():
            record(
                repository,
                ap,
                guid="two",
                title="Major earthquake strikes coastal region",
                published_at_ms=NOW_MS - 20_000,
                started_at_ms=NOW_MS + 1,
            )
            record(
                repository,
                reuters,
                guid="three",
                title="Cyber attack disrupts regional infrastructure",
                published_at_ms=NOW_MS - 10_000,
                started_at_ms=NOW_MS + 2,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        assert worker.run_once_sync().processed == 1
        ready = repository.get_brief(now_ms=NOW_MS + 60_000)
        assert ready["state"] == "ready"
        publication_id = ready["publication"]["publication_id"]

        with conn.transaction():
            record(
                repository,
                ap,
                guid="four",
                title="Government announces emergency tariff package",
                published_at_ms=NOW_MS + 70_000,
                started_at_ms=NOW_MS + 120_000,
            )
            repository.rebuild_stories(now_ms=NOW_MS + 120_000)
        stale = repository.get_brief(now_ms=NOW_MS + 120_000)
        assert stale["state"] == "stale_fallback"
        assert stale["publication"]["publication_id"] == publication_id

        failing = NewsWorldBriefWorker(
            settings=brief_settings(),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            publisher=RaisingBriefPublisher(),
            clock_ms=lambda: NOW_MS + 120_000,
        )
        assert failing.run_once_sync().failed == 1
        after_failure = repository.get_brief(now_ms=NOW_MS + 120_000)
        assert after_failure["state"] == "stale_fallback"
        assert after_failure["publication"]["publication_id"] == publication_id
        assert after_failure["latest_run"]["status"] == "failed"
    finally:
        conn.close()


def test_news_status_exposes_warming_coverage_paths_and_complete_rebuild(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        direct = source("direct", "Direct")
        relayed = source("relayed", "Relayed")
        with conn.transaction():
            repository.sync_sources((direct, relayed), now_ms=NOW_MS)
        warming = repository.health_snapshot(now_ms=NOW_MS)
        assert warming["status"] == "warming"
        assert warming["layers"]["ingest"]["configured_sources"] == 2
        assert warming["layers"]["ingest"]["attempted_sources"] == 0

        with conn.transaction():
            repository.record_fetch_success(
                source=direct,
                entries=(),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                fetch_path="direct",
                direct_error_code=None,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repository.record_fetch_failure(
                source_id=relayed.source_id,
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                error=RuntimeError("relay unavailable"),
                status_code=503,
                fetch_path="relay",
                direct_error_code="http_403",
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        degraded = repository.health_snapshot(now_ms=NOW_MS)
        ingest = degraded["layers"]["ingest"]
        assert degraded["status"] == "degraded"
        assert ingest["terminal_sources"] == 2
        assert ingest["empty_sources"] == 1
        assert ingest["failing_sources"] == 1
        assert ingest["direct_success_sources"] == 1
        assert ingest["relay_success_sources"] == 0
        assert ingest["both_failed_sources"] == 1
        assert degraded["layers"]["story"]["invariant_error_count"] == 0

        attach_pipeline_runtime_health(
            degraded,
            worker_status={
                "enabled": True,
                "effective_status": "stopped",
                "last_finished_at_ms": NOW_MS,
                "last_error": None,
                "last_result": {
                    "failed": 0,
                    "dead": 0,
                    "notes": {
                        "items": 0,
                        "stories": 0,
                        "story_writes": 0,
                        "membership_writes": 0,
                    },
                },
            },
            now_ms=NOW_MS,
        )
        story = degraded["layers"]["story"]
        assert story["last_complete_rebuild_at_ms"] == NOW_MS
        assert story["last_complete_rebuild_age_ms"] == 0
    finally:
        conn.close()


def test_news_maintenance_rebuild_seeds_incremental_features_and_edges(
    tmp_path,
) -> None:
    conn = connect_postgres_test(
        tmp_path / "postgres_test_db",
        read_only=False,
    )
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters")
        ap = source("ap", "AP")
        with conn.transaction():
            repository.sync_sources((reuters, ap), now_ms=NOW_MS)
            record(
                repository,
                reuters,
                guid="maintenance-r",
                title="Iran threatens to close Strait of Hormuz",
                published_at_ms=NOW_MS - 60_000,
            )
            record(
                repository,
                ap,
                guid="maintenance-a",
                title="Iran threatens to close Strait of Hormuz — live updates",
                published_at_ms=NOW_MS - 30_000,
                started_at_ms=NOW_MS + 1,
            )

        result = rebuild_all_news_for_maintenance(
            db=SingleConnectionDB(conn),
            now_ms=NOW_MS + 60_001,
        )

        assert result["projection_status"] == "rebuilt"
        assert result["items_seeded"] == 2
        assert result["active_features"] == 2
        assert result["similarity_edges"] == 1
        assert result["active_stories"] == 1
    finally:
        conn.close()


def test_wallstengine_empty_success_and_failure_use_ordinary_ingest_health(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        wallstengine = next(definition for definition in default_sources() if definition.name == "WallStEngine")
        with conn.transaction():
            repository.sync_sources((wallstengine,), now_ms=NOW_MS)
            repository.record_fetch_success(
                source=wallstengine,
                entries=(),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                fetch_path="direct",
                direct_error_code=None,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        empty = repository.health_snapshot(now_ms=NOW_MS)
        assert empty["layers"]["ingest"]["empty_sources"] == 1
        assert empty["layers"]["ingest"]["failing_sources"] == 0

        with conn.transaction():
            repository.record_fetch_failure(
                source_id=wallstengine.source_id,
                started_at_ms=NOW_MS + 120_000,
                finished_at_ms=NOW_MS + 120_000,
                error=RuntimeError("RSSHub credentials unavailable"),
                status_code=503,
                fetch_path="direct",
                direct_error_code=None,
            )
        failed = repository.health_snapshot(now_ms=NOW_MS + 120_000)
        ingest = failed["layers"]["ingest"]
        assert failed["status"] == "degraded"
        assert ingest["failing_sources"] == 1
        assert ingest["relay_success_sources"] == 0
    finally:
        conn.close()


def test_news_status_detects_persisted_story_aggregate_corruption(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters")
        with conn.transaction():
            repository.sync_sources((reuters,), now_ms=NOW_MS)
            record(
                repository,
                reuters,
                guid="one",
                title="Major earthquake strikes coastal region",
                published_at_ms=NOW_MS - 10_000,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
            conn.execute(
                """
                UPDATE news_stories
                   SET item_count = item_count + 1
                 WHERE active
                """
            )
            repository.refresh_projection_summary_for_maintenance(now_ms=NOW_MS)

        health = repository.health_snapshot(now_ms=NOW_MS)
        story = health["layers"]["story"]
        assert story["status"] == "degraded"
        assert story["invalid_story_aggregate_count"] == 1
        assert story["invariant_error_count"] == 1
        assert "story_aggregate_invalid" in story["reasons"]
    finally:
        conn.close()


def test_expired_brief_lease_is_publicly_failed_not_running(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters")
        ap = source("ap", "AP")
        with conn.transaction():
            repository.sync_sources((reuters, ap), now_ms=NOW_MS)
            record(
                repository,
                reuters,
                guid="one",
                title="Central bank raises interest rate after policy shock",
                published_at_ms=NOW_MS - 30_000,
            )
            record(
                repository,
                ap,
                guid="two",
                title="Major earthquake strikes coastal region",
                published_at_ms=NOW_MS - 20_000,
                started_at_ms=NOW_MS + 1,
            )
            record(
                repository,
                reuters,
                guid="three",
                title="Cyber attack disrupts regional infrastructure",
                published_at_ms=NOW_MS - 10_000,
                started_at_ms=NOW_MS + 2,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
            candidates = repository.brief_candidates()
            claim = repository.claim_brief_run(
                fingerprint=brief_fingerprint(candidates),
                story_count=len(candidates),
                source_count=2,
                now_ms=NOW_MS,
                max_attempts=3,
                lease_owner="test-runtime",
            )
            assert claim is not None
        expired = repository.get_brief(now_ms=NOW_MS + 121_000)
        assert expired["state"] == "failed"
        assert expired["latest_run"]["status"] == "failed"
        assert expired["latest_run"]["last_error"] == "brief_lease_expired"
    finally:
        conn.close()


def test_brief_publication_rejects_changed_source_fingerprint(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source("reuters", "Reuters")
        ap = source("ap", "AP")
        with conn.transaction():
            repository.sync_sources((reuters, ap), now_ms=NOW_MS)
            record(
                repository,
                reuters,
                guid="one",
                title="Central bank raises interest rate after policy shock",
                published_at_ms=NOW_MS - 30_000,
            )
            record(
                repository,
                ap,
                guid="two",
                title="Major earthquake strikes coastal region",
                published_at_ms=NOW_MS - 20_000,
                started_at_ms=NOW_MS + 1,
            )
            record(
                repository,
                reuters,
                guid="three",
                title="Cyber attack disrupts regional infrastructure",
                published_at_ms=NOW_MS - 10_000,
                started_at_ms=NOW_MS + 2,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
        candidates = repository.brief_candidates()
        fingerprint = brief_fingerprint(candidates)
        with conn.transaction():
            claim = repository.claim_brief_run(
                fingerprint=fingerprint,
                story_count=len(candidates),
                source_count=2,
                now_ms=NOW_MS,
                max_attempts=3,
                lease_owner="test-runtime",
            )
        assert claim is not None
        with conn.transaction():
            record(
                repository,
                ap,
                guid="four",
                title="Government announces emergency tariff package",
                published_at_ms=NOW_MS + 30_000,
                started_at_ms=NOW_MS + 30_000,
            )
            repository.rebuild_stories(now_ms=NOW_MS + 30_000)
        with (
            pytest.raises(
                RuntimeError,
                match="news_brief_source_fingerprint_changed",
            ),
            conn.transaction(),
        ):
            repository.publish_brief(
                run_id=claim["run_id"],
                lease_owner=claim["lease_owner"],
                fingerprint=fingerprint,
                stories=candidates,
                draft=NewsBriefDraft(
                    lead="今日重点发生变化 [1]",
                    lines=tuple(
                        f"第{index}条：{story['representative_title']} [{index}]"
                        for index, story in enumerate(candidates, 1)
                    ),
                    provider="test",
                    model="test",
                    raw_response="{}",
                ),
                validation={"citation_index_lock": True},
                now_ms=NOW_MS + 31_000,
            )
        assert conn.execute("SELECT count(*) AS count FROM news_brief_publications").fetchone()["count"] == 0
    finally:
        conn.close()


def test_destructive_schema_contains_only_current_news_tables(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        tables = {
            row["tablename"]
            for row in conn.execute(
                """
                SELECT tablename
                  FROM pg_tables
                 WHERE schemaname = 'public' AND tablename LIKE 'news_%'
                """
            ).fetchall()
        }
        assert tables == {
            "news_sources",
            "news_source_memberships",
            "news_source_fetches",
            "news_feed_observations",
            "news_items",
            "news_stories",
            "news_story_members",
            "news_story_aliases",
            "news_story_input_state",
            "news_projection_summary",
            "news_projection_frontiers",
            "news_identity_features",
            "news_similarity_edges",
            "news_brief_runs",
            "news_brief_publications",
            "news_brief_current",
        }
    finally:
        conn.close()
