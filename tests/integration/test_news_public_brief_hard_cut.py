from __future__ import annotations

import json
import re

import httpx
from fastapi.testclient import TestClient

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
    prepare_postgres_database,
    reset_postgres_schema,
)
from tracefold.app.http.app import create_app
from tracefold.integrations.news_ai import ProviderChainNewsBriefPublisher
from tracefold.news import (
    INSIGHTS_SYNTHESIS_PROVIDER,
    NewsBriefSource,
    NewsBriefStory,
    NewsBriefStoryLine,
    NewsBriefSynthesisResult,
    NewsRepository,
    compose_none_brief,
    opennews_source,
    parse_opennews_message,
)
from tracefold.platform.config.settings import Settings

NOW_MS = 1_786_082_400_000


def _report(*, record_id: str, title: str, origin: str, published_at_ms: int):
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": record_id,
                "text": title,
                "description": "Independent reporting confirms the material event and its immediate impact.",
                "newsType": origin,
                "engineType": "news",
                "link": f"https://example.test/{record_id}",
                "ts": published_at_ms,
            },
        }
    )
    assert event is not None
    return event


def test_opennews_to_public_http_brief_uses_the_production_chain_without_read_writes(tmp_path) -> None:
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_source(source, now_ms=NOW_MS)
            repository.record_opennews_events(
                source=source,
                events=(
                    _report(
                        record_id="http-reuters",
                        title="Iran and Israel announce ceasefire talks after overnight attacks",
                        origin="Reuters",
                        published_at_ms=NOW_MS - 60_000,
                    ),
                    _report(
                        record_id="http-ap",
                        title="Iran and Israel announce ceasefire talks after overnight attacks",
                        origin="AP",
                        published_at_ms=NOW_MS - 50_000,
                    ),
                    _report(
                        record_id="http-bbc",
                        title="Major earthquake kills dozens and triggers coastal emergency",
                        origin="BBC",
                        published_at_ms=NOW_MS - 40_000,
                    ),
                ),
                observed_at_ms=NOW_MS,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
            assert repository.peek_brief_candidate(now_ms=NOW_MS) is None
            candidate = repository.peek_brief_candidate(now_ms=NOW_MS + 600_000)
            assert candidate is not None
            prepared = repository.prepare_brief_run(
                target_fingerprint=str(candidate["target_fingerprint"]),
                lease_owner="http-seam",
                lease_token="http-seam-lease",
                now_ms=NOW_MS + 600_000,
            )
        assert prepared is not None and not prepared["completed_without_model"]
        stories = tuple(NewsBriefStory.model_validate(story) for story in prepared["top_stories"])
        synthesis = json.dumps(
            {
                "lead": f"{stories[0].primary_title} [1].",
                "lines": [
                    {"n": index, "text": f"{story.primary_title} [{index}]."}
                    for index, story in enumerate(stories, start=1)
                ],
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "ollama.test"
            return httpx.Response(
                200,
                json={
                    "model": "llama3.1:8b",
                    "choices": [{"message": {"content": synthesis}}],
                },
            )

        publisher = ProviderChainNewsBriefPublisher(
            ollama_base_url="https://ollama.test/v1",
            openrouter_api_key=None,
            groq_api_key=None,
            transport=httpx.MockTransport(handler),
        )
        try:
            result = publisher.publish(stories, date_iso="2026-08-07")
        finally:
            publisher.close()
        assert result.brief_kind == "l1"
        assert result.provider == "ollama"

        with conn.transaction():
            assert repository.start_brief_model(
                run_id=str(prepared["claim"]["run_id"]),
                lease_owner="http-seam",
                lease_token="http-seam-lease",
                now_ms=NOW_MS + 600_001,
            )
            publication_id = repository.publish_brief(
                claim=prepared["claim"],
                selection=prepared["selection"],
                result=result,
                now_ms=NOW_MS + 600_002,
            )
        assert publication_id is not None
        serving_before = _brief_serving_facts(conn)

        settings = Settings(ws_token="secret", storage=postgres_settings_storage())
        settings.set_config_dir(tmp_path / "app-home")
        app = create_app(settings=settings)
        headers = {"Authorization": "Bearer secret"}
        with TestClient(app) as client:
            response = client.get("/api/news/brief", headers=headers)
            unchanged = client.get(
                "/api/news/brief",
                headers={**headers, "If-None-Match": response.headers["etag"]},
            )

        assert response.status_code == 200
        assert unchanged.status_code == 304
        public = response.json()["data"]
        assert public["state"] == "current"
        assert public["publication"]["publication_id"] == publication_id
        assert public["publication"]["top_stories"] == list(prepared["top_stories"])
        assert public["publication"]["provider"] == "ollama"
        assert public["publication"]["locale"] == "en"
        assert not ({"country", "language", "personalization", "topic_weights"} & set(public))
        assert _brief_serving_facts(conn) == serving_before
    finally:
        conn.close()


def test_story_turn_atomically_seals_one_public_selection_and_replay_writes_nothing() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_source(source, now_ms=NOW_MS)
            repository.record_opennews_events(
                source=source,
                events=(
                    _report(
                        record_id="reuters-ceasefire",
                        title="Iran and Israel announce ceasefire talks after overnight attacks",
                        origin="Reuters",
                        published_at_ms=NOW_MS - 60_000,
                    ),
                    _report(
                        record_id="ap-ceasefire",
                        title="Iran and Israel announce ceasefire talks after overnight attacks",
                        origin="AP",
                        published_at_ms=NOW_MS - 50_000,
                    ),
                    _report(
                        record_id="bbc-earthquake",
                        title="Major earthquake kills dozens and triggers coastal emergency",
                        origin="BBC",
                        published_at_ms=NOW_MS - 40_000,
                    ),
                ),
                observed_at_ms=NOW_MS,
            )
            first = repository.rebuild_stories(now_ms=NOW_MS)

        row = conn.execute("SELECT * FROM news_brief_selection_current WHERE singleton_key = true").fetchone()
        assert row is not None
        snapshot = dict(row)
        assert re.fullmatch(r"[0-9a-f]{64}", str(snapshot["selection_fingerprint"]))
        assert (
            snapshot["projection_revision"]
            == conn.execute(
                "SELECT input_fingerprint FROM news_projection_summary WHERE singleton_key='current'"
            ).fetchone()["input_fingerprint"]
        )
        assert snapshot["selector_evaluated_at_ms"] == NOW_MS - (NOW_MS % 3_600_000)
        assert snapshot["identity_version"]
        assert snapshot["selector_version"]

        top_stories = list(snapshot["top_stories"])
        assert 1 <= len(top_stories) <= 8
        assert any(story["unique_source_count"] == 2 for story in top_stories)
        assert all(
            {
                "story_id",
                "primary_title",
                "primary_source",
                "primary_link",
                "primary_published_at_ms",
                "source_count",
                "unique_source_count",
                "sources",
                "last_updated_ms",
                "member_titles",
                "source_tier",
                "upstream_importance_score",
                "entity_corroboration",
                "corroboration_source_count",
                "importance_score",
                "effective_importance_score",
                "is_alert",
                "threat_level",
                "category",
            }
            <= set(story)
            for story in top_stories
        )
        assert set(snapshot["selection_stats"]) == {
            "considered",
            "admissibility_dropped",
            "source_cap_dropped",
            "overflow_dropped",
            "brief_eligible_considered",
            "brief_eligible_promoted",
        }

        with conn.transaction():
            replay = repository.rebuild_stories(now_ms=NOW_MS)
        replay_row = conn.execute("SELECT * FROM news_brief_selection_current WHERE singleton_key = true").fetchone()

        assert first["projection_status"] == "rebuilt"
        assert replay["projection_status"] == "unchanged_input"
        assert replay["rows_written"] == 0
        assert dict(replay_row) == snapshot
        assert conn.execute("SELECT count(*) AS count FROM news_brief_selection_current").fetchone()["count"] == 1
    finally:
        conn.close()


def test_no_eligible_lead_publishes_degraded_top_stories_without_model_and_waits() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_source(source, now_ms=NOW_MS)
            repository.record_opennews_events(
                source=source,
                events=(
                    _report(
                        record_id="single-source-alert",
                        title="Missile attack kills dozens and triggers regional emergency",
                        origin="Reuters",
                        published_at_ms=NOW_MS - 30_000,
                    ),
                ),
                observed_at_ms=NOW_MS,
            )
            repository.rebuild_stories(now_ms=NOW_MS)

        with conn.transaction():
            assert repository.peek_brief_candidate(now_ms=NOW_MS) is None
        current = conn.execute("SELECT * FROM news_brief_current WHERE singleton_key = true").fetchone()
        assert current["target_fingerprint"] is not None
        assert current["pending_first_dirty_at_ms"] == NOW_MS
        assert current["pending_due_at_ms"] == NOW_MS + 600_000

        with conn.transaction():
            candidate = repository.peek_brief_candidate(now_ms=NOW_MS + 600_000)
        assert candidate is not None
        with conn.transaction():
            prepared = repository.prepare_brief_run(
                target_fingerprint=str(candidate["target_fingerprint"]),
                lease_owner="test-worker",
                lease_token="test-lease",
                now_ms=NOW_MS + 600_000,
            )

        assert prepared == {"completed_without_model": True}
        public = repository.get_brief(now_ms=NOW_MS + 600_000)
        assert public["state"] == "degraded"
        assert public["publication"]["brief_kind"] == "none"
        assert public["publication"]["quality"] == "degraded"
        assert public["publication"]["world_brief"] == ""
        assert public["publication"]["brief_story_lines"] == []
        assert public["publication"]["sources"] == []
        assert len(public["publication"]["top_stories"]) == 1
        assert public["latest_run"]["status"] == "waiting_input"
        assert public["latest_run"]["model_outcome"] == "none"
        assert public["latest_run"]["pointer_action"] == "advance_degraded"

        with conn.transaction():
            assert repository.peek_brief_candidate(now_ms=NOW_MS + 3_600_000) is None
    finally:
        conn.close()


def test_degraded_retry_preserves_the_whole_healthy_lkg_then_same_target_can_recover() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_source(source, now_ms=NOW_MS)
            repository.record_opennews_events(
                source=source,
                events=(
                    _report(
                        record_id="healthy-reuters",
                        title="Iran and Israel announce ceasefire talks after overnight attacks",
                        origin="Reuters",
                        published_at_ms=NOW_MS - 60_000,
                    ),
                    _report(
                        record_id="healthy-ap",
                        title="Iran and Israel announce ceasefire talks after overnight attacks",
                        origin="AP",
                        published_at_ms=NOW_MS - 50_000,
                    ),
                ),
                observed_at_ms=NOW_MS,
            )
            repository.rebuild_stories(now_ms=NOW_MS)

        with conn.transaction():
            assert repository.peek_brief_candidate(now_ms=NOW_MS) is None
        first_due_ms = NOW_MS + 600_000
        with conn.transaction():
            first_candidate = repository.peek_brief_candidate(now_ms=first_due_ms)
            assert first_candidate is not None
            first = repository.prepare_brief_run(
                target_fingerprint=str(first_candidate["target_fingerprint"]),
                lease_owner="brief-worker",
                lease_token="healthy-lease",
                now_ms=first_due_ms,
            )
        assert first is not None and not first["completed_without_model"]
        first_stories = list(first["top_stories"])
        healthy_result = _healthy_result(first_stories)
        with conn.transaction():
            assert repository.start_brief_model(
                run_id=str(first["claim"]["run_id"]),
                lease_owner="brief-worker",
                lease_token="healthy-lease",
                now_ms=first_due_ms + 1,
            )
            healthy_publication_id = repository.publish_brief(
                claim=first["claim"],
                selection=first["selection"],
                result=healthy_result,
                now_ms=first_due_ms + 2,
            )
        healthy = repository.get_brief(now_ms=first_due_ms + 2)
        assert healthy_publication_id is not None
        assert healthy["state"] == "current"
        assert healthy["publication"]["quality"] == "ok"
        sealed_healthy_publication = dict(healthy["publication"])

        next_turn_ms = NOW_MS + 3_600_000
        with conn.transaction():
            repository.record_opennews_events(
                source=source,
                events=(
                    _report(
                        record_id="new-alert",
                        title="Major earthquake kills dozens and triggers coastal emergency",
                        origin="BBC",
                        published_at_ms=next_turn_ms - 30_000,
                    ),
                ),
                observed_at_ms=next_turn_ms,
            )
            repository.rebuild_stories(now_ms=next_turn_ms)
            assert repository.peek_brief_candidate(now_ms=next_turn_ms) is None

        second_due_ms = next_turn_ms + 600_000
        with conn.transaction():
            second_candidate = repository.peek_brief_candidate(now_ms=second_due_ms)
            assert second_candidate is not None
            second = repository.prepare_brief_run(
                target_fingerprint=str(second_candidate["target_fingerprint"]),
                lease_owner="brief-worker",
                lease_token="degraded-lease",
                now_ms=second_due_ms,
            )
        assert second is not None and not second["completed_without_model"]
        eligible_story = next(
            story
            for story in second["top_stories"]
            if story["unique_source_count"] >= 2 or story["entity_corroboration"]
        )
        degraded_result = compose_none_brief(
            NewsBriefStory.model_validate(eligible_story),
            failure_code=INSIGHTS_SYNTHESIS_PROVIDER,
        )
        with conn.transaction():
            assert repository.start_brief_model(
                run_id=str(second["claim"]["run_id"]),
                lease_owner="brief-worker",
                lease_token="degraded-lease",
                now_ms=second_due_ms + 1,
            )
            served_id = repository.publish_brief(
                claim=second["claim"],
                selection=second["selection"],
                result=degraded_result,
                now_ms=second_due_ms + 2,
            )
        preserved = repository.get_brief(now_ms=second_due_ms + 2)
        assert served_id == healthy_publication_id
        assert preserved["state"] == "last_known_good"
        assert preserved["publication"] == sealed_healthy_publication
        assert preserved["latest_run"]["status"] == "retry_wait"
        assert preserved["latest_run"]["model_outcome"] == "none"
        assert preserved["latest_run"]["pointer_action"] == "preserve_lkg"

        retry_due_ms = second_due_ms + 1_800_002
        with conn.transaction():
            retry_candidate = repository.peek_brief_candidate(now_ms=retry_due_ms)
            assert retry_candidate is not None
            retry = repository.prepare_brief_run(
                target_fingerprint=str(retry_candidate["target_fingerprint"]),
                lease_owner="brief-worker",
                lease_token="recovery-lease",
                now_ms=retry_due_ms,
            )
        assert retry is not None and not retry["completed_without_model"]
        recovered_result = _healthy_result(list(retry["top_stories"]))
        with conn.transaction():
            assert repository.start_brief_model(
                run_id=str(retry["claim"]["run_id"]),
                lease_owner="brief-worker",
                lease_token="recovery-lease",
                now_ms=retry_due_ms + 1,
            )
            recovered_id = repository.publish_brief(
                claim=retry["claim"],
                selection=retry["selection"],
                result=recovered_result,
                now_ms=retry_due_ms + 2,
            )
        recovered = repository.get_brief(now_ms=retry_due_ms + 2)
        assert recovered_id != healthy_publication_id
        assert recovered["state"] == "current"
        assert recovered["publication"]["target_fingerprint"] == second_candidate["target_fingerprint"]
        assert recovered["latest_run"]["status"] == "published"
        assert recovered["latest_run"]["model_outcome"] == "ok"
        assert recovered["latest_run"]["pointer_action"] == "advance_ok"
    finally:
        conn.close()


def test_changed_selection_fences_an_inflight_target_before_late_publication() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_source(source, now_ms=NOW_MS)
            repository.record_opennews_events(
                source=source,
                events=(
                    _report(
                        record_id="old-reuters",
                        title="Iran and Israel announce ceasefire talks after overnight attacks",
                        origin="Reuters",
                        published_at_ms=NOW_MS - 60_000,
                    ),
                    _report(
                        record_id="old-ap",
                        title="Iran and Israel announce ceasefire talks after overnight attacks",
                        origin="AP",
                        published_at_ms=NOW_MS - 50_000,
                    ),
                ),
                observed_at_ms=NOW_MS,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
            assert repository.peek_brief_candidate(now_ms=NOW_MS) is None
        due_ms = NOW_MS + 600_000
        with conn.transaction():
            candidate = repository.peek_brief_candidate(now_ms=due_ms)
            assert candidate is not None
            prepared = repository.prepare_brief_run(
                target_fingerprint=str(candidate["target_fingerprint"]),
                lease_owner="brief-worker",
                lease_token="stale-lease",
                now_ms=due_ms,
            )
            assert prepared is not None and not prepared["completed_without_model"]
            assert repository.start_brief_model(
                run_id=str(prepared["claim"]["run_id"]),
                lease_owner="brief-worker",
                lease_token="stale-lease",
                now_ms=due_ms + 1,
            )

        changed_ms = NOW_MS + 3_600_000
        with conn.transaction():
            repository.record_opennews_events(
                source=source,
                events=(
                    _report(
                        record_id="new-bbc",
                        title="Major earthquake kills dozens and triggers coastal emergency",
                        origin="BBC",
                        published_at_ms=changed_ms - 30_000,
                    ),
                ),
                observed_at_ms=changed_ms,
            )
            repository.rebuild_stories(now_ms=changed_ms)
            assert repository.peek_brief_candidate(now_ms=changed_ms) is None

        fenced = conn.execute(
            "SELECT * FROM news_brief_runs WHERE run_id = %s",
            (str(prepared["claim"]["run_id"]),),
        ).fetchone()
        assert fenced["status"] == "retry_wait"
        assert fenced["model_outcome"] == "none"
        assert fenced["pointer_action"] == "none"
        assert fenced["lease_owner"] is None
        assert fenced["lease_token"] is None

        with conn.transaction():
            assert (
                repository.publish_brief(
                    claim=prepared["claim"],
                    selection=prepared["selection"],
                    result=_healthy_result(list(prepared["top_stories"])),
                    now_ms=changed_ms + 1,
                )
                is None
            )
        public = repository.get_brief(now_ms=changed_ms + 1)
        assert public["state"] == "unavailable"
        assert public["publication"] is None
        assert public["latest_run"] is None
        assert public["target_fingerprint"] != candidate["target_fingerprint"]
    finally:
        conn.close()


def test_identical_degraded_retry_writes_no_new_publication_or_current_row() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_source(source, now_ms=NOW_MS)
            repository.record_opennews_events(
                source=source,
                events=(
                    _report(
                        record_id="retry-reuters",
                        title="Iran and Israel announce ceasefire talks after overnight attacks",
                        origin="Reuters",
                        published_at_ms=NOW_MS - 60_000,
                    ),
                    _report(
                        record_id="retry-ap",
                        title="Iran and Israel announce ceasefire talks after overnight attacks",
                        origin="AP",
                        published_at_ms=NOW_MS - 50_000,
                    ),
                ),
                observed_at_ms=NOW_MS,
            )
            repository.rebuild_stories(now_ms=NOW_MS)
            assert repository.peek_brief_candidate(now_ms=NOW_MS) is None

        first_due_ms = NOW_MS + 600_000
        with conn.transaction():
            candidate = repository.peek_brief_candidate(now_ms=first_due_ms)
            assert candidate is not None
            first = repository.prepare_brief_run(
                target_fingerprint=str(candidate["target_fingerprint"]),
                lease_owner="brief-worker",
                lease_token="first-none-lease",
                now_ms=first_due_ms,
            )
        assert first is not None and not first["completed_without_model"]
        lead = next(
            NewsBriefStory.model_validate(story)
            for story in first["top_stories"]
            if story["unique_source_count"] >= 2 or story["entity_corroboration"]
        )
        result = compose_none_brief(lead, failure_code=INSIGHTS_SYNTHESIS_PROVIDER)
        with conn.transaction():
            assert repository.start_brief_model(
                run_id=str(first["claim"]["run_id"]),
                lease_owner="brief-worker",
                lease_token="first-none-lease",
                now_ms=first_due_ms + 1,
            )
            first_publication_id = repository.publish_brief(
                claim=first["claim"],
                selection=first["selection"],
                result=result,
                now_ms=first_due_ms + 2,
            )
        current_before = dict(conn.execute("SELECT * FROM news_brief_current WHERE singleton_key=true").fetchone())
        publication_count_before = conn.execute("SELECT count(*) AS count FROM news_brief_publications").fetchone()[
            "count"
        ]

        retry_due_ms = first_due_ms + 1_800_002
        with conn.transaction():
            retry_candidate = repository.peek_brief_candidate(now_ms=retry_due_ms)
            assert retry_candidate is not None
            retry = repository.prepare_brief_run(
                target_fingerprint=str(retry_candidate["target_fingerprint"]),
                lease_owner="brief-worker",
                lease_token="second-none-lease",
                now_ms=retry_due_ms,
            )
        assert retry is not None and not retry["completed_without_model"]
        with conn.transaction():
            assert repository.start_brief_model(
                run_id=str(retry["claim"]["run_id"]),
                lease_owner="brief-worker",
                lease_token="second-none-lease",
                now_ms=retry_due_ms + 1,
            )
            second_publication_id = repository.publish_brief(
                claim=retry["claim"],
                selection=retry["selection"],
                result=result,
                now_ms=retry_due_ms + 2,
            )

        assert second_publication_id == first_publication_id
        assert (
            conn.execute("SELECT count(*) AS count FROM news_brief_publications").fetchone()["count"]
            == publication_count_before
        )
        assert (
            dict(conn.execute("SELECT * FROM news_brief_current WHERE singleton_key=true").fetchone()) == current_before
        )
    finally:
        conn.close()


def _brief_serving_facts(conn) -> tuple[dict[str, object], dict[str, object], tuple[tuple[object, ...], ...]]:
    publication = dict(
        conn.execute(
            """
            SELECT count(*) AS count,
                   min(publication_id) AS first_id,
                   max(publication_id) AS last_id,
                   min(created_at_ms) AS first_created_at_ms,
                   max(created_at_ms) AS last_created_at_ms
              FROM news_brief_publications
            """
        ).fetchone()
    )
    current = dict(conn.execute("SELECT * FROM news_brief_current WHERE singleton_key=true").fetchone())
    runs = tuple(
        tuple(row.values())
        for row in conn.execute(
            """
            SELECT run_id, status, model_outcome, pointer_action, failure_count,
                   next_due_at_ms, lease_owner, lease_token, lease_expires_at_ms,
                   last_error_code, created_at_ms, updated_at_ms,
                   last_attempt_at_ms, completed_at_ms
              FROM news_brief_runs
             ORDER BY run_id
            """
        ).fetchall()
    )
    return publication, current, runs


def _healthy_result(stories: list[dict[str, object]]) -> NewsBriefSynthesisResult:
    return NewsBriefSynthesisResult(
        brief_kind="l1",
        quality="ok",
        world_brief="Iran and Israel opened ceasefire talks after overnight attacks [1].",
        brief_story_lines=tuple(
            NewsBriefStoryLine(n=index, text=f"{story['primary_title']} [{index}]")
            for index, story in enumerate(stories, start=1)
        ),
        sources=tuple(
            NewsBriefSource(
                title=str(story["primary_title"]),
                source=str(story["primary_source"]),
                url=str(story["primary_link"] or ""),
                published_at_ms=int(story["primary_published_at_ms"]),
            )
            for story in stories
        ),
        provider="fake-provider",
        model="fake-model",
        validation={"failure_code": None},
    )
