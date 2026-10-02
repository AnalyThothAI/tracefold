"""Production-sized recall over real PostgreSQL, with no external model calls.

The first invocation is reported separately from 40 subsequent invocations. This
does not claim a cold disk cache: seeding and ANALYZE necessarily touch the pages.
Only adapter reads are timed; fact construction and embedding are excluded.
Latency budgets are reported for observation; correctness and transaction
boundaries remain assertions. The owner removed latency as a release gate.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from typing import Any

import httpx
import numpy as np
import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_update_pg import EVENT, TEXT, Clock, adopted_head, extraction_for, store
from tracefold.app.claim_embedding import EMBEDDING_SECONDS, ClaimEmbedder
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.claim_recall import CALIBRATION, PRIOR_WINDOW_MS, RECEIPT_WINDOW_MS, Probe, vector_bytes
from tracefold.news.notifications.contracts import NEWS_CHANNEL
from tracefold.news.storage.claim_recall import PgClaimRecall
from tracefold.news.storage.notification_store import PgNotificationStore
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import EventUpdate, Evidence, FrozenInput, Source
from tracefold.news.updates.identity import digest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

CURRENT_CLAIMS = 17_500
SENT_CLAIMS = 2_000
TRIALS = 40


class MeasuredDb:
    """A real read-only transaction per adapter read, including connection overhead."""

    def __init__(self) -> None:
        self.active = False
        self.timings: dict[str, list[float]] = {}

    async def read(
        self,
        name: str,
        fn: Callable[[Any], Any],
        *,
        timeout_seconds: float = 3.0,
        repeatable_read: bool = False,
    ) -> Any:
        started = time.perf_counter()
        result = await asyncio.to_thread(self._read, fn, timeout_seconds, repeatable_read)
        self.timings.setdefault(name, []).append((time.perf_counter() - started) * 1000)
        return result

    def _read(self, fn: Callable[[Any], Any], timeout: float, repeatable: bool) -> Any:
        with closing(connect_postgres_test(read_only=True)) as conn, conn.transaction():
            if repeatable:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SELECT set_config('statement_timeout',%s,true)", (str(int(timeout * 1000)),))
            assert not self.active
            self.active = True
            try:
                return fn(repositories_for_connection(conn))
            finally:
                self.active = False


class PrecomputedEmbedding:
    """Encoding is not part of retrieval latency, but its transaction boundary is."""

    identity = CALIBRATION.embedder

    def __init__(self, db: MeasuredDb, vector: bytes) -> None:
        self.db = db
        self.vector = vector
        self.calls = 0

    async def probes(self, texts: Sequence[str]) -> tuple[Probe, ...]:
        assert not self.db.active, "embedding must run after the previous transaction ends"
        self.calls += 1
        await asyncio.sleep(0)
        assert not self.db.active
        return tuple(Probe(text, self.vector, self.identity.key) for text in texts)


def _seed_windows(head: EventUpdate, now_ms: int, vector: bytes) -> None:
    """Bulk-store validated adopted versions and exact frozen receipts at full scale."""
    rng = np.random.default_rng(791)
    vectors = rng.normal(size=(CURRENT_CLAIMS + SENT_CLAIMS, CALIBRATION.embedder.dimensions)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    encoded = vectors.astype("<f2")
    with closing(connect_postgres_test()) as conn, conn.transaction():
        conn.execute(
            """CREATE TEMP TABLE recall_seed (
                 ordinal integer, event_id text, item_id text, analysis_id text,
                 statement text, text_sha text, available bigint, document jsonb,
                 extraction jsonb, claim_ref text, content_revision text, update_ref text,
                 vector bytea, sent boolean) ON COMMIT DROP"""
        )
        with conn.cursor().copy("COPY recall_seed FROM STDIN") as copy:
            for ordinal in range(CURRENT_CLAIMS + SENT_CLAIMS):
                sent = ordinal >= CURRENT_CLAIMS
                event_id = f"latency-event-{ordinal:05}"
                item_id = f"it-{event_id}"
                # The frozen reserve is deliberately outside the ordinary 7 d
                # window; receipt recall must still inspect all 2,000 versions.
                available = now_ms - (PRIOR_WINDOW_MS + 86_400_000 if sent else 2 + ordinal * 30_000)
                text = (
                    TEXT
                    if ordinal % 127 == 0
                    else (
                        f"Agency {ordinal} orders a {ordinal % 20 + 1}% tariff on industrial imports "
                        f"effective October {ordinal % 28 + 1}."
                    )
                )
                source = FrozenInput(
                    event_id=event_id,
                    revision=1,
                    lineage_id=event_id,
                    evidence=(
                        Evidence.issue(
                            text,
                            Source(
                                publisher_id="opennews",
                                artifact_id=item_id,
                                artifact_revision=digest(text),
                                record_id=item_id,
                                first_available_at_ms=available,
                                published_at_ms=available,
                                url=f"https://www.reuters.com/{item_id}",
                            ),
                        ),
                    ),
                )
                extracted = extraction_for(source)
                update = assemble_update(source, extracted, None, adopted_at_ms=available + 1)
                assert update is not None
                # Validate identities and cross-references before materializing
                # SQL facts; this is not a ranker-only synthetic benchmark.
                EventUpdate.model_validate(update.model_dump())
                claim = update.claims[0]
                copy.write_row(
                    (
                        ordinal,
                        event_id,
                        item_id,
                        f"analysis-{event_id}",
                        text,
                        digest(text),
                        available,
                        update.model_dump_json(),
                        extracted.model_dump_json(),
                        claim.ref,
                        update.content_revision,
                        update.ref,
                        vector if ordinal % 127 == 0 else encoded[ordinal].tobytes(),
                        sent,
                    )
                )
        conn.execute(
            """INSERT INTO news_items
               SELECT (jsonb_populate_record(NULL::news_items,to_jsonb(i)||jsonb_build_object(
                 'item_id',s.item_id,'source_item_key',s.item_id,'source_artifact_id',s.item_id,
                 'title',s.statement,'raw_first_line',s.statement,'description','',
                 'evidence_text',s.statement,'evidence_text_sha256',s.text_sha,
                 'canonical_url','https://www.reuters.com/'||s.item_id,
                 'published_at_ms',s.available,'observed_at_ms',s.available,
                 'created_at_ms',s.available,'updated_at_ms',s.available))).*
                 FROM recall_seed s CROSS JOIN news_items i WHERE i.item_id=%s""",
            (head.evidence[0].source.record_id,),
        )
        conn.execute(
            """INSERT INTO news_events(event_id,leader_item_id,dedupe_family,comparison_fingerprint,
                 comparison_title,leader_title,opened_at_ms,last_member_at_ms,expires_at_ms,admission,
                 ingest_mode,trace_id,created_at_ms,updated_at_ms,focus_fact_id,focus_fact_text,
                 focus_fact_context,focus_fact_method,focus_span_start,focus_span_end,event_kind)
               SELECT event_id,item_id,'general',event_id,statement,statement,available,available,
                      available+86400000,'candidate','live','latency-fixture',available,available,
                      item_id,statement,'','whole_item',0,length(statement),'news' FROM recall_seed"""
        )
        conn.execute(
            """INSERT INTO news_event_members(event_id,item_id,joined_at_ms,match_kind,fact_id,fact_text)
               SELECT event_id,item_id,available,'leader',item_id,statement FROM recall_seed"""
        )
        conn.execute(
            """INSERT INTO news_analyses(analysis_id,event_id,origin,input_revision,completed_at_ms,
                 work_id,input_sha256,program_identity,understanding,content_revision,update_ref,
                 adopted_at_ms,document)
               SELECT analysis_id,event_id,'semantic',1,available+1,analysis_id,text_sha,
                      'production-scale-fixture',extraction,content_revision,update_ref,available+1,document
                 FROM recall_seed"""
        )
        conn.execute(
            """UPDATE news_events e SET current_analysis_id=s.analysis_id
                 FROM recall_seed s WHERE e.event_id=s.event_id"""
        )
        conn.execute(
            """INSERT INTO news_claim_index(claim_ref,text_sha256,event_id,first_available_at_ms,
                 embed_text,numbers,structure_keys,embedder,vector)
               SELECT claim_ref,text_sha,event_id,available,statement,'{}','{}',%s,vector FROM recall_seed""",
            (CALIBRATION.embedder.key,),
        )
        conn.execute(
            """INSERT INTO news_notifications(notification_id,intent_id,event_id,kind,origin,state,
                 update_ref,input_digest,input_snapshot,plan,decided_at_ms,content_revision,claim_refs,
                 plan_key,card,receipt,history_context,sent_claims,settled_at_ms,created_at_ms,updated_at_ms)
               SELECT 'receipt-'||event_id,'receipt-'||event_id,event_id,'update','reader_v2','sent',
                      update_ref,text_sha,'{}','{}',%s,content_revision,jsonb_build_array(claim_ref),false,
                      jsonb_build_object('body',statement,'payload_sha256',text_sha),
                      jsonb_build_object('message_id',ordinal),'{}',document->'claims',
                      %s - (ordinal-%s+1)*60000,%s,%s
                 FROM recall_seed WHERE sent""",
            (now_ms - RECEIPT_WINDOW_MS, now_ms, CURRENT_CLAIMS, now_ms - RECEIPT_WINDOW_MS, now_ms),
        )
        for relation in ("news_items", "news_events", "news_analyses", "news_claim_index", "news_notifications"):
            conn.execute(f"ANALYZE {relation}")
        assert conn.execute("SELECT count(*) AS n FROM news_claim_index WHERE vector IS NOT NULL").fetchone()["n"] == (
            CURRENT_CLAIMS + SENT_CLAIMS
        )
        receipts = conn.execute("SELECT count(*) AS n FROM news_notifications WHERE state='sent'").fetchone()["n"]
        assert receipts == SENT_CLAIMS


def _report(name: str, values: list[float], record_property: Any) -> float:
    warm = sorted(values[1:])
    assert len(warm) == TRIALS
    p95 = warm[math.ceil(len(warm) * 0.95) - 1]
    result = {"first_ms": values[0], "warm_p95_ms": p95, "warm_max_ms": warm[-1], "trials": len(warm)}
    record_property(name, json.dumps(result, sort_keys=True))
    print(f"{name}: {json.dumps(result, sort_keys=True)}")
    return p95


@pytest.mark.slow
def test_production_scale_prior_and_receipt_transactions_report_p95(monkeypatch, record_property) -> None:
    pg, _, clock = store()
    head = adopted_head(pg.semantic, clock)
    now_ms = clock.now_ms + 1
    monkeypatch.setattr("tracefold.news.storage.claim_recall.clock_ms", lambda: now_ms)
    vector = vector_bytes([1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 1))], CALIBRATION.embedder)
    _seed_windows(head, now_ms, vector)
    source = FrozenInput(event_id=EVENT, revision=1, lineage_id="latency-query", evidence=head.evidence)
    extracted = extraction_for(source)
    db = MeasuredDb()
    embedder = PrecomputedEmbedding(db, vector)
    prior = PgClaimRecall(db, embedder=embedder)  # type: ignore[arg-type]
    receipt = PgNotificationStore(db, clock=Clock(now_ms), embedder=embedder)  # type: ignore[arg-type]

    async def measure() -> None:
        for _ in range(TRIALS + 1):
            batch = await prior.priors(source, extracted)
            candidates = batch.by_slot
            assert batch.diagnostics["a"]["candidate_count"] == CURRENT_CLAIMS + SENT_CLAIMS
            assert batch.diagnostics["a"]["degraded"] is False
            assert candidates["a"] and len(candidates["a"]) <= CALIBRATION.prior.k
            assert any(
                candidate.claim.first_available_at_ms < now_ms - PRIOR_WINDOW_MS for candidate in candidates["a"]
            )
            snapshot = await receipt.notification_snapshot(EVENT, NEWS_CHANNEL)
            assert snapshot is not None
            assert snapshot.recall_diagnostics[head.claims[0].ref]["candidate_count"] == SENT_CLAIMS
            assert snapshot.recall_diagnostics[head.claims[0].ref]["degraded"] is False
            assert snapshot.reader.receipts and len(snapshot.reader.receipts) <= CALIBRATION.receipt.k

    asyncio.run(measure())
    assert embedder.calls == 2 * (TRIALS + 1)
    prior_p95 = _report("prior_transaction", db.timings["news_claim_prior_recall"], record_property)
    receipt_p95 = _report("receipt_transaction", db.timings["news_update_notification_snapshot"], record_property)
    record_property("prior_advisory_budget_met", prior_p95 <= 200.0)
    record_property("receipt_advisory_budget_met", receipt_p95 <= 80.0)


def test_real_embedding_adapter_timeout_is_bounded_before_the_recall_read(monkeypatch, record_property) -> None:
    pg, _, clock = store()
    head = adopted_head(pg.semantic, clock)
    source = FrozenInput(event_id="timeout-query", revision=1, lineage_id="timeout-query", evidence=head.evidence)
    monkeypatch.setattr("tracefold.news.storage.claim_recall.clock_ms", lambda: clock.now_ms + 1)
    db = MeasuredDb()
    cancelled = []
    provider_elapsed = []
    requests = []

    async def respond(request: httpx.Request) -> httpx.Response:
        assert not db.active
        texts = json.loads(request.content)["input"]
        requests.append(texts)
        if len(requests) == 1:
            rows = []
            for index, _text in enumerate(texts):
                values = [0.0] * CALIBRATION.embedder.dimensions
                values[1 if index == 2 else 0] = 1.0
                rows.append({"index": index, "embedding": values})
            return httpx.Response(200, json={"data": rows})
        provider_started = time.perf_counter()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            provider_elapsed.append(time.perf_counter() - provider_started)
            raise
        raise AssertionError("the embedding timeout did not cancel the request")

    async def recall() -> Any:
        embedder = ClaimEmbedder(
            model=CALIBRATION.embedder.model,
            base_url="https://embedding.invalid/v1",
            api_key="test-only",
            transport=httpx.MockTransport(respond),
            max_batch_size=4,
        )
        try:
            return await PgClaimRecall(db, embedder=embedder).priors(source, extraction_for(source))  # type: ignore[arg-type]
        finally:
            await embedder.aclose()

    started = time.perf_counter()

    async def bounded_recall() -> Any:
        # The outer guard makes removal of the provider deadline fail this
        # test. It allows unrelated connection/scheduler overhead after the
        # provider's cancellation, which is reported separately.
        return await asyncio.wait_for(recall(), timeout=EMBEDDING_SECONDS + 15.0)

    result = asyncio.run(bounded_recall())
    elapsed = time.perf_counter() - started
    assert cancelled == [True] and len(requests) == 2
    record_property("embedding_cancelled_after_seconds", provider_elapsed[0])
    record_property("recall_including_connection_seconds", elapsed)
    assert result.by_slot["a"]
    assert len(db.timings["news_claim_prior_recall"]) == 1
