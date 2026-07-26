from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
)
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news import (
    AiPublicationResult,
    BriefEvidenceBundle,
    NewsAiPublishWorker,
    NewsFeedEntry,
    NewsInterface,
    NewsPublicationContract,
    NewsRepository,
    NewsSourceDefinition,
    StoryAnalysisEvidence,
    brief_publication_contract,
    story_analysis_contract,
)

NOW_MS = 1_779_000_000_000
MODEL = "fake-news-model"


class SingleConnectionDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def worker_session(self, *_args: Any, **_kwargs: Any):
        return repository_session_for_connection(self.conn)


class RepairingPublisher:
    def __init__(self, *, repair_succeeds: bool = True) -> None:
        self.repair_succeeds = repair_succeeds
        self.brief_calls = 0
        self.story_calls = 0
        self.repair_calls = 0

    async def synthesize_brief(
        self,
        evidence: BriefEvidenceBundle,
    ) -> AiPublicationResult:
        self.brief_calls += 1
        return AiPublicationResult(payload={"headline": "invalid"}, receipt={"attempt": "initial"})

    async def analyze_story(
        self,
        evidence: StoryAnalysisEvidence,
    ) -> AiPublicationResult:
        self.story_calls += 1
        return AiPublicationResult(
            payload=story_payload(evidence, fact_text="该事件涉及999%的变化"),
            receipt={"attempt": "initial"},
        )

    async def repair(
        self,
        *,
        publication_kind: str,
        evidence: BriefEvidenceBundle | StoryAnalysisEvidence,
        validation_errors: tuple[str, ...],
    ) -> AiPublicationResult:
        self.repair_calls += 1
        if not self.repair_succeeds:
            return AiPublicationResult(
                payload={"headline": "still invalid"},
                receipt={"attempt": "repair"},
            )
        if publication_kind == "brief":
            assert isinstance(evidence, BriefEvidenceBundle)
            payload = brief_payload(evidence)
        else:
            assert isinstance(evidence, StoryAnalysisEvidence)
            payload = story_payload(evidence)
        return AiPublicationResult(
            payload=payload,
            receipt={
                "attempt": "repair",
                "validation_error_count": len(validation_errors),
            },
        )


def test_story_ai_repairs_once_publishes_immutably_and_reuses_cache_across_request_kinds(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository, story_id = seed_two_origin_story(conn)
        request = repository.request_story_analysis(
            story_id=story_id,
            now_ms=NOW_MS + 2_000,
        )
        assert request is not None
        assert request["status"] == "pending"
        publisher = RepairingPublisher()
        worker = ai_worker(conn, publisher=publisher, now_ms=NOW_MS + 3_000)

        result = asyncio.run(worker.run_once())

        assert result.processed == 1
        assert result.failed == 0
        assert publisher.story_calls == 1
        assert publisher.repair_calls == 1
        publication = conn.execute("SELECT * FROM news_story_analysis_publications").fetchone()
        assert publication is not None
        assert publication["evidence_references"]
        attempt = conn.execute("SELECT * FROM news_ai_attempts").fetchone()
        assert attempt["status"] == "available"
        assert attempt["attempt_count"] == 1
        assert attempt["repair_count"] == 1
        current = conn.execute(
            "SELECT * FROM news_story_analysis_current WHERE story_id = %s",
            (story_id,),
        ).fetchone()
        assert current["publication_id"] == publication["publication_id"]
        repeated_request = repository.request_story_analysis(
            story_id=story_id,
            now_ms=NOW_MS + 3_500,
        )
        assert repeated_request is not None
        assert repeated_request["status"] == "published"

        story = conn.execute(
            "SELECT material_evidence_hash FROM news_stories WHERE story_id = %s",
            (story_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO news_story_analysis_requests (
              request_id, story_id, material_evidence_hash, request_kind,
              reason, status, requested_at_ms, updated_at_ms
            )
            VALUES (
              'automatic-cache-probe', %s, %s, 'automatic',
              '{}'::jsonb, 'pending', %s, %s
            )
            """,
            (
                story_id,
                story["material_evidence_hash"],
                NOW_MS + 4_000,
                NOW_MS + 4_000,
            ),
        )
        worker.clock_ms = lambda: NOW_MS + 5_000

        cached = asyncio.run(worker.run_once())

        assert cached.skipped == 1
        assert publisher.story_calls == 1
        assert publisher.repair_calls == 1
        assert (
            conn.execute(
                """
                SELECT status
                  FROM news_story_analysis_requests
                 WHERE request_id = 'automatic-cache-probe'
                """
            ).fetchone()["status"]
            == "published"
        )
        assert conn.execute("SELECT count(*) AS count FROM news_story_analysis_publications").fetchone()["count"] == 1
        conn.commit()
    finally:
        conn.close()


def test_brief_validation_failure_keeps_deterministic_fallback_and_no_publication(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository, _story_id = seed_two_origin_story(conn)
        conn.execute("UPDATE news_stories SET brief_eligible = true, impact_score = 95, priority_score = 95")
        planned = repository.plan_global_brief(
            now_ms=NOW_MS + 2_000,
            candidate_limit=100,
            debounce_ms=0,
            critical_debounce_ms=0,
        )
        assert planned["status"] == "publishable"
        publisher = RepairingPublisher(repair_succeeds=False)
        worker = ai_worker(conn, publisher=publisher, now_ms=NOW_MS + 3_000)

        result = asyncio.run(worker.run_once())

        assert result.processed == 0
        assert result.failed == 1
        assert publisher.brief_calls == 1
        assert publisher.repair_calls == 1
        assert conn.execute("SELECT count(*) AS count FROM news_brief_publications").fetchone()["count"] == 0
        attempt = conn.execute("SELECT * FROM news_ai_attempts WHERE publication_kind = 'brief'").fetchone()
        assert attempt["status"] == "failed"
        assert attempt["repair_count"] == 1
        assert attempt["next_attempt_at_ms"] > 9_000_000_000_000_000_000
        brief = NewsInterface(repository).get_global_brief()
        assert brief["current"] is None
        assert brief["fallback"]["status"] == "publishable"
        assert brief["latest_failure"]["validation_errors"]
        conn.commit()
    finally:
        conn.close()


def test_ai_claim_lease_is_recoverable_and_stale_owner_cannot_complete(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository, story_id = seed_two_origin_story(conn)
        request = repository.request_story_analysis(
            story_id=story_id,
            now_ms=NOW_MS + 2_000,
        )
        assert request is not None and request["status"] == "pending"
        brief_contract = brief_publication_contract(MODEL)
        story_contract = story_analysis_contract(MODEL)

        first = repository.claim_ai_work(
            brief_contract=brief_contract,
            story_contract=story_contract,
            now_ms=NOW_MS + 3_000,
            limit=1,
            lease_ms=1_000,
            max_attempts=3,
        )
        assert len(first) == 1
        assert (
            repository.claim_ai_work(
                brief_contract=brief_contract,
                story_contract=story_contract,
                now_ms=NOW_MS + 3_500,
                limit=1,
                lease_ms=1_000,
                max_attempts=3,
            )
            == []
        )
        second = repository.claim_ai_work(
            brief_contract=brief_contract,
            story_contract=story_contract,
            now_ms=NOW_MS + 4_001,
            limit=1,
            lease_ms=1_000,
            max_attempts=3,
        )
        assert len(second) == 1
        kind, attempt_key, stale_token, evidence = first[0]
        _, second_attempt_key, current_token, second_evidence = second[0]
        assert kind == "story_analysis"
        assert attempt_key == second_attempt_key
        assert stale_token != current_token
        assert isinstance(evidence, StoryAnalysisEvidence)
        assert isinstance(second_evidence, StoryAnalysisEvidence)

        with pytest.raises(RuntimeError, match="news_ai_attempt_lease_lost"):
            repository.complete_ai_publication(
                publication_kind=kind,
                attempt_key=attempt_key,
                lease_token=stale_token,
                evidence=evidence,
                contract=story_contract,
                payload=story_payload(evidence),
                evidence_references=(str(evidence.articles[0]["evidence_ref"]),),
                receipt={},
                published_at_ms=NOW_MS + 4_100,
                repair_count=0,
            )

        publication_id = repository.complete_ai_publication(
            publication_kind=kind,
            attempt_key=attempt_key,
            lease_token=current_token,
            evidence=second_evidence,
            contract=story_contract,
            payload=story_payload(second_evidence),
            evidence_references=(str(second_evidence.articles[0]["evidence_ref"]),),
            receipt={},
            published_at_ms=NOW_MS + 4_200,
            repair_count=0,
        )

        attempt = conn.execute(
            "SELECT * FROM news_ai_attempts WHERE attempt_key = %s",
            (attempt_key,),
        ).fetchone()
        assert attempt["status"] == "available"
        assert attempt["attempt_count"] == 2
        assert (
            conn.execute(
                "SELECT publication_id FROM news_story_analysis_current WHERE story_id = %s",
                (story_id,),
            ).fetchone()["publication_id"]
            == publication_id
        )
        conn.commit()
    finally:
        conn.close()


def test_published_story_and_brief_are_republished_when_contract_changes(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository, story_id = seed_two_origin_story(conn)
        request = repository.request_story_analysis(
            story_id=story_id,
            now_ms=NOW_MS + 2_000,
        )
        assert request is not None and request["status"] == "pending"
        conn.execute(
            """
            UPDATE news_stories
               SET brief_eligible = true,
                   impact_score = 95,
                   priority_score = 95
            """
        )
        planned = repository.plan_global_brief(
            now_ms=NOW_MS + 2_100,
            candidate_limit=100,
            debounce_ms=0,
            critical_debounce_ms=0,
        )
        assert planned["status"] == "publishable"
        initial_publisher = RepairingPublisher()
        initial_worker = ai_worker(
            conn,
            publisher=initial_publisher,
            now_ms=NOW_MS + 3_000,
        )

        initial = asyncio.run(initial_worker.run_once())

        assert initial.processed == 2
        assert conn.execute("SELECT count(*) AS count FROM news_brief_publications").fetchone()["count"] == 1
        assert conn.execute("SELECT count(*) AS count FROM news_story_analysis_publications").fetchone()["count"] == 1
        old_brief_current = conn.execute(
            "SELECT publication_id FROM news_brief_current WHERE singleton_key"
        ).fetchone()["publication_id"]
        old_story_current = conn.execute(
            "SELECT publication_id FROM news_story_analysis_current WHERE story_id = %s",
            (story_id,),
        ).fetchone()["publication_id"]
        next_brief_contract = brief_publication_contract(MODEL).model_copy(
            update={"prompt_version": "brief-prompt-next"}
        )
        next_story_contract = story_analysis_contract(MODEL).model_copy(update={"prompt_version": "story-prompt-next"})
        upgraded_publisher = RepairingPublisher()
        upgraded_worker = ai_worker(
            conn,
            publisher=upgraded_publisher,
            now_ms=NOW_MS + 4_000,
            brief_contract_override=next_brief_contract,
            story_contract_override=next_story_contract,
        )

        upgraded = asyncio.run(upgraded_worker.run_once())

        assert upgraded.processed == 2
        assert upgraded_publisher.brief_calls == 1
        assert upgraded_publisher.story_calls == 1
        assert conn.execute("SELECT count(*) AS count FROM news_brief_publications").fetchone()["count"] == 2
        assert conn.execute("SELECT count(*) AS count FROM news_story_analysis_publications").fetchone()["count"] == 2
        new_brief_current = conn.execute(
            "SELECT publication_id FROM news_brief_current WHERE singleton_key"
        ).fetchone()["publication_id"]
        new_story_current = conn.execute(
            "SELECT publication_id FROM news_story_analysis_current WHERE story_id = %s",
            (story_id,),
        ).fetchone()["publication_id"]
        assert new_brief_current != old_brief_current
        assert new_story_current != old_story_current
        assert (
            conn.execute(
                "SELECT prompt_version FROM news_brief_publications WHERE publication_id = %s",
                (new_brief_current,),
            ).fetchone()["prompt_version"]
            == "brief-prompt-next"
        )
        assert (
            conn.execute(
                "SELECT prompt_version FROM news_story_analysis_publications WHERE publication_id = %s",
                (new_story_current,),
            ).fetchone()["prompt_version"]
            == "story-prompt-next"
        )
        conn.commit()
    finally:
        conn.close()


def seed_two_origin_story(conn: Any) -> tuple[NewsRepository, str]:
    repository = NewsRepository(conn)
    reuters = source("reuters", "Reuters", "reuters.example", "reuters")
    ap = source("ap", "Associated Press", "ap.example", "ap")
    repository.sync_sources((reuters, ap), now_ms=NOW_MS)
    repository.record_fetch_success(
        source=reuters,
        entries=(
            entry(
                "reuters-rate-cut",
                "https://reuters.example/rate-cut",
                "Federal Reserve cuts interest rates",
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
                "ap-rate-cut",
                "https://ap.example/rate-cut",
                "Fed lowers interest rates",
            ),
        ),
        started_at_ms=NOW_MS + 100,
        finished_at_ms=NOW_MS + 100,
        status_code=200,
        etag=None,
        last_modified=None,
        not_modified=False,
    )
    repository.project_pending_revisions(now_ms=NOW_MS + 1_000, limit=100)
    stories = NewsInterface(repository).list_stories(limit=10)["items"]
    assert len(stories) == 1
    assert stories[0]["independent_origin_count"] == 2
    return repository, str(stories[0]["story_id"])


def ai_worker(
    conn: Any,
    *,
    publisher: RepairingPublisher,
    now_ms: int,
    brief_contract_override: NewsPublicationContract | None = None,
    story_contract_override: NewsPublicationContract | None = None,
) -> NewsAiPublishWorker:
    settings = SimpleNamespace(
        enabled=True,
        interval_seconds=1,
        batch_size=10,
        lease_ms=30_000,
        max_attempts=3,
        retry_ms=1_000,
        statement_timeout_seconds=30.0,
    )
    return NewsAiPublishWorker(
        settings=settings,
        db=SingleConnectionDB(conn),
        telemetry=SimpleNamespace(),
        publisher=publisher,  # type: ignore[arg-type]
        brief_contract=brief_contract_override or brief_publication_contract(MODEL),
        story_contract=story_contract_override or story_analysis_contract(MODEL),
        clock_ms=lambda: now_ms,
    )


def source(
    source_id: str,
    name: str,
    domain: str,
    chain: str,
) -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id=source_id,
        name=name,
        feed_url=f"https://{domain}/feed.xml",
        source_domain=domain,
        source_role="wire_service",
        trust_tier="trusted",
        source_chain_id=chain,
        default_language="en",
        refresh_interval_seconds=60,
    )


def entry(guid: str, link: str, title: str) -> NewsFeedEntry:
    return NewsFeedEntry(
        guid=guid,
        link=link,
        title=title,
        published_at_ms=NOW_MS - 60_000,
        language="en",
    )


def story_payload(
    evidence: StoryAnalysisEvidence,
    *,
    fact_text: str = "该事件已经得到两家独立来源报道",
) -> dict[str, object]:
    evidence_ref = str(evidence.articles[0]["evidence_ref"])
    return {
        "what_happened": [
            {
                "text": fact_text,
                "evidence_references": [evidence_ref],
            }
        ],
        "why_it_matters": "这可能改变政策预期",
        "political_impact": "政治影响取决于后续执行",
        "economic_market_impact": "市场影响取决于政策传导",
        "disagreements_unknowns": [],
        "transmission_scenarios": [
            {
                "condition": "如果政策继续执行",
                "mechanism": "通过融资条件传导",
                "possible_effect": "市场波动可能上升",
                "confidence": "medium",
            }
        ],
        "next_checkpoint": "观察后续官方文件",
    }


def brief_payload(evidence: BriefEvidenceBundle) -> dict[str, object]:
    return {
        "headline": "全球新闻简报",
        "executive_summary": "多项事件值得持续观察",
        "items": [
            {
                "story_id": str(story["story_id"]),
                "what_happened": [
                    {
                        "text": "该事件已经被来源报道",
                        "evidence_references": [str(story["evidence_articles"][0]["evidence_ref"])],
                    }
                ],
                "why_it_matters": "这可能改变政策预期",
                "transmission_scenarios": [],
                "uncertainties": [],
                "watchpoints": ["观察后续官方文件"],
            }
            for story in evidence.stories
        ],
        "narratives": [],
        "global_watchpoints": ["关注政策执行"],
    }
