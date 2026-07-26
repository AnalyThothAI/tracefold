from __future__ import annotations

from types import SimpleNamespace

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
)
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news import (
    NewsAnalysisContract,
    NewsFeedEntry,
    NewsFeedFetch,
    NewsIngestWorker,
    NewsRepository,
    NewsSourceDefinition,
    NewsStoryAnalysisDraft,
    StoryInterface,
)
from tracefold.platform.config.settings import NewsIngestWorkerSettings

NOW_MS = 1_779_000_000_000


def test_news_story_pipeline_deduplicates_replay_groups_sources_and_versions_analysis(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source(
            source_id="reuters-world",
            name="Reuters World",
            domain="reuters.com",
            chain="reuters",
        )
        ap = source(
            source_id="ap-global",
            name="AP News",
            domain="apnews.com",
            chain="ap",
        )
        news6551 = source(
            source_id="6551news",
            name="6551News",
            domain="t.me",
            chain="6551",
            role="trusted_aggregator",
            default_language="zh",
        )
        repository.sync_sources((reuters, ap, news6551), now_ms=NOW_MS)

        headline = "Federal Reserve cuts rates by 25 basis points"
        first = repository.record_fetch_success(
            source=reuters,
            entries=(entry("reuters-1", "https://reuters.com/rates-cut", headline),),
            now_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        replay = repository.record_fetch_success(
            source=reuters,
            entries=(entry("reuters-1", "https://reuters.com/rates-cut", headline),),
            now_ms=NOW_MS + 1_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.record_fetch_success(
            source=ap,
            entries=(entry("ap-1", "https://apnews.com/rates-cut", headline),),
            now_ms=NOW_MS + 2_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.record_fetch_success(
            source=news6551,
            entries=(
                entry(
                    "telegram-1",
                    "https://t.me/news6551/1",
                    headline,
                    summary="Source: Reuters https://reuters.com/rates-cut",
                ),
            ),
            now_ms=NOW_MS + 3_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.record_fetch_success(
            source=ap,
            entries=(
                entry(
                    "ap-2",
                    "https://apnews.com/rates-rise",
                    "Federal Reserve raises rates by 50 basis points",
                ),
            ),
            now_ms=NOW_MS + 4_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        conn.commit()

        stories = repository.list_story_rows(
            limit=10,
            cursor=None,
            q=None,
            verification_status=None,
            source=None,
        )
        grouped = next(row for row in stories if row["article_count"] == 3)

        assert first == {
            "articles_inserted": 1,
            "articles_changed": 1,
            "stories_created": 1,
            "memberships_created": 1,
        }
        assert replay == {
            "articles_inserted": 0,
            "articles_changed": 0,
            "stories_created": 0,
            "memberships_created": 0,
        }
        assert len(stories) == 2
        assert grouped["source_count"] == 3
        assert grouped["trusted_source_count"] == 3
        assert grouped["independent_origin_count"] == 2
        assert grouped["verification_status"] == "corroborated"
        claimed = repository.claim_analysis_evidence(
            model="deepseek-chat",
            now_ms=NOW_MS + 5_000,
            limit=10,
            lease_ms=60_000,
            max_attempts=3,
        )
        analysis_key, evidence = next(
            item for item in claimed if item[1].story_id == grouped["story_id"]
        )
        analysis_id = repository.complete_analysis(
            analysis_key=analysis_key,
            evidence=evidence,
            model="deepseek-chat",
            draft=NewsStoryAnalysisDraft(
                what_happened="美联储宣布降息 25 个基点。",
                why_it_matters="政策利率路径发生变化。",
                political_impact="政策沟通压力上升。",
                economic_market_impact="利率与汇率预期可能重新定价。",
                confirmed_facts=("两家独立权威媒体报道同一决定。",),
                disagreements_unknowns=("后续路径仍未知。",),
                next_checkpoint="观察下一次政策会议纪要。",
                evidence_references=tuple(
                    str(article["article_id"]) for article in evidence.articles
                ),
            ),
            published_at_ms=NOW_MS + 6_000,
            receipt={"response_id": "deepseek-response-1"},
        )
        conn.commit()

        detail = repository.get_story(
            story_id=str(grouped["story_id"]),
            analysis_contract=NewsAnalysisContract(model="deepseek-chat"),
        )
        assert detail is not None
        assert detail["analysis_id"] == analysis_id
        assert detail["why_it_matters"] == "政策利率路径发生变化。"

        repository.record_fetch_success(
            source=reuters,
            entries=(
                entry(
                    "reuters-1",
                    "https://reuters.com/rates-cut",
                    headline,
                    summary="Officials confirmed the decision and published an updated statement.",
                ),
            ),
            now_ms=NOW_MS + 7_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        next_claims = repository.claim_analysis_evidence(
            model="deepseek-chat",
            now_ms=NOW_MS + 8_000,
            limit=10,
            lease_ms=60_000,
            max_attempts=3,
        )
        conn.commit()

        assert any(item[1].story_id == grouped["story_id"] for item in next_claims)
        analysis_count = conn.execute(
            "SELECT COUNT(*) AS count FROM news_story_analyses WHERE story_id = %s",
            (grouped["story_id"],),
        ).fetchone()["count"]
        assert analysis_count == 1
    finally:
        conn.close()


def test_news_6551_distribution_chain_counts_one_verified_origin_and_survives_restart(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        telegram = source(
            source_id="6551-telegram",
            name="6551News Telegram",
            domain="t.me",
            chain="6551",
            role="trusted_aggregator",
            default_language="zh",
        )
        api_copy = source(
            source_id="6551-api",
            name="6551News API",
            domain="news6551.example",
            chain="6551",
            role="trusted_aggregator",
            default_language="zh",
        )
        repository.sync_sources((telegram, api_copy), now_ms=NOW_MS)
        title = "币安发布新的全球合规政策"
        original_url = "https://www.binance.com/en/support/announcement/policy"
        repository.record_fetch_success(
            source=telegram,
            entries=(
                entry(
                    "telegram-1",
                    "https://t.me/news6551/1",
                    title,
                    summary=f"原文 {original_url}",
                ),
            ),
            now_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        first_story = repository.list_story_rows(
            limit=10,
            cursor=None,
            q=None,
            verification_status=None,
            source=None,
        )[0]
        repository.record_fetch_success(
            source=api_copy,
            entries=(
                entry(
                    "api-1",
                    "https://news6551.example/items/1",
                    title,
                    summary=f"原文 {original_url}",
                ),
            ),
            now_ms=NOW_MS + 1_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        conn.commit()

        grouped = repository.get_story(story_id=str(first_story["story_id"]))
        assert grouped is not None
        assert grouped["story_id"] == first_story["story_id"]
        assert grouped["source_count"] == 2
        assert grouped["trusted_source_count"] == 2
        assert grouped["independent_origin_count"] == 1
        assert grouped["verification_status"] == "trusted"
        assert {article["source_chain_id"] for article in grouped["articles"]} == {"6551"}
        assert {article["origin_domain"] for article in grouped["articles"]} == {"binance.com"}
        assert {article["provenance_status"] for article in grouped["articles"]} == {"verified"}

        conn.close()
        conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
        restarted = NewsRepository(conn)
        restarted.record_fetch_success(
            source=telegram,
            entries=(
                entry(
                    "telegram-1",
                    "https://t.me/news6551/1",
                    title,
                    summary=f"原文 {original_url}",
                ),
            ),
            now_ms=NOW_MS + 2_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        conn.commit()
        after_restart = restarted.get_story(story_id=str(first_story["story_id"]))

        assert after_restart is not None
        assert after_restart["article_count"] == 2
        assert after_restart["anchor_article_id"] == grouped["anchor_article_id"]
    finally:
        conn.close()


def test_news_primary_selection_prefers_authority_without_changing_story_id(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        standard = source(
            source_id="standard-wire",
            name="Standard Wire",
            domain="standard.example",
            chain="standard",
            trust="standard",
        )
        authoritative = source(
            source_id="authority-wire",
            name="Authority Wire",
            domain="authority.example",
            chain="authority",
        )
        repository.sync_sources((standard, authoritative), now_ms=NOW_MS)
        title = "Government approves emergency fiscal package"
        repository.record_fetch_success(
            source=standard,
            entries=(entry("standard-1", "https://standard.example/1", title),),
            now_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        initial = repository.list_story_rows(
            limit=10,
            cursor=None,
            q=None,
            verification_status=None,
            source=None,
        )[0]
        repository.record_fetch_success(
            source=authoritative,
            entries=(entry("authority-1", "https://authority.example/1", title),),
            now_ms=NOW_MS + 1_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        conn.commit()
        updated = repository.get_story(story_id=str(initial["story_id"]))

        assert updated is not None
        assert updated["story_id"] == initial["story_id"]
        assert updated["primary_article_id"] != initial["primary_article"]["article_id"]
        primary = next(
            article
            for article in updated["articles"]
            if article["article_id"] == updated["primary_article_id"]
        )
        assert primary["source_id"] == "authority-wire"
    finally:
        conn.close()


def test_news_analysis_failure_is_retryable_and_story_remains_readable(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        reuters = source(
            source_id="reuters-world",
            name="Reuters World",
            domain="reuters.com",
            chain="reuters",
        )
        repository.sync_sources((reuters,), now_ms=NOW_MS)
        repository.record_fetch_success(
            source=reuters,
            entries=(
                entry(
                    "reuters-1",
                    "https://reuters.com/policy",
                    "Government announces new trade policy",
                ),
            ),
            now_ms=NOW_MS,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        conn.commit()
        story_id = str(
            repository.list_story_rows(
                limit=1,
                cursor=None,
                q=None,
                verification_status=None,
                source=None,
            )[0]["story_id"]
        )
        unavailable = StoryInterface(repository, analysis_contract=None).get_story(
            story_id=story_id
        )
        assert unavailable is not None
        assert unavailable["analysis_status"] == "unavailable"

        [(analysis_key, _evidence)] = repository.claim_analysis_evidence(
            model="deepseek-chat",
            now_ms=NOW_MS + 1_000,
            limit=1,
            lease_ms=60_000,
            max_attempts=3,
        )
        repository.fail_analysis(
            analysis_key=analysis_key,
            now_ms=NOW_MS + 2_000,
            error="provider_timeout",
            retry_ms=5_000,
        )
        conn.commit()
        failed = StoryInterface(
            repository,
            analysis_contract=NewsAnalysisContract(model="deepseek-chat"),
        ).get_story(story_id=story_id)

        assert failed is not None
        assert failed["title"] == "Government announces new trade policy"
        assert failed["analysis_status"] == "failed"
        assert failed["analysis_error"] == "provider_timeout"
        replacement_contract = StoryInterface(
            repository,
            analysis_contract=NewsAnalysisContract(model="deepseek-v4-flash"),
        ).get_story(story_id=story_id)
        assert replacement_contract is not None
        assert replacement_contract["analysis_status"] == "pending"
        assert replacement_contract["analysis_error"] is None
        assert (
            repository.claim_analysis_evidence(
                model="deepseek-chat",
                now_ms=NOW_MS + 6_999,
                limit=1,
                lease_ms=60_000,
                max_attempts=3,
            )
            == []
        )
        retried = repository.claim_analysis_evidence(
            model="deepseek-chat",
            now_ms=NOW_MS + 7_000,
            limit=1,
            lease_ms=60_000,
            max_attempts=3,
        )
        conn.commit()

        assert len(retried) == 1
        assert retried[0][0] == analysis_key
        attempt_count = conn.execute(
            "SELECT attempt_count FROM news_story_analysis_attempts WHERE analysis_key = %s",
            (analysis_key,),
        ).fetchone()["attempt_count"]
        assert attempt_count == 2
    finally:
        conn.close()


def test_news_ingest_worker_isolates_one_failed_source(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        bad = source(
            source_id="a-bad-feed",
            name="Bad Feed",
            domain="bad.example",
            chain="bad",
        )
        good = source(
            source_id="b-good-feed",
            name="Good Feed",
            domain="good.example",
            chain="good",
        )
        worker = NewsIngestWorker(
            settings=NewsIngestWorkerSettings(batch_size=8),
            db=SingleConnectionDB(conn),
            telemetry=SimpleNamespace(),
            sources=(bad, good),
            feed_reader=IsolatingFeedReader(),
            clock_ms=lambda: NOW_MS,
        )

        result = worker.run_once_sync()
        conn.commit()
        sources = {
            row["source_id"]: row
            for row in NewsRepository(conn).list_sources()
        }
        story_count = conn.execute(
            "SELECT COUNT(*) AS count FROM news_stories"
        ).fetchone()["count"]

        assert result.processed == 1
        assert result.failed == 1
        assert sources["a-bad-feed"]["consecutive_failures"] == 1
        assert sources["a-bad-feed"]["last_error"].startswith("RuntimeError:")
        assert sources["b-good-feed"]["last_success_at_ms"] == NOW_MS
        assert story_count == 1
    finally:
        conn.close()


def source(
    *,
    source_id: str,
    name: str,
    domain: str,
    chain: str,
    role: str = "original_publisher",
    default_language: str = "en",
    trust: str = "authoritative",
) -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id=source_id,
        name=name,
        feed_url=f"https://{domain}/feed",
        source_domain=domain,
        source_role=role,
        trust_tier=trust,
        source_chain_id=chain,
        coverage_tags=("politics", "economy"),
        default_language=default_language,
    )


def entry(
    guid: str,
    link: str,
    title: str,
    *,
    summary: str = "Officials confirmed the policy decision.",
) -> NewsFeedEntry:
    return NewsFeedEntry(
        guid=guid,
        link=link,
        title=title,
        summary=summary,
        published_at_ms=NOW_MS,
    )


class SingleConnectionDB:
    def __init__(self, conn) -> None:
        self.conn = conn

    def worker_session(self, *_args, **_kwargs):
        return repository_session_for_connection(self.conn)


class IsolatingFeedReader:
    def fetch(
        self,
        *,
        source: NewsSourceDefinition,
        etag: str | None,
        last_modified: str | None,
    ) -> NewsFeedFetch:
        del etag, last_modified
        if source.source_id == "a-bad-feed":
            raise RuntimeError("feed unavailable")
        return NewsFeedFetch(
            status_code=200,
            entries=(
                entry(
                    "good-1",
                    "https://good.example/policy",
                    "Government confirms fiscal policy",
                ),
            ),
        )

    def close(self) -> None:
        return None
