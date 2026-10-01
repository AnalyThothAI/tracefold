"""Compact evidence CAS, Event row locking and indexed band lookup on real PostgreSQL."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import closing
from types import SimpleNamespace

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_update_pg import EVENT, STAMP, seed_event
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.storage.events import BAND_CANDIDATES_SQL, prepare_evidence_snapshot
from tracefold.news.storage.update_commit import lock_event

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_evidence_cas_rejects_the_loser_and_unchanged_material_keeps_its_version():
    seed_event()
    with closing(connect_postgres_test()) as conn, conn.transaction():
        events = repositories_for_connection(conn).news
        first = events.latest_evidence_snapshot(EVENT)
        same = events.append_evidence_snapshot(event_id=EVENT, now_ms=STAMP + 1)
        assert same["evidence_version"] == first["evidence_version"]
        assert same["evidence_sha256"] == first["evidence_sha256"]
        material = events.evidence_snapshot_material(event_id=EVENT, focus_item_id=f"it-{EVENT}")
        focus = SimpleNamespace(
            fact_id="changed-focus", text="New tariff scope", context="", method="whole_item", span_start=0, span_end=16
        )
        one = prepare_evidence_snapshot(material, event_id=EVENT, now_ms=STAMP + 2, focus_fact=focus)
        two = prepare_evidence_snapshot(material, event_id=EVENT, now_ms=STAMP + 3, focus_fact=focus)
        winner = events.append_prepared_evidence_snapshot(one)
        assert winner["evidence_version"] == first["evidence_version"] + 1
        with pytest.raises(RuntimeError, match="news_event_evidence_snapshot_changed"), conn.transaction():
            events.append_prepared_evidence_snapshot(two)
        state = conn.execute("SELECT evidence FROM news_events WHERE event_id=%s", (EVENT,)).fetchone()["evidence"]
        assert len(state["versions"]) == 2
        assert set(state) == {"material_sha256", "focus_item_id", "fact_scopes", "versions"}
        assert "snapshot" not in state["versions"][0]


def test_event_lock_serializes_writers_and_does_not_block_membership_foreign_key():
    seed_event()
    with closing(connect_postgres_test()) as owner, ThreadPoolExecutor(max_workers=2) as pool:
        owner.execute("BEGIN")
        lock_event(owner, EVENT)

        def member():
            with closing(connect_postgres_test()) as conn, conn.transaction():
                conn.execute(
                    "INSERT INTO news_event_members"
                    "(event_id,item_id,fact_id,fact_text,joined_at_ms,match_kind) "
                    "VALUES (%s,%s,'compatible-member','same item',%s,'leader')",
                    (EVENT, f"it-{EVENT}", STAMP),
                )
                return True

        def writer():
            with closing(connect_postgres_test()) as conn, conn.transaction():
                lock_event(conn, EVENT)
                return True

        assert pool.submit(member).result(timeout=2)
        waiting = pool.submit(writer)
        with pytest.raises(TimeoutError):
            waiting.result(timeout=0.1)
        owner.commit()
        assert waiting.result(timeout=2)


def test_band_candidates_use_gin_at_forty_thousand_events():
    seed_event()
    with closing(connect_postgres_test()) as conn, conn.transaction():
        conn.execute(
            """INSERT INTO news_events(event_id,leader_item_id,dedupe_family,comparison_fingerprint,comparison_title,
                 leader_title,opened_at_ms,last_member_at_ms,expires_at_ms,admission,ingest_mode,
                 created_at_ms,updated_at_ms,focus_fact_id,focus_fact_method,event_kind,dedupe_bands,evidence_version,evidence)
               SELECT 'band:'||g,%s,'general','fp:'||g,'title','title',%s,%s,%s,'candidate','live',%s,%s,
                      'fact:'||g,'whole_item','news',ARRAY['0:key:'||g],1,'{}'::jsonb
                 FROM generate_series(1,40000) g""",
            (f"it-{EVENT}", STAMP, STAMP, STAMP + 86_400_000, STAMP, STAMP),
        )
        conn.execute("ANALYZE news_events")
        params = (["0:key:39876"], "general", STAMP + 1, "news", ["candidate"])
        plan = conn.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + BAND_CANDIDATES_SQL, params).fetchone()["QUERY PLAN"][0]

        def indexes(node):
            return {node.get("Index Name"), *(name for child in node.get("Plans", []) for name in indexes(child))}

        assert "news_events_dedupe_bands" in indexes(plan["Plan"])
        assert [row["event_id"] for row in conn.execute(BAND_CANDIDATES_SQL, params)] == ["band:39876"]
        assert "MATERIALIZED" in BAND_CANDIDATES_SQL
