"""A missing offline model degrades query recall while semantic Workers still adopt."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager

import pytest

from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tests.support.news_update_admission import RecordingBus, work
from tests.support.news_update_pg import (
    EVENT,
    STAMP,
    StubAnalyzer,
    adopted_head,
    draft,
    seed_event,
    sql,
    store,
)
from tracefold.app.claim_embedding import ClaimEmbedder
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.wiring.database import WorkerNewsDatabase
from tracefold.news.bus import BusMessage
from tracefold.news.claim_recall import CALIBRATION, Probe, vector_bytes
from tracefold.news.pipeline.semantic import SemanticWorker
from tracefold.news.storage.claim_recall import PgClaimRecall
from tracefold.news.storage.semantic_store import PgSemanticStore
from tracefold.news.updates.contracts import Extraction, FrozenInput, SupportDraft
from tracefold.news.updates.service import NewsAgent
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.client import create_pool

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


@pytest.mark.parametrize("claim_count", [4, 11])
def test_missing_model_adopts_every_claim_with_separate_native_recall_reads(
    monkeypatch, tmp_path, claim_count, record_property
) -> None:
    pg, fixture_db, clock = store()
    prior = adopted_head(pg.semantic, clock)
    encoded = vector_bytes([1.0, *([0.0] * 383)], CALIBRATION.embedder)
    asyncio.run(
        fixture_db.tx(
            "ready_candidate",
            lambda r: r.news.claim_index.index_claim(
                EVENT,
                prior.claims[0],
                probe=Probe(prior.claims[0].statement, encoded, CALIBRATION.embedder.key),
            ),
        )
    )
    statements = tuple(
        f"Agency orders a {i + 1}% tariff on steel imports effective October {i + 1}." for i in range(claim_count)
    )
    event_id = f"missing-model-{claim_count}"
    seed_event(event_id, text="\n".join(statements), fingerprint=event_id)
    monkeypatch.setattr("tracefold.news.storage.claim_recall.clock_ms", lambda: clock.now_ms + 1)

    def extraction(source: FrozenInput) -> Extraction:
        evidence = source.evidence[0]
        return Extraction(
            claims=tuple(
                draft(evidence, slot=f"a{i}", action=f"orders tariff measure {i + 1}", quote=statement)
                for i, statement in enumerate(statements)
            ),
            supports=tuple(
                SupportDraft(slot=f"a{i}", evidence_ref=evidence.ref, relation="supports") for i in range(claim_count)
            ),
        )

    downloads = []
    sessions = []

    def no_download(*args, **kwargs):
        downloads.append((args, kwargs))
        raise AssertionError("Workers must never download a missing model")

    def no_session(*args, **kwargs):
        sessions.append((args, kwargs))
        raise AssertionError("A missing snapshot must never construct an ONNX session")

    monkeypatch.setattr("huggingface_hub.snapshot_download", no_download)
    monkeypatch.setattr("onnxruntime.InferenceSession", no_session)
    status = []
    cache_dir = tmp_path / "missing-model-cache"
    embedder = ClaimEmbedder(model=CALIBRATION.embedder.model, cache_dir=cache_dir, on_status=status.append)
    pool = create_pool(
        _test_postgres_dsn(),
        min_size=1,
        max_size=4,
        max_waiting=3,
        connect_timeout_seconds=5.0,
        application_name="tracefold_missing_model_test",
        statement_timeout_seconds=3.0,
        lock_timeout_seconds=0.25,
        idle_in_transaction_session_timeout_seconds=5.0,
    )
    pool.wait(timeout=5.0)
    database = WorkerDatabase(worker_pool=pool, telemetry=TelemetryRegistry())
    db = WorkerNewsDatabase(database)
    semantic_store = PgSemanticStore(db, clock=clock)
    recall = PgClaimRecall(db, embedder=embedder)
    batches = []
    original_priors = recall.priors

    async def recorded_priors(source, extracted):
        batch = await original_priors(source, extracted)
        batches.append(batch)
        return batch

    monkeypatch.setattr(recall, "priors", recorded_priors)
    transactions = []
    original_session = WorkerDatabase.worker_session

    @contextmanager
    def recorded_session(self, name, *args, **kwargs):
        started = time.perf_counter()
        with original_session(self, name, *args, **kwargs) as repos:
            if name in {"news_claim_prior_pool", "news_claim_prior_recall"}:
                row = repos.conn.execute(
                    """SELECT txid_current() AS transaction_id,
                              current_setting('transaction_timeout') AS transaction_timeout,
                              current_setting('statement_timeout') AS statement_timeout,
                              current_setting('transaction_read_only') AS read_only,
                              current_setting('transaction_isolation') AS isolation"""
                ).fetchone()
                metric = {"name": name, **dict(row)}
                transactions.append(metric)
            else:
                metric = None
            yield repos
        if metric is not None:
            metric["seconds"] = time.perf_counter() - started

    monkeypatch.setattr(WorkerDatabase, "worker_session", recorded_session)
    analyzer = StubAnalyzer(extraction)
    worker = SemanticWorker(
        bus=RecordingBus(),
        db=db,
        store=semantic_store,
        agent=NewsAgent(semantic_store, analyzer, program_identity="missing-model", clock=clock, recall=recall),
        concurrency=1,
        circuit_failures=10,
        circuit_open_seconds=60.0,
        clock=clock,
    )

    async def run():
        try:
            assert not await embedder.self_test()
            assert embedder.unavailable_reason == "news_embedding_model_missing"
            started = time.perf_counter()
            await worker.handle(
                BusMessage("event", f"event:{event_id}:1", "event.general.normal", {"event_id": event_id}, "t", STAMP)
            )
            elapsed = time.perf_counter() - started
            adopted = await semantic_store.head(event_id)
            assert adopted is not None
            assert {claim.statement for claim in adopted.claims} == set(statements)
            assert len(adopted.claims) == claim_count
            return elapsed
        finally:
            await embedder.aclose()

    try:
        elapsed = asyncio.run(run())
    finally:
        database.close_executors()
        pool.close()

    assert status == [False] and embedder._encoder is None
    assert downloads == sessions == [] and not cache_dir.exists()
    assert analyzer.extract_calls == 1 and len(batches) == 1
    batch = batches[0]
    assert len(batch.probes) == len(batch.by_slot) == claim_count
    assert all(probe.vector is None for probe in batch.probes.values())
    assert all(rows for rows in batch.by_slot.values())
    assert all(diagnostic["degraded"] for diagnostic in batch.diagnostics.values())
    assert len(transactions) == claim_count + 1
    assert len({row["transaction_id"] for row in transactions}) == claim_count + 1
    assert all(row["read_only"] == "on" and row["isolation"] == "repeatable read" for row in transactions)
    assert all(row["transaction_timeout"] == "8s" and row["statement_timeout"] == "3s" for row in transactions)
    assert max(row["seconds"] for row in transactions) < 8
    state = work(event_id)
    assert state["last_outcome"] == "adopted" and state["last_error_code"] is None
    assert state["wanted_revision"] == state["done_revision"] == 1
    rows = sql("SELECT vector,embedder FROM news_claim_index WHERE event_id=%s", (event_id,))
    assert len(rows) == claim_count and all(row == {"vector": None, "embedder": None} for row in rows)
    metric = {
        "claims": claim_count,
        "model_missing": True,
        "query_vectors_available": 0,
        "candidate_vectors_available": 1,
        "downloads": len(downloads),
        "onnx_sessions": len(sessions),
        "adopted_claims": len(rows),
        "semantic_outcome": state["last_outcome"],
        "elapsed_seconds": elapsed,
        "transactions": transactions,
    }
    record_property("missing_model_semantic_worker", json.dumps(metric, sort_keys=True))
    print(f"missing_model_semantic_worker: {json.dumps(metric, sort_keys=True)}")
