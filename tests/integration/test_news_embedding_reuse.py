"""Query vectors commit with exact adopted facts and survive notification retries."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.news_update_pg import (
    EVENT,
    TEXT,
    StubAnalyzer,
    adopted_head,
    evidence,
    extraction_for,
    run_agent,
    seed_event,
    set_semantic_job,
    sql,
    store,
)
from tracefold.news.claim_recall import CALIBRATION, Probe, text_sha, vector_bytes
from tracefold.news.storage.claim_recall import PgClaimRecall
from tracefold.news.storage.notification_store import PgNotificationStore
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import FrozenInput, PriorClaim, RelationDraft, SemanticLease
from tracefold.news.updates.identity import identity
from tracefold.news.updates.ports import SemanticObservation
from tracefold.news.updates.projection import reading_views
from tracefold.news.updates.public import public_updates
from tracefold.news.updates.service import NewsAgent

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


class Embedder:
    identity = CALIBRATION.embedder

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.vector = vector_bytes([1.0, *([0.0] * (self.identity.dimensions - 1))], self.identity)

    async def probes(self, texts):
        if not texts:
            return ()
        self.calls.append(tuple(texts))
        return tuple(Probe(text, self.vector, self.identity.key) for text in texts)


def test_semantic_query_vector_is_committed_once_and_reused_by_notifications_and_maintenance(monkeypatch) -> None:
    pg, db, clock = store()
    embedder = Embedder()
    monkeypatch.setattr("tracefold.news.storage.claim_recall.clock_ms", clock)
    seed_event()
    recall = PgClaimRecall(db, embedder=embedder)
    agent = NewsAgent(pg.semantic, StubAnalyzer(), program_identity="reuse", clock=clock, recall=recall)
    notifications = PgNotificationStore(db, clock=clock, embedder=embedder)

    async def run():
        assert await run_agent(agent, EVENT) == "adopted"
        head = await pg.semantic.head(EVENT)
        assert head is not None
        rows = sql("SELECT vector,embedder,text_sha256 FROM news_claim_index WHERE event_id=%s", (EVENT,))
        assert rows == [
            {"vector": embedder.vector, "embedder": embedder.identity.key, "text_sha256": text_sha(head.claims[0])}
        ]
        before = sql("SELECT revision FROM news_reader_clock")[0]["revision"]
        for _ in range(2):
            snapshot = await notifications.notification_snapshot(EVENT, "news")
            assert snapshot is not None and snapshot.update == head
        assert not await recall.advance()
        assert sql("SELECT revision FROM news_reader_clock")[0]["revision"] == before

    asyncio.run(run())
    assert embedder.calls == [(TEXT,)]


@pytest.mark.parametrize("old_identity", [None, "old-embedding"])
def test_notification_encodes_only_the_missing_or_incompatible_exact_query(old_identity) -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    embedder = Embedder()
    claim = head.claims[0]
    if old_identity is not None:
        sql(
            "UPDATE news_claim_index SET vector=%s,embedder=%s WHERE event_id=%s",
            (embedder.vector, old_identity, EVENT),
        )
    historical = claim.model_copy(update={"statement": "A historical wording."})
    asyncio.run(db.tx("historical", lambda r: r.news.claim_index.index_claim(EVENT, historical)))
    asyncio.run(
        db.tx(
            "vector",
            lambda r: r.news.claim_index.save_vectors(
                [(claim.ref, text_sha(historical), embedder.vector)], embedder=embedder.identity.key
            ),
        )
    )
    notifications = PgNotificationStore(db, clock=clock, embedder=embedder)
    snapshot = asyncio.run(notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and snapshot.update == head
    assert embedder.calls == [(claim.statement,)]
    # A planning read does not masquerade as a durable index completion.
    row = sql(
        "SELECT vector,embedder FROM news_claim_index WHERE claim_ref=%s AND text_sha256=%s",
        (claim.ref, text_sha(claim)),
    )[0]
    assert row["embedder"] == old_identity


def test_a_head_change_during_encoding_cannot_use_the_previous_statement_probe() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    old = head.claims[0]
    new_text = "Agency cuts its steel import tariff."
    source = FrozenInput(
        event_id=EVENT,
        revision=2,
        lineage_id="new",
        evidence=(evidence(new_text),),
        prior=(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=old),),
    )
    extraction = extraction_for(source)
    claim = extraction.claims[0]
    extraction = extraction.model_copy(
        update={
            "claims": (claim.model_copy(update={"fields": claim.fields.model_copy(update={"action": "cuts tariff"})}),)
        }
    )
    update = assemble_update(source, extraction, head, adopted_at_ms=clock.now_ms)
    assert update is not None

    class ChangingEmbedder(Embedder):
        async def probes(self, texts):
            encoded = await super().probes(texts)
            await adopt_with_probes(pg.semantic, source, extraction, head, {})
            return encoded

    embedder = ChangingEmbedder()
    snapshot = asyncio.run(PgNotificationStore(db, clock=clock, embedder=embedder).notification_snapshot(EVENT, "news"))
    assert snapshot is not None and snapshot.update.ref == update.ref
    assert embedder.calls == [(old.statement,)]
    changed_claim = next(c for c in update.claims if c.statement == new_text)
    assert snapshot.recall_diagnostics[changed_claim.ref]["degraded"]


async def adopt_with_probes(pg, source, extraction, head, probes, *, expected=None):
    observation = SemanticObservation(
        result_id=identity("reuse-result", source.input_sha),
        work_id="reuse-work",
        event_id=source.event_id,
        input_revision=source.revision,
        input_sha256=source.input_sha,
        program_identity="reuse",
        completed_at_ms=pg.clock(),
        understanding=extraction,
        read_refs=tuple(view.read_ref for view in reading_views(source)),
    )
    await pg.save_observation(observation)
    update = assemble_update(source, extraction, head, adopted_at_ms=pg.clock())
    assert update is not None
    set_semantic_job(
        source.event_id, wanted_revision=source.revision, lease_token="reuse", lease_until_ms=pg.clock() + 180_000
    )
    adopted = await pg.atomic_adopt(
        expected_head_ref=head.ref if expected is None else expected,
        lease=SemanticLease(source=source, lease_token="reuse", attempts=1),
        observation=observation,
        update=update,
        public=public_updates(update, semantic_completed_at_ms=pg.clock()),
        probes=probes,
    )
    return adopted, update


def test_equivalent_keeps_the_adopted_wording_and_a_failed_cas_writes_no_query_vector() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    old = head.claims[0]
    source = FrozenInput(
        event_id=EVENT,
        revision=2,
        lineage_id="same",
        evidence=(evidence("The agency repeats its tariff order."),),
        prior=(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=old),),
    )
    extraction = extraction_for(source).model_copy(
        update={"relations": (RelationDraft(slot="a", previous_ref=old.ref, relation="equivalent"),)}
    )
    probe = Probe(extraction.claims[0].statement, Embedder().vector, CALIBRATION.embedder.key)
    adopted, update = asyncio.run(adopt_with_probes(pg.semantic, source, extraction, head, {probe.text: probe}))
    assert adopted and update.claims[0].statement == old.statement
    assert sql("SELECT vector FROM news_claim_index WHERE event_id=%s", (EVENT,)) == [{"vector": None}]
    source = source.model_copy(
        update={"revision": 3, "evidence": (evidence("Agency cancels its steel import tariff.", revision="3"),)}
    )
    extraction = extraction_for(source)
    probe = Probe(extraction.claims[0].statement, Embedder().vector, CALIBRATION.embedder.key)
    adopted, _ = asyncio.run(
        adopt_with_probes(pg.semantic, source, extraction, update, {probe.text: probe}, expected="stale-head")
    )
    assert not adopted
    assert sql("SELECT vector FROM news_claim_index WHERE event_id=%s", (EVENT,)) == [{"vector": None}]
