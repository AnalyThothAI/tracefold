"""Shared real PostgreSQL News update builders and controllable ports.

No database work runs at import time; each test supplies its own cloned database.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tests.postgres_test_utils import connect_postgres_test, seed_current_news_evidence
from tests.support.news_reader import PushAll
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.notifications.contracts import CardCopy, CardLine, ClaimDecision, FrozenCard, NotificationPlan
from tracefold.news.notifications.planner import NotificationPlanner
from tracefold.news.notifications.ports import IntentLease, SendOutcome
from tracefold.news.notifications.service import Notifications
from tracefold.news.storage.judgment_store import PgJudgmentCache
from tracefold.news.storage.notification_store import PgNotificationStore
from tracefold.news.storage.semantic_store import PgSemanticStore
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import (
    Citation,
    ClaimFields,
    DraftClaim,
    EventUpdate,
    Evidence,
    Extraction,
    FrozenInput,
    PriorClaim,
    SemanticLease,
    Source,
    SupportDraft,
)
from tracefold.news.updates.identity import digest, identity
from tracefold.news.updates.judgment import Answer, BatchResult, ProviderUnavailable, Question, Task
from tracefold.news.updates.ports import SemanticObservation
from tracefold.news.updates.projection import reading_views
from tracefold.news.updates.public import public_updates
from tracefold.news.updates.service import NewsAgent

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

    async def read(
        self,
        name: str,
        fn: Callable[[Any], Any],
        *,
        timeout_seconds: float = 3.0,
        repeatable_read: bool = False,
    ) -> Any:
        return await asyncio.to_thread(self._run, name, fn, repeatable_read)

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        return await asyncio.to_thread(self._run, name, fn)

    def _run(self, name: str, fn: Callable[[Any], Any], repeatable_read: bool = False) -> Any:
        self.names.append(name)
        conn = connect_postgres_test(read_only=False)
        try:
            repos = repositories_for_connection(conn)
            with repos.transaction():
                if repeatable_read:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
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
            repositories_for_connection(conn).news.semantic_work.request_semantic_revision(
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
        self,
        source: FrozenInput,
        extracted: Extraction,
        budget: Any,
        *,
        rebase_only: bool = False,
        final_attempt: bool = True,
        relation_pairs: frozenset[tuple[str, str]] | None = None,
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
    identity = "test_card_composer"

    def __init__(self) -> None:
        self.calls = 0

    async def compose(self, claims: tuple[Any, ...], *, sources: Any, earlier: Any = None) -> CardCopy:
        self.calls += 1
        return CardCopy(
            headline_zh="机构对钢铁进口加征关税",
            lines=tuple(CardLine(claim_ref=claim.ref, text_zh="机构宣布加征百分之二十五关税") for claim in claims),
        )


async def save_card(pg: PgNotificationStore, lease: IntentLease, card: FrozenCard) -> FrozenCard:
    parts = card.body.split("\n\n")
    copy = CardCopy(
        headline_zh=parts[0],
        lines=tuple(
            CardLine(claim_ref=ref, text_zh=text) for ref, text in zip(card.claim_refs, parts[1:], strict=True)
        ),
    )
    return await pg.save_card(lease, card, copy=copy, input_digest=digest(copy.model_dump(mode="json")))


class Sender:
    @contextlib.asynccontextmanager
    async def send_slot(self):
        yield

    async def preflight(self, card: FrozenCard, *, plan: NotificationPlan, update: EventUpdate) -> None:
        return None

    def __init__(self, *outcomes: str) -> None:
        self.outcomes = list(outcomes) or ["sent"]
        self.cards: list[FrozenCard] = []

    async def send(self, card: FrozenCard, *, plan: NotificationPlan, update: EventUpdate) -> SendOutcome:
        assert plan.intent_id == card.intent_id and update.ref == plan.update_ref
        self.cards.append(card)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if outcome == "raise":
            raise RuntimeError("provider connection dropped")
        if outcome == "not_sent":
            return SendOutcome(
                state="not_sent", payload_sha256=card.payload_sha256, error_code="rate_limited", retryable=True
            )
        message_id = 40 + len(self.cards)
        return SendOutcome(
            state="sent",
            payload_sha256=card.payload_sha256,
            message_id=str(message_id),
            receipt={
                "provider": "telegram",
                "message_id": message_id,
                "pushed_at_ms": STAMP,
                "target_sha256": "a" * 64,
            },
        )


@dataclass(frozen=True)
class NewsStores:
    """Two concrete adapters sharing one test database; no delegated runtime facade."""

    semantic: PgSemanticStore
    notifications: PgNotificationStore


def store(clock: Clock | None = None) -> tuple[NewsStores, ThreadedDb, Clock]:
    db = ThreadedDb()
    clock = clock or Clock()
    return NewsStores(PgSemanticStore(db, clock=clock), PgNotificationStore(db, clock=clock)), db, clock


async def run_agent(subject: NewsAgent, event_id: str) -> str:
    assert isinstance(subject.store, PgSemanticStore)
    lease = await subject.store.claim_semantic_work(event_id, lease_ms=180_000)
    if lease is None:
        return "unchanged"
    return await subject.process(lease)


def agent(pg: PgSemanticStore, clock: Clock, analyzer: StubAnalyzer | None = None) -> NewsAgent:
    return NewsAgent(pg, analyzer or StubAnalyzer(), program_identity="program-test", clock=clock)  # type: ignore[arg-type]


class Turns:
    """One notification service and the sender its turns hand the frozen card to, as the Deliverer does."""

    def __init__(self, pg: PgNotificationStore, clock: Clock, sender: Sender, composer: Composer | None = None) -> None:
        self.service = Notifications(
            pg, NotificationPlanner(PushAll(), PgJudgmentCache(pg.db)), composer or Composer(), clock=clock
        )
        self.sender = sender

    async def process(self, event_id: str, channel: str) -> str:
        return (await self.service.process(event_id, channel, self.sender)).status


def notifications(pg: PgNotificationStore, clock: Clock, sender: Sender, composer: Composer | None = None) -> Turns:
    return Turns(pg, clock, sender, composer)


def adopted_head(pg: PgSemanticStore, clock: Clock) -> EventUpdate:
    seed_event()
    assert asyncio.run(run_agent(agent(pg, clock), EVENT)) == "adopted"
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
    pg: PgSemanticStore,
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
        input_sha256=source.input_sha,
        program_identity="program-test",
        completed_at_ms=STAMP + 100,
        understanding=extracted,
        read_refs=tuple(view.read_ref for view in reading_views(source)),
    )
    await pg.save_observation(observation)
    # Adoption happens after the semantic completion it adopts; the two clocks stay distinct.
    update = assemble_update(source, extracted, head, adopted_at_ms=STAMP + 150)
    assert update is not None
    # Storage CAS tests prepare a real owner for this controlled frozen input.
    set_semantic_job(
        source.event_id,
        wanted_revision=source.revision,
        lease_token="storage-test",
        lease_until_ms=pg.clock() + 180_000,
    )
    lease = SemanticLease(source=source, lease_token="storage-test", attempts=1)
    adopted = await pg.atomic_adopt(
        lease=lease,
        expected_head_ref=expected_head_ref if head is None else head.ref,
        observation=observation,
        update=update,
        public=public_updates(update, semantic_completed_at_ms=observation.completed_at_ms),
    )
    return adopted, update


def notify_plan(head: EventUpdate, revision: str, *, deferred: tuple[str, ...] = ()) -> NotificationPlan:
    input_snapshot = {"update": head.model_dump(mode="json"), "fixture": "notify", "deferred": deferred}
    decisions = tuple(
        ClaimDecision(claim_ref=claim.ref, decision="deferred", reason="send_outcome_unresolved")
        if claim.ref in deferred
        else ClaimDecision(claim_ref=claim.ref, decision="notify", reason="reader_push")
        for claim in head.claims
    )
    return NotificationPlan(
        action="notify",
        reason="uncovered_claims",
        update_ref=head.ref,
        claim_decisions=decisions,
        channel="news",
        reader_revision=revision,
        reader_identity="fixture_reader",
        input_digest=digest(input_snapshot),
    )


def trade_rows() -> list[dict[str, Any]]:
    return sql("SELECT kind, source_fact_key, source_revision, payload, acknowledged_at_ms FROM news_trade_events")


async def adopt_other_event(pg: PgSemanticStore) -> PriorClaim:
    source = FrozenInput(
        event_id="ev-other", revision=1, lineage_id="lineage-o", evidence=(evidence("Agency orders a 25% tariff."),)
    )
    adopted, update = await adopt_next(pg, None, source, extraction_for(source), work_id="work-other")
    assert adopted
    return PriorClaim(event_id="ev-other", content_revision=update.content_revision, claim=update.claims[0])


def set_semantic_job(event_id: str | None = None, **changes: Any) -> None:
    """Prepare controlled revisions/leases through the typed job shape in a real transaction."""
    from tracefold.news.storage.semantic_jobs import SemanticJobs

    with contextlib.closing(connect_postgres_test()) as conn, conn.transaction():
        jobs = SemanticJobs(conn)
        events = conn.execute(
            "SELECT subject_id FROM news_jobs WHERE job_kind='semantic' AND (%s::text IS NULL OR subject_id=%s)",
            (event_id, event_id),
        ).fetchall()
        for event in events:
            row = jobs.lock(str(event["subject_id"]))
            assert row is not None
            row.update(changes)
            jobs.save(row)
