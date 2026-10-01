"""Real PostgreSQL ownership and same-record reversal regression tests."""

import asyncio
from itertools import pairwise

import pytest

from tests.support.news_event_updates import _draft, material
from tests.support.news_update_admission import TITLE, RecordingBus, add_member_evidence, event_of, raw, work
from tests.support.news_update_pg import (
    EVENT,
    STAMP,
    Clock,
    Composer,
    StubAnalyzer,
    ThreadedDb,
    adopt_next,
    adopt_other_event,
    evidence,
    notify_plan,
    run_agent,
    save_card,
    seed_event,
    sql,
)
from tests.support.news_update_semantic import prior_of
from tracefold.news.notifications.card import freeze_card
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.storage.errors import SemanticLeaseLost
from tracefold.news.storage.notification_store import PgNotificationStore
from tracefold.news.storage.semantic_store import PgSemanticStore
from tracefold.news.updates.contracts import Extraction, FrozenInput, PriorClaim, RelationDraft
from tracefold.news.updates.judgment import ProviderUnavailable
from tracefold.news.updates.projection import reading_views
from tracefold.news.updates.service import NewsAgent

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_local_generation_waits_do_not_exhaust_semantic_work_but_provider_failures_do():
    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
    for _ in range(5):
        lease = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
        assert lease is not None and lease.attempts == 1
        asyncio.run(pg.defer_semantic_event(lease, reason="news_generation_capacity_wait", charge_attempt=False))
        row = work(EVENT)
        assert row["attempts"] == 0
        assert row["last_outcome"] == row["last_error_code"] == "news_generation_capacity_wait"
        assert row["failed_read_refs"] == []
        assert row["lease_token"] is None and row["leased_until_ms"] is None
        assert row["next_attempt_at_ms"] > clock.now_ms
        assert asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000)) is None
        clock.now_ms = row["next_attempt_at_ms"]

    for attempt in range(1, 4):
        lease = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
        assert lease is not None and lease.attempts == attempt
        asyncio.run(pg.defer_semantic_event(lease, reason="news_generation_lm_timeout_error"))
        row = work(EVENT)
        assert row["attempts"] == attempt
        assert row["last_error_code"] == "news_generation_lm_timeout_error"
        assert row["last_outcome"] == ("failed" if attempt == 3 else "news_generation_lm_timeout_error")
        clock.now_ms = row["next_attempt_at_ms"]
    assert row["failed_read_refs"] == [view.read_ref for view in reading_views(lease.source)]
    assert asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000)) is None


def test_semantic_worker_postpones_pure_generation_waits_without_spending_pg_attempts():
    from tracefold.news.generation_capacity import NewsGenerationCapacity
    from tracefold.news.pipeline.semantic import SemanticWorker

    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)

    async def run():
        capacity = NewsGenerationCapacity(1)

        class WaitingAgent:
            async def process(self, lease, *, final_attempt=True):
                async with asyncio.timeout(0.005), capacity.acquire():
                    raise AssertionError("The occupied News slot must never start a provider call.")

        worker = SemanticWorker(
            bus=RecordingBus(),
            db=pg.db,
            store=pg,
            agent=WaitingAgent(),
            concurrency=1,
            circuit_failures=1,
            circuit_open_seconds=60,
            clock=clock,
        )
        async with capacity.acquire():
            for _ in range(5):
                lease = await pg.claim_semantic_work(EVENT, lease_ms=180_000)
                assert lease is not None and lease.attempts == 1
                assert await worker.turn(lease) == "deferred"
                row = work(EVENT)
                assert row["attempts"] == 0 and row["last_outcome"] == "news_generation_capacity_wait"
                assert row["failed_read_refs"] == [] and row["next_attempt_at_ms"] > clock.now_ms
                assert not worker.breaker.is_open(clock.now_ms)
                clock.now_ms = row["next_attempt_at_ms"]

    asyncio.run(run())


def test_uncharged_wait_refunds_only_the_current_claim_not_prior_provider_attempts():
    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
    sql("UPDATE news_semantic_work SET attempts=2")
    lease = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert lease is not None and lease.attempts == 3
    asyncio.run(pg.defer_semantic_event(lease, reason="news_generation_capacity_wait", charge_attempt=False))
    row = work(EVENT)
    assert row["attempts"] == 2 and row["last_outcome"] == "news_generation_capacity_wait"
    assert row["failed_read_refs"] == [] and row["next_attempt_at_ms"] > clock.now_ms

    clock.now_ms = row["next_attempt_at_ms"]
    final = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert final is not None and final.attempts == 3
    asyncio.run(pg.defer_semantic_event(final, reason="news_generation_lm_timeout_error"))
    row = work(EVENT)
    assert row["attempts"] == 3 and row["last_outcome"] == "failed"
    assert row["failed_read_refs"] == [view.read_ref for view in reading_views(final.source)]


def test_uncharged_old_wait_cannot_refund_or_delay_a_new_revision():
    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
    old = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert old is not None and old.attempts == 1
    add_member_evidence(EVENT, "new-material", "Agency adds a new exemption.", now_ms=clock.now_ms)
    before = work(EVENT)
    assert before["wanted_revision"] == 2 and before["attempts"] == 0
    asyncio.run(
        pg.defer_semantic_event(
            old, reason="news_generation_capacity_wait", retry_after_ms=999_000, charge_attempt=False
        )
    )
    after = work(EVENT)
    assert after["attempts"] == 0 and after["next_attempt_at_ms"] == before["next_attempt_at_ms"]
    assert after["last_error_code"] is None and after["failed_read_refs"] == []
    new = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert new is not None and new.wanted_revision == 2 and new.attempts == 1
    before = work(EVENT)
    asyncio.run(pg.defer_semantic_event(old, reason="news_generation_capacity_wait", charge_attempt=False))
    assert work(EVENT) == before


@pytest.mark.parametrize("replacement_owner", [False, True], ids=["expired", "reclaimed"])
def test_uncharged_wait_cannot_refund_an_expired_or_replaced_owner(replacement_owner):
    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
    old = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=10))
    assert old is not None and old.attempts == 1
    clock.now_ms += 11
    if replacement_owner:
        new = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
        assert new is not None and new.attempts == 2 and new.lease_token != old.lease_token
    before = work(EVENT)
    asyncio.run(pg.defer_semantic_event(old, reason="news_generation_capacity_wait", charge_attempt=False))
    assert work(EVENT) == before


@pytest.mark.parametrize("outcome", ["fail", "defer"])
def test_old_last_attempt_cannot_spend_or_delay_new_revision(outcome):
    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
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
    assert len(old.source.evidence) == 1
    if outcome == "fail":
        # #742 S3: the failed revision's material is quarantined; the new member is read on its own.
        assert [item.text for item in new.source.evidence] == ["Agency adds a new exemption."]
        assert work(EVENT)["failed_read_refs"] == [view.read_ref for view in reading_views(old.source)]
    else:
        assert len(new.source.evidence) == 2


def test_new_evidence_does_not_starve_valid_old_adoption():
    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
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
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
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
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
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
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
    notification_store = PgNotificationStore(pg.db, clock=clock)
    seed_event()
    agent = NewsAgent(pg, StubAnalyzer(), program_identity="p", clock=clock)
    assert asyncio.run(run_agent(agent, EVENT)) == "adopted"
    head = asyncio.run(pg.head(EVENT))
    snapshot = asyncio.run(notification_store.notification_snapshot(EVENT, "news"))
    assert head is not None and snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(notification_store.atomic_record_plan(plan)).lease
    assert lease is not None
    copy = asyncio.run(Composer().compose(head.claims, sources={}))
    card = freeze_card(plan, head, copy)
    asyncio.run(save_card(notification_store, lease, card))
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
    assert asyncio.run(notification_store.atomic_begin_send(lease, card)) == "reader_changed"
    fresh = asyncio.run(notification_store.notification_snapshot(EVENT, "news"))
    assert fresh is not None and fresh.reader.invalidated_claim_refs == (head.claims[0].ref,)
    assert sql("SELECT count(*) AS n FROM news_deliveries WHERE kind='update'")[0]["n"] == 0


class FailsFirstUnderstanding(StubAnalyzer):
    def __init__(self) -> None:
        super().__init__()
        self.understood: list[tuple[str, ...]] = []

    async def understand(self, source, extracted, budget, **kwargs):
        self.understood.append(tuple(row.claim.ref for row in source.prior))
        if len(self.understood) == 1:
            raise ProviderUnavailable("retry")
        return extracted


def test_retry_re_extracts_when_a_new_related_event_adds_a_read_target():
    seed_event()
    clock = Clock(STAMP + 60_000)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
    analyzer = FailsFirstUnderstanding()
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
    assert new is not None and new.source.prior and new.source.read_targets != old.source.read_targets
    assert old.source.evidence == new.source.evidence
    assert old.source.revision == new.source.revision
    assert asyncio.run(agent.process(new)) == "adopted"
    # A new read target is extraction input, so this input is new.
    assert analyzer.extract_calls == 2


def test_retry_reuses_the_extraction_when_only_a_related_head_changes():
    # #742 W6: related Events' claims are comparison candidates, not extraction input. One of them adopting
    # again between two attempts changes the comparisons, not the stored extraction.
    seed_event("ev-other", text="Agency orders a 25% tariff on steel.", fingerprint="fp-other")
    clock = Clock(STAMP + 60_000)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
    related = asyncio.run(adopt_other_event(pg))
    sql("UPDATE news_items SET provider_params_available_at_ms=%s WHERE item_id='it-ev-other'", (STAMP,))
    seed_event()
    analyzer = FailsFirstUnderstanding()
    agent = NewsAgent(pg, analyzer, program_identity="p", clock=clock)
    old = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert old is not None and [row.claim.ref for row in old.source.prior] == [related.claim.ref]
    with pytest.raises(ProviderUnavailable):
        asyncio.run(agent.process(old))
    asyncio.run(pg.defer_semantic_event(old, reason="retry"))

    head = asyncio.run(pg.head("ev-other"))
    later = evidence("Agency raises the steel tariff to 50%.", revision="3", publisher="other")
    source = FrozenInput(
        event_id="ev-other", revision=2, lineage_id="lineage-o2", evidence=(later,), prior=prior_of(head)
    )
    extraction = Extraction(
        claims=(_draft(later, rate="50"),),
        relations=(
            RelationDraft(
                slot="a", previous_ref=related.claim.ref, relation="real_world_change", change_kind="parameter_change"
            ),
        ),
    )
    assert asyncio.run(adopt_next(pg, head, source, extraction, work_id="work-other-2"))[0]
    clock.now_ms += 60_000
    new = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert new is not None and new.source.input_sha == old.source.input_sha
    assert new.source.prior != old.source.prior
    assert asyncio.run(agent.process(new)) == "adopted"
    assert analyzer.extract_calls == 1
    assert analyzer.understood[-1] == tuple(row.claim.ref for row in new.source.prior)


def test_a_crashed_final_attempt_quarantines_exactly_the_reads_it_was_given():
    # #742 S3: the Janitor settles a final attempt that died holding its lease the way a failed attempt is
    # settled -- its material is quarantined -- and a member that joins afterwards is still read.
    from tests.postgres_test_utils import connect_postgres_test
    from tracefold.app.repository_session import repositories_for_connection

    seed_event()
    clock = Clock(STAMP + 10)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
    sql("UPDATE news_semantic_work SET attempts=2")
    crashed = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=10))
    assert crashed is not None and crashed.attempts == 3
    given = [view.read_ref for view in reading_views(crashed.source)]
    assert work(EVENT)["attempt_read_refs"] == given
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            news = repositories_for_connection(conn).news
            assert news.semantic_work.terminalize_exhausted_semantic_work(now_ms=clock.now_ms + 11, limit=10) == 1
    finally:
        conn.close()
    row = work(EVENT)
    assert (row["last_outcome"], row["failed_read_refs"]) == ("failed", given)

    add_member_evidence(EVENT, "late-member", "Agency adds a new exemption.", now_ms=clock.now_ms + 20)
    clock.now_ms += 20
    later = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=180_000))
    assert later is not None and [item.text for item in later.source.evidence] == ["Agency adds a new exemption."]
