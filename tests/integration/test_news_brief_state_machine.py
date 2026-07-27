from __future__ import annotations

from typing import Any

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news import (
    NewsFeedEntry,
    NewsInterface,
    NewsRepository,
    NewsSourceDefinition,
)

NOW_MS = 1_779_100_000_000
ORDINARY_DEBOUNCE_MS = 120_000
CRITICAL_DEBOUNCE_MS = 10_000


def test_ordinary_proposal_preserves_first_observation_then_activates_once(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        _seed_story(
            repository,
            source_id="wire-a",
            title="Government implements a new semiconductor export policy",
            observed_at_ms=NOW_MS,
            impact_score=80,
        )
        first_at_ms = NOW_MS + 2_000

        first = _plan(repository, now_ms=first_at_ms)
        continued = _plan(repository, now_ms=first_at_ms + 30_000)
        almost_due = _plan(repository, now_ms=first_at_ms + ORDINARY_DEBOUNCE_MS - 1)
        matured = _plan(repository, now_ms=first_at_ms + ORDINARY_DEBOUNCE_MS)
        repeated = _plan(repository, now_ms=first_at_ms + ORDINARY_DEBOUNCE_MS + 30_000)

        assert first["status"] == continued["status"] == almost_due["status"] == "pending"
        assert first["lane"] == "ordinary"
        assert first["proposal_id"] == continued["proposal_id"] == almost_due["proposal_id"]
        proposal = conn.execute(
            "SELECT * FROM news_brief_proposals WHERE proposal_id = %s",
            (first["proposal_id"],),
        ).fetchone()
        assert proposal["first_proposed_at_ms"] == first_at_ms
        assert proposal["activation_due_at_ms"] == first_at_ms + ORDINARY_DEBOUNCE_MS
        assert proposal["last_observed_at_ms"] == first_at_ms + ORDINARY_DEBOUNCE_MS
        assert proposal["status"] == "activated"
        assert matured["status"] == "active"
        assert repeated["status"] == "active"
        assert repeated["changed"] is False
        assert conn.execute("SELECT count(*) AS count FROM news_brief_activations").fetchone()["count"] == 1
        conn.commit()
    finally:
        conn.close()


def test_pending_proposal_is_cancelled_on_return_to_active_and_superseded_by_new_candidate(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        story_a = _seed_story(
            repository,
            source_id="wire-a",
            title="Central bank implements an emergency liquidity facility",
            observed_at_ms=NOW_MS,
            impact_score=80,
        )
        active_a = _plan(repository, now_ms=NOW_MS + 2_000, ordinary_ms=0)
        assert active_a["status"] == "active"

        story_b = _seed_story(
            repository,
            source_id="wire-b",
            title="Government implements new semiconductor export controls",
            observed_at_ms=NOW_MS + 3_000,
            impact_score=80,
        )
        _set_only_eligible(conn, story_b)
        pending_b = _plan(repository, now_ms=NOW_MS + 5_000)
        assert pending_b["status"] == "pending"

        _set_only_eligible(conn, story_a)
        returned_a = _plan(repository, now_ms=NOW_MS + 35_000)
        assert returned_a["status"] == "active"
        assert returned_a["changed"] is False
        assert returned_a["activation_id"] == active_a["activation_id"]
        cancelled = conn.execute(
            "SELECT status, reason FROM news_brief_proposals WHERE proposal_id = %s",
            (pending_b["proposal_id"],),
        ).fetchone()
        assert cancelled["status"] == "cancelled"
        assert cancelled["reason"]["resolution"] == "planner_returned_to_active"

        _set_only_eligible(conn, story_b)
        next_b = _plan(repository, now_ms=NOW_MS + 40_000)
        story_c = _seed_story(
            repository,
            source_id="wire-c",
            title="Regional government closes a strategic border crossing",
            observed_at_ms=NOW_MS + 41_000,
            impact_score=80,
        )
        _set_only_eligible(conn, story_c)
        pending_c = _plan(repository, now_ms=NOW_MS + 42_000)

        assert next_b["proposal_id"] != pending_c["proposal_id"]
        assert (
            conn.execute(
                "SELECT status FROM news_brief_proposals WHERE proposal_id = %s",
                (next_b["proposal_id"],),
            ).fetchone()["status"]
            == "superseded"
        )
        current = conn.execute("SELECT * FROM news_brief_proposals WHERE status = 'pending'").fetchone()
        assert current["proposal_id"] == pending_c["proposal_id"]
        assert current["first_proposed_at_ms"] == NOW_MS + 42_000
        conn.commit()
    finally:
        conn.close()


def test_verified_critical_addition_uses_ten_second_lane(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        _seed_story(
            repository,
            source_id="official",
            title="Central bank implements an emergency interest rate cut",
            observed_at_ms=NOW_MS,
            impact_score=95,
            source_role="official_authority",
        )
        first_at_ms = NOW_MS + 2_000

        first = _plan(repository, now_ms=first_at_ms)
        before_due = _plan(repository, now_ms=first_at_ms + CRITICAL_DEBOUNCE_MS - 1)
        matured = _plan(repository, now_ms=first_at_ms + CRITICAL_DEBOUNCE_MS)

        assert first["lane"] == "verified_critical"
        assert first["status"] == before_due["status"] == "pending"
        assert matured["status"] == "active"
        assert (
            conn.execute(
                "SELECT activation_due_at_ms FROM news_brief_proposals WHERE proposal_id = %s",
                (first["proposal_id"],),
            ).fetchone()["activation_due_at_ms"]
            == first_at_ms + CRITICAL_DEBOUNCE_MS
        )
        conn.commit()
    finally:
        conn.close()


def test_weakly_evidenced_critical_addition_keeps_ordinary_debounce(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        _seed_story(
            repository,
            source_id="single-wire",
            title="Government announces emergency semiconductor export controls",
            observed_at_ms=NOW_MS,
            impact_score=95,
        )
        first_at_ms = NOW_MS + 2_000

        first = _plan(repository, now_ms=first_at_ms)
        after_fast_lane = _plan(
            repository,
            now_ms=first_at_ms + CRITICAL_DEBOUNCE_MS,
        )

        assert first["lane"] == "ordinary"
        assert first["status"] == after_fast_lane["status"] == "pending"
        proposal = conn.execute(
            "SELECT activation_due_at_ms FROM news_brief_proposals WHERE proposal_id = %s",
            (first["proposal_id"],),
        ).fetchone()
        assert proposal["activation_due_at_ms"] == first_at_ms + ORDINARY_DEBOUNCE_MS
        conn.commit()
    finally:
        conn.close()


def test_active_story_conflict_activates_rectification_on_next_planner_cycle(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        title = "Central bank announces an emergency interest rate cut"
        story_id = _seed_story(
            repository,
            source_id="official",
            title=title,
            summary="The authority reported a 25 basis point reduction.",
            observed_at_ms=NOW_MS,
            impact_score=95,
            source_role="official_authority",
        )
        first = _plan(repository, now_ms=NOW_MS + 2_000, ordinary_ms=0, critical_ms=0)
        before = NewsInterface(repository).get_global_brief()["active_selection"]
        assert before is not None

        second_source = _source("independent-wire", "wire_service")
        repository.sync_sources((second_source,), now_ms=NOW_MS + 3_000)
        repository.record_fetch_success(
            source=second_source,
            entries=(
                _entry(
                    source_id="independent-wire",
                    title=title,
                    summary="The wire reported a 50 basis point reduction.",
                    published_at_ms=NOW_MS + 3_000,
                ),
            ),
            started_at_ms=NOW_MS + 3_000,
            finished_at_ms=NOW_MS + 3_000,
            status_code=200,
            etag=None,
            last_modified=None,
            not_modified=False,
        )
        repository.project_pending_revisions(now_ms=NOW_MS + 4_000, limit=100)
        conn.execute(
            """
            UPDATE news_stories
               SET brief_eligible = true, impact_score = 95, priority_score = 95
             WHERE story_id = %s
            """,
            (story_id,),
        )

        rectified = _plan(repository, now_ms=NOW_MS + 5_000)
        after = NewsInterface(repository).get_global_brief()["active_selection"]

        assert rectified["status"] == "active"
        assert rectified["lane"] == "rectification"
        assert rectified["activation_id"] != first["activation_id"]
        assert after is not None
        assert after["activation_sequence"] == 2
        assert after["evidence_bundle"]["stories"][0]["evidence_posture"] == "contested"
        assert after["synthesis_input_hash"] != before["synthesis_input_hash"]
        conn.commit()
    finally:
        conn.close()


def _seed_story(
    repository: NewsRepository,
    *,
    source_id: str,
    title: str,
    observed_at_ms: int,
    impact_score: int,
    source_role: str = "wire_service",
    summary: str = "",
) -> str:
    source = _source(source_id, source_role)
    repository.sync_sources((source,), now_ms=observed_at_ms)
    repository.record_fetch_success(
        source=source,
        entries=(
            _entry(
                source_id=source_id,
                title=title,
                summary=summary,
                published_at_ms=observed_at_ms,
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
    story_id = str(
        repository.conn.execute(
            """
            SELECT story_id
              FROM news_stories
             ORDER BY created_at_ms DESC, story_id DESC
             LIMIT 1
            """
        ).fetchone()["story_id"]
    )
    repository.conn.execute(
        """
        UPDATE news_stories
           SET brief_eligible = true,
               impact_score = %s,
               priority_score = %s
         WHERE story_id = %s
        """,
        (impact_score, impact_score, story_id),
    )
    return story_id


def _source(source_id: str, source_role: str) -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id=source_id,
        name=source_id,
        feed_url=f"https://{source_id}.example/feed.xml",
        source_domain=f"{source_id}.example",
        source_role=source_role,  # type: ignore[arg-type]
        trust_tier="authoritative" if source_role == "official_authority" else "trusted",
        source_chain_id=source_id,
        publisher_organization_id=source_id,
        default_language="en",
        refresh_interval_seconds=60,
    )


def _entry(
    *,
    source_id: str,
    title: str,
    summary: str,
    published_at_ms: int,
) -> NewsFeedEntry:
    slug = source_id.replace("_", "-")
    return NewsFeedEntry(
        guid=f"{slug}-{published_at_ms}",
        link=f"https://{slug}.example/{published_at_ms}",
        title=title,
        summary=summary,
        published_at_ms=published_at_ms,
        language="en",
    )


def _plan(
    repository: NewsRepository,
    *,
    now_ms: int,
    ordinary_ms: int = ORDINARY_DEBOUNCE_MS,
    critical_ms: int = CRITICAL_DEBOUNCE_MS,
) -> dict[str, Any]:
    return repository.plan_global_brief(
        now_ms=now_ms,
        candidate_limit=100,
        debounce_ms=ordinary_ms,
        critical_debounce_ms=critical_ms,
    )


def _set_only_eligible(conn: Any, story_id: str) -> None:
    conn.execute(
        """
        UPDATE news_stories
           SET brief_eligible = (story_id = %s)
        """,
        (story_id,),
    )
