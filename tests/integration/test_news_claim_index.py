"""Versioned retrieval facts and generation fencing on real PostgreSQL."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_update_pg import EVENT, STAMP, adopted_head, seed_event, sql, store
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.claim_recall import CALIBRATION, Probe, embed_text, text_sha, vector_bytes
from tracefold.news.storage.claim_index import ClaimIndexStorage
from tracefold.news.updates.contracts import content_revision_for

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_adoption_indexes_each_claim_version_and_vector_completion_does_not_change_reader_generation() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    claim = head.claims[0]
    rows = sql("SELECT * FROM news_claim_index WHERE event_id=%s", (EVENT,))
    assert len(rows) == 1 and rows[0]["claim_ref"] == claim.ref and rows[0]["text_sha256"] == text_sha(claim)
    assert rows[0]["embed_text"] == embed_text(claim) and rows[0]["vector"] is None
    generation = sql("SELECT revision FROM news_reader_clock")[0]["revision"]
    vector = vector_bytes([1.0, *([0.0] * 383)], CALIBRATION.embedder)
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
    assert asyncio.run(db.read("status", lambda r: r.news.claim_index.status())) == {
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
    vector = vector_bytes([1.0, *([0.0] * 383)], CALIBRATION.embedder)
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
    vector = vector_bytes([1.0, *([0.0] * 383)], CALIBRATION.embedder)
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
        return await db.read("frozen", lambda r: r.news.claim_index.claim_candidates((claim,), Probe(claim.statement)))

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
            assert ClaimIndexStorage(conn).status()["claim_index_pending"] == 1
            repos.conn.execute("DELETE FROM news_events WHERE event_id=%s", (EVENT,))
            assert ClaimIndexStorage(conn).status()["claim_index_pending"] == 0
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
