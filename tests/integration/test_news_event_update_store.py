"""The EventUpdate `NewsStore` over real PostgreSQL: every port guarantee, races and replays (#706)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test, seed_current_news_evidence
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.storage.decisions import legacy_intent_id
from tracefold.news.storage.event_update_store import PgJudgmentCache, PgNewsStore
from tracefold.news.storage.event_updates import EventUpdateConflict, IntentLeaseLost, frozen_input
from tracefold.news.updates.contracts import (
    Citation,
    ClaimFields,
    DraftClaim,
    EventUpdate,
    Evidence,
    Extraction,
    FrozenInput,
    PriorClaim,
    ReadTarget,
    RelationDraft,
    Source,
    SupportDraft,
)
from tracefold.news.updates.identity import digest, identity
from tracefold.news.updates.judgment import Answer, BatchResult, NewsJudgments, ProviderUnavailable, Question, Task
from tracefold.news.updates.notification import (
    CardCopy,
    CardLine,
    ClaimDecision,
    FrozenCard,
    NotificationPlan,
    NotificationPlanner,
)
from tracefold.news.updates.ports import SemanticObservation, SendOutcome
from tracefold.news.updates.public import public_updates
from tracefold.news.updates.semantics import assemble_update
from tracefold.news.updates.service import NewsAgent, Notifications

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

STAMP = 1_790_405_000_000
EVENT = "ev-tariff"
TEXT = "Agency orders a 25% tariff on steel imports effective October 1."


class Clock:
    def __init__(self, now_ms: int = STAMP + 60_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class ThreadedDb:
    """The News database port with one fresh connection and transaction per call, in a worker thread.

    Two coroutines therefore hold two real PostgreSQL transactions at once.
    """

    def __init__(self) -> None:
        self.names: list[str] = []

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        return await asyncio.to_thread(self._run, name, fn)

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        return await asyncio.to_thread(self._run, name, fn)

    def _run(self, name: str, fn: Callable[[Any], Any]) -> Any:
        self.names.append(name)
        conn = connect_postgres_test(read_only=False)
        try:
            repos = repositories_for_connection(conn)
            with repos.transaction():
                return fn(repos)
        finally:
            conn.close()


def sql(statement: str, params: Any = None) -> list[dict[str, Any]]:
    conn = connect_postgres_test(read_only=False)
    try:
        cursor = conn.execute(statement, params)
        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        conn.commit()
        return rows
    finally:
        conn.close()


def seed_event(
    event_id: str = EVENT,
    *,
    text: str = TEXT,
    title: str = "Agency orders steel tariff",
    at_ms: int = STAMP,
    fingerprint: str = "fp-tariff",
) -> None:
    item_id = f"it-{event_id}"
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
                ) VALUES (%(item)s, 'opennews', %(item)s, %(title)s, %(title)s, '', 'https://www.reuters.com/a',
                          'Reuters', %(at)s, %(at)s, '{}'::jsonb, '[]'::jsonb, 'live', 'trace', %(at)s, %(at)s,
                          %(item)s, %(text)s, %(sha)s)
                """,
                {"item": item_id, "title": title, "at": at_ms, "text": text, "sha": digest(text)},
            )
            conn.execute(
                """
                INSERT INTO news_events (
                  event_id, leader_item_id, dedupe_family, comparison_fingerprint, comparison_title,
                  leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, admission, ingest_mode,
                  trace_id, created_at_ms, updated_at_ms, focus_fact_id, focus_fact_text,
                  focus_fact_context, focus_fact_method, focus_span_start, focus_span_end, event_kind
                ) VALUES (%(event)s, %(item)s, 'general', %(fp)s, %(title)s, %(title)s, %(at)s, %(at)s,
                          %(expires)s, 'candidate', 'live', 'trace', %(at)s, %(at)s, %(fact)s, %(title)s, '',
                          'whole_item', 0, 10, 'news')
                """,
                {
                    "event": event_id,
                    "item": item_id,
                    "fp": fingerprint,
                    "title": title,
                    "at": at_ms,
                    "expires": at_ms + 86_400_000,
                    "fact": f"fact-{item_id}",
                },
            )
            conn.execute(
                """
                INSERT INTO news_event_members (event_id, item_id, joined_at_ms, match_kind, fact_id, fact_text)
                VALUES (%s, %s, %s, 'leader', %s, %s)
                """,
                (event_id, item_id, at_ms, f"fact-{item_id}", title),
            )
            seed_current_news_evidence(conn)
            repositories_for_connection(conn).news.request_semantic_revision(
                event_id=event_id, lineage_id=f"lineage-{event_id}", now_ms=at_ms
            )
    finally:
        conn.close()


def draft(source: Evidence, slot: str = "a", *, action: str = "orders tariff", quote: str | None = None) -> DraftClaim:
    return DraftClaim(
        slot=slot,
        statement=quote or source.text,
        fields=ClaimFields.model_validate(
            {
                "subject": "Agency",
                "action": action,
                "mode": "decision",
                "phase": "ordered",
                "content_kind": "official_measure",
                "assets": [{"symbol": "X", "market_type": "equity", "role": "primary"}],
            }
        ),
        citations=(Citation(evidence_ref=source.ref, quote=quote or source.text),),
    )


def extraction_for(source: FrozenInput) -> Extraction:
    evidence = source.evidence[0]
    return Extraction(
        claims=(draft(evidence),),
        supports=(SupportDraft(slot="a", evidence_ref=evidence.ref, relation="supports"),),
    )


class StubAnalyzer:
    """The analyzer boundary with a fixed extraction; no model."""

    identity = "stub-analyzer-v1"
    judgments: Any = None

    def __init__(self, build: Callable[[FrozenInput], Extraction] = extraction_for) -> None:
        self.build = build
        self.extract_calls = 0

    async def extract(self, source: FrozenInput, budget: Any) -> Extraction:
        self.extract_calls += 1
        return self.build(source)

    async def understand(
        self, source: FrozenInput, extracted: Extraction, budget: Any, *, rebase_only: bool = False
    ) -> Extraction:
        return extracted


class TaskBackend:
    def __init__(self, values: dict[Task, str | bool] | None = None) -> None:
        self.identity = "generated-test"
        self.values = values or {}
        self.calls: list[Task] = []

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        self.calls.append(task)
        if task not in self.values:
            raise ProviderUnavailable("controlled provider failure")
        return BatchResult(
            answers=tuple(
                Answer(item_id=item.item_id, value=self.values[task], backend=self.identity) for item in items
            )
        )


class Composer:
    def __init__(self) -> None:
        self.calls = 0

    async def compose(self, claims: tuple[Any, ...]) -> CardCopy:
        self.calls += 1
        return CardCopy(
            headline_zh="机构对钢铁进口加征关税",
            lines=tuple(CardLine(claim_ref=claim.ref, text_zh="机构宣布加征百分之二十五关税") for claim in claims),
        )


class Sender:
    def __init__(self, *outcomes: str) -> None:
        self.outcomes = list(outcomes) or ["sent"]
        self.cards: list[FrozenCard] = []

    async def send(self, card: FrozenCard, *, channel: str) -> SendOutcome:
        self.cards.append(card)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if outcome == "raise":
            raise RuntimeError("provider connection dropped")
        if outcome == "not_sent":
            return SendOutcome(
                state="not_sent", payload_sha256=card.payload_sha256, error_code="rate_limited", retryable=True
            )
        return SendOutcome(state="sent", payload_sha256=card.payload_sha256, message_id=str(40 + len(self.cards)))


def store(clock: Clock | None = None, **kwargs: Any) -> tuple[PgNewsStore, ThreadedDb, Clock]:
    db = ThreadedDb()
    clock = clock or Clock()
    return PgNewsStore(db, clock=clock, **kwargs), db, clock


def agent(pg: PgNewsStore, clock: Clock, analyzer: StubAnalyzer | None = None) -> NewsAgent:
    return NewsAgent(pg, analyzer or StubAnalyzer(), program_identity="program-test", clock=clock)  # type: ignore[arg-type]


def notifications(pg: PgNewsStore, clock: Clock, sender: Sender, composer: Composer | None = None) -> Notifications:
    judgments = NewsJudgments(generated=TaskBackend({"coverage": "full"}), cache=PgJudgmentCache(pg.db))
    return Notifications(pg, NotificationPlanner(judgments), composer or Composer(), sender, clock=clock)


def adopted_head(pg: PgNewsStore, clock: Clock) -> EventUpdate:
    seed_event()
    assert asyncio.run(agent(pg, clock).process(EVENT)) == "adopted"
    head = asyncio.run(pg.head(EVENT))
    assert head is not None
    return head


def evidence(text: str, *, revision: str = "2", publisher: str = "wire") -> Evidence:
    return Evidence.issue(
        text,
        Source(
            publisher_id=publisher,
            artifact_id=f"{publisher}-a",
            artifact_revision=revision,
            first_available_at_ms=STAMP,
        ),
    )


async def adopt_next(
    pg: PgNewsStore,
    head: EventUpdate | None,
    source: FrozenInput,
    extracted: Extraction,
    *,
    expected_head_ref: str | None = None,
    work_id: str = "work-next",
) -> tuple[bool, EventUpdate]:
    observation = SemanticObservation(
        result_id=identity("semantic_result", work_id, source.prior, extracted),
        work_id=work_id,
        event_id=source.event_id,
        input_revision=source.revision,
        input_sha256=source.evidence_sha,
        program_identity="program-test",
        completed_at_ms=STAMP + 100,
        understanding=extracted,
    )
    await pg.save_observation(observation)
    update = assemble_update(source, extracted, head, adopted_at_ms=STAMP + 100)
    assert update is not None
    adopted = await pg.atomic_adopt(
        expected_head_ref=expected_head_ref if head is None else head.ref,
        observation=observation,
        update=update,
        public=public_updates(update, semantic_completed_at_ms=observation.completed_at_ms),
    )
    return adopted, update


def notify_plan(head: EventUpdate, revision: str, *, deferred: tuple[str, ...] = ()) -> NotificationPlan:
    decisions = tuple(
        ClaimDecision(claim_ref=claim.ref, decision="deferred", reason="send_outcome_unresolved")
        if claim.ref in deferred
        else ClaimDecision(claim_ref=claim.ref, decision="notify", reason="actionable_content")
        for claim in head.claims
    )
    return NotificationPlan(
        action="notify",
        reason="uncovered_claims",
        update_ref=head.ref,
        claim_decisions=decisions,
        channel="news",
        reader_revision=revision,
    )


def trade_rows() -> list[dict[str, Any]]:
    return sql("SELECT kind, source_fact_key, source_revision, payload, acknowledged_at_ms FROM news_trade_events")


# ------------------------------------------------------------------ identities


def test_legacy_intent_and_text_digest_match_the_python_identities() -> None:
    for event_id, kind in (("ev-1", "first"), ('e"x\\y', "followup"), ("ev-中文", "first")):
        row = sql(
            "SELECT news_identity('legacy_intent', jsonb_build_array(%s::text, %s::text)) AS intent",
            (event_id, kind),
        )[0]
        assert row["intent"] == legacy_intent_id(event_id, kind)
    for body in ('标题\n\n第一行 "引号" \\ tab\t', "é combining", "emoji 🚀  "):
        assert sql("SELECT news_text_digest(%s) AS sha", (body,))[0]["sha"] == digest(body)
    with pytest.raises(ValueError, match="news_legacy_delivery_kind_invalid"):
        legacy_intent_id("ev-1", "update")


# ------------------------------------------------------------------ semantic turn


def test_agent_turn_adopts_once_with_public_row_and_pending_notification() -> None:
    pg, db, clock = store()
    seed_event()
    analyzer = StubAnalyzer()
    source = asyncio.run(pg.input_for(EVENT))
    assert source.revision == 1 and source.lineage_id == f"lineage-{EVENT}"
    assert [item.text for item in source.evidence] == [TEXT]
    assert source.evidence[0].source.source_authority == "reputable_secondary"
    assert source.evidence[0].source.first_available_at_ms == STAMP

    assert asyncio.run(agent(pg, clock, analyzer).process(EVENT)) == "adopted"
    head = asyncio.run(pg.head(EVENT))
    assert head is not None and head.input_revision == 1
    work = sql("SELECT wanted_revision, done_revision, lease_token, last_outcome FROM news_semantic_work")[0]
    assert work == {"wanted_revision": 1, "done_revision": 1, "lease_token": None, "last_outcome": "adopted"}
    rows = trade_rows()
    assert [(row["kind"], row["source_fact_key"], row["source_revision"]) for row in rows] == [
        ("catalyst", EVENT, head.content_revision)
    ]
    assert rows[0]["payload"]["schema_version"] == "news_public_update_v1"
    notification = sql("SELECT channel, state, content_revision, plan FROM news_notification_work")[0]
    assert notification == {
        "channel": "news",
        "state": "pending",
        "content_revision": head.content_revision,
        "plan": None,
    }

    # A replay of the same work reuses its checkpoints and adopts nothing new.
    assert asyncio.run(agent(pg, clock, analyzer).process(EVENT)) == "unchanged"
    assert analyzer.extract_calls == 1
    assert sql("SELECT count(*) AS n FROM news_event_updates")[0]["n"] == 1
    assert len(trade_rows()) == 1
    assert "news_update_adopt" in db.names


def test_checkpoints_and_observations_are_insert_only() -> None:
    pg, _db, _clock = store()
    seed_event()
    source = asyncio.run(pg.input_for(EVENT))
    first = extraction_for(source)
    other = Extraction(claims=(draft(source.evidence[0], action="denies tariff"),))
    assert asyncio.run(pg.save_extraction("work-1", first)) == first
    assert asyncio.run(pg.save_extraction("work-1", other)) == first
    checkpoint = asyncio.run(pg.checkpoint("work-1"))
    assert checkpoint is not None and checkpoint.extraction == first and checkpoint.understanding is None

    observation = SemanticObservation(
        result_id="result-1",
        work_id="work-1",
        event_id=EVENT,
        input_revision=1,
        input_sha256=source.evidence_sha,
        program_identity="program-test",
        completed_at_ms=STAMP + 10,
        understanding=first,
    )
    assert asyncio.run(pg.save_observation(observation)) == observation
    replay = observation.model_copy(update={"completed_at_ms": STAMP + 999})
    assert asyncio.run(pg.save_observation(replay)).completed_at_ms == STAMP + 10
    with pytest.raises(EventUpdateConflict, match="news_semantic_observation_conflict"):
        asyncio.run(pg.save_observation(observation.model_copy(update={"program_identity": "other"})))


def test_two_adopters_of_one_head_adopt_exactly_once() -> None:
    pg, _db, _clock = store()
    seed_event()
    base = asyncio.run(pg.input_for(EVENT))

    async def race() -> list[bool]:
        results = await asyncio.gather(
            *(
                adopt_next(
                    pg,
                    None,
                    base.model_copy(update={"evidence": (evidence(f"Agency orders a {rate}% tariff."),)}),
                    extraction_for(
                        base.model_copy(update={"evidence": (evidence(f"Agency orders a {rate}% tariff."),)})
                    ),
                    work_id=f"work-{rate}",
                )
                for rate in (25, 30)
            )
        )
        return [adopted for adopted, _update in results]

    assert sorted(asyncio.run(race())) == [False, True]
    assert sql("SELECT count(*) AS n FROM news_event_updates")[0]["n"] == 1
    head = asyncio.run(pg.head(EVENT))
    assert head is not None
    assert [row["source_revision"] for row in trade_rows()] == [head.content_revision]
    assert sql("SELECT content_revision FROM news_notification_work")[0]["content_revision"] == head.content_revision


def test_adoption_never_downgrades_the_input_revision() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    stale = FrozenInput(
        event_id=EVENT,
        revision=1,
        lineage_id="lineage",
        evidence=(evidence("Agency adds aluminium to the tariff."),),
        prior=tuple(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=c) for c in head.claims),
    )
    newer = stale.model_copy(update={"revision": 2})
    extracted = extraction_for(newer)
    adopted, update = asyncio.run(adopt_next(pg, head, newer, extracted))
    assert adopted
    older = FrozenInput(
        event_id=EVENT,
        revision=1,
        lineage_id="lineage",
        evidence=(evidence("Agency adds copper to the tariff.", revision="3"),),
        prior=tuple(
            PriorClaim(event_id=EVENT, content_revision=update.content_revision, claim=c) for c in update.claims
        ),
    )
    with pytest.raises(EventUpdateConflict, match="news_update_input_revision_downgrade"):
        asyncio.run(adopt_next(pg, update, older, extraction_for(older), work_id="work-old"))
    assert sql("SELECT input_revision FROM news_event_update_heads")[0]["input_revision"] == 2


def test_possible_new_is_adopted_and_marked_for_notification_without_a_public_row() -> None:
    pg, _db, _clock = store()
    seed_event()
    seed_event("ev-other", text="Agency orders a 25% tariff on steel.", fingerprint="fp-other")
    other_head_claim = asyncio.run(adopt_other_event(pg))
    assert not trade_rows() or all(row["source_fact_key"] == "ev-other" for row in trade_rows())
    source = FrozenInput(
        event_id=EVENT,
        revision=1,
        lineage_id="lineage",
        evidence=(evidence(TEXT),),
        prior=(other_head_claim,),
    )
    extracted = Extraction(
        claims=(draft(source.evidence[0]),),
        relations=(RelationDraft(slot="a", previous_ref=other_head_claim.claim.ref, relation="unresolved"),),
    )
    adopted, update = asyncio.run(adopt_next(pg, None, source, extracted))
    assert adopted
    assert [change.kind for change in update.changes] == ["possible_new"]
    assert [row for row in trade_rows() if row["source_fact_key"] == EVENT] == []
    work = sql("SELECT state, content_revision FROM news_notification_work WHERE event_id = %s", (EVENT,))[0]
    assert work == {"state": "pending", "content_revision": update.content_revision}


async def adopt_other_event(pg: PgNewsStore) -> PriorClaim:
    source = FrozenInput(
        event_id="ev-other", revision=1, lineage_id="lineage-o", evidence=(evidence("Agency orders a 25% tariff."),)
    )
    adopted, update = await adopt_next(pg, None, source, extraction_for(source), work_id="work-other")
    assert adopted
    return PriorClaim(event_id="ev-other", content_revision=update.content_revision, claim=update.claims[0])


def test_a_correction_is_a_source_update_row_that_the_relay_reads_and_acknowledges() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    correction = evidence("Correction: Agency orders a 50% tariff, not 25%.")
    source = FrozenInput(
        event_id=EVENT,
        revision=2,
        lineage_id="lineage",
        evidence=(correction,),
        prior=tuple(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=c) for c in head.claims),
    )
    extracted = Extraction(
        claims=(draft(correction, action="orders 50% tariff"),),
        relations=(
            RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="corrects", change_kind="correction"),
        ),
    )
    adopted, update = asyncio.run(adopt_next(pg, head, source, extracted))
    assert adopted
    kinds = {(row["kind"], row["source_revision"]) for row in trade_rows()}
    assert ("source_update", update.content_revision) in kinds

    pending = asyncio.run(pg.pending_public_updates(10))
    assert {row.kind for row in pending} == {"catalyst_delta", "source_update"}
    corrected = next(row for row in pending if row.kind == "source_update")
    assert corrected.retired_claim_refs == (head.claims[0].ref,)
    for row in pending:
        asyncio.run(pg.acknowledge_public_update(row.update_id))
    assert asyncio.run(pg.pending_public_updates(10)) == ()


# ------------------------------------------------------------------ semantic work bookkeeping


def test_semantic_work_is_leased_bounded_and_reopened_by_a_new_revision() -> None:
    pg, _db, clock = store()
    seed_event()
    lease = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=30_000))
    assert lease is not None and lease.wanted_revision == 1 and lease.attempts == 1
    assert asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=30_000)) is None
    for attempt in range(1, 4):
        if attempt > 1:
            clock.now_ms += 10 * 60_000
            lease = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=30_000))
            assert lease is not None and lease.attempts == attempt
        assert lease is not None
        asyncio.run(pg.defer_semantic_event(lease, reason="provider_unavailable"))
    clock.now_ms += 10 * 60_000
    assert asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=30_000)) is None
    assert asyncio.run(pg.pending_semantic_events(10)) == ()
    row = sql("SELECT attempts, last_outcome, last_error_code FROM news_semantic_work")[0]
    assert row == {"attempts": 3, "last_outcome": "failed", "last_error_code": "provider_unavailable"}

    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            revision = repositories_for_connection(conn).news.request_semantic_revision(
                event_id=EVENT, lineage_id="lineage-2", now_ms=clock.now_ms
            )
    finally:
        conn.close()
    assert revision == 2
    assert asyncio.run(pg.pending_semantic_events(10)) == (EVENT,)
    asyncio.run(pg.mark_semantic_work_published(EVENT))
    assert asyncio.run(pg.pending_semantic_events(10)) == ()
    clock.now_ms += 20_000
    assert asyncio.run(pg.pending_semantic_events(10)) == (EVENT,)


def test_one_optional_read_per_lineage_and_its_attached_input() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    source = asyncio.run(pg.input_for(EVENT))
    assert asyncio.run(pg.reserve_extra_read(source.lineage_id, "target-1"))
    assert not asyncio.run(pg.reserve_extra_read(source.lineage_id, "target-2"))
    read = evidence("The ministry's order lists steel and aluminium.", publisher="ministry")
    target = ReadTarget(ref="target-1", action="read_current_artifact", description="ministry order")
    asyncio.run(pg.attach_extra_evidence(source, target, (read,), (head.claims[0].ref,)))
    asyncio.run(pg.record_read_outcome(source.lineage_id, outcome="attached"))
    attached = asyncio.run(pg.input_for(EVENT))
    assert attached.revision == 2 and attached.lineage_id == source.lineage_id
    assert attached.evidence == (read,) and attached.focus_claim_refs == (head.claims[0].ref,)
    assert [row.claim.ref for row in attached.prior] == [head.claims[0].ref]
    assert sql("SELECT extra_read_state FROM news_semantic_work")[0]["extra_read_state"] == "attached"
    assert not asyncio.run(pg.reserve_extra_read(source.lineage_id, "target-3"))


# ------------------------------------------------------------------ notifications


def test_a_notification_turn_sends_once_and_keeps_the_exact_receipt() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    sender = Sender("sent")
    assert asyncio.run(notifications(pg, clock, sender).process(EVENT, "news")) == "sent"
    card = sender.cards[0]
    ledger = sql("SELECT * FROM news_deliveries")[0]
    assert ledger["kind"] == "update" and ledger["state"] == "sent"
    assert ledger["intent_id"] == card.intent_id and ledger["body"] == card.body
    assert ledger["payload_sha256"] == card.payload_sha256 == digest(card.body)
    assert ledger["claim_refs"] == [head.claims[0].ref] and ledger["content_revision"] == head.content_revision
    assert ledger["receipt"]["provider_message_id"] == "41"
    assert ledger["history_context"]["headline_zh"] == card.headline_zh
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0
    work = sql("SELECT state, plan FROM news_notification_work")[0]
    assert work["state"] == "done"
    assert [row["reason"] for row in work["plan"]["claim_decisions"]] == ["actionable_content"]
    # Done for this head: a second turn has no work and never resends.
    assert asyncio.run(notifications(pg, clock, sender).process(EVENT, "news")) == "no_work"
    assert len(sender.cards) == 1


def test_two_planners_reserve_one_intent_without_resetting_it() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)

    async def race() -> list[Any]:
        return list(await asyncio.gather(pg.atomic_record_plan(plan), pg.atomic_record_plan(plan)))

    leases = asyncio.run(race())
    assert sum(lease is not None for lease in leases) == 1
    winner = next(lease for lease in leases if lease is not None)
    queued = sql(
        "SELECT intent_id, lease_token, attempts, content_revision, claim_refs, plan_key FROM news_delivery_queue"
    )
    assert queued == [
        {
            "intent_id": plan.intent_id,
            "lease_token": winner.lease_token,
            "attempts": 1,
            "content_revision": head.content_revision,
            "claim_refs": list(plan.selected_claim_refs),
            "plan_key": False,
        }
    ]
    # The marker stays pending while the reserved intent is in flight, due only after its lease.
    work = sql("SELECT state, next_attempt_at_ms FROM news_notification_work")[0]
    assert work == {"state": "pending", "next_attempt_at_ms": clock.now_ms + 60_000}
    assert asyncio.run(pg.pending_notification_events("news", 10)) == ()


def test_a_turn_that_dies_after_reserving_is_reclaimed_after_its_lease() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    orphan = asyncio.run(pg.atomic_record_plan(notify_plan(head, snapshot.reader.revision)))
    assert orphan is not None
    clock.now_ms += 60_001
    assert asyncio.run(pg.pending_notification_events("news", 10)) == (EVENT,)
    sender = Sender("sent")
    assert asyncio.run(notifications(pg, clock, sender).process(EVENT, "news")) == "sent"
    assert sender.cards[0].intent_id == orphan.intent_id
    assert sql("SELECT attempts FROM news_delivery_queue") == []
    assert sql("SELECT state FROM news_notification_work")[0]["state"] == "done"


def test_the_janitor_holds_an_unsettled_update_send_ambiguous_and_releases_its_reservation() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(pg.atomic_record_plan(plan))
    assert lease is not None
    body = "关税\n\n机构加征关税"
    card = FrozenCard(
        intent_id=lease.intent_id,
        claim_refs=plan.selected_claim_refs,
        headline_zh="关税",
        body=body,
        payload_sha256=digest(body),
    )
    asyncio.run(pg.save_card(lease, card))
    assert asyncio.run(pg.atomic_begin_send(lease, card))
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            settled = repositories_for_connection(conn).news.terminalize_interrupted_deliveries(
                now_ms=clock.now_ms + 120_000
            )
    finally:
        conn.close()
    assert settled == 1
    assert sql("SELECT state, error_code FROM news_deliveries") == [
        {"state": "ambiguous", "error_code": "ambiguous_after_crash"}
    ]
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0


def test_a_reader_ledger_change_is_a_version_race_that_leaves_work_pending() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    seed_event("ev-legacy", fingerprint="fp-legacy")
    sql(
        """
        INSERT INTO news_deliveries (intent_id, event_id, kind, state, card, attempted_at_ms, settled_at_ms,
                                     created_at_ms)
        VALUES (%s, 'ev-legacy', 'first', 'sent', '{}'::jsonb, %s, %s, %s)
        """,
        (legacy_intent_id("ev-legacy", "first"), clock.now_ms - 5_000, clock.now_ms - 5_000, clock.now_ms - 5_000),
    )
    assert asyncio.run(pg.atomic_record_plan(notify_plan(head, snapshot.reader.revision))) is None
    assert sql("SELECT state, plan FROM news_notification_work WHERE event_id = %s", (EVENT,))[0] == {
        "state": "pending",
        "plan": None,
    }
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0


def test_deferred_claims_keep_notification_pending_beside_the_reserved_intent() -> None:
    pg, _db, clock = store()

    def two_claims(source: FrozenInput) -> Extraction:
        item = source.evidence[0]
        return Extraction(
            claims=(
                draft(item, "a", action="orders tariff", quote="Agency orders a 25% tariff"),
                draft(item, "b", action="sets effective date", quote="effective October 1"),
            )
        )

    seed_event()
    assert asyncio.run(agent(pg, clock, StubAnalyzer(two_claims)).process(EVENT)) == "adopted"
    head = asyncio.run(pg.head(EVENT))
    assert head is not None and len(head.claims) == 2
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision, deferred=(head.claims[1].ref,))
    lease = asyncio.run(pg.atomic_record_plan(plan))
    assert lease is not None and lease.card is None
    work = sql("SELECT state, attempts, plan FROM news_notification_work")[0]
    assert work["state"] == "pending" and work["attempts"] == 1
    assert {row["decision"] for row in work["plan"]["claim_decisions"]} == {"notify", "deferred"}


def test_not_sent_retries_the_same_identity_and_frozen_payload() -> None:
    pg, _db, clock = store()
    adopted_head(pg, clock)
    sender = Sender("not_sent", "sent")
    composer = Composer()
    turn = notifications(pg, clock, sender, composer)
    assert asyncio.run(turn.process(EVENT, "news")) == "not_sent"
    assert sql("SELECT count(*) AS n FROM news_deliveries")[0]["n"] == 0
    queued = sql("SELECT lease_token, attempts, frozen_card, error_code FROM news_delivery_queue")[0]
    assert queued["lease_token"] is None and queued["attempts"] == 1 and queued["error_code"] == "rate_limited"
    assert sql("SELECT state FROM news_notification_work")[0]["state"] == "pending"

    clock.now_ms += 5 * 60_000
    assert asyncio.run(pg.pending_notification_events("news", 10)) == (EVENT,)
    assert asyncio.run(turn.process(EVENT, "news")) == "sent"
    assert composer.calls == 1
    assert sender.cards[0] == sender.cards[1]
    ledger = sql("SELECT state, payload_sha256, intent_id FROM news_deliveries")[0]
    assert ledger == {
        "state": "sent",
        "payload_sha256": sender.cards[0].payload_sha256,
        "intent_id": sender.cards[0].intent_id,
    }


def test_an_ambiguous_send_is_held_and_blocks_its_claims() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    with pytest.raises(RuntimeError, match="provider connection dropped"):
        asyncio.run(notifications(pg, clock, Sender("raise")).process(EVENT, "news"))
    ledger = sql("SELECT state, error_code, claim_refs FROM news_deliveries")[0]
    assert ledger == {"state": "ambiguous", "error_code": "RuntimeError", "claim_refs": [head.claims[0].ref]}
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0

    sql("UPDATE news_notification_work SET state = 'pending'")
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and snapshot.reader.blocked_claim_refs == (head.claims[0].ref,)
    assert snapshot.reader.receipts == ()
    sender = Sender("sent")
    assert asyncio.run(notifications(pg, clock, sender).process(EVENT, "news")) == "unresolved"
    assert sender.cards == []
    work = sql("SELECT state, attempts, plan FROM news_notification_work")[0]
    assert work["state"] == "pending" and work["attempts"] == 1
    assert work["plan"]["claim_decisions"][0]["reason"] == "send_outcome_unresolved"
    assert sql("SELECT state FROM news_deliveries")[0]["state"] == "ambiguous"


def test_begin_send_rechecks_the_head_and_keeps_the_frozen_card_for_the_same_identity() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(pg.atomic_record_plan(plan))
    assert lease is not None
    card = FrozenCard(
        intent_id=lease.intent_id,
        claim_refs=plan.selected_claim_refs,
        headline_zh="关税",
        body="关税\n\n机构加征关税",
        payload_sha256=digest("关税\n\n机构加征关税"),
    )
    assert asyncio.run(pg.save_card(lease, card)) == card
    other = card.model_copy(update={"body": "另一版本", "payload_sha256": digest("另一版本")})
    assert asyncio.run(pg.save_card(lease, other)) == card

    source = FrozenInput(
        event_id=EVENT,
        revision=2,
        lineage_id="lineage",
        evidence=(evidence("Agency adds aluminium to the tariff."),),
        prior=tuple(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=c) for c in head.claims),
    )
    adopted, update = asyncio.run(adopt_next(pg, head, source, extraction_for(source)))
    assert adopted
    assert not asyncio.run(pg.atomic_begin_send(lease, card))
    queued = sql("SELECT lease_token, frozen_card FROM news_delivery_queue")[0]
    assert queued["lease_token"] is None and FrozenCard.model_validate(queued["frozen_card"]) == card
    assert sql("SELECT count(*) AS n FROM news_deliveries")[0]["n"] == 0
    work = sql("SELECT state, content_revision FROM news_notification_work")[0]
    assert work == {"state": "pending", "content_revision": update.content_revision}
    # The released lease no longer fences a card write.
    with pytest.raises(IntentLeaseLost):
        asyncio.run(pg.save_card(lease, card))


def test_snapshot_recalls_own_and_band_receipts_and_decodes_legacy_text() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    seed_event("ev-legacy", title="Agency orders steel tariff", fingerprint="fp-tariff", at_ms=STAMP - 7_200_000)
    card = {"header": {"title": {"content": "机构加征钢铁关税"}}}
    context = {"why_zh": "影响钢铁进口", "dedupe_family": "general", "comparison_fingerprint": "fp-tariff"}
    sql(
        """
        INSERT INTO news_deliveries (intent_id, event_id, kind, state, card, receipt, attempted_at_ms,
                                     settled_at_ms, created_at_ms, history_context)
        VALUES (%s, 'ev-legacy', 'first', 'sent', %s::jsonb, '{"message_id": 7}'::jsonb, %s, %s, %s, %s::jsonb)
        """,
        (
            legacy_intent_id("ev-legacy", "first"),
            json.dumps(card),
            STAMP - 3_600_000,
            STAMP - 3_600_000,
            STAMP - 3_600_000,
            json.dumps(context),
        ),
    )
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and snapshot.update == head
    (legacy,) = snapshot.reader.receipts
    assert legacy.intent_id == legacy_intent_id("ev-legacy", "first")
    assert legacy.intent_id.startswith("legacy_intent:")
    assert legacy.body == "机构加征钢铁关税\n\n影响钢铁进口"
    assert legacy.payload_sha256 == digest(legacy.body) and legacy.provider_message_id == "7"


def test_card_failure_releases_the_lease_and_the_third_is_dead() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    for attempt in range(1, 4):
        snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
        assert snapshot is not None
        lease = asyncio.run(pg.atomic_record_plan(notify_plan(head, snapshot.reader.revision)))
        assert lease is not None
        asyncio.run(pg.record_card_failure(lease, error_code="TimeoutError"))
        queued = sql("SELECT state, attempts, lease_token FROM news_delivery_queue")[0]
        assert queued["attempts"] == attempt and queued["lease_token"] is None
        clock.now_ms += 15 * 60_000
    assert queued["state"] == "dead"
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is None  # done: the dead identity is not reopened by the marker


def test_judgment_cache_keeps_the_first_answer_and_retention_purges_old_rows() -> None:
    db = ThreadedDb()
    clock = Clock()
    cache = PgJudgmentCache(db, clock=clock)
    first = Answer(item_id="q1", value="full", backend="generated")
    asyncio.run(cache.put("key-1", first))
    asyncio.run(cache.put("key-1", first.model_copy(update={"value": "none"})))
    assert asyncio.run(cache.get("key-1")) == first
    assert asyncio.run(cache.get("missing")) is None
    pg = PgNewsStore(db, clock=clock)
    clock.now_ms += 15 * 24 * 3_600_000
    assert asyncio.run(pg.purge_semantic_caches(limit=100)) == 1
    assert asyncio.run(cache.get("key-1")) is None


def test_frozen_input_requires_material() -> None:
    with pytest.raises(LookupError, match="news_event_input_missing"):
        frozen_input(EVENT, {"work": None, "items": [], "item_ids": [], "head": None})


def test_a_no_notification_plan_is_final_for_the_head_and_retires_an_unsent_reservation() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    lease = asyncio.run(pg.atomic_record_plan(notify_plan(head, snapshot.reader.revision)))
    assert lease is not None
    asyncio.run(pg.record_card_failure(lease, error_code="TimeoutError"))
    clock.now_ms += 5 * 60_000
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    silent = NotificationPlan(
        action="no_notification",
        reason="no_uncovered_actionable_claims",
        update_ref=head.ref,
        claim_decisions=tuple(
            ClaimDecision(claim_ref=claim.ref, decision="not_notified", reason="mode_commentary")
            for claim in head.claims
        ),
        channel="news",
        reader_revision=snapshot.reader.revision,
    )
    assert asyncio.run(pg.atomic_record_plan(silent)) is None
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0
    work = sql("SELECT state, reader_revision, plan FROM news_notification_work")[0]
    assert work["state"] == "done" and work["reader_revision"] == snapshot.reader.revision
    assert work["plan"]["claim_decisions"][0]["reason"] == "mode_commentary"
    assert asyncio.run(pg.notification_snapshot(EVENT, "news")) is None
