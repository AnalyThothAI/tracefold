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
        assert planned["status"] == "active"
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
        assert brief["active_selection"] is not None
        assert brief["analysis"] is None
        assert brief["analysis_status"] == "failed"
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
        assert planned["status"] == "active"
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
            """
            SELECT publication_id
              FROM news_brief_activation_analysis
             ORDER BY attached_at_ms DESC, publication_id DESC
             LIMIT 1
            """
        ).fetchone()["publication_id"]
        old_story_current = conn.execute(
            "SELECT publication_id FROM news_story_analysis_current WHERE story_id = %s",
            (story_id,),
        ).fetchone()["publication_id"]
        next_brief_contract = brief_publication_contract(MODEL).model_copy(
            update={"prompt_version": "brief-prompt-next"}
        )
        next_story_contract = story_analysis_contract(MODEL).model_copy(update={"prompt_version": "story-prompt-next"})
        claimed = repository.claim_ai_work(
            brief_contract=next_brief_contract,
            story_contract=next_story_contract,
            now_ms=NOW_MS + 4_000,
            limit=10,
            lease_ms=30_000,
            max_attempts=3,
        )
        conn.commit()

        assert {kind for kind, *_ in claimed} == {"brief", "story_analysis"}
        pending_brief = NewsInterface(repository).get_global_brief()
        pending_story = NewsInterface(repository).get_story(story_id=story_id)
        assert pending_brief["analysis"] is None
        assert pending_brief["analysis_status"] == "pending"
        assert pending_story is not None
        assert pending_story["analysis"]["current"] is None
        assert pending_story["analysis"]["status"] == "claimed"
        assert (
            conn.execute(
                """
                SELECT count(*) AS count
                  FROM news_brief_activation_analysis
                 WHERE superseded_at_ms IS NULL
                """
            ).fetchone()["count"]
            == 0
        )
        assert (
            conn.execute(
                """
                SELECT superseded_at_ms
                  FROM news_brief_activation_analysis
                 WHERE publication_id = %s
                """,
                (old_brief_current,),
            ).fetchone()["superseded_at_ms"]
            == NOW_MS + 4_000
        )
        upgraded_publisher = RepairingPublisher()
        upgraded_worker = ai_worker(
            conn,
            publisher=upgraded_publisher,
            now_ms=NOW_MS + 35_000,
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
            """
            SELECT publication_id
              FROM news_brief_activation_analysis
             WHERE superseded_at_ms IS NULL
             ORDER BY attached_at_ms DESC, publication_id DESC
             LIMIT 1
            """
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

        cached_reversions = repository.claim_ai_work(
            brief_contract=brief_publication_contract(MODEL),
            story_contract=story_analysis_contract(MODEL),
            now_ms=NOW_MS + 36_000,
            limit=10,
            lease_ms=30_000,
            max_attempts=3,
        )

        assert cached_reversions == []
        reverted_brief = NewsInterface(repository).get_global_brief()
        reverted_story = NewsInterface(repository).get_story(story_id=story_id)
        assert reverted_brief["analysis_status"] == "reused"
        assert reverted_brief["analysis"]["publication_id"] == old_brief_current
        assert reverted_story is not None
        assert reverted_story["analysis"]["current"]["publication_id"] == old_story_current
        assert (
            conn.execute(
                """
                SELECT count(*) AS count
                  FROM news_brief_activation_analysis
                 WHERE superseded_at_ms IS NULL
                """
            ).fetchone()["count"]
            == 1
        )
        assert (
            conn.execute(
                """
                SELECT superseded_at_ms
                  FROM news_brief_activation_analysis
                 WHERE publication_id = %s
                """,
                (new_brief_current,),
            ).fetchone()["superseded_at_ms"]
            == NOW_MS + 36_000
        )
        conn.commit()
    finally:
        conn.close()


def test_late_brief_result_from_superseded_contract_stays_history_only(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository, _story_id = seed_two_origin_story(conn)
        conn.execute(
            """
            UPDATE news_stories
               SET brief_eligible = true,
                   impact_score = 95,
                   priority_score = 95
            """
        )
        active = repository.plan_global_brief(
            now_ms=NOW_MS + 2_000,
            candidate_limit=100,
            debounce_ms=0,
            critical_debounce_ms=0,
        )
        old_contract = brief_publication_contract(MODEL)
        old_claim = next(
            claim
            for claim in repository.claim_ai_work(
                brief_contract=old_contract,
                story_contract=story_analysis_contract(MODEL),
                now_ms=NOW_MS + 3_000,
                limit=10,
                lease_ms=30_000,
                max_attempts=3,
            )
            if claim[0] == "brief"
        )
        new_contract = old_contract.model_copy(update={"prompt_version": "brief-prompt-next"})
        new_claim = next(
            claim
            for claim in repository.claim_ai_work(
                brief_contract=new_contract,
                story_contract=story_analysis_contract(MODEL),
                now_ms=NOW_MS + 4_000,
                limit=10,
                lease_ms=30_000,
                max_attempts=3,
            )
            if claim[0] == "brief"
        )
        _, new_attempt_key, new_lease_token, new_evidence = new_claim
        assert isinstance(new_evidence, BriefEvidenceBundle)
        new_publication_id = repository.complete_ai_publication(
            publication_kind="brief",
            attempt_key=new_attempt_key,
            lease_token=new_lease_token,
            evidence=new_evidence,
            contract=new_contract,
            payload=brief_payload(new_evidence),
            evidence_references=tuple(
                str(story["evidence_articles"][0]["evidence_ref"]) for story in new_evidence.stories
            ),
            receipt={"contract": "new"},
            published_at_ms=NOW_MS + 5_000,
            repair_count=0,
        )
        _, old_attempt_key, old_lease_token, old_evidence = old_claim
        assert isinstance(old_evidence, BriefEvidenceBundle)
        old_publication_id = repository.complete_ai_publication(
            publication_kind="brief",
            attempt_key=old_attempt_key,
            lease_token=old_lease_token,
            evidence=old_evidence,
            contract=old_contract,
            payload=brief_payload(old_evidence),
            evidence_references=tuple(
                str(story["evidence_articles"][0]["evidence_ref"]) for story in old_evidence.stories
            ),
            receipt={"contract": "old-late"},
            published_at_ms=NOW_MS + 6_000,
            repair_count=0,
        )

        assert old_publication_id != new_publication_id
        current = NewsInterface(repository).get_global_brief()
        assert current["active_selection"]["activation_id"] == active["activation_id"]
        assert current["analysis"]["publication_id"] == new_publication_id
        assert current["analysis"]["contract"]["prompt_version"] == "brief-prompt-next"
        assert (
            conn.execute(
                """
                SELECT count(*) AS count
                  FROM news_brief_activation_analysis
                 WHERE publication_id = %s
                """,
                (old_publication_id,),
            ).fetchone()["count"]
            == 0
        )
        assert conn.execute("SELECT count(*) AS count FROM news_brief_publications").fetchone()["count"] == 2
        conn.commit()
    finally:
        conn.close()


def test_late_story_result_from_superseded_contract_stays_history_only(
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
        assert request is not None and request["status"] == "pending"
        old_contract = story_analysis_contract(MODEL)
        old_claim = next(
            claim
            for claim in repository.claim_ai_work(
                brief_contract=brief_publication_contract(MODEL),
                story_contract=old_contract,
                now_ms=NOW_MS + 3_000,
                limit=10,
                lease_ms=30_000,
                max_attempts=3,
            )
            if claim[0] == "story_analysis"
        )
        new_contract = old_contract.model_copy(update={"prompt_version": "story-prompt-next"})
        new_claim = next(
            claim
            for claim in repository.claim_ai_work(
                brief_contract=brief_publication_contract(MODEL),
                story_contract=new_contract,
                now_ms=NOW_MS + 4_000,
                limit=10,
                lease_ms=30_000,
                max_attempts=3,
            )
            if claim[0] == "story_analysis"
        )
        _, new_attempt_key, new_lease_token, new_evidence = new_claim
        assert isinstance(new_evidence, StoryAnalysisEvidence)
        new_publication_id = repository.complete_ai_publication(
            publication_kind="story_analysis",
            attempt_key=new_attempt_key,
            lease_token=new_lease_token,
            evidence=new_evidence,
            contract=new_contract,
            payload=story_payload(new_evidence),
            evidence_references=(str(new_evidence.articles[0]["evidence_ref"]),),
            receipt={"contract": "new"},
            published_at_ms=NOW_MS + 5_000,
            repair_count=0,
        )
        _, old_attempt_key, old_lease_token, old_evidence = old_claim
        assert isinstance(old_evidence, StoryAnalysisEvidence)
        old_publication_id = repository.complete_ai_publication(
            publication_kind="story_analysis",
            attempt_key=old_attempt_key,
            lease_token=old_lease_token,
            evidence=old_evidence,
            contract=old_contract,
            payload=story_payload(old_evidence),
            evidence_references=(str(old_evidence.articles[0]["evidence_ref"]),),
            receipt={"contract": "old-late"},
            published_at_ms=NOW_MS + 6_000,
            repair_count=0,
        )

        assert old_publication_id != new_publication_id
        current = NewsInterface(repository).get_story(story_id=story_id)
        assert current is not None
        assert current["analysis"]["current"]["publication_id"] == new_publication_id
        assert current["analysis"]["current"]["prompt_version"] == "story-prompt-next"
        assert len(current["analysis"]["history"]) == 2
        conn.commit()
    finally:
        conn.close()


def test_brief_a_to_b_to_a_reuses_exact_publication_without_model_call(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository, story_a = seed_two_origin_story(conn)
        conn.execute(
            """
            UPDATE news_stories
               SET brief_eligible = true, impact_score = 95, priority_score = 95
             WHERE story_id = %s
            """,
            (story_a,),
        )
        first_plan = repository.plan_global_brief(
            now_ms=NOW_MS + 2_000,
            candidate_limit=100,
            debounce_ms=0,
            critical_debounce_ms=0,
        )
        first_publisher = RepairingPublisher()
        first_result = asyncio.run(
            ai_worker(
                conn,
                publisher=first_publisher,
                now_ms=NOW_MS + 3_000,
            ).run_once()
        )
        assert first_result.processed == 1
        assert first_publisher.brief_calls == 1
        original = conn.execute("SELECT * FROM news_brief_publications").fetchone()
        assert original is not None

        second_source = source("second-policy", "Second Policy", "second-policy.example", "second-policy")
        repository.sync_sources((second_source,), now_ms=NOW_MS + 4_000)
        repository.record_fetch_success(
            source=second_source,
            entries=(
                entry(
                    "second-policy",
                    "https://second-policy.example/export-controls",
                    "Government implements emergency semiconductor export controls",
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
        story_b = str(
            conn.execute(
                """
                SELECT story_id
                  FROM news_stories
                 WHERE story_id <> %s
                 ORDER BY story_id
                 LIMIT 1
                """,
                (story_a,),
            ).fetchone()["story_id"]
        )
        conn.execute(
            """
            UPDATE news_stories
               SET brief_eligible = (story_id = %s),
                   impact_score = CASE WHEN story_id = %s THEN 95 ELSE impact_score END,
                   priority_score = CASE WHEN story_id = %s THEN 95 ELSE priority_score END
            """,
            (story_b, story_b, story_b),
        )
        second_plan = repository.plan_global_brief(
            now_ms=NOW_MS + 6_000,
            candidate_limit=100,
            debounce_ms=0,
            critical_debounce_ms=0,
        )
        while_b = NewsInterface(repository).get_global_brief()
        assert second_plan["activation_id"] != first_plan["activation_id"]
        assert while_b["analysis"] is None
        assert while_b["analysis_status"] == "pending"
        assert while_b["previous_publication"]["publication_id"] == original["publication_id"]

        conn.execute(
            "UPDATE news_stories SET brief_eligible = (story_id = %s)",
            (story_a,),
        )
        recurring = repository.plan_global_brief(
            now_ms=NOW_MS + 7_000,
            candidate_limit=100,
            debounce_ms=0,
            critical_debounce_ms=0,
        )
        reuse_publisher = RepairingPublisher()
        reuse_result = asyncio.run(
            ai_worker(
                conn,
                publisher=reuse_publisher,
                now_ms=NOW_MS + 8_000,
            ).run_once()
        )

        assert recurring["selection_fingerprint"] == first_plan["selection_fingerprint"]
        assert recurring["activation_id"] != first_plan["activation_id"]
        assert reuse_result.skipped == 1
        assert reuse_publisher.brief_calls == 0
        assert reuse_publisher.repair_calls == 0
        assert conn.execute("SELECT count(*) AS count FROM news_brief_publications").fetchone()["count"] == 1
        attachment = conn.execute(
            """
            SELECT *
              FROM news_brief_activation_analysis
             WHERE activation_id = %s
            """,
            (recurring["activation_id"],),
        ).fetchone()
        assert attachment["publication_id"] == original["publication_id"]
        assert attachment["attachment_kind"] == "reused"
        public = NewsInterface(repository).get_global_brief()
        assert public["analysis_status"] == "reused"
        assert public["analysis"]["publication_id"] == original["publication_id"]
        assert public["analysis"]["published_at_ms"] == original["published_at_ms"]
        assert public["analysis"]["evidence_cutoff_at_ms"] == original["evidence_cutoff_at_ms"]
        assert public["active_selection"]["activated_at_ms"] == NOW_MS + 7_000
        conn.commit()
    finally:
        conn.close()


def test_late_brief_completion_is_cached_but_cannot_attach_to_new_active_selection(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository, story_a = seed_two_origin_story(conn)
        conn.execute(
            """
            UPDATE news_stories
               SET brief_eligible = true, impact_score = 95, priority_score = 95
             WHERE story_id = %s
            """,
            (story_a,),
        )
        active_a = repository.plan_global_brief(
            now_ms=NOW_MS + 2_000,
            candidate_limit=100,
            debounce_ms=0,
            critical_debounce_ms=0,
        )
        brief_contract = brief_publication_contract(MODEL)
        claimed = repository.claim_ai_work(
            brief_contract=brief_contract,
            story_contract=story_analysis_contract(MODEL),
            now_ms=NOW_MS + 3_000,
            limit=1,
            lease_ms=30_000,
            max_attempts=3,
        )
        assert len(claimed) == 1
        kind, attempt_key, lease_token, evidence = claimed[0]
        assert kind == "brief"
        assert isinstance(evidence, BriefEvidenceBundle)

        second_source = source("late-b", "Late B", "late-b.example", "late-b")
        repository.sync_sources((second_source,), now_ms=NOW_MS + 4_000)
        repository.record_fetch_success(
            source=second_source,
            entries=(
                entry(
                    "late-b",
                    "https://late-b.example/border",
                    "Regional government closes a strategic border crossing",
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
        story_b = str(
            conn.execute(
                "SELECT story_id FROM news_stories WHERE story_id <> %s LIMIT 1",
                (story_a,),
            ).fetchone()["story_id"]
        )
        conn.execute(
            """
            UPDATE news_stories
               SET brief_eligible = (story_id = %s),
                   impact_score = CASE WHEN story_id = %s THEN 95 ELSE impact_score END,
                   priority_score = CASE WHEN story_id = %s THEN 95 ELSE priority_score END
            """,
            (story_b, story_b, story_b),
        )
        active_b = repository.plan_global_brief(
            now_ms=NOW_MS + 6_000,
            candidate_limit=100,
            debounce_ms=0,
            critical_debounce_ms=0,
        )
        assert active_b["activation_id"] != active_a["activation_id"]

        publication_id = repository.complete_ai_publication(
            publication_kind="brief",
            attempt_key=attempt_key,
            lease_token=lease_token,
            evidence=evidence,
            contract=brief_contract,
            payload=brief_payload(evidence),
            evidence_references=tuple(str(story["evidence_articles"][0]["evidence_ref"]) for story in evidence.stories),
            receipt={"completion": "late"},
            published_at_ms=NOW_MS + 7_000,
            repair_count=0,
        )

        assert (
            conn.execute(
                "SELECT count(*) AS count FROM news_brief_publications WHERE publication_id = %s",
                (publication_id,),
            ).fetchone()["count"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS count FROM news_brief_activation_analysis WHERE publication_id = %s",
                (publication_id,),
            ).fetchone()["count"]
            == 0
        )
        public = NewsInterface(repository).get_global_brief()
        assert public["active_selection"]["activation_id"] == active_b["activation_id"]
        assert public["analysis"] is None
        assert public["analysis_status"] == "pending"
        assert public["previous_publication"] is None
        history = NewsInterface(repository).list_global_brief_history(limit=10)
        assert [row["publication_id"] for row in history["items"]] == [publication_id]
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
