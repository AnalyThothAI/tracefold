"""Real PostgreSQL ownership and same-record reversal regression tests."""

import asyncio
from itertools import pairwise

import pytest

from tests.integration.test_news_event_update_store import (
    EVENT,
    STAMP,
    Clock,
    Composer,
    StubAnalyzer,
    ThreadedDb,
    adopt_next,
    notify_plan,
    run_agent,
    seed_event,
    sql,
)
from tests.integration.test_news_semantic_pipeline import (
    TITLE,
    RecordingBus,
    add_member_evidence,
    event_of,
    raw,
    work,
)
from tests.support.news_event_updates import _draft, material
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.storage.event_update_store import PgNewsStore
from tracefold.news.storage.event_updates import SemanticLeaseLost
from tracefold.news.updates.contracts import Extraction, FrozenInput, PriorClaim, RelationDraft
from tracefold.news.updates.notification import freeze_card
from tracefold.news.updates.service import NewsAgent

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


@pytest.mark.parametrize("outcome", ["fail", "defer"])
def test_old_last_attempt_cannot_spend_or_delay_new_revision(outcome):
    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgNewsStore(ThreadedDb(), clock=clock)
    sql("UPDATE news_semantic_work SET attempts=2")
    old = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert old is not None and old.attempts == 3 and old.source.revision == 1
    add_member_evidence(EVENT, "new-material", "Agency adds a new exemption.", now_ms=clock.now_ms)
    if outcome == "fail":
        asyncio.run(pg.fail_semantic_event(old, error_code="old_fault"))
    else:
        asyncio.run(pg.defer_semantic_event(old, reason="old_timeout", retry_after_ms=999_000))
    new = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert new is not None and new.wanted_revision == 2 and new.attempts == 1
    assert len(old.source.evidence) == 1 and len(new.source.evidence) == 2


def test_new_evidence_does_not_starve_valid_old_adoption():
    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgNewsStore(ThreadedDb(), clock=clock)
    old = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert old is not None
    add_member_evidence(EVENT, "new-material", "Agency adds a new exemption.", now_ms=clock.now_ms)
    agent = NewsAgent(pg, StubAnalyzer(), program_identity="p", clock=clock)
    assert asyncio.run(agent.process(old)) == "adopted"
    row = work(EVENT)
    assert row["done_revision"] == 1 and row["wanted_revision"] == 2 and row["attempts"] == 0
    new = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert new is not None and new.source.revision == 2 and len(new.source.evidence) == 1


def test_old_adopt_and_finish_cannot_clear_a_replacement_owner():
    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgNewsStore(ThreadedDb(), clock=clock)
    old = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=10))
    assert old is not None
    clock.now_ms += 11
    new = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert new is not None
    agent = NewsAgent(pg, StubAnalyzer(), program_identity="p", clock=clock)
    with pytest.raises(SemanticLeaseLost):
        asyncio.run(agent.process(old))
    assert work(EVENT)["lease_token"] == new.lease_token
    observation = sql("SELECT work_id FROM news_semantic_observations")[0]
    with pytest.raises(SemanticLeaseLost):
        asyncio.run(pg.finish_semantic_work(observation["work_id"], lease=old, reason="late"))
    assert work(EVENT)["lease_token"] == new.lease_token
    assert asyncio.run(agent.process(new)) == "adopted"


@pytest.mark.parametrize("sequence", [(0, 1, 0), (0, 1, 2, 1)])
def test_same_record_reversal_reaches_frozen_input_and_old_envelope_replay_is_inert(monkeypatch, sequence):
    clock = Clock(STAMP)
    monkeypatch.setattr("tracefold.news.pipeline.admission.now_ms", clock)
    bus = RecordingBus()
    pg = PgNewsStore(ThreadedDb(), clock=clock)
    consumer = DeduperConsumer(bus=bus, db=pg.db, watchlist_symbols=frozenset({"BTC"}))
    texts = [
        TITLE + "<br/>The opening is this month.",
        TITLE + "<br/>The opening is delayed.",
        TITLE + "<br/>The opening moves to next year.",
    ]
    frames = []
    for i, body in enumerate(texts[index] for index in sequence):
        clock.now_ms = STAMP + i * 1000
        frame = raw(888, body, stamp=clock.now_ms)
        frames.append(frame)
        asyncio.run(consumer.handle(frame))
        event = event_of(888)
        assert (
            asyncio.run(run_agent(NewsAgent(pg, StubAnalyzer(), program_identity="p", clock=clock), event)) == "adopted"
        )
    rows = sql(
        "SELECT revision_sha256,content_sha256,previous_revision_sha256,revision_sequence "
        "FROM news_item_revisions ORDER BY revision_sequence"
    )
    assert len(rows) == len(sequence) - 1
    assert [r["revision_sequence"] for r in rows] == list(range(1, len(sequence)))
    assert all(
        current["previous_revision_sha256"] == previous["revision_sha256"] for previous, current in pairwise(rows)
    )
    head = asyncio.run(pg.head(event))
    assert head is not None and len(head.evidence) == len(sequence)
    assert len({e.ref for e in head.evidence}) == len(sequence)
    before = work(event)["wanted_revision"]
    asyncio.run(consumer.handle(frames[1]))  # Older original envelope.
    clock.now_ms += 1000
    asyncio.run(consumer.handle(raw(888, texts[sequence[-1]], stamp=clock.now_ms)))  # Current content re-observed.
    assert work(event)["wanted_revision"] == before
    assert sql("SELECT count(*) AS n FROM news_item_revisions")[0]["n"] == len(sequence) - 1


def test_cross_event_correction_invalidates_frozen_unsent_card():
    clock = Clock(STAMP + 60_000)
    pg = PgNewsStore(ThreadedDb(), clock=clock)
    seed_event()
    agent = NewsAgent(pg, StubAnalyzer(), program_identity="p", clock=clock)
    assert asyncio.run(run_agent(agent, EVENT)) == "adopted"
    head = asyncio.run(pg.head(EVENT))
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert head is not None and snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(pg.atomic_record_plan(plan))
    assert lease is not None
    copy = asyncio.run(Composer().compose(head.claims))
    card = freeze_card(plan, head, copy)
    asyncio.run(pg.save_card(lease, card))
    seed_event("correction", fingerprint="correction")
    ev = material("Correction: Agency did not order the tariff.", publisher="correction")
    source = FrozenInput(
        event_id="correction",
        revision=1,
        lineage_id="c",
        evidence=(ev,),
        prior=(PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=head.claims[0]),),
    )
    extraction = Extraction(
        claims=(_draft(ev, rate="0"),),
        relations=(
            RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="corrects", change_kind="correction"),
        ),
    )
    assert asyncio.run(adopt_next(pg, None, source, extraction))[0]
    assert not asyncio.run(pg.atomic_begin_send(lease, card))
    fresh = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert fresh is not None and fresh.reader.invalidated_claim_refs == (head.claims[0].ref,)
    assert sql("SELECT count(*) AS n FROM news_deliveries WHERE kind='update'")[0]["n"] == 0


def test_retry_checkpoint_cannot_reuse_extraction_for_changed_prior_context():
    from tests.integration.test_news_event_update_store import adopt_other_event
    from tracefold.news.updates.judgment import ProviderUnavailable

    class FailsOnce(StubAnalyzer):
        async def understand(self, source, extracted, budget, **kwargs):
            if not source.prior:
                raise ProviderUnavailable("retry")
            return extracted

    seed_event()
    clock = Clock(STAMP + 60_000)
    pg = PgNewsStore(ThreadedDb(), clock=clock)
    analyzer = FailsOnce()
    agent = NewsAgent(pg, analyzer, program_identity="p", clock=clock)
    old = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert old is not None and not old.source.prior
    with pytest.raises(ProviderUnavailable):
        asyncio.run(agent.process(old))
    assert analyzer.extract_calls == 1
    asyncio.run(pg.defer_semantic_event(old, reason="retry"))
    seed_event("ev-other", text="Agency orders a 25% tariff on steel.", fingerprint="fp-other")
    asyncio.run(adopt_other_event(pg))
    sql("UPDATE news_items SET provider_params_available_at_ms=%s WHERE item_id='it-ev-other'", (STAMP,))
    clock.now_ms += 60_000
    new = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert new is not None and new.source.prior
    assert old.source.evidence == new.source.evidence
    assert old.source.revision == new.source.revision
    assert asyncio.run(agent.process(new)) == "adopted"
    assert analyzer.extract_calls == 2
