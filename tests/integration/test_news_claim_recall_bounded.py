"""Exact version filtering, multi-claim transaction bounds and resumable bulk projection."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tests.support.news_extraction_809 import extraction_case
from tests.support.news_reader import FixedReader
from tests.support.news_update_pg import (
    EVENT,
    adopt_next,
    adopted_head,
    agent,
    extraction_for,
    run_agent,
    seed_event,
    sql,
    store,
)
from tests.support.news_update_semantic import MemoryCache
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.wiring.database import WorkerNewsDatabase
from tracefold.news.claim_recall import CALIBRATION, Probe, lexical_text, text_sha, vector_bytes
from tracefold.news.notifications.contracts import CardCopy, CardLine
from tracefold.news.notifications.planner import NotificationPlanner
from tracefold.news.notifications.service import Notifications
from tracefold.news.storage.claim_index import ClaimIndexStorage
from tracefold.news.storage.claim_recall import PgClaimRecall
from tracefold.news.updates.contracts import FrozenInput, content_revision_for
from tracefold.news.updates.identity import digest
from tracefold.news.updates.judgment import Budget
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.client import create_pool

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def _receipt_809(head, claim, body, clock):
    """Synthetic receipt with explicitly frozen wording, never a cleaned production receipt."""
    intent = f"receipt-{head.event_id}"
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
            json.dumps({"body": body, "payload_sha256": digest(body)}),
            json.dumps([claim.model_dump(mode="json")]),
            *([clock.now_ms - 1] * 4),
        ),
    )
    return intent


@pytest.mark.parametrize("case_id", ["R1", "R2"])
@pytest.mark.usefixtures("synthetic_reader_calibration")
def test_809_complete_query_selects_exact_old_receipt_before_duplicate_policy(case_id):
    pg, db, clock = store()
    source, extracted = extraction_case(case_id)
    seed_event(source.event_id, text=source.evidence[0].text, fingerprint=case_id)
    adopted, head = asyncio.run(adopt_next(pg.semantic, None, source, extracted, work_id=case_id))
    assert adopted
    frozen = head.claims[0].model_copy(update={"ref": f"original-{case_id}"})
    history = head.model_copy(update={"event_id": f"history-{case_id}"})
    seed_event(history.event_id, fingerprint=f"history-{case_id}")
    intent = _receipt_809(history, frozen, frozen.statement, clock)
    # The current index also has a different wording under the same ref. Only the
    # frozen exact version has the matching vector; controlled vectors prove SQL
    # and selection seams, not actual model similarity or business certification.
    newer = frozen.model_copy(update={"statement": "The previous measure was cancelled."})
    matching = vector_bytes([1.0, *([0.0] * 383)], CALIBRATION.embedder)
    other = vector_bytes([0.0, 1.0, *([0.0] * 382)], CALIBRATION.embedder)

    async def run():
        await db.tx(
            "809-versions",
            lambda r: [
                r.news.claim_index.index_claim(
                    history.event_id, frozen, probe=Probe(frozen.statement, matching, CALIBRATION.embedder.key)
                ),
                r.news.claim_index.index_claim(
                    history.event_id, newer, probe=Probe(newer.statement, other, CALIBRATION.embedder.key)
                ),
                r.news.claim_index.index_claim(
                    head.event_id,
                    head.claims[0],
                    probe=Probe(head.claims[0].statement, matching, CALIBRATION.embedder.key),
                ),
            ],
        )
        snapshot = await pg.notifications.notification_snapshot(head.event_id, "news")
        assert snapshot is not None
        assert snapshot.reader.receipt_intents_by_claim[head.claims[0].ref] == (intent,)
        judge = FixedReader(0, anchor="m1")
        plan = await NotificationPlanner(judge, MemoryCache()).plan(
            head, snapshot.reader, Budget.start(5), now_ms=clock()
        )
        assert judge.asked[0].messages == (frozen.statement,)
        assert plan.selected_claim_refs == () and plan.action == "no_notification"
        assert plan.claim_decisions[0].reason == "reader_feed"
        return await db.read(
            "809-original",
            lambda r: r.conn.execute(
                "SELECT sent_claims,card FROM news_notifications WHERE intent_id=%s", (intent,)
            ).fetchone(),
        )

    saved = asyncio.run(run())
    assert saved["sent_claims"][0]["statement"] == frozen.statement
    assert saved["card"]["body"] == frozen.statement


@pytest.mark.usefixtures("synthetic_reader_calibration")
def test_809_mixed_duplicate_and_increment_keeps_only_new_claim_in_frozen_card():
    pg, db, clock = store()
    first_source, first = extraction_case("R1")
    second_source, second = extraction_case("R2")
    source = first_source.model_copy(update={"evidence": first_source.evidence + second_source.evidence})
    extracted = first.model_copy(update={"claims": first.claims + second.claims})
    seed_event(source.event_id, fingerprint="mixed-809")
    adopted, head = asyncio.run(adopt_next(pg.semantic, None, source, extracted, work_id="mixed-809"))
    assert adopted
    history = head.model_copy(update={"event_id": "history-mixed-809"})
    seed_event(history.event_id, fingerprint=history.event_id)
    frozen = head.claims[0].model_copy(update={"ref": "old-809"})
    _receipt_809(history, frozen, frozen.statement, clock)
    vector = vector_bytes([1.0, *([0.0] * 383)], CALIBRATION.embedder)

    class Reader:
        identity = "synthetic-809-mixed"

        async def judge(self, reader, budget):
            return await FixedReader(0 if reader.claim.statement == frozen.statement else 3).judge(reader, budget)

    class Copy:
        identity = "synthetic-809-copy"

        async def compose(self, claims, **kwargs):
            return CardCopy(
                headline_zh="Blast 用户提现安排",
                lines=tuple(
                    CardLine(
                        claim_ref=c.ref,
                        text_zh="Blast 用户须于十月二十六日前将资产提现至以太坊主网，延迟缩短为二十四小时",
                    )
                    for c in claims
                ),
            )

    async def run():
        await db.tx(
            "809-vectors",
            lambda r: [
                r.news.claim_index.index_claim(
                    event, claim, probe=Probe(claim.statement, vector, CALIBRATION.embedder.key)
                )
                for event, claim in [(history.event_id, frozen), *((head.event_id, c) for c in head.claims)]
            ],
        )
        return await Notifications(
            pg.notifications, NotificationPlanner(Reader(), MemoryCache()), Copy(), clock=clock
        ).prepare(head.event_id, "news")

    turn = asyncio.run(run())
    assert turn.status == "ready" and turn.card is not None, turn.error_code
    assert turn.card.claim_refs == (head.claims[1].ref,)
    assert first.claims[0].statement not in turn.card.body
    assert "Blast" in turn.card.body and "提现" in turn.card.body


def test_degraded_fts_filters_stale_versions_before_route_budget(monkeypatch) -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    claim = head.claims[0]
    stale = [claim.model_copy(update={"statement": f"{claim.statement} Prior wording {i}."}) for i in range(70)]
    asyncio.run(db.tx("stale", lambda r: [r.news.claim_index.index_claim(EVENT, row) for row in stale]))

    def no_complete_lexical_scan(*_args, **_kwargs):
        raise AssertionError("degraded recall must filter the SQL FTS route before complete head reads")

    monkeypatch.setattr(ClaimIndexStorage, "lexical_scores", no_complete_lexical_scan)
    diagnostics = {}
    result = asyncio.run(
        db.read(
            "prior",
            lambda r: r.news.claim_index.prior(
                "query",
                Probe(claim.statement),
                lexical_query=lexical_text(claim),
                now_ms=clock.now_ms + 1,
                sources=(),
                diagnostics=diagnostics,
            ),
            repeatable_read=True,
        )
    )
    assert [row.claim for row in result] == [claim]
    assert diagnostics["degraded"] and diagnostics["candidate_count"] == 1


@pytest.mark.parametrize("vectors", ["missing", "partial"])
@pytest.mark.parametrize("claim_count", [4, 11, 12])
def test_many_claims_do_not_accumulate_in_one_native_worker_transaction(
    monkeypatch, vectors, claim_count, record_property
) -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    seed_event("null-prior", fingerprint="null-prior")
    assert asyncio.run(run_agent(agent(pg.semantic, clock), "null-prior")) == "adopted"
    encoded = vector_bytes([1.0, *([0.0] * 383)], CALIBRATION.embedder)
    if vectors == "partial":
        asyncio.run(
            db.tx(
                "vector",
                lambda r: r.news.claim_index.index_claim(
                    EVENT, head.claims[0], probe=Probe(head.claims[0].statement, encoded, CALIBRATION.embedder.key)
                ),
            )
        )

    class Embedding:
        identity = CALIBRATION.embedder

        async def probes(self, texts):
            return tuple(Probe(text, encoded, self.identity.key) for text in texts)

    source = FrozenInput(event_id="query", revision=1, lineage_id="many-claims", evidence=head.evidence)
    extraction = extraction_for(source)
    extraction = extraction.model_copy(
        update={"claims": tuple(extraction.claims[0].model_copy(update={"slot": f"a{i}"}) for i in range(claim_count))}
    )
    monkeypatch.setattr("tracefold.news.storage.claim_recall.clock_ms", lambda: clock.now_ms + 1)
    original = ClaimIndexStorage.prior
    pools = []

    def delayed_prior(self, *args, **kwargs):
        # Every statement is below its 3s deadline. Twelve comparisons in the
        # previous shared session exceeded PostgreSQL's complete 8s deadline.
        self.conn.execute("SELECT pg_sleep(0.7)")
        pools.append(id(kwargs["pool"]))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ClaimIndexStorage, "prior", delayed_prior)
    pool = create_pool(
        _test_postgres_dsn(),
        min_size=1,
        max_size=4,
        max_waiting=3,
        connect_timeout_seconds=5.0,
        application_name="tracefold_many_claim_recall_test",
        statement_timeout_seconds=3.0,
        lock_timeout_seconds=0.25,
        idle_in_transaction_session_timeout_seconds=5.0,
    )
    pool.wait(timeout=5.0)
    worker = WorkerDatabase(worker_pool=pool, telemetry=TelemetryRegistry())
    try:
        started = time.perf_counter()
        result = asyncio.run(PgClaimRecall(WorkerNewsDatabase(worker), embedder=Embedding()).priors(source, extraction))
        elapsed = time.perf_counter() - started
        assert len(result.by_slot) == claim_count and all(result.by_slot.values())
        assert all(diagnostic["degraded"] for diagnostic in result.diagnostics.values())
        assert len(pools) == claim_count and len(set(pools)) == 1
        metric = {
            "claims": claim_count,
            "vectors": vectors,
            "elapsed_seconds": elapsed,
            "native_transaction_timeout_seconds": 8,
            "minimum_comparison_seconds": claim_count * 0.7,
        }
        record_property("multi_claim_native_transactions", json.dumps(metric, sort_keys=True))
        print(f"multi_claim_native_transactions: {json.dumps(metric, sort_keys=True)}")
    finally:
        worker.close_executors()
        pool.close()


def _historical_many_claims(head, clock, count):
    claims = tuple(
        head.claims[0].model_copy(
            update={"ref": f"historical-claim-{i:03}", "statement": f"Earlier tariff proposition {i}."}
        )
        for i in range(count)
    )
    document = head.model_copy(update={"claims": claims, "changes": (), "evidence_relations": ()})
    sha = digest(document.content_material())
    document = document.model_copy(
        update={"content_sha": sha, "content_revision": content_revision_for(sha, document.previous_content_revision)}
    )
    sql(
        """INSERT INTO news_analyses(analysis_id,event_id,origin,input_revision,completed_at_ms,work_id,
             input_sha256,program_identity,understanding,content_revision,update_ref,adopted_at_ms,document)
           VALUES ('historical-many',%s,'semantic',1,%s,'historical-many','historical-many','fixture','{}',
                   %s,%s,%s,%s::jsonb)""",
        (
            EVENT,
            clock.now_ms - 1,
            document.content_revision,
            document.ref,
            clock.now_ms - 1,
            json.dumps(document.model_dump(mode="json")),
        ),
    )
    return claims


def test_bulk_backfill_resumes_last_committed_claim_cursor_after_cancellation(monkeypatch) -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    claims = _historical_many_claims(head, clock, 65)
    encoded = vector_bytes([1.0, *([0.0] * 383)], CALIBRATION.embedder)
    checkpoints = []
    texts = []

    class Embedding:
        identity = CALIBRATION.embedder
        calls = 0
        cancel = True

        async def probes(self, batch):
            self.calls += 1
            if self.calls == 2 and self.cancel:
                raise asyncio.CancelledError
            texts.extend(batch)
            return tuple(Probe(text, encoded, self.identity.key) for text in batch)

    async def checkpoint(state):
        checkpoints.append(state)

    embedder = Embedding()
    recall = PgClaimRecall(db, embedder=embedder)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(recall.bulk_backfill(batch_size=32, now_ms=clock.now_ms + 1, checkpoint=checkpoint))
    committed = checkpoints[-1]
    assert committed["phase"] == "adopted" and committed["after"][2] == 32
    assert committed["projected"] == committed["embedded"] == 32
    live = head.claims[0].model_copy(update={"ref": "live-during-bulk", "statement": "A live tariff update."})
    asyncio.run(
        db.tx(
            "live",
            lambda r: r.news.claim_index.index_claim(
                EVENT, live, probe=Probe(live.statement, encoded, CALIBRATION.embedder.key)
            ),
        )
    )
    embedder.cancel = False
    report = asyncio.run(recall.bulk_backfill(batch_size=32, resume=committed, checkpoint=checkpoint))
    assert report["phase"] == "done" and report["as_of_ms"] == clock.now_ms + 1
    assert report["projected"] == report["embedded"] == 66
    assert len(texts) == len(set(texts)) == 66
    rows = sql("SELECT claim_ref,vector FROM news_claim_index")
    assert len(rows) == 67 and all(bytes(row["vector"]) == encoded for row in rows)
    assert sql("SELECT vector FROM news_claim_index WHERE claim_ref=%s AND text_sha256=%s", (live.ref, text_sha(live)))
    assert {row["claim_ref"] for row in rows} >= {claim.ref for claim in claims}
    assert report["database_seconds"] > 0 and report["elapsed_seconds"] >= report["database_seconds"]


@pytest.mark.parametrize("resume", [{"phase": "bogus"}, {"after": [0, "x"]}, {"as_of_ms": -1}])
def test_bulk_backfill_rejects_invalid_checkpoint(resume) -> None:
    class Embedding:
        identity = CALIBRATION.embedder

    with pytest.raises(ValueError, match="news_claim_backfill_checkpoint_invalid"):
        asyncio.run(PgClaimRecall(object(), embedder=Embedding()).bulk_backfill(resume=resume))


def test_sent_then_adopted_projection_recovers_sources_and_preserves_ready_vectors() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    claim = head.claims[0]
    encoded = vector_bytes([1.0, *([0.0] * 383)], CALIBRATION.embedder)
    sql("DELETE FROM news_claim_index")
    # A frozen receipt can project a missing version without source evidence.
    asyncio.run(
        db.tx(
            "frozen",
            lambda r: r.news.claim_index.index_claim(
                EVENT, claim, probe=Probe(claim.statement, encoded, CALIBRATION.embedder.key)
            ),
        )
    )
    row = sql("SELECT xmin::text AS xmin,structure_keys,vector FROM news_claim_index")[0]
    adopted = asyncio.run(
        db.read(
            "adopted",
            lambda r: r.news.claim_index.historical_batch(
                phase="adopted", after=[0, "", 0], limit=32, now_ms=clock.now_ms + 1
            ),
        )
    )
    asyncio.run(db.tx("sources", lambda r: r.news.claim_index.project_batch(adopted)))
    enriched = sql("SELECT xmin::text AS xmin,structure_keys,vector FROM news_claim_index")[0]
    assert set(enriched["structure_keys"]) > set(row["structure_keys"])
    assert any(key.startswith("artifact:") for key in enriched["structure_keys"])
    assert bytes(enriched["vector"]) == encoded
    asyncio.run(db.tx("repeat", lambda r: r.news.claim_index.project_batch(adopted)))
    assert sql("SELECT xmin::text AS xmin FROM news_claim_index")[0]["xmin"] == enriched["xmin"]
