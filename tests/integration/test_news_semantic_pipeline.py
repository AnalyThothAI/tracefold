"""Admission -> semantic work -> News Agent -> adoption over real PostgreSQL (#706).

The consumers are the production classes over the production News repository; the broker is a recording
fake and the analyzer boundary is scripted, so every assertion is about durable rows: evidence snapshots,
item revisions, semantic work, observations, adopted heads and the public outbox.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_update_admission import TITLE, RecordingBus, add_member_evidence, event_of, raw, snapshots, work
from tests.support.news_update_pg import (
    EVENT,
    STAMP,
    TEXT,
    Clock,
    StubAnalyzer,
    ThreadedDb,
    draft,
    run_agent,
    seed_event,
    sql,
)
from tests.support.scripted_lm import ScriptedLM
from tracefold.app.cli.commands import news_diagnostics
from tracefold.app.cli.parser import build_parser
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.adapters.extraction import DspyExtractor
from tracefold.news.bus import BusMessage
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.pipeline.semantic import SemanticWorker
from tracefold.news.storage.judgment_cache import JUDGMENT_CACHE_RETENTION_MS
from tracefold.news.storage.judgment_store import PgJudgmentCache
from tracefold.news.storage.semantic_store import PgSemanticStore, PgSourceReader
from tracefold.news.storage.semantic_work import SEMANTIC_ATTEMPTS_MAX
from tracefold.news.updates.contracts import (
    Citation,
    ClaimFields,
    DraftClaim,
    Extraction,
    FrozenInput,
    ReadTarget,
    RelationDraft,
    SupportDraft,
)
from tracefold.news.updates.judgment import (
    Answer,
    BatchResult,
    NewsJudgments,
    ProviderUnavailable,
    Question,
    Task,
)
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.service import NewsAgent

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


@pytest.fixture(autouse=True)
def admission_clock(monkeypatch):
    # Arrival envelopes and the admission clock share a fixed test epoch; wall time must not
    # expire the near-match window or make semantic work due in the future.
    monkeypatch.setattr("tracefold.news.pipeline.admission.now_ms", lambda: STAMP)


# ------------------------------------------------------------------ admission


def test_a_body_revision_of_one_provider_record_is_new_evidence_and_new_semantic_work() -> None:
    bus = RecordingBus()
    deduper = DeduperConsumer(bus=bus, db=ThreadedDb(), watchlist_symbols=frozenset({"BTC"}))
    first = f"{TITLE}<br/>Officials say the order takes effect in October."
    asyncio.run(deduper.handle(raw(7001, first, stamp=STAMP)))
    event_id = event_of(7001)
    item = sql("SELECT item_id, observed_at_ms, evidence_text FROM news_items WHERE source_item_key = '7001'")[0]
    assert work(event_id)["wanted_revision"] == 1
    assert bus.wakes() == [f"event:{event_id}:1"]

    # Exact redelivery of the same record and body: no revision, no snapshot, no wake, same first clock.
    asyncio.run(deduper.handle(raw(7001, first, stamp=STAMP + 5_000)))
    assert [row["evidence_version"] for row in snapshots(event_id)] == [1]
    assert work(event_id)["wanted_revision"] == 1 and len(bus.wakes()) == 1
    assert sql("SELECT count(*) AS n FROM news_item_revisions")[0]["n"] == 0
    store = PgSemanticStore(ThreadedDb(), clock=Clock(STAMP + 6_000))
    assert asyncio.run(run_agent(NewsAgent(store, StubAnalyzer(), program_identity="p"), event_id)) == "adopted"
    original = asyncio.run(store.head(event_id))
    assert original is not None and original.input_revision == 1

    # The same record with a changed body keeps both bodies and is new evidence for its Event.
    revised = f"{TITLE}<br/>Officials say pharmaceutical imports are exempt."
    asyncio.run(deduper.handle(raw(7001, revised, stamp=STAMP + 10_000)))
    kept = sql("SELECT observed_at_ms, evidence_text FROM news_items WHERE item_id = %s", (item["item_id"],))[0]
    assert kept == {"observed_at_ms": item["observed_at_ms"], "evidence_text": item["evidence_text"]}
    revision = sql("SELECT revision_sha256, evidence_text, received_at_ms FROM news_item_revisions")
    assert len(revision) == 1 and revision[0]["received_at_ms"] == STAMP + 10_000
    versions = snapshots(event_id)
    assert [row["evidence_version"] for row in versions] == [1, 2]
    member = next(m for m in versions[1]["snapshot"]["members"] if m["item_id"] == item["item_id"])
    assert member["evidence_revisions"] == [revision[0]["revision_sha256"]]
    assert work(event_id)["wanted_revision"] == 2
    assert bus.wakes() == [f"event:{event_id}:1", f"event:{event_id}:2"]

    source = asyncio.run(PgSemanticStore(ThreadedDb(), clock=Clock(STAMP + 20_000)).input_for(event_id))
    assert source.revision == 2
    texts = {evidence.text: evidence.source for evidence in source.evidence}
    assert set(texts) == {revision[0]["evidence_text"]}
    assert {row.claim.ref for row in source.prior} >= {claim.ref for claim in original.claims}
    revised_source = texts[revision[0]["evidence_text"]]
    assert revised_source.artifact_revision == revision[0]["revision_sha256"]
    assert revised_source.first_available_at_ms == STAMP + 10_000
    assert original.evidence[0].source.first_available_at_ms == item["observed_at_ms"]

    # Redelivering the revised body is exact again.
    asyncio.run(deduper.handle(raw(7001, revised, stamp=STAMP + 30_000)))
    assert work(event_id)["wanted_revision"] == 2 and len(bus.wakes()) == 2


def test_an_attribution_only_revision_is_new_evidence_with_the_new_source() -> None:
    bus = RecordingBus()
    deduper = DeduperConsumer(bus=bus, db=ThreadedDb(), watchlist_symbols=frozenset({"BTC"}))
    asyncio.run(deduper.handle(raw(7051, TITLE, stamp=STAMP, source="Reuters")))
    event_id = event_of(7051)
    clock = Clock(STAMP + 5_000)
    store = PgSemanticStore(ThreadedDb(), clock=clock)
    agent = NewsAgent(store, StubAnalyzer(), program_identity="p", clock=clock)
    assert asyncio.run(run_agent(agent, event_id)) == "adopted"
    first = asyncio.run(store.head(event_id))
    assert first is not None

    asyncio.run(deduper.handle(raw(7051, TITLE, stamp=STAMP + 10_000, source="Associated Press")))
    assert work(event_id)["wanted_revision"] == 2
    assert bus.wakes() == [f"event:{event_id}:1", f"event:{event_id}:2"]
    source = asyncio.run(store.input_for(event_id))
    assert len(source.evidence) == 1
    assert source.evidence[0].text == first.evidence[0].text
    assert source.evidence[0].source.origin_id == "associated press"
    assert source.evidence[0].ref != first.evidence[0].ref
    assert source.evidence[0].source.first_available_at_ms == STAMP + 10_000
    assert {row.claim.ref for row in source.prior} == {claim.ref for claim in first.claims}

    asyncio.run(deduper.handle(raw(7051, TITLE, stamp=STAMP + 20_000, source="Associated Press")))
    assert work(event_id)["wanted_revision"] == 2


def test_a_near_match_joins_as_a_member_and_wakes_semantics_instead_of_settling() -> None:
    bus = RecordingBus()
    deduper = DeduperConsumer(bus=bus, db=ThreadedDb(), watchlist_symbols=frozenset({"BTC"}))
    asyncio.run(deduper.handle(raw(7101, f"{TITLE} effective October 1", stamp=STAMP)))
    event_id = event_of(7101)
    asyncio.run(deduper.handle(raw(7102, f"{TITLE} effective October 1, officials said", stamp=STAMP + 1_000)))

    members = sql("SELECT match_kind FROM news_event_members WHERE event_id = %s ORDER BY joined_at_ms", (event_id,))
    assert [row["match_kind"] for row in members] == ["leader", "near"]
    assert event_of(7102) == event_id
    assert work(event_id)["wanted_revision"] == 2
    assert bus.wakes() == [f"event:{event_id}:1", f"event:{event_id}:2"]


def test_consecutive_members_extract_only_the_unadopted_material_and_carry_old_claims() -> None:
    clock = Clock(STAMP + 60_000)
    store = PgSemanticStore(ThreadedDb(), clock=clock)
    seed_event()
    first_analyzer = StubAnalyzer()
    assert (
        asyncio.run(run_agent(NewsAgent(store, first_analyzer, program_identity="p", clock=clock), EVENT)) == "adopted"
    )
    first = asyncio.run(store.head(EVENT))
    assert first is not None
    original = first.claims[0]

    add_member_evidence(EVENT, "it-member-a", "Agency announces a medicine exemption.", now_ms=clock.now_ms)
    add_member_evidence(EVENT, "it-member-b", "Agency confirms the start date.", now_ms=clock.now_ms + 1)
    source = asyncio.run(store.input_for(EVENT))
    clock.now_ms += 2
    assert source.revision == 3
    assert {item.text for item in source.evidence} == {
        "Agency announces a medicine exemption.",
        "Agency confirms the start date.",
    }
    assert original.ref in {row.claim.ref for row in source.prior}

    analyzer = StubAnalyzer(
        lambda current: Extraction(
            claims=tuple(
                draft(item, slot=f"s{index}", action=f"new action {index}")
                for index, item in enumerate(current.evidence)
            )
        )
    )
    assert asyncio.run(run_agent(NewsAgent(store, analyzer, program_identity="p", clock=clock), EVENT)) == "adopted"
    adopted = asyncio.run(store.head(EVENT))
    assert adopted is not None and adopted.input_revision == 3
    assert analyzer.extract_calls == 1
    assert len(adopted.claims) == 3
    assert adopted.claims[0] == original
    assert {item.ref for item in adopted.evidence} == {item.ref for item in first.evidence} | {
        item.ref for item in source.evidence
    }


def test_equivalent_new_member_amends_the_source_without_a_new_claim() -> None:
    clock = Clock(STAMP + 60_000)
    store = PgSemanticStore(ThreadedDb(), clock=clock)
    seed_event()
    assert (
        asyncio.run(run_agent(NewsAgent(store, StubAnalyzer(), program_identity="p", clock=clock), EVENT)) == "adopted"
    )
    first = asyncio.run(store.head(EVENT))
    assert first is not None

    add_member_evidence(EVENT, "it-member", "Agency confirms the same tariff order.", now_ms=clock.now_ms)
    analyzer = StubAnalyzer(
        lambda source: Extraction(
            claims=(draft(source.evidence[0]),),
            relations=(RelationDraft(slot="a", previous_ref=first.claims[0].ref, relation="equivalent"),),
            supports=(SupportDraft(slot="a", evidence_ref=source.evidence[0].ref, relation="supports"),),
        )
    )
    assert asyncio.run(run_agent(NewsAgent(store, analyzer, program_identity="p", clock=clock), EVENT)) == "adopted"
    head = asyncio.run(store.head(EVENT))
    assert head is not None
    assert [claim.ref for claim in head.claims] == [first.claims[0].ref]
    assert len(head.evidence) == 2
    assert len(head.evidence_relations) == 2
    public = sql("SELECT kind FROM news_trade_events WHERE source_revision = %s", (head.content_revision,))
    assert [row["kind"] for row in public] == ["source_update"]


def test_no_new_source_identity_settles_the_revision_without_reextracting() -> None:
    clock = Clock(STAMP + 60_000)
    store = PgSemanticStore(ThreadedDb(), clock=clock)
    seed_event()
    analyzer = StubAnalyzer()
    agent = NewsAgent(store, analyzer, program_identity="p", clock=clock)
    assert asyncio.run(run_agent(agent, EVENT)) == "adopted"
    first = asyncio.run(store.head(EVENT))
    assert first is not None
    # A work revision can be requested for metadata whose source identity is already adopted.
    sql("UPDATE news_semantic_work SET wanted_revision = 2 WHERE event_id = %s", (EVENT,))
    source = asyncio.run(store.input_for(EVENT))
    assert source.evidence == ()
    assert asyncio.run(run_agent(agent, EVENT)) == "unchanged"
    assert analyzer.extract_calls == 1
    assert work(EVENT)["done_revision"] == 2


def test_analyzed_material_without_a_claim_is_not_reextracted_on_the_next_revision() -> None:
    clock = Clock(STAMP + 60_000)
    store = PgSemanticStore(ThreadedDb(), clock=clock)
    seed_event()
    first_agent = NewsAgent(store, StubAnalyzer(), program_identity="p", clock=clock)
    assert asyncio.run(run_agent(first_agent, EVENT)) == "adopted"
    first = asyncio.run(store.head(EVENT))
    assert first is not None

    add_member_evidence(EVENT, "it-member-a", "Agency repeats background context.", now_ms=clock.now_ms)
    analyzer = StubAnalyzer(lambda _source: Extraction(claims=()))
    agent = NewsAgent(store, analyzer, program_identity="p", clock=clock)
    assert asyncio.run(run_agent(agent, EVENT)) == "unchanged"
    assert work(EVENT)["done_revision"] == 2
    assert asyncio.run(store.head(EVENT)) == first

    add_member_evidence(EVENT, "it-member-b", "Agency adds a pharmaceutical exemption.", now_ms=clock.now_ms)
    source = asyncio.run(store.input_for(EVENT))
    assert [row.text for row in source.evidence] == ["Agency adds a pharmaceutical exemption."]
    assert asyncio.run(run_agent(agent, EVENT)) == "unchanged"
    assert analyzer.extract_calls == 2
    assert work(EVENT)["done_revision"] == 3
    assert asyncio.run(store.head(EVENT)) == first


def test_recovery_evidence_joins_its_event_but_never_wakes_semantics() -> None:
    bus = RecordingBus()
    deduper = DeduperConsumer(bus=bus, db=ThreadedDb(), watchlist_symbols=frozenset({"BTC"}))
    asyncio.run(deduper.handle(raw(7201, f"{TITLE} effective October 1", stamp=STAMP)))
    event_id = event_of(7201)
    asyncio.run(
        deduper.handle(
            raw(7202, f"{TITLE} effective October 1, officials said", stamp=STAMP + 1_000, ingest_mode="recovery")
        )
    )

    assert [row["evidence_version"] for row in snapshots(event_id)] == [1, 2]
    assert work(event_id)["wanted_revision"] == 1
    assert bus.wakes() == [f"event:{event_id}:1"]


# ------------------------------------------------------------------ semantic worker over PostgreSQL


class RelationBackend:
    """Generated judgments whose relation answers fail until `recover()`; sources always `supports`."""

    identity = "relation-backend-test"

    def __init__(self) -> None:
        self.failing = True

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        if task == "relation" and self.failing:
            raise ProviderUnavailable("news_generation_LMRateLimitError")
        value = "adds_information" if task == "relation" else "supports"
        return BatchResult(
            answers=tuple(Answer(item_id=item.item_id, value=value, backend=self.identity) for item in items)
        )


class LeaderExtractor:
    """One grounded claim per evidence item, with the item's own text as its condition."""

    identity = "leader-extractor-test"

    async def extract(self, source: FrozenInput) -> Extraction:
        claims = tuple(
            DraftClaim(
                slot=f"s{index}",
                statement=evidence.text,
                fields=ClaimFields(
                    subject="Agency",
                    action="orders tariff",
                    mode="decision",
                    phase="ordered",
                    conditions=(evidence.text,),
                ),
                citations=(Citation(evidence_ref=evidence.ref, quote=evidence.text),),
            )
            for index, evidence in enumerate(source.evidence)
        )
        return Extraction(claims=claims)


def test_provider_failures_defer_and_only_the_last_attempt_adopts_an_unresolved_comparison() -> None:
    clock = Clock(STAMP + 60_000)
    db = ThreadedDb()
    store = PgSemanticStore(db, clock=clock)
    backend = RelationBackend()
    analyzer = SemanticAnalyzer(LeaderExtractor(), NewsJudgments(generated=backend, cache=PgJudgmentCache(db)))
    agent = NewsAgent(store, analyzer, program_identity="program-test", clock=clock)
    worker = SemanticWorker(
        bus=RecordingBus(),
        db=db,
        store=store,
        agent=agent,
        concurrency=1,
        circuit_failures=10,
        circuit_open_seconds=60.0,
        clock=clock,
    )
    wake = BusMessage("event", f"event:{EVENT}:1", "event.general.normal", {"event_id": EVENT}, "t", STAMP)

    seed_event()
    asyncio.run(worker.handle(wake))  # no prior claims yet: nothing to compare, adopted
    first = asyncio.run(store.head(EVENT))
    assert first is not None and work(EVENT)["done_revision"] == 1

    add_member_evidence(EVENT, "it-member", "Agency adds a pharmaceutical exemption.", now_ms=clock.now_ms)
    assert work(EVENT)["wanted_revision"] == 2
    for attempt in range(1, SEMANTIC_ATTEMPTS_MAX):
        asyncio.run(worker.handle(wake))
        row = work(EVENT)
        assert (row["attempts"], row["done_revision"], row["last_outcome"]) == (
            attempt,
            1,
            "news_relation_unavailable",
        )
        assert asyncio.run(store.head(EVENT)) == first
        # Backoff: nothing is due until the retry delay has passed.
        asyncio.run(worker.handle(wake))
        assert work(EVENT)["attempts"] == attempt
        clock.now_ms += 10 * 60_000

    asyncio.run(worker.handle(wake))
    row = work(EVENT)
    assert (row["done_revision"], row["last_outcome"]) == (2, "adopted")
    head = asyncio.run(store.head(EVENT))
    assert head is not None and head.input_revision == 2
    new_claims = [change for change in head.changes if change.kind == "possible_new"]
    assert new_claims and all(change.relation == "unresolved" for change in new_claims)
    # `possible_new` is adopted content but never a public catalyst. This turn extracted only the
    # new member, and its unresolved comparison did not establish support for the old claim.
    public = sql("SELECT kind FROM news_trade_events WHERE source_revision = %s", (head.content_revision,))
    assert public == []
    assert sql("SELECT count(*) AS n FROM news_semantic_observations WHERE event_id = %s", (EVENT,))[0]["n"] == 2


def test_a_reclaimed_slow_turn_cannot_regress_the_head_or_settle_the_new_owner() -> None:
    from tracefold.news.storage.errors import SemanticLeaseLost

    clock = Clock(STAMP + 60_000)
    store = PgSemanticStore(ThreadedDb(), clock=clock)
    seed_event()
    stale = asyncio.run(store.claim_semantic_work(EVENT, lease_ms=10))
    assert stale is not None
    add_member_evidence(EVENT, "it-member", "Agency adds a pharmaceutical exemption.", now_ms=clock.now_ms)
    clock.now_ms += 11
    subject = NewsAgent(store, StubAnalyzer(), program_identity="p", clock=clock)
    assert asyncio.run(run_agent(subject, EVENT)) == "adopted"
    head = asyncio.run(store.head(EVENT))
    with pytest.raises(SemanticLeaseLost):
        asyncio.run(subject.process(stale))
    assert asyncio.run(store.head(EVENT)) == head
    assert work(EVENT)["done_revision"] == 2


# ------------------------------------------------------------------ input and repair


def test_input_recalls_related_heads_and_prepares_read_targets_from_stored_material() -> None:
    clock = Clock(STAMP + 60_000)
    db = ThreadedDb()
    store = PgSemanticStore(db, clock=clock)
    seed_event("ev-related", text="Agency previously proposed a 10% tariff on $BTC miners.", fingerprint="fp-a")
    assert asyncio.run(run_agent(NewsAgent(store, StubAnalyzer(), program_identity="p", clock=clock), "ev-related"))
    seed_event(EVENT, text="Agency orders a 25% tariff on $BTC miners.", fingerprint="fp-b")
    # Both leaders came from the same source URL: the existing explicit-origin recall channel links them.
    sql("UPDATE news_items SET provider_params_available_at_ms = %s", (STAMP,))
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            conn.execute("UPDATE news_events SET grounded_assets = '[\"BTC\"]'::jsonb, asset_class = 'crypto'")
            # The Gate-grounded asset is part of the evidence snapshot the input is frozen from.
            repositories_for_connection(conn).news.append_evidence_snapshot(event_id=EVENT, now_ms=STAMP + 1)
    finally:
        conn.close()

    source = asyncio.run(store.input_for(EVENT))

    related = asyncio.run(store.head("ev-related"))
    assert related is not None
    assert {row.event_id for row in source.prior} == {"ev-related"}
    assert {row.claim.ref for row in source.prior} == {claim.ref for claim in related.claims}
    assert [target.ref for target in source.read_targets] == ["news_item:it-ev-related"]
    assert source.read_targets[0].action == "load_prior_statement"
    # A cashtag supplies a retrieval feature, not proof that the referenced asset is this claim's actor.
    assert source.identity_hints == ()
    read = asyncio.run(PgSourceReader(db).read(source.read_targets[0]))
    assert [item.text for item in read] == ["Agency previously proposed a 10% tariff on $BTC miners."]
    unknown = ReadTarget(ref="https://example.org/anything", action="read_current_artifact", description="x")
    assert asyncio.run(PgSourceReader(db).read(unknown)) == ()


def test_the_janitor_re_wakes_stale_pending_work_and_drops_expired_judgment_answers(monkeypatch) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.admission.now_ms", lambda: STAMP + 60_000)
    monkeypatch.setattr("tracefold.news.pipeline.maintenance.now_ms", lambda: STAMP + 60_000)
    monkeypatch.setattr(
        "tracefold.news.pipeline.maintenance.PgSemanticStore",
        lambda db: PgSemanticStore(db, clock=Clock(STAMP + 60_000)),
    )
    seed_event()
    old = STAMP - JUDGMENT_CACHE_RETENTION_MS - 1
    sql(
        "INSERT INTO news_judgment_cache (cache_key, answer, created_at_ms) VALUES"
        " ('judgment:old', '{}'::jsonb, %s), ('judgment:fresh', '{}'::jsonb, %s)",
        (old, STAMP),
    )
    sql("UPDATE news_semantic_work SET published_at_ms = %s", (STAMP - 60_000,))
    bus = RecordingBus()
    janitor = JanitorLoop(db=ThreadedDb(), cold_db=ThreadedDb(), bus=bus)

    asyncio.run(janitor.turn())

    assert bus.wakes() == [f"event:{EVENT}:1"]
    assert bus.published[0].routing_key == "event.general.normal"
    assert work(EVENT)["published_at_ms"] > STAMP
    assert [row["cache_key"] for row in sql("SELECT cache_key FROM news_judgment_cache")] == ["judgment:fresh"]
    # A freshly woken revision is not woken again inside the stale window.
    asyncio.run(janitor.repair_semantic_wakes())
    assert len(bus.wakes()) == 1


# ------------------------------------------------------------------ #742 admission (U1, U3, W6)


def test_a_live_report_is_not_absorbed_by_a_recovery_event() -> None:
    bus = RecordingBus()
    deduper = DeduperConsumer(bus=bus, db=ThreadedDb(), watchlist_symbols=frozenset({"BTC"}))
    text = f"{TITLE} effective October 1"
    asyncio.run(deduper.handle(raw(7501, text, stamp=STAMP, ingest_mode="recovery")))
    recovered = event_of(7501)
    assert bus.wakes() == []
    asyncio.run(deduper.handle(raw(7502, text, stamp=STAMP + 1_000)))
    live = event_of(7502)
    assert live != recovered
    assert work(live)["wanted_revision"] == 1 and bus.wakes() == [f"event:{live}:1"]
    # A later recovered copy may still join the recovery Event as history.
    asyncio.run(deduper.handle(raw(7503, text, stamp=STAMP + 2_000, ingest_mode="recovery")))
    assert event_of(7503) in {recovered, live}


def test_a_revised_headline_of_one_record_stays_one_event() -> None:
    bus = RecordingBus()
    deduper = DeduperConsumer(bus=bus, db=ThreadedDb(), watchlist_symbols=frozenset({"BTC"}))
    asyncio.run(deduper.handle(raw(7601, "Fed cuts rates by 25 bps in surprise move", stamp=STAMP)))
    event_id = event_of(7601)
    asyncio.run(deduper.handle(raw(7601, "Fed cuts rates by 50 bps in surprise move", stamp=STAMP + 10_000)))
    assert event_of(7601) == event_id
    assert sql("SELECT count(*) AS n FROM news_events")[0]["n"] == 1
    assert work(event_id)["wanted_revision"] == 2


def test_the_same_record_resent_under_another_strategy_is_no_semantic_work() -> None:
    bus = RecordingBus()
    deduper = DeduperConsumer(bus=bus, db=ThreadedDb(), watchlist_symbols=frozenset({"BTC"}))
    asyncio.run(deduper.handle(raw(7701, TITLE, stamp=STAMP)))
    event_id = event_of(7701)
    asyncio.run(deduper.handle(raw(7701, TITLE, stamp=STAMP + 5_000, strategy_id="1030")))
    strategies = sql("SELECT provider_metadata FROM news_items WHERE source_item_key = '7701'")[0]
    assert {str(row["id"]) for row in strategies["provider_metadata"]["strategies"]} == {"1018", "1030"}
    assert work(event_id)["wanted_revision"] == 1
    assert bus.wakes() == [f"event:{event_id}:1"]


# ------------------------------------------------------------------ #742 failure isolation (S3, U2)


class PoisonExtractor:
    """Every claim quotes text its source does not contain unless the source is the new member."""

    identity = "poison-extractor-test"

    def __init__(self) -> None:
        self.seen: list[tuple[str, ...]] = []

    async def extract(self, source: FrozenInput) -> Extraction:
        self.seen.append(tuple(item.text for item in source.evidence))
        claims = tuple(
            DraftClaim(
                slot=f"s{index}",
                statement=item.text,
                fields=ClaimFields(subject="Agency", action="orders tariff", mode="decision", phase="ordered"),
                citations=(
                    Citation(
                        evidence_ref=item.ref,
                        quote=item.text if "exemption" in item.text else "text the source never said",
                    ),
                ),
            )
            for index, item in enumerate(source.evidence)
        )
        return Extraction(claims=claims)


def _worker(db: ThreadedDb, store: PgSemanticStore, analyzer: SemanticAnalyzer, clock: Clock) -> SemanticWorker:
    return SemanticWorker(
        bus=RecordingBus(),
        db=db,
        store=store,
        agent=NewsAgent(store, analyzer, program_identity="program-test", clock=clock),
        concurrency=1,
        circuit_failures=10,
        circuit_open_seconds=60.0,
        clock=clock,
    )


def _wake(event_id: str) -> BusMessage:
    return BusMessage("event", f"event:{event_id}:1", "event.general.normal", {"event_id": event_id}, "t", STAMP)


def test_failed_material_is_quarantined_and_a_later_member_is_still_adopted() -> None:
    clock = Clock(STAMP + 60_000)
    db = ThreadedDb()
    store = PgSemanticStore(db, clock=clock)
    extractor = PoisonExtractor()
    analyzer = SemanticAnalyzer(extractor, NewsJudgments(generated=RelationBackend(), cache=PgJudgmentCache(db)))
    worker = _worker(db, store, analyzer, clock)
    seed_event()
    asyncio.run(worker.handle(_wake(EVENT)))
    failed = work(EVENT)
    # The contract fault failed on its first attempt, which is what the row now says.
    assert (failed["last_outcome"], failed["last_error_code"], failed["attempts"]) == (
        "failed",
        "news_citation_not_in_frozen_source",
        1,
    )
    assert len(failed["failed_read_refs"]) == 1 and asyncio.run(store.head(EVENT)) is None

    add_member_evidence(EVENT, "it-member", "Agency adds a pharmaceutical exemption.", now_ms=clock.now_ms)
    asyncio.run(worker.handle(_wake(EVENT)))
    assert extractor.seen[-1] == ("Agency adds a pharmaceutical exemption.",)
    head = asyncio.run(store.head(EVENT))
    assert head is not None and [claim.statement for claim in head.claims] == [
        "Agency adds a pharmaceutical exemption."
    ]
    row = work(EVENT)
    assert (row["done_revision"], row["last_outcome"]) == (2, "adopted")
    assert row["failed_read_refs"] == failed["failed_read_refs"]

    # The quarantined read is listed and can be reanalysed by exact revision.
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            news = repositories_for_connection(conn).news
            listing = news.semantic_work.reanalysis_scope_list(
                event_id=EVENT, now_ms=clock.now_ms, input=news.semantic_input
            )
            assert [scope["failed"] for scope in listing["scopes"]] == [True, False]
    finally:
        conn.close()


def test_a_failed_revision_can_be_reanalysed_by_its_exact_revision() -> None:
    clock = Clock(STAMP + 60_000)
    db = ThreadedDb()
    store = PgSemanticStore(db, clock=clock)
    analyzer = SemanticAnalyzer(
        PoisonExtractor(), NewsJudgments(generated=RelationBackend(), cache=PgJudgmentCache(db))
    )
    seed_event()
    asyncio.run(_worker(db, store, analyzer, clock).handle(_wake(EVENT)))
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            news = repositories_for_connection(conn).news
            listing = news.semantic_work.reanalysis_scope_list(
                event_id=EVENT, now_ms=clock.now_ms, input=news.semantic_input
            )
            assert listing["failed"] and listing["last_error_code"] == "news_citation_not_in_frozen_source"
            revision = news.semantic_work.request_reanalysis(
                event_id=EVENT,
                expected_wanted_revision=1,
                expected_head_revision=None,
                read_ref=listing["scopes"][0]["read_ref"],
                reason="operator checked the quote",
                now_ms=clock.now_ms,
                input=news.semantic_input,
            )
    finally:
        conn.close()
    assert revision == 2
    source = asyncio.run(store.input_for(EVENT))
    assert [item.text for item in source.evidence] == [TEXT]


def test_a_revision_failed_by_unusable_claims_is_rerun_by_the_retry_work_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #742 PR-6: a revision whose every generated claim was unusable failed on its first attempt and
    # quarantined its read. After the fix is deployed the operator re-runs that exact revision with
    # `tracefold news retry-work --event <id> --kind semantic --revision <wanted>`; the same material is
    # read again and adopted.
    clock = Clock(STAMP + 60_000)
    db = ThreadedDb()
    store = PgSemanticStore(db, clock=clock)
    seed_event()
    claim = {
        "slot": "a",
        "statement": TEXT,
        "fields": {"subject": "Agency", "action": "orders tariff", "mode": "decision", "phase": "ordered"},
        "citations": [{"evidence_ref": "e1", "quote": TEXT}],
    }

    def analyzer(reply: dict[str, Any]) -> SemanticAnalyzer:
        route = ScriptedLM([{"result": {"claims": [reply]}}])
        return SemanticAnalyzer(
            DspyExtractor(lambda: route, model_identity="fixture", topics={}),
            NewsJudgments(generated=RelationBackend(), cache=PgJudgmentCache(db)),
        )

    asyncio.run(_worker(db, store, analyzer({**claim, "citations": []}), clock).handle(_wake(EVENT)))
    failed = work(EVENT)
    assert (failed["last_outcome"], failed["last_error_code"], failed["attempts"]) == (
        "failed",
        "news_claim_schema_invalid",
        1,
    )
    assert len(failed["failed_read_refs"]) == 1

    conn = connect_postgres_test(read_only=False)

    @contextmanager
    def test_database(*_args: Any, **_kwargs: Any) -> Iterator[Any]:
        yield conn

    monkeypatch.setattr(news_diagnostics, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", test_database)
    monkeypatch.setattr("tracefold.news.bus.now_ms", clock)
    command = ["news", "retry-work", "--event", EVENT, "--kind", "semantic", "--revision", "1"]
    try:
        assert news_diagnostics.handle_news(build_parser().parse_args(command))[0] == 0
        # The exact revision is reopened once; a second run finds nothing failed.
        assert news_diagnostics.handle_news(build_parser().parse_args(command))[1]["status"] == (
            "not_failed_or_version_changed"
        )
    finally:
        conn.close()
    reopened = work(EVENT)
    assert (reopened["last_outcome"], reopened["attempts"], reopened["failed_read_refs"]) == (None, 0, [])

    asyncio.run(_worker(db, store, analyzer(claim), clock).handle(_wake(EVENT)))
    head = asyncio.run(store.head(EVENT))
    assert head is not None and [claim.statement for claim in head.claims] == [TEXT]
    assert (work(EVENT)["done_revision"], work(EVENT)["last_outcome"]) == (1, "adopted")


def test_an_input_that_cannot_be_built_fails_only_its_own_work() -> None:
    clock = Clock(STAMP + 60_000)
    db = ThreadedDb()
    store = PgSemanticStore(db, clock=clock)
    analyzer = SemanticAnalyzer(
        LeaderExtractor(), NewsJudgments(generated=RelationBackend(), cache=PgJudgmentCache(db))
    )
    worker = _worker(db, store, analyzer, clock)
    seed_event("ev-broken", text="Agency orders a 10% tariff on copper.", fingerprint="fp-broken")
    seed_event()
    # A reanalysis of a read that no longer exists cannot form a frozen input.
    sql("UPDATE news_semantic_work SET reanalysis_read_ref = 'news_read:gone' WHERE event_id = 'ev-broken'")
    asyncio.run(worker.handle(_wake("ev-broken")))  # the consumer does not raise
    broken = work("ev-broken")
    assert (broken["last_outcome"], broken["last_error_code"], broken["lease_token"]) == (
        "failed",
        "news_reanalysis_read_scope_changed",
        None,
    )
    assert asyncio.run(store.pending_semantic_events(10)) == (EVENT,)
    asyncio.run(worker.handle(_wake(EVENT)))
    assert work(EVENT)["last_outcome"] == "adopted"


# ------------------------------------------------------------------ #742 W1: notification obligations


def _notification_work() -> dict[str, Any]:
    return sql("SELECT state, attempts, content_revision FROM news_notification_work WHERE event_id = %s", (EVENT,))[0]


def test_only_substantive_changes_open_notification_work_while_unfinished_work_follows_the_head() -> None:
    clock = Clock(STAMP + 60_000)
    store = PgSemanticStore(ThreadedDb(), clock=clock)
    seed_event()
    assert asyncio.run(run_agent(NewsAgent(store, StubAnalyzer(), program_identity="p", clock=clock), EVENT)) == (
        "adopted"
    )
    first = asyncio.run(store.head(EVENT))
    assert first is not None
    sql("UPDATE news_notification_work SET attempts = 1 WHERE event_id = %s", (EVENT,))

    def restate(source: FrozenInput) -> Extraction:
        return Extraction(
            claims=(draft(source.evidence[0]),),
            relations=(RelationDraft(slot="a", previous_ref=first.claims[0].ref, relation="equivalent"),),
            supports=(SupportDraft(slot="a", evidence_ref=source.evidence[0].ref, relation="supports"),),
        )

    # A new source for the adopted claim: the unfinished work moves to the new head, keeping its budget.
    add_member_evidence(EVENT, "it-copy", "Agency confirms the same tariff order.", now_ms=clock.now_ms)
    assert asyncio.run(
        run_agent(NewsAgent(store, StubAnalyzer(restate), program_identity="p", clock=clock), EVENT)
    ) == ("adopted")
    second = asyncio.run(store.head(EVENT))
    assert second is not None and {change.kind for change in second.changes} == {"evidence_change"}
    assert _notification_work() == {"state": "pending", "attempts": 1, "content_revision": second.content_revision}

    # A new fact reopens it with a fresh budget.
    add_member_evidence(EVENT, "it-new", "Agency adds a pharmaceutical exemption.", now_ms=clock.now_ms)
    analyzer = StubAnalyzer(lambda source: Extraction(claims=(draft(source.evidence[0], action="adds exemption"),)))
    assert asyncio.run(run_agent(NewsAgent(store, analyzer, program_identity="p", clock=clock), EVENT)) == "adopted"
    latest = asyncio.run(store.head(EVENT))
    assert latest is not None
    assert _notification_work() == {"state": "pending", "attempts": 0, "content_revision": latest.content_revision}
