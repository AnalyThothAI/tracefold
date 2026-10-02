"""Root-fix concurrency contracts against independent PostgreSQL transactions."""

from __future__ import annotations

import asyncio
from threading import Event
from time import monotonic

import pytest

from tests.support.news_update_admission import work
from tests.support.news_update_pg import EVENT, STAMP, Clock, ThreadedDb, seed_event, sql
from tracefold.news.storage.evidence import EvidenceStorage
from tracefold.news.storage.semantic_store import PgSemanticStore

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_slow_input_does_not_hold_admission_and_revision_cas_spends_no_attempt(monkeypatch):
    seed_event()
    seed_event("ev-incoming", fingerprint="incoming")
    started = Event()
    original = EvidenceStorage.evidence_candidates

    def slow(self, query):
        assert self.conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        started.set()
        self.conn.execute("SELECT pg_sleep(1)")
        return original(self, query)

    monkeypatch.setattr(EvidenceStorage, "evidence_candidates", slow)
    db = ThreadedDb()
    store = PgSemanticStore(db, clock=Clock(STAMP + 10))

    async def race():
        claim = asyncio.create_task(store.claim_semantic_work(EVENT, lease_ms=180_000))
        assert await asyncio.to_thread(started.wait, 3)

        def admit(repos):
            repos.conn.execute("SET LOCAL lock_timeout='250ms'")
            assert repos.news.add_member(
                event_id=EVENT,
                item_id="it-ev-incoming",
                joined_at_ms=STAMP + 11,
                match_kind="near",
                jaccard_estimate=0.9,
                provider_score=90,
                fact_id="incoming",
                fact_text="New evidence",
                now_ms=STAMP + 11,
            )
            repos.news.semantic_work.request_semantic_revision(event_id=EVENT, lineage_id="same", now_ms=STAMP + 11)

        begin = monotonic()
        await asyncio.wait_for(db.tx("admission_during_input", admit), 0.25)
        assert monotonic() - begin < 0.25
        assert await claim is None

    asyncio.run(race())
    assert work(EVENT)["wanted_revision"] == 2
    assert work(EVENT)["attempts"] == 0


def test_two_claimants_have_one_owner_for_fifty_reopened_revisions():
    seed_event()
    clock = Clock(STAMP + 10)
    db = ThreadedDb()
    stores = [PgSemanticStore(db, clock=clock), PgSemanticStore(db, clock=clock)]

    async def race():
        for revision in range(1, 51):
            leases = await asyncio.gather(*(store.claim_semantic_work(EVENT, lease_ms=1_000) for store in stores))
            winners = [lease for lease in leases if lease is not None]
            assert len(winners) == 1 and winners[0].attempts == 1
            assert work(EVENT)["wanted_revision"] == revision
            clock.now_ms += 1_001
            if revision < 50:
                await db.tx(
                    "reopen",
                    lambda r: r.news.semantic_work.request_semantic_revision(
                        event_id=EVENT, lineage_id="same", now_ms=clock.now_ms
                    ),
                )

    asyncio.run(race())


def test_collector_noop_and_fifty_normal_frames_preserve_the_row_version():
    db = ThreadedDb()

    def frame(r, stamp):
        return r.news.record_published_frame(now_ms=stamp)

    asyncio.run(db.tx("first_frame", lambda r: frame(r, STAMP)))
    before = sql("SELECT xmin::text AS version,state FROM news_collectors WHERE collector_id='opennews'")[0]
    for frame_no in range(1, 51):
        assert asyncio.run(db.tx("frame", lambda r, n=frame_no: frame(r, STAMP + n * 50))) == 0
    assert sql("SELECT xmin::text AS version,state FROM news_collectors WHERE collector_id='opennews'")[0] == before
    asyncio.run(db.tx("later_frame", lambda r: frame(r, STAMP + 5_000)))
    assert (
        sql("SELECT xmin::text AS version FROM news_collectors WHERE collector_id='opennews'")[0]["version"]
        != before["version"]
    )


def test_semantic_orphan_sweep_skips_a_live_event_and_locked_owner():
    from contextlib import closing

    from tests.postgres_test_utils import connect_postgres_test
    from tracefold.app.repository_session import repositories_for_connection

    seed_event()
    sql(
        "INSERT INTO news_jobs(job_kind,subject_id,state,created_at_ms,updated_at_ms) "
        "VALUES ('semantic','orphan-free','pending',0,0),('semantic','orphan-held','pending',0,0)"
    )
    with closing(connect_postgres_test()) as held, held.transaction():
        held.execute("SELECT 1 FROM news_jobs WHERE subject_id='orphan-held' FOR UPDATE")
        with closing(connect_postgres_test()) as sweep, sweep.transaction():
            assert repositories_for_connection(sweep).news.sweep_orphan_jobs(limit=1) == 1
        assert {row["subject_id"] for row in sql("SELECT subject_id FROM news_jobs")} == {EVENT, "orphan-held"}
    with closing(connect_postgres_test()) as sweep, sweep.transaction():
        assert repositories_for_connection(sweep).news.sweep_orphan_jobs(limit=1) == 1
        assert repositories_for_connection(sweep).news.sweep_orphan_jobs(limit=1) == 0


def test_first_successful_frame_closes_broker_incident_without_waiting_five_seconds():
    db = ThreadedDb()
    asyncio.run(db.tx("first", lambda r: r.news.record_published_frame(now_ms=STAMP)))
    asyncio.run(db.tx("failure", lambda r: r.news.open_incident(cause_class="broker_unavailable", now_ms=STAMP + 1)))
    assert asyncio.run(db.tx("recovery", lambda r: r.news.record_published_frame(now_ms=STAMP + 2))) == 1
    row = sql("SELECT state,incidents FROM news_collectors WHERE collector_id='opennews'")[0]
    assert row["state"]["last_publish_at_ms"] == STAMP + 2
    assert row["incidents"][0]["closed_at_ms"] == STAMP + 2
    assert row["incidents"][0]["recovery_status"] == "pending"


def test_reader_generation_rolls_back_with_its_fact_and_ignores_noop_metadata():
    from contextlib import closing

    from tests.postgres_test_utils import connect_postgres_test

    seed_event()
    before = sql("SELECT revision FROM news_reader_clock")[0]["revision"]
    sql("UPDATE news_items SET provider_metadata=provider_metadata WHERE item_id=%s", (f"it-{EVENT}",))
    assert sql("SELECT revision FROM news_reader_clock")[0]["revision"] == before
    with (
        closing(connect_postgres_test()) as conn,
        pytest.raises(RuntimeError, match="rollback_fact"),
        conn.transaction(),
    ):
        conn.execute(
            "UPDATE news_items SET provider_metadata='{\"reader_test\":true}' WHERE item_id=%s",
            (f"it-{EVENT}",),
        )
        assert conn.execute("SELECT revision FROM news_reader_clock").fetchone()["revision"] == before + 1
        raise RuntimeError("rollback_fact")
    assert sql("SELECT revision FROM news_reader_clock")[0]["revision"] == before
    assert (
        sql("SELECT provider_metadata FROM news_items WHERE item_id=%s", (f"it-{EVENT}",))[0]["provider_metadata"] == {}
    )


def test_leader_swap_preserves_native_job_detail_bytes():
    seed_event()
    seed_event("ev-other", fingerprint="other")
    before = sql("SELECT job_kind,subject_id,detail::text FROM news_jobs ORDER BY job_kind,subject_id")
    sql("UPDATE news_events SET leader_item_id='it-ev-other' WHERE event_id=%s", (EVENT,))
    assert sql("SELECT job_kind,subject_id,detail::text FROM news_jobs ORDER BY job_kind,subject_id") == before
