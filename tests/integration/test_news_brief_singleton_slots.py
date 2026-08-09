from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tracefold.news import brief_store
from tracefold.news.models import (
    INSIGHTS_SYNTHESIS_PROVIDER,
    NewsBriefSource,
    NewsBriefStoryLine,
    NewsBriefSynthesisResult,
)

SLOT_MS = 30 * 60 * 1_000
SLOT_AT_MS = 1_786_082_400_000


def test_missing_story_selection_does_not_open_or_complete_the_current_slot() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = SimpleNamespace(conn=conn)

        with conn.transaction():
            candidate = brief_store.peek_brief_candidate(repository, now_ms=SLOT_AT_MS)
            prepared = brief_store.prepare_brief_run(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="missing-selection",
                now_ms=SLOT_AT_MS,
            )

        current = conn.execute(
            "SELECT slot_at_ms, slot_status, active_selection FROM news_brief_current WHERE singleton_key = true"
        ).fetchone()
        assert candidate is None
        assert prepared is None
        assert dict(current) == {
            "slot_at_ms": None,
            "slot_status": "due",
            "active_selection": None,
        }
    finally:
        conn.close()


def test_half_hour_slot_freezes_selection_and_skips_old_downtime_slots() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = SimpleNamespace(conn=conn)
        with conn.transaction():
            _replace_selection(conn, fingerprint="a" * 64, title="Frozen A")
            candidate = brief_store.peek_brief_candidate(repository, now_ms=SLOT_AT_MS + 123)
            assert candidate == {"slot_at_ms": SLOT_AT_MS, "next_due_at_ms": SLOT_AT_MS}
            prepared = brief_store.prepare_brief_run(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="lease-a",
                now_ms=SLOT_AT_MS + 124,
            )
        assert prepared is not None and not prepared["completed_without_model"]

        with conn.transaction():
            _replace_selection(conn, fingerprint="b" * 64, title="Live B")
            assert brief_store.peek_brief_candidate(repository, now_ms=SLOT_AT_MS + 125) is None
            assert brief_store.start_brief_model(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="lease-a",
                now_ms=SLOT_AT_MS + 126,
            )
            publication_id = brief_store.publish_brief(
                repository,
                claim=prepared["claim"],
                result=_healthy_result(prepared["top_stories"]),
                now_ms=SLOT_AT_MS + 127,
            )
        assert publication_id is not None
        public = brief_store.get_brief(repository, now_ms=SLOT_AT_MS + 128)
        assert public["state"] == "current"
        assert public["publication"]["top_stories"][0]["primary_title"] == "Frozen A"
        assert public["latest_run"]["status"] == "completed"

        newest_slot = SLOT_AT_MS + 4 * SLOT_MS
        with conn.transaction():
            candidate = brief_store.peek_brief_candidate(repository, now_ms=newest_slot + 500)
        assert candidate == {"slot_at_ms": newest_slot, "next_due_at_ms": newest_slot}
        current = conn.execute("SELECT * FROM news_brief_current WHERE singleton_key = true").fetchone()
        assert current["slot_at_ms"] == newest_slot
        assert current["slot_status"] == "due"
        assert current["next_due_at_ms"] == newest_slot + SLOT_MS
    finally:
        conn.close()


def test_expired_same_slot_lease_reclaims_the_original_frozen_selection() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = SimpleNamespace(conn=conn)
        with conn.transaction():
            _replace_selection(conn, fingerprint="a" * 64, title="Frozen A")
            brief_store.peek_brief_candidate(repository, now_ms=SLOT_AT_MS)
            first = brief_store.prepare_brief_run(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="lease-a",
                now_ms=SLOT_AT_MS,
            )
        assert first is not None and not first["completed_without_model"]

        with conn.transaction():
            assert brief_store.start_brief_model(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="lease-a",
                now_ms=SLOT_AT_MS + 1,
            )
            _replace_selection(conn, fingerprint="b" * 64, title="Live B")
            reclaim_at_ms = SLOT_AT_MS + brief_store.BRIEF_LEASE_MS
            candidate = brief_store.peek_brief_candidate(repository, now_ms=reclaim_at_ms)
            assert candidate is not None
            reclaimed = brief_store.prepare_brief_run(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="lease-b",
                now_ms=reclaim_at_ms,
            )
        assert reclaimed is not None and not reclaimed["completed_without_model"]
        assert reclaimed["selection"]["selection_fingerprint"] == "a" * 64
        assert reclaimed["top_stories"][0]["primary_title"] == "Frozen A"
        current = conn.execute("SELECT * FROM news_brief_current WHERE singleton_key = true").fetchone()
        assert current["attempt_count"] == 1
        assert current["failure_count"] == 1
        assert current["lease_token"] == "lease-b"
        with conn.transaction():
            assert brief_store.start_brief_model(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="lease-b",
                now_ms=reclaim_at_ms + 1,
            )
        retried = conn.execute("SELECT * FROM news_brief_current WHERE singleton_key = true").fetchone()
        assert retried["attempt_count"] == 2
        assert retried["failure_count"] == 1
    finally:
        conn.close()


def test_degraded_slot_preserves_whole_lkg_and_empty_selection_is_not_claimed() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = SimpleNamespace(conn=conn)
        with conn.transaction():
            _replace_selection(conn, fingerprint="a" * 64, title="Healthy A")
            brief_store.peek_brief_candidate(repository, now_ms=SLOT_AT_MS)
            healthy = brief_store.prepare_brief_run(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="healthy",
                now_ms=SLOT_AT_MS,
            )
        assert healthy is not None and not healthy["completed_without_model"]
        with conn.transaction():
            assert brief_store.start_brief_model(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="healthy",
                now_ms=SLOT_AT_MS + 1,
            )
            brief_store.publish_brief(
                repository,
                claim=healthy["claim"],
                result=_healthy_result(healthy["top_stories"]),
                now_ms=SLOT_AT_MS + 2,
            )
        sealed_lkg = brief_store.get_brief(repository, now_ms=SLOT_AT_MS + 2)["publication"]

        second_slot = SLOT_AT_MS + SLOT_MS
        with conn.transaction():
            _replace_selection(conn, fingerprint="b" * 64, title="Degraded B")
            brief_store.peek_brief_candidate(repository, now_ms=second_slot)
            degraded = brief_store.prepare_brief_run(
                repository,
                slot_at_ms=second_slot,
                lease_owner="slot-test",
                lease_token="degraded",
                now_ms=second_slot,
            )
        assert degraded is not None and not degraded["completed_without_model"]
        with conn.transaction():
            assert brief_store.start_brief_model(
                repository,
                slot_at_ms=second_slot,
                lease_owner="slot-test",
                lease_token="degraded",
                now_ms=second_slot + 1,
            )
            served_id = brief_store.publish_brief(
                repository,
                claim=degraded["claim"],
                result=_none_result(),
                now_ms=second_slot + 2,
            )
        preserved = brief_store.get_brief(repository, now_ms=second_slot + 2)
        assert served_id == sealed_lkg["publication_id"]
        assert preserved["state"] == "last_known_good"
        assert preserved["publication"] == sealed_lkg
        assert preserved["latest_run"]["pointer_action"] == "preserve_lkg"

        empty_lkg_slot = second_slot + SLOT_MS
        with conn.transaction():
            _replace_selection(conn, fingerprint="c" * 64, title=None)
            assert brief_store.peek_brief_candidate(repository, now_ms=empty_lkg_slot) is None
            assert (
                brief_store.prepare_brief_run(
                    repository,
                    slot_at_ms=empty_lkg_slot,
                    lease_owner="slot-test",
                    lease_token="empty-lkg",
                    now_ms=empty_lkg_slot,
                )
                is None
            )
        empty_lkg = brief_store.get_brief(repository, now_ms=empty_lkg_slot)
        assert empty_lkg["state"] == "last_known_good"
        assert empty_lkg["publication"] == sealed_lkg
        assert empty_lkg["latest_run"]["status"] == "due"
        assert empty_lkg["latest_run"]["attempt_count"] == 0

        conn.execute("TRUNCATE news_brief_selection_current, news_brief_current")
        conn.execute(
            """
            INSERT INTO news_brief_current (
              singleton_key, slot_status, next_due_at_ms,
              attempt_count, failure_count, pointer_action,
              created_at_ms, updated_at_ms
            ) VALUES (true, 'due', 0, 0, 0, 'none', 0, 0)
            """
        )
        conn.commit()
        with conn.transaction():
            _replace_selection(conn, fingerprint="d" * 64, title="No LKG D")
            brief_store.peek_brief_candidate(repository, now_ms=SLOT_AT_MS)
            no_lkg = brief_store.prepare_brief_run(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="no-lkg",
                now_ms=SLOT_AT_MS,
            )
        assert no_lkg is not None and not no_lkg["completed_without_model"]
        with conn.transaction():
            assert brief_store.start_brief_model(
                repository,
                slot_at_ms=SLOT_AT_MS,
                lease_owner="slot-test",
                lease_token="no-lkg",
                now_ms=SLOT_AT_MS + 1,
            )
            brief_store.publish_brief(
                repository,
                claim=no_lkg["claim"],
                result=_none_result(),
                now_ms=SLOT_AT_MS + 2,
            )
        no_lkg_public = brief_store.get_brief(repository, now_ms=SLOT_AT_MS + 2)
        assert no_lkg_public["state"] == "degraded"
        assert no_lkg_public["publication"]["quality"] == "degraded"

        conn.execute("TRUNCATE news_brief_selection_current, news_brief_current")
        conn.execute(
            """
            INSERT INTO news_brief_current (
              singleton_key, slot_status, next_due_at_ms,
              attempt_count, failure_count, pointer_action,
              created_at_ms, updated_at_ms
            ) VALUES (true, 'due', 0, 0, 0, 'none', 0, 0)
            """
        )
        conn.commit()
        with conn.transaction():
            _replace_selection(conn, fingerprint="e" * 64, title=None)
            assert brief_store.peek_brief_candidate(repository, now_ms=SLOT_AT_MS) is None
            assert (
                brief_store.prepare_brief_run(
                    repository,
                    slot_at_ms=SLOT_AT_MS,
                    lease_owner="slot-test",
                    lease_token="empty",
                    now_ms=SLOT_AT_MS,
                )
                is None
            )
        unavailable = brief_store.get_brief(repository, now_ms=SLOT_AT_MS)
        assert unavailable["state"] == "unavailable"
        assert unavailable["publication"] is None
        assert unavailable["latest_run"]["status"] == "due"
        assert unavailable["latest_run"]["attempt_count"] == 0
        assert unavailable["latest_run"]["failure_count"] == 0
        assert unavailable["latest_run"]["model_outcome"] is None
    finally:
        conn.close()


def _replace_selection(conn: Any, *, fingerprint: str, title: str | None) -> None:
    top_stories = [] if title is None else [_story(title)]
    conn.execute("DELETE FROM news_brief_selection_current")
    conn.execute(
        """
        INSERT INTO news_brief_selection_current (
          singleton_key, selection_fingerprint, projection_revision,
          selector_evaluated_at_ms, top_stories, selection_stats,
          selector_version, identity_version, updated_at_ms
        ) VALUES (true, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
        """,
        (
            fingerprint,
            "projection-revision",
            SLOT_AT_MS,
            json.dumps(top_stories),
            json.dumps(
                {
                    "considered": len(top_stories),
                    "admissibility_dropped": 0,
                    "source_cap_dropped": 0,
                    "overflow_dropped": 0,
                    "brief_eligible_considered": len(top_stories),
                    "brief_eligible_promoted": False,
                }
            ),
            "selector-v1",
            "identity-v1",
            SLOT_AT_MS,
        ),
    )


def _story(title: str) -> dict[str, object]:
    return {
        "story_id": f"story-{title.lower().replace(' ', '-')}",
        "primary_title": title,
        "primary_source": "Reuters",
        "primary_link": "https://example.test/story",
        "primary_published_at_ms": SLOT_AT_MS - 1_000,
        "source_count": 2,
        "unique_source_count": 2,
        "sources": ["Reuters", "AP"],
        "last_updated_ms": SLOT_AT_MS - 500,
        "member_titles": [title, f"{title} confirmed"],
        "source_tier": 1,
        "upstream_importance_score": 10.0,
        "entity_corroboration": False,
        "corroboration_source_count": 0,
        "importance_score": 10.0,
        "effective_importance_score": 10.0,
        "is_alert": False,
        "threat_level": "moderate",
        "category": "general",
    }


def _healthy_result(stories: list[dict[str, object]]) -> NewsBriefSynthesisResult:
    return NewsBriefSynthesisResult(
        brief_kind="l1",
        quality="ok",
        world_brief="A corroborated public report leads this cycle [1].",
        brief_story_lines=tuple(
            NewsBriefStoryLine(n=index, text=f"{story['primary_title']} [{index}].")
            for index, story in enumerate(stories, start=1)
        ),
        sources=tuple(
            NewsBriefSource(
                title=str(story["primary_title"]),
                source=str(story["primary_source"]),
                url=str(story["primary_link"]),
                published_at_ms=cast(int, story["primary_published_at_ms"]),
            )
            for story in stories
        ),
        provider="direct",
        model="deepseek-chat",
        validation={"failure_code": None, "stripped_citations": 0, "line_fallbacks": []},
    )


def _none_result() -> NewsBriefSynthesisResult:
    return NewsBriefSynthesisResult(
        brief_kind="none",
        quality="degraded",
        world_brief="",
        brief_story_lines=(),
        sources=(),
        provider="",
        model="",
        validation={"failure_code": INSIGHTS_SYNTHESIS_PROVIDER},
    )
