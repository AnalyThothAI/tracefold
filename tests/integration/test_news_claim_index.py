"""Versioned retrieval facts and generation fencing on real PostgreSQL."""

from __future__ import annotations

import asyncio
import json

import pytest

from scripts.news_recall_receipts import measure, read_facts
from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_update_pg import (
    EVENT,
    STAMP,
    StubAnalyzer,
    adopted_head,
    notify_plan,
    run_agent,
    seed_event,
    sql,
    store,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.claim_recall import CALIBRATION, Probe, embed_text, text_sha, vector_bytes
from tracefold.news.storage.claim_index import ClaimIndexStorage
from tracefold.news.storage.claim_recall import PgClaimRecall
from tracefold.news.updates.contracts import content_revision_for
from tracefold.news.updates.identity import digest
from tracefold.news.updates.service import NewsAgent

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_adoption_indexes_each_claim_version_and_vector_completion_does_not_change_reader_generation() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    claim = head.claims[0]
    rows = sql("SELECT * FROM news_claim_index WHERE event_id=%s", (EVENT,))
    assert len(rows) == 1 and rows[0]["claim_ref"] == claim.ref and rows[0]["text_sha256"] == text_sha(claim)
    assert rows[0]["embed_text"] == embed_text(claim) and rows[0]["vector"] is None
    generation = sql("SELECT revision FROM news_reader_clock")[0]["revision"]
    vector = vector_bytes([1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 1))], CALIBRATION.embedder)
    asyncio.run(
        db.tx(
            "vector",
            lambda r: r.news.claim_index.save_vectors(
                [(claim.ref, text_sha(claim), vector)],
                embedder=CALIBRATION.embedder.key,
            ),
        )
    )
    assert sql("SELECT revision FROM news_reader_clock")[0]["revision"] == generation
    assert asyncio.run(db.read("status", lambda r: r.news.claim_index.status(now_ms=clock.now_ms))) == {
        "recall_dense": "on",
        "claim_index_pending": 0,
    }
    old = claim.model_copy(update={"statement": "The earlier wording of the tariff."})
    asyncio.run(db.tx("old-version", lambda r: r.news.claim_index.index_claim(EVENT, old)))
    assert sql("SELECT count(*) AS n FROM news_claim_index")[0]["n"] == 2
    asyncio.run(db.tx("repeat", lambda r: r.news.claim_index.index_update(head)))
    assert sql("SELECT count(*) AS n FROM news_claim_index")[0]["n"] == 2


def test_prior_uses_only_the_current_exact_text_and_excludes_own_and_future_versions() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    claim = head.claims[0]
    seed_event("query-event", fingerprint="query")
    vector = vector_bytes([1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 1))], CALIBRATION.embedder)
    fake = claim.model_copy(update={"statement": "A different historical proposition."})
    asyncio.run(db.tx("old-version", lambda r: r.news.claim_index.index_claim(EVENT, fake)))
    asyncio.run(
        db.tx(
            "vector",
            lambda r: r.news.claim_index.save_vectors(
                [(c.ref, text_sha(c), vector) for c in (claim, fake)],
                embedder=CALIBRATION.embedder.key,
            ),
        )
    )
    probe = Probe(claim.statement, vector, CALIBRATION.embedder.key)

    def recall(event, stamp):
        return asyncio.run(db.read("prior", lambda r: r.news.claim_index.prior(event, probe, now_ms=stamp, sources=())))

    assert [p.claim for p in recall("query-event", clock.now_ms + 1)] == [claim]
    assert recall(EVENT, clock.now_ms + 1) == ()
    assert recall("query-event", STAMP) == ()


def test_frozen_receipt_candidate_keeps_its_version_when_a_head_or_index_changes() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    claim = head.claims[0]
    vector = vector_bytes([1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 1))], CALIBRATION.embedder)
    newer = claim.model_copy(update={"statement": "The tariff was cancelled."})

    async def run():
        await db.tx("new-version", lambda r: r.news.claim_index.index_claim(EVENT, newer))
        await db.tx(
            "vector",
            lambda r: r.news.claim_index.save_vectors(
                [(claim.ref, text_sha(claim), vector)],
                embedder=CALIBRATION.embedder.key,
            ),
        )
        return await db.read(
            "frozen", lambda r: r.news.claim_index.claim_candidates({f"{claim.ref}:{text_sha(claim)}": claim})
        )

    candidates = asyncio.run(run())
    assert len(candidates) == 1 and candidates[0].vector == vector
    assert candidates[0].key == f"{claim.ref}:{text_sha(claim)}"


def test_event_deletion_cascades_index_facts_without_leaving_pending_work() -> None:
    pg, _db, clock = store()
    adopted_head(pg.semantic, clock)
    conn = connect_postgres_test()
    try:
        with conn.transaction():
            repos = repositories_for_connection(conn)
            assert ClaimIndexStorage(conn).status(now_ms=clock.now_ms)["claim_index_pending"] == 1
            repos.conn.execute("DELETE FROM news_events WHERE event_id=%s", (EVENT,))
            assert ClaimIndexStorage(conn).status(now_ms=clock.now_ms)["claim_index_pending"] == 0
    finally:
        conn.close()


def test_backfill_finds_historical_text_versions_even_when_the_claim_ref_is_already_indexed() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    old_claim = head.claims[0].model_copy(update={"statement": "Agency announced an earlier tariff proposal."})
    old = head.model_copy(
        update={
            "claims": (old_claim,),
            "previous_content_revision": head.content_revision,
            "content_revision": content_revision_for(head.content_sha, head.content_revision),
        }
    )
    sql(
        """INSERT INTO news_analyses(analysis_id,event_id,origin,input_revision,completed_at_ms,work_id,
             input_sha256,program_identity,understanding,content_revision,update_ref,adopted_at_ms,document)
           VALUES ('historical',%s,'semantic',1,%s,'historical','historical','fixture','{}',%s,%s,%s,%s::jsonb)""",
        (
            EVENT,
            clock.now_ms - 1,
            old.content_revision,
            old.ref,
            clock.now_ms - 1,
            json.dumps(old.model_dump(mode="json")),
        ),
    )
    assert asyncio.run(db.tx("backfill", lambda r: r.news.claim_index.backfill(limit=64, now_ms=clock.now_ms))) == 1
    assert {row["text_sha256"] for row in sql("SELECT text_sha256 FROM news_claim_index")} == {
        text_sha(head.claims[0]),
        text_sha(old_claim),
    }
    assert asyncio.run(db.tx("backfill", lambda r: r.news.claim_index.backfill(limit=64, now_ms=clock.now_ms))) == 0


def seed_frozen_receipt(head, claim, *, settled_at_ms: int, intent: str = "frozen-version") -> None:
    sql(
        """INSERT INTO news_notifications(notification_id,intent_id,event_id,kind,origin,state,
             content_revision,claim_refs,plan_key,card,receipt,history_context,sent_claims,
             attempted_at_ms,settled_at_ms,created_at_ms,updated_at_ms)
           VALUES (%s,%s,%s,'update','legacy_delivery','sent',%s,%s::jsonb,false,%s::jsonb,'{}','{}',
                   %s::jsonb,%s,%s,%s,%s)""",
        (
            intent,
            intent,
            head.event_id,
            head.content_revision,
            json.dumps([claim.ref]),
            json.dumps(
                {"body": "Earlier delivered proposition", "payload_sha256": digest("Earlier delivered proposition")}
            ),
            json.dumps([claim.model_dump(mode="json")]),
            settled_at_ms,
            settled_at_ms,
            settled_at_ms,
            settled_at_ms,
        ),
    )


def test_sent_48h_prior_uses_the_frozen_exact_version_beyond_7d_and_excludes_expired_or_future_receipts() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    old = head.claims[0].model_copy(
        update={
            "statement": "Agency announced an earlier tariff proposal.",
            "first_available_at_ms": clock.now_ms - 45 * 86400_000,
        }
    )
    vector = vector_bytes([1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 1))], CALIBRATION.embedder)
    asyncio.run(db.tx("frozen-index", lambda r: r.news.claim_index.index_claim(EVENT, old)))
    asyncio.run(
        db.tx(
            "frozen-vector",
            lambda r: r.news.claim_index.save_vectors(
                [(old.ref, text_sha(old), vector)], embedder=CALIBRATION.embedder.key
            ),
        )
    )
    seed_frozen_receipt(head, old, settled_at_ms=clock.now_ms - 1)
    probe = Probe(old.statement, vector, CALIBRATION.embedder.key)

    def prior(stamp):
        return asyncio.run(
            db.read("sent-prior", lambda r: r.news.claim_index.prior("another-event", probe, now_ms=stamp, sources=()))
        )

    assert [p.claim for p in prior(clock.now_ms)] == [old]
    assert prior(clock.now_ms - 1) == ()
    # The receipt expires, but the Event's different current version still owns
    # the ordinary 7 d prior window. It must never resurrect the old sent text.
    assert all(p.claim != old for p in prior(clock.now_ms + 48 * 3600_000))
    assert (
        asyncio.run(db.read("own", lambda r: r.news.claim_index.prior(EVENT, probe, now_ms=clock.now_ms, sources=())))
        == ()
    )


def test_backfill_and_pending_prioritize_the_exact_sent_version_even_when_older_than_30d() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    current = head.claims[0]
    old = current.model_copy(
        update={
            "statement": "Agency previously proposed tariffs.",
            "first_available_at_ms": clock.now_ms - 45 * 86400_000,
        }
    )
    unreceived = old.model_copy(update={"statement": "A different unreceived wording."})
    asyncio.run(db.tx("unreceived", lambda r: r.news.claim_index.index_claim(EVENT, unreceived)))
    seed_frozen_receipt(head, old, settled_at_ms=clock.now_ms - 1)
    assert asyncio.run(db.tx("backfill-sent", lambda r: r.news.claim_index.backfill(limit=1, now_ms=clock.now_ms))) == 1
    assert asyncio.run(db.tx("repeat-sent", lambda r: r.news.claim_index.backfill(limit=1, now_ms=clock.now_ms))) == 0
    rows = asyncio.run(db.read("pending", lambda r: r.news.claim_index.pending(64, now_ms=clock.now_ms)))
    assert [(r["claim_ref"], r["text_sha256"]) for r in rows] == [
        (old.ref, text_sha(old)),
        (current.ref, text_sha(current)),
    ]
    assert (
        asyncio.run(db.read("status", lambda r: r.news.claim_index.status(now_ms=clock.now_ms)))["claim_index_pending"]
        == 2
    )
    expired = asyncio.run(
        db.read("expired", lambda r: r.news.claim_index.pending(64, now_ms=clock.now_ms + 48 * 3600_000))
    )
    assert [(r["claim_ref"], r["text_sha256"]) for r in expired] == [(current.ref, text_sha(current))]


def test_daily_sql_reads_frozen_diagnostics_from_semantic_observations_and_notification_decisions(monkeypatch) -> None:
    pg, db, clock = store()
    monkeypatch.setattr("tracefold.news.storage.claim_recall.clock_ms", lambda: clock.now_ms)
    seed_event()
    subject = NewsAgent(
        pg.semantic, StubAnalyzer(), program_identity="recall-observation", clock=clock, recall=PgClaimRecall(db)
    )
    assert asyncio.run(run_agent(subject, EVENT)) == "adopted"
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(snapshot.update, snapshot.reader.revision)
    assert (
        asyncio.run(pg.notifications.atomic_record_plan(plan, recall_diagnostics=snapshot.recall_diagnostics)).status
        == "committed"
    )
    conn = connect_postgres_test(read_only=True)
    try:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            facts = read_facts(conn, as_of_ms=clock.now_ms + 1)
    finally:
        conn.close()
    stats = measure(facts, as_of_ms=clock.now_ms + 1)["statistics"]
    assert stats["semantic_observations_with_recall"] == 1
    assert stats["prior_calls"] == stats["prior_degraded"] == 1
    assert stats["receipt_calls"] == stats["receipt_degraded"] == 1
    assert stats["prior_degraded_fraction"] == stats["receipt_degraded_fraction"] == 1.0
    assert stats["relation_pairs"] == 0 and stats["useful_relation_rate"] is None
