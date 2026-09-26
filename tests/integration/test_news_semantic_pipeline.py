"""Admission -> semantic work -> News Agent -> adoption over real PostgreSQL (#706).

The consumers are the production classes over the production News repository; the broker is a recording
fake and the analyzer boundary is scripted, so every assertion is about durable rows: evidence snapshots,
item revisions, semantic work, observations, adopted heads and the public outbox.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.integration.test_news_event_update_store import (
    EVENT,
    STAMP,
    Clock,
    StubAnalyzer,
    ThreadedDb,
    draft,
    seed_event,
    sql,
)
from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.bus import RK_RAW_LIVE, BusMessage
from tracefold.news.pipeline.admission import DeduperConsumer, append_admission_evidence
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.pipeline.semantic import SemanticWorker
from tracefold.news.storage.event_update_store import PgJudgmentCache, PgNewsStore, PgSourceReader
from tracefold.news.storage.event_updates import JUDGMENT_CACHE_RETENTION_MS, SEMANTIC_ATTEMPTS_MAX
from tracefold.news.storage.events import prepare_evidence_snapshot
from tracefold.news.updates.contracts import (
    Citation,
    ClaimFields,
    DraftClaim,
    Extraction,
    FrozenInput,
    ReadTarget,
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

TITLE = "Agency orders 25% tariff on steel imports from Canada"


class RecordingBus:
    prefix = ""
    last_publish_failure = None

    def __init__(self) -> None:
        self.published: list[BusMessage] = []

    async def broker_snapshot(self) -> dict[str, Any]:
        return {}

    async def publish(self, message: BusMessage) -> None:
        self.published.append(message)

    def wakes(self) -> list[str]:
        return [message.message_id for message in self.published if message.kind == "event"]


def raw(
    record: int,
    text: str,
    *,
    stamp: int,
    link: str | None = None,
    source: str = "Reuters",
    ingest_mode: str = "live",
) -> BusMessage:
    params = {
        "id": record,
        "text": text,
        "link": link or f"https://example.org/{record}",
        "source": source,
        "engineType": "news",
        "ts": stamp,
        "coins": [{"symbol": "BTC", "grade": "A"}],
        "strategy": {"id": 1018, "name": "News Score > 70", "engine_type": "news", "source_type": "news"},
        "aiRating": {"score": 90},
    }
    return BusMessage(
        kind="raw",
        message_id=f"raw:{record}:{stamp}",
        routing_key=RK_RAW_LIVE.format(strategy_id="1018"),
        payload={"params": params, "strategy_id": "1018", "ingest_mode": ingest_mode, "observed_at_ms": stamp},
        trace_id=f"trace-{record}",
        occurred_at_ms=stamp,
    )


def event_of(record: int) -> str:
    rows = sql(
        """
        SELECT DISTINCT m.event_id FROM news_event_members m JOIN news_items i ON i.item_id = m.item_id
         WHERE i.source_item_key = %s
        """,
        (str(record),),
    )
    assert len(rows) == 1, rows
    return str(rows[0]["event_id"])


def work(event_id: str) -> dict[str, Any]:
    return sql("SELECT * FROM news_semantic_work WHERE event_id = %s", (event_id,))[0]


def snapshots(event_id: str) -> list[dict[str, Any]]:
    return sql(
        "SELECT evidence_version, snapshot FROM news_event_evidence_snapshots WHERE event_id = %s"
        " ORDER BY evidence_version",
        (event_id,),
    )


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
    store = PgNewsStore(ThreadedDb(), clock=Clock(STAMP + 6_000))
    assert asyncio.run(NewsAgent(store, StubAnalyzer(), program_identity="p").process(event_id)) == "adopted"
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

    source = asyncio.run(PgNewsStore(ThreadedDb(), clock=Clock(STAMP + 20_000)).input_for(event_id))
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
    store = PgNewsStore(ThreadedDb(), clock=clock)
    agent = NewsAgent(store, StubAnalyzer(), program_identity="p", clock=clock)
    assert asyncio.run(agent.process(event_id)) == "adopted"
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
    store = PgNewsStore(ThreadedDb(), clock=clock)
    seed_event()
    first_analyzer = StubAnalyzer()
    assert asyncio.run(NewsAgent(store, first_analyzer, program_identity="p", clock=clock).process(EVENT)) == "adopted"
    first = asyncio.run(store.head(EVENT))
    assert first is not None
    original = first.claims[0]

    add_member_evidence(EVENT, "it-member-a", "Agency announces a medicine exemption.", now_ms=clock.now_ms)
    add_member_evidence(EVENT, "it-member-b", "Agency confirms the start date.", now_ms=clock.now_ms + 1)
    source = asyncio.run(store.input_for(EVENT))
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
    assert asyncio.run(NewsAgent(store, analyzer, program_identity="p", clock=clock).process(EVENT)) == "adopted"
    adopted = asyncio.run(store.head(EVENT))
    assert adopted is not None and adopted.input_revision == 3
    assert analyzer.extract_calls == 1
    assert len(adopted.claims) == 3
    assert adopted.claims[0] == original
    assert {item.ref for item in adopted.evidence} == {item.ref for item in first.evidence} | {
        item.ref for item in source.evidence
    }


def test_no_new_source_identity_settles_the_revision_without_reextracting() -> None:
    clock = Clock(STAMP + 60_000)
    store = PgNewsStore(ThreadedDb(), clock=clock)
    seed_event()
    analyzer = StubAnalyzer()
    agent = NewsAgent(store, analyzer, program_identity="p", clock=clock)
    assert asyncio.run(agent.process(EVENT)) == "adopted"
    first = asyncio.run(store.head(EVENT))
    assert first is not None
    # A work revision can be requested for metadata whose source identity is already adopted.
    sql("UPDATE news_semantic_work SET wanted_revision = 2 WHERE event_id = %s", (EVENT,))
    source = asyncio.run(store.input_for(EVENT))
    assert source.evidence == ()
    assert asyncio.run(agent.process(EVENT)) == "unchanged"
    assert analyzer.extract_calls == 1
    assert work(EVENT)["done_revision"] == 2


def test_analyzed_material_without_a_claim_is_not_reextracted_on_the_next_revision() -> None:
    clock = Clock(STAMP + 60_000)
    store = PgNewsStore(ThreadedDb(), clock=clock)
    seed_event()
    first_agent = NewsAgent(store, StubAnalyzer(), program_identity="p", clock=clock)
    assert asyncio.run(first_agent.process(EVENT)) == "adopted"
    first = asyncio.run(store.head(EVENT))
    assert first is not None

    add_member_evidence(EVENT, "it-member-a", "Agency repeats background context.", now_ms=clock.now_ms)
    analyzer = StubAnalyzer(lambda _source: Extraction(claims=()))
    agent = NewsAgent(store, analyzer, program_identity="p", clock=clock)
    assert asyncio.run(agent.process(EVENT)) == "unchanged"
    assert work(EVENT)["done_revision"] == 2
    assert asyncio.run(store.head(EVENT)) == first

    add_member_evidence(EVENT, "it-member-b", "Agency adds a pharmaceutical exemption.", now_ms=clock.now_ms)
    source = asyncio.run(store.input_for(EVENT))
    assert [row.text for row in source.evidence] == ["Agency adds a pharmaceutical exemption."]
    assert asyncio.run(agent.process(EVENT)) == "unchanged"
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

    async def extract(self, source: FrozenInput, *, extract_only: bool) -> Extraction:
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


def add_member_evidence(event_id: str, item_id: str, text: str, *, now_ms: int) -> None:
    """A second member joins the Event the way admission records it: evidence and semantic work together."""

    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO news_items (
                  item_id, source_id, source_item_key, title, raw_first_line, description, canonical_url,
                  reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
                  first_ingest_mode, trace_id, created_at_ms, updated_at_ms, source_artifact_id,
                  evidence_text, evidence_text_sha256
                ) VALUES (%(item)s, 'opennews', %(item)s, %(text)s, %(text)s, '', 'https://example.org/m',
                          'Wire', %(at)s, %(at)s, '{}'::jsonb, '[]'::jsonb, 'live', 'trace', %(at)s, %(at)s,
                          %(item)s, %(text)s, %(item)s)
                """,
                {"item": item_id, "text": text, "at": now_ms},
            )
            repos = repositories_for_connection(conn)
            repos.news.add_member(
                event_id=event_id,
                item_id=item_id,
                joined_at_ms=now_ms,
                match_kind="near",
                jaccard_estimate=0.8,
                provider_score=None,
                fact_id=f"fact-{item_id}",
                fact_text=text,
                now_ms=now_ms,
            )
            material = repos.news.evidence_snapshot_material(event_id=event_id, focus_item_id=None)
            snapshot = prepare_evidence_snapshot(material, event_id=event_id, now_ms=now_ms, focus_fact=None)
            assert append_admission_evidence(repos, snapshot, ingest_mode="live", now_ms=now_ms) is not None
    finally:
        conn.close()


def test_provider_failures_defer_and_only_the_last_attempt_adopts_an_unresolved_comparison() -> None:
    clock = Clock(STAMP + 60_000)
    db = ThreadedDb()
    store = PgNewsStore(db, clock=clock)
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
        program_identity="program-test",
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


def test_a_slow_turn_behind_a_newer_adoption_records_its_observation_and_never_regresses_the_head() -> None:
    clock = Clock(STAMP + 60_000)
    db = ThreadedDb()
    store = PgNewsStore(db, clock=clock)
    seed_event()
    stale = asyncio.run(store.input_for(EVENT))
    add_member_evidence(EVENT, "it-member", "Agency adds a pharmaceutical exemption.", now_ms=clock.now_ms)
    assert asyncio.run(NewsAgent(store, StubAnalyzer(), program_identity="p", clock=clock).process(EVENT)) == "adopted"
    head = asyncio.run(store.head(EVENT))
    assert head is not None and head.input_revision == 2

    class StaleInput(PgNewsStore):
        async def input_for(self, event_id: str) -> FrozenInput:
            return stale

    slow = NewsAgent(StaleInput(db, clock=clock), StubAnalyzer(), program_identity="p", clock=clock)
    assert asyncio.run(slow.process(EVENT)) == "newer_head"
    assert asyncio.run(store.head(EVENT)) == head
    assert work(EVENT)["done_revision"] == 2


# ------------------------------------------------------------------ input and repair


def test_input_recalls_related_heads_and_prepares_read_targets_from_stored_material() -> None:
    clock = Clock(STAMP + 60_000)
    db = ThreadedDb()
    store = PgNewsStore(db, clock=clock)
    seed_event("ev-related", text="Agency previously proposed a 10% tariff on $BTC miners.", fingerprint="fp-a")
    assert asyncio.run(NewsAgent(store, StubAnalyzer(), program_identity="p", clock=clock).process("ev-related"))
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
    assert [(hint.key, hint.value, hint.surface) for hint in source.identity_hints] == [("subject_id", "BTC", "$BTC")]
    read = asyncio.run(PgSourceReader(db).read(source.read_targets[0]))
    assert [item.text for item in read] == ["Agency previously proposed a 10% tariff on $BTC miners."]
    unknown = ReadTarget(ref="https://example.org/anything", action="read_current_artifact", description="x")
    assert asyncio.run(PgSourceReader(db).read(unknown)) == ()


def test_the_janitor_re_wakes_stale_pending_work_and_drops_expired_judgment_answers() -> None:
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
