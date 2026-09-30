"""News update delivery against real PostgreSQL: plan, intent, card, send, settle and the crash windows (#706).

Every object on the path is the production one: the semantic and notification stores, the
`Notifications` workflow, its `NotificationSender` and the `DelivererLoop` scheduler, over one real
database with a fresh connection and transaction
per port call. Only the two outside parties are doubles: the card model (a composer that answers or
fails on cue) and the push provider (a sender that sends, refuses or goes silent on cue). Assertions
are durable rows and what reached the provider, never a private call sequence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_reader import FeedOnly, FixedReader, PushAll
from tests.support.news_update_pg import (
    EVENT,
    STAMP,
    Clock,
    Composer,
    StubAnalyzer,
    TaskBackend,
    ThreadedDb,
    adopt_next,
    agent,
    draft,
    evidence,
    extraction_for,
    run_agent,
    seed_event,
    sql,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.bus import TransientError
from tracefold.news.delivery_contracts import COMMIT_PHASE_NOT_SENT, COMMIT_PHASE_UNKNOWN
from tracefold.news.models import ReaderDeliveryPresentation
from tracefold.news.notifications.contracts import CardCopy, CardLine, FrozenCard
from tracefold.news.notifications.planner import NotificationPlanner
from tracefold.news.notifications.service import Notifications
from tracefold.news.pipeline.delivery import DelivererLoop
from tracefold.news.pipeline.delivery_enrichment import DeliveryEnrichment
from tracefold.news.pipeline.notification_sender import NotificationSender
from tracefold.news.pipeline.send_entry import InitialSendEntry
from tracefold.news.reader_card import ReaderCard
from tracefold.news.storage.judgment_store import PgJudgmentCache
from tracefold.news.storage.notification_store import PgNotificationStore
from tracefold.news.storage.semantic_store import PgSemanticStore
from tracefold.news.updates.contracts import (
    Asset,
    ClaimFields,
    DraftClaim,
    EventUpdate,
    Extraction,
    FrozenInput,
    PriorClaim,
)
from tracefold.news.updates.identity import identity
from tracefold.news.updates.judgment import ProviderUnavailable
from tracefold.news.updates.ports import SemanticObservation
from tracefold.news.updates.projection import reading_views

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

TELEGRAM_TARGET = "a" * 64


class FaultDb(ThreadedDb):
    """The threaded News port, with named transactions armable to fail before they run."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_operations: set[str] = set()

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        if name in self.fail_operations:
            raise TransientError(f"injected_fault:{name}")
        return await super().tx(name, fn, timeout_seconds=timeout_seconds)


class InlineFinite:
    async def run(self, _name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout_seconds", None)
        kwargs.pop("allow_shutdown", None)
        return fn(*args, **kwargs)


def _provider_error(code: str, *, commit_phase: str, retryable: bool = False, retry_after: float | None = None):
    error = RuntimeError(code)
    error.code = code  # type: ignore[attr-defined]
    error.commit_phase = commit_phase  # type: ignore[attr-defined]
    error.retryable = retryable  # type: ignore[attr-defined]
    error.retry_after_seconds = retry_after  # type: ignore[attr-defined]
    return error


class Provider:
    """A Telegram-shaped provider: sends and edits, or fails exactly as scripted, recording what it got."""

    def __init__(self, *failures: BaseException | None) -> None:
        self.failures = list(failures)
        self.sent: list[ReaderCard] = []
        self.payloads: list[dict[str, Any]] = []
        self.edits: list[ReaderCard] = []
        self.on_send: Callable[[], None] | None = None

    def prepare(self) -> None:
        return None

    def send_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        del presentation
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure
        if self.on_send is not None:
            self.on_send()
        self.sent.append(card)
        self.payloads.append(dict(channel_payload))
        return {
            "provider": "telegram",
            "message_id": 41 + len(self.sent),
            "pushed_at_ms": STAMP + 90_000,
            "target_sha256": TELEGRAM_TARGET,
        }

    def edit_card(
        self,
        receipt: Mapping[str, Any],
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        del channel_payload, presentation
        self.edits.append(card)
        return {**dict(receipt), "edited_at_ms": int(receipt["pushed_at_ms"]) + 1_000}

    def close(self) -> None:
        return None


class Rig:
    """One real store, one core notification service and one Deliverer, on one clock."""

    def __init__(
        self,
        provider: Provider | None,
        *,
        composer: Any = None,
        clock: Clock | None = None,
        db: FaultDb | None = None,
        backend: TaskBackend | None = None,
        assessor: Any = None,
    ) -> None:
        self.clock = clock or Clock()
        self.db = db or FaultDb()
        self.semantic_store = PgSemanticStore(self.db, clock=self.clock)
        self.store = PgNotificationStore(self.db, clock=self.clock)
        self.composer = composer or Composer()
        self.notifications = Notifications(
            self.store,
            NotificationPlanner(assessor or PushAll(), PgJudgmentCache(self.db)),
            self.composer,
            clock=self.clock,
        )
        self.provider = provider
        entry = InitialSendEntry(sender=provider, finite_operations=InlineFinite(), min_interval_seconds=0.0)
        enrichment = DeliveryEnrichment(db=self.db, send_entry=entry)
        self.loop = DelivererLoop(
            db=self.db,
            notification_sender=NotificationSender(entry, enrichment),
            enrichment=enrichment,
            notifications=self.notifications,
        )

    def advance(self) -> int:
        async def turn() -> int:
            worked = await self.loop.advance()
            await self.loop.drain()
            return worked

        return asyncio.run(turn())


def _adopt(clock: Clock, analyzer: StubAnalyzer | None = None) -> EventUpdate:
    seed_event()
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
    assert asyncio.run(run_agent(agent(pg, clock, analyzer), EVENT)) == "adopted"
    head = asyncio.run(pg.head(EVENT))
    assert head is not None
    return head


def _claim(source: Any, slot: str, *, mode: str = "decision", symbol: str = "X") -> DraftClaim:
    base = draft(source, slot)
    fields = {**base.fields.model_dump(), "mode": mode}
    fields["assets"] = [Asset(symbol=symbol, market_type="equity", role="primary").model_dump()]
    return base.model_copy(update={"fields": ClaimFields.model_validate(fields)})


def _ledger() -> list[dict[str, Any]]:
    return sql("SELECT * FROM news_deliveries ORDER BY created_at_ms, intent_id")


def _queue() -> list[dict[str, Any]]:
    return sql("SELECT intent_id, state, attempts, lease_token, error_code, frozen_card FROM news_delivery_queue")


def _work() -> dict[str, Any]:
    return sql("""SELECT w.state,w.attempts,w.next_attempt_at_ms,w.content_revision,w.last_error_code,
                         d.plan AS plan
                    FROM news_notification_work w
                    LEFT JOIN news_notification_decisions d ON d.decision_ref=w.decision_ref""")[0]


def test_planner_intent_card_and_send_record_the_exact_frozen_body_and_receipt() -> None:
    clock = Clock()
    head = _adopt(clock)
    provider = Provider()
    rig = Rig(provider, clock=clock)

    assert rig.advance() == 1

    (ledger,) = _ledger()
    card = FrozenCard.model_validate(ledger["card"])
    # The card was composed once, for exactly the selected claim, and only after the plan chose it.
    assert rig.composer.calls == 1
    assert (ledger["kind"], ledger["state"]) == ("update", "sent")
    assert ledger["claim_refs"] == [head.claims[0].ref] and ledger["content_revision"] == head.content_revision
    # The exact frozen body is the payload the reader saw: rendered whole, never re-generated.
    assert ledger["body"] == card.body and ledger["payload_sha256"] == card.payload_sha256
    assert f"{provider.sent[0].header.subject}\n\n{provider.sent[0].lead}" == card.body
    assert ledger["card"]["headline_zh"] == card.headline_zh
    assert ledger["card_copy_input_digest"]
    assert asyncio.run(rig.store.lookup_card_copy(ledger["card_copy_input_digest"])) == CardCopy.model_validate(
        ledger["card_copy_document"]
    )
    # The provider's message id and receipt are what the ledger keeps.
    assert ledger["receipt"]["provider_message_id"] == "42"
    assert ledger["receipt"]["message_id"] == 42 and ledger["receipt"]["target_sha256"] == TELEGRAM_TARGET
    assert _queue() == []
    assert _work()["state"] == "done"
    # A redelivered turn has no work: the reader gets one card.
    assert rig.advance() == 0 and len(provider.sent) == 1


def test_a_no_notification_plan_composes_no_card_and_sends_nothing() -> None:
    clock = Clock()

    def commentary(source: FrozenInput) -> Extraction:
        return Extraction(claims=(_claim(source.evidence[0], "a", mode="commentary"),))

    _adopt(clock, StubAnalyzer(commentary))
    provider = Provider()
    rig = Rig(provider, clock=clock, assessor=FeedOnly())

    rig.advance()

    assert rig.composer.calls == 0
    assert provider.sent == [] and _ledger() == [] and _queue() == []
    work = _work()
    assert work["state"] == "done"
    assert [row["reason"] for row in work["plan"]["claim_decisions"]] == ["reader_feed"]


def test_a_failed_plan_spends_one_bounded_attempt_and_the_third_fails_the_work_visibly() -> None:
    """#742 N5: a programming error is recorded against its work, never raised out of the loop. The third
    failure ends the work `failed` with its error code instead of leaving it pending and never picked up;
    an explicit retry of that exact revision reopens it."""

    clock = Clock()

    def market_report(source: FrozenInput) -> Extraction:
        claim = _claim(source.evidence[0], "a", mode="observation")
        fields = claim.fields.model_copy(update={"content_kind": "level_crossed"})
        return Extraction(claims=(claim.model_copy(update={"fields": fields}),))

    head = _adopt(clock, StubAnalyzer(market_report))
    provider = Provider()

    class BrokenEditor(PushAll):
        async def judge(self, reader, budget):
            raise RuntimeError("editor_bug")

    rig = Rig(provider, clock=clock, assessor=BrokenEditor())

    for attempt in range(1, 3):
        assert rig.advance() == 1
        work = _work()
        assert (work["state"], work["attempts"], work["plan"]) == ("pending", attempt, None)
        assert work["last_error_code"] == "editor_bug"
        assert work["next_attempt_at_ms"] > clock.now_ms
        clock.now_ms = work["next_attempt_at_ms"]
    assert rig.advance() == 1
    work = _work()
    assert (work["state"], work["attempts"], work["last_error_code"]) == ("failed", 3, "editor_bug")

    # Terminal: it is no longer due, and nothing else moved.
    assert asyncio.run(rig.store.pending_notification_events("news", 10)) == ()
    assert rig.composer.calls == 0 and provider.sent == [] and _queue() == [] and _ledger() == []
    assert asyncio.run(rig.semantic_store.head(EVENT)) == head

    retried = asyncio.run(
        rig.db.tx(
            "retry",
            lambda r: r.news.notification_work.retry_failed_revision(
                event_id=EVENT, revision=head.content_revision, now_ms=clock.now_ms
            ),
        )
    )
    assert retried and _work()["state"] == "pending" and _work()["attempts"] == 0
    assert asyncio.run(rig.store.pending_notification_events("news", 10)) == (EVENT,)


class FlakyComposer(Composer):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def compose(self, claims: tuple[Any, ...], *, sources: Any, earlier: Any = None) -> CardCopy:
        if self.failures:
            self.calls += 1
            self.failures -= 1
            raise ProviderUnavailable("news_generation_LMRateLimitError")
        return await Composer.compose(self, claims, sources=sources)


def test_a_card_failure_retries_only_that_intents_card() -> None:
    clock = Clock()
    head = _adopt(clock)
    outbox_before = sql("SELECT kind, source_revision, acknowledged_at_ms FROM news_trade_events")
    provider = Provider()
    composer = FlakyComposer(failures=1)
    rig = Rig(provider, composer=composer, clock=clock)

    rig.advance()

    # One card attempt spent; nothing sent; the semantic head and the public outbox are untouched.
    (queued,) = _queue()
    assert (queued["state"], queued["attempts"], queued["lease_token"]) == ("pending", 1, None)
    assert queued["error_code"] == "news_card:ProviderUnavailable" and queued["frozen_card"] is None
    assert _ledger() == [] and provider.sent == []
    assert asyncio.run(rig.semantic_store.head(EVENT)) == head
    assert sql("SELECT kind, source_revision, acknowledged_at_ms FROM news_trade_events") == outbox_before
    assert _work()["state"] == "pending"

    clock.now_ms += 30_000
    rig.advance()

    assert composer.calls == 2
    (ledger,) = _ledger()
    assert ledger["intent_id"] == queued["intent_id"] and ledger["state"] == "sent"
    assert len(provider.sent) == 1 and _queue() == []


def test_a_head_that_changes_before_the_send_retires_the_unsent_reservation() -> None:
    """The preflight recheck: a newer head means this selection is not what the reader should get."""

    clock = Clock()
    head = _adopt(clock)
    provider = Provider()

    class AdoptingComposer(Composer):
        """While the card model writes copy, the Event gets a newer adopted head."""

        async def compose(self, claims: tuple[Any, ...], *, sources: Any, earlier: Any = None) -> CardCopy:
            if self.calls == 0:
                source = FrozenInput(
                    event_id=EVENT,
                    revision=2,
                    lineage_id="lineage",
                    evidence=(evidence("Agency adds aluminium to the tariff."),),
                    prior=tuple(
                        PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=claim)
                        for claim in head.claims
                    ),
                )
                store = PgSemanticStore(ThreadedDb(), clock=clock)
                adopted, _update = await adopt_next(store, head, source, extraction_for(source))
                assert adopted
            return await Composer.compose(self, claims, sources=sources)

    rig = Rig(provider, composer=AdoptingComposer(), clock=clock)

    rig.advance()

    # Nothing was sent for the stale selection, and no ledger row claims it was.
    assert provider.sent == [] and _ledger() == []
    stale_intent = _queue()[0]["intent_id"]
    assert _work()["state"] == "pending"

    rig.advance()

    # The next turn plans the new head: the stale reservation is retired and one card is sent for it.
    (ledger,) = _ledger()
    newer = asyncio.run(rig.semantic_store.head(EVENT))
    assert newer is not None and ledger["content_revision"] == newer.content_revision
    assert ledger["intent_id"] != stale_intent
    assert stale_intent not in {row["intent_id"] for row in _queue()}
    assert len(provider.sent) == 1


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_lost_settlement_commit_response_retries_same_result_once(outcome: str) -> None:
    class LostResponseDb(FaultDb):
        def __init__(self) -> None:
            super().__init__()
            self.settlements = 0

        async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
            result = await super().tx(name, fn, timeout_seconds=timeout_seconds)
            if name == "news_update_settle_send":
                self.settlements += 1
                if self.settlements == 1:
                    raise TransientError("injected_lost_commit_response")
            return result

    clock = Clock()
    _adopt(clock)
    provider = (
        Provider()
        if outcome == "sent"
        else Provider(
            _provider_error("news_delivery_telegram_http_failed", commit_phase=COMMIT_PHASE_NOT_SENT, retryable=True)
        )
    )
    db = LostResponseDb()
    rig = Rig(provider, clock=clock, db=db)

    assert rig.advance() == 1
    assert db.settlements == 2
    if outcome == "sent":
        assert len(provider.sent) == 1
        assert _ledger()[0]["state"] == "sent"
        assert _queue() == []
    else:
        assert provider.sent == []
        assert _ledger() == []
        assert _queue()[0]["attempts"] == 1


def test_a_send_the_provider_proved_unsent_retries_the_same_identity_and_payload() -> None:
    clock = Clock()
    _adopt(clock)
    provider = Provider(
        _provider_error(
            "news_delivery_telegram_http_failed", commit_phase=COMMIT_PHASE_NOT_SENT, retryable=True, retry_after=90.0
        )
    )
    rig = Rig(provider, clock=clock)

    rig.advance()

    (queued,) = _queue()
    assert (queued["state"], queued["attempts"], queued["error_code"]) == (
        "pending",
        1,
        "news_delivery_telegram_http_failed",
    )
    assert _ledger() == []
    # The provider's own wait outranks the lane's 30 s: nothing is due before it.
    assert _work()["next_attempt_at_ms"] == clock.now_ms + 90_000
    clock.now_ms += 60_000
    assert rig.advance() == 0

    clock.now_ms += 30_000
    rig.advance()

    (ledger,) = _ledger()
    assert ledger["intent_id"] == queued["intent_id"] and ledger["state"] == "sent"
    assert FrozenCard.model_validate(queued["frozen_card"]) == FrozenCard.model_validate(ledger["card"])
    assert rig.composer.calls == 1, "the retry sends the frozen payload, it does not compose again"


def test_a_send_whose_outcome_is_unknown_is_held_ambiguous_and_never_resent() -> None:
    clock = Clock()
    head = _adopt(clock)
    provider = Provider(_provider_error("news_delivery_telegram_transport_failed", commit_phase=COMMIT_PHASE_UNKNOWN))
    rig = Rig(provider, clock=clock)

    rig.advance()

    (ledger,) = _ledger()
    assert (ledger["state"], ledger["error_code"]) == ("ambiguous", "news_delivery_telegram_transport_failed")
    ambiguous_claim = head.claims[0].ref

    # A newer head adds a claim. The new claim goes out; the claim whose send is unresolved is held,
    # never counted as received and never sent again.
    source = FrozenInput(
        event_id=EVENT,
        revision=2,
        lineage_id="lineage",
        evidence=(evidence("Agency adds aluminium to the tariff."),),
        prior=tuple(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=c) for c in head.claims),
    )
    aluminium = Extraction(claims=(draft(source.evidence[0], "a", action="adds aluminium to the tariff"),))
    adopted, update = asyncio.run(adopt_next(PgSemanticStore(ThreadedDb(), clock=clock), head, source, aluminium))
    assert adopted and {claim.ref for claim in update.claims} > {ambiguous_claim}
    for _ in range(4):
        rig.advance()
        clock.now_ms += 15 * 60_000

    sent = [row for row in _ledger() if row["state"] == "sent"]
    assert len(provider.sent) == 1 and len(sent) == 1
    assert ambiguous_claim not in sent[0]["claim_refs"]
    held = next(row for row in _ledger() if row["state"] == "ambiguous")
    assert held["body"] == ledger["body"] and held["payload_sha256"] == ledger["payload_sha256"]
    # Possibly already read, so never sent again -- and not holding the new claim back either (#742 N5).
    decisions = {row["claim_ref"]: row["reason"] for row in _work()["plan"]["claim_decisions"]}
    assert decisions[ambiguous_claim] == "send_outcome_ambiguous"
    assert _work()["state"] == "done"


def test_a_crash_after_the_send_leaves_the_sending_payload_untouched_and_it_is_never_resent() -> None:
    """The settlement is lost after the provider took the card: `sending` stays, then becomes ambiguous."""

    clock = Clock()
    _adopt(clock)
    provider = Provider()
    rig = Rig(provider, clock=clock)
    rig.db.fail_operations = {"news_update_settle_send"}

    with pytest.raises(RuntimeError, match="news_send_settlement_unavailable"):
        rig.advance()

    assert len(provider.sent) == 1, "the card really did reach the provider"
    (sending,) = _ledger()
    assert sending["state"] == "sending"
    frozen = (sending["body"], sending["payload_sha256"], sending["card"])

    # The marker comes due again while the outcome is unknown: the claim in flight is held, not
    # re-composed or re-sent, and the sending payload does not move.
    rig.db.fail_operations = set()
    clock.now_ms += 5 * 60_000
    rig.advance()
    assert len(provider.sent) == 1
    (still,) = _ledger()
    assert (still["state"], still["body"], still["payload_sha256"], still["card"]) == ("sending", *frozen)

    # The next process's startup reconciliation holds it ambiguous; it is never sent again.
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            repositories_for_connection(conn).news.notification_delivery.terminalize_interrupted_deliveries(
                now_ms=clock.now_ms + 120_000
            )
    finally:
        conn.close()
    clock.now_ms += 15 * 60_000
    rig.advance()
    (settled,) = _ledger()
    assert (settled["state"], settled["error_code"], settled["body"]) == (
        "ambiguous",
        "ambiguous_after_crash",
        frozen[0],
    )
    assert len(provider.sent) == 1


def test_two_deliverers_on_one_event_send_one_card() -> None:
    clock = Clock()
    _adopt(clock)
    provider = Provider()
    first = Rig(provider, clock=clock)
    second = Rig(provider, clock=clock)

    async def race() -> None:
        await asyncio.gather(first.loop.advance(), second.loop.advance())
        await asyncio.gather(first.loop.drain(), second.loop.drain())

    asyncio.run(race())

    assert len(provider.sent) == 1
    assert [row["state"] for row in _ledger()] == ["sent"]


def _unrelated_copper(source: FrozenInput) -> Extraction:
    """A claim sharing no word, subject, asset or claim ref with the steel tariff."""

    base = _claim(source.evidence[0], "a", symbol="COPPER")
    fields = base.fields.model_copy(update={"subject": "Miner", "action": "halts pit", "object": "Chile"})
    return Extraction(claims=(base.model_copy(update={"fields": fields}),))


class _GatedSettlement(FaultDb):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        if name == "news_update_settle_send":
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                await self.release.wait()
        return await super().tx(name, fn, timeout_seconds=timeout_seconds)


class _DistinctComposer(Composer):
    """Copy per story; the copper card is composed first, so it is the one in the provider."""

    async def compose(self, claims: tuple[Any, ...], *, sources: Any, earlier: Any = None) -> CardCopy:
        self.calls += 1
        copper = any("copper" in claim.statement.lower() for claim in claims)
        if not copper:
            await asyncio.sleep(0.3)
        return CardCopy(
            headline_zh="智利铜矿停产" if copper else "机构对钢铁进口加征关税",
            lines=tuple(
                CardLine(claim_ref=claim.ref, text_zh="矿企暂停智利铜矿" if copper else "机构宣布加征百分之二十五关税")
                for claim in claims
            ),
        )


class _RepeatAware(FixedReader):
    """Worth a push alone; once a related message is supplied, what the claim adds is not."""

    async def judge(self, reader: Any, budget: Any) -> Any:
        self.value = 1.0 if reader.messages else 2.6
        return await super().judge(reader, budget)


@pytest.mark.parametrize("related", [False, True])
def test_only_a_related_receipt_settled_meanwhile_invalidates_a_ready_card(related: bool) -> None:
    """#742 W5. One card is ready while another Event's is in the provider. The reader revision is the
    waiting Event's own related receipts, not the whole channel: an unrelated receipt leaves its plan valid
    and it is sent in the same turn, while a related one makes it stale, and its re-plan compares that
    receipt (here: is covered by it). Before, any send anywhere invalidated every ready card."""

    clock = Clock()
    _adopt(clock)
    if related:
        seed_event(
            "event-second",
            text="Agency confirms a copper mine closure next month.",
            title="Agency confirms copper mine closure",
            fingerprint="fp-second",
        )
        analyzer = None
    else:
        seed_event(
            "event-second", text="Miner halts copper pit in Chile.", title="Chile pit halted", fingerprint="fp-2"
        )
        analyzer = StubAnalyzer(_unrelated_copper)
    pg = PgSemanticStore(ThreadedDb(), clock=clock)
    assert asyncio.run(run_agent(agent(pg, clock, analyzer), "event-second")) == "adopted"

    provider = Provider()
    db = _GatedSettlement()
    rig = Rig(provider, clock=clock, db=db, composer=_DistinctComposer(), assessor=_RepeatAware())

    async def run() -> None:
        task = asyncio.create_task(rig.loop.advance())
        await asyncio.wait_for(db.entered.wait(), 5)
        assert len(provider.sent) == 1
        assert [row["state"] for row in _ledger()] == ["sending"]
        clock.now_ms += 1_000
        db.release.set()
        assert await task == 2
        await rig.loop.drain()

    asyncio.run(run())

    def works() -> dict[str, dict[str, Any]]:
        return {
            row["event_id"]: row
            for row in sql(
                "SELECT w.event_id,w.state,d.plan FROM news_notification_work w "
                "LEFT JOIN news_notification_decisions d ON d.decision_ref=w.decision_ref"
            )
        }

    # The copper card is composed first, so it is in the provider while the tariff card waits ready.
    if not related:
        assert len(provider.sent) == 2
        assert [row["state"] for row in _ledger()] == ["sent", "sent"]
        assert {row["state"] for row in works().values()} == {"done"}
        return
    # The related receipt made the tariff's ready card stale: nothing of it was sent, and it is planned again.
    assert len(provider.sent) == 1 and works()[EVENT]["state"] == "pending"
    clock.now_ms += 1_000
    assert rig.advance() == 1
    tariff = works()[EVENT]
    assert tariff["state"] == "done" and len(provider.sent) == 1
    assert [row["reason"] for row in tariff["plan"]["claim_decisions"]] == ["reader_feed"]
    assert {row["intent_id"] for row in tariff["plan"]["compared_receipts"]} == {
        row["intent_id"] for row in _ledger() if row["event_id"] == "event-second"
    }


def test_a_settlement_that_cannot_be_recorded_leaves_sending_and_faults_the_capability() -> None:
    clock = Clock()
    _adopt(clock)
    provider = Provider()
    db = _GatedSettlement()
    db.fail_operations.add("news_update_settle_send")
    rig = Rig(provider, clock=clock, db=db)

    async def run() -> None:
        task = asyncio.create_task(rig.loop.advance())
        await asyncio.wait_for(db.entered.wait(), 5)
        db.release.set()
        with pytest.raises(RuntimeError, match="news_send_settlement_unavailable"):
            await task

    asyncio.run(run())
    assert len(provider.sent) == 1
    assert [row["state"] for row in _ledger()] == ["sending"]


def test_the_telegram_edit_enriches_the_intents_receipt_and_keeps_the_frozen_card() -> None:
    clock = Clock()
    _adopt(clock)
    provider = Provider()
    rig = Rig(provider, clock=clock)

    rig.advance()

    (ledger,) = _ledger()
    # The claim names an asset, so the sent message is edited in place once the quote read answered.
    assert len(provider.edits) == 1 and provider.edits[0].lead == provider.sent[0].lead
    assert (ledger["edit_state"], ledger["pending_card"]) == ("edited", None)
    assert ledger["receipt"]["edited_at_ms"] == STAMP + 91_000
    # The edit is fenced by the intent and its receipt; the frozen card, body and digest do not move.
    assert ledger["receipt"]["payload_sha256"] == ledger["payload_sha256"]
    assert FrozenCard.model_validate(ledger["card"]).body == ledger["body"]


def test_the_stage_breakdown_from_adoption_to_the_provider_is_read_back_with_sql() -> None:
    """#742 W8: every stage of §1.3 comes out of the recorded decision and the delivery row alone."""

    clock = Clock()
    _adopt(clock)
    rig = Rig(Provider(), clock=clock)

    assert rig.advance() == 1

    (row,) = sql(
        """
        SELECT u.adopted_at_ms,
               (d.plan #>> '{timings,due_at_ms}')::bigint AS due_at_ms,
               (d.plan #>> '{timings,started_at_ms}')::bigint AS started_at_ms,
               (d.plan #>> '{timings,snapshot_ms}')::bigint AS snapshot_ms,
               (d.plan #>> '{timings,judgment_ms}')::bigint AS judgment_ms,
               (d.plan #>> '{timings,planned_at_ms}')::bigint AS planned_at_ms,
               d.created_at_ms AS decided_at_ms,
               jsonb_array_length(d.plan -> 'compared_receipts') AS compared,
               (x.history_context #>> '{timings,card_started_at_ms}')::bigint AS card_started_at_ms,
               (x.history_context #>> '{timings,card_finished_at_ms}')::bigint AS card_finished_at_ms,
               (x.history_context #>> '{timings,ready_at_ms}')::bigint AS ready_at_ms,
               (x.history_context #>> '{timings,send_slot_wait_ms}')::bigint AS send_slot_wait_ms,
               x.attempted_at_ms, x.settled_at_ms
          FROM news_deliveries x
          JOIN news_notification_decisions d ON d.decision_ref = x.decision_ref
          JOIN news_event_updates u ON u.event_id = x.event_id AND u.content_revision = x.content_revision
        """
    )
    assert all(value is not None for value in row.values()), row
    # adoption -> due -> taken -> planned -> decided -> card -> ready -> slot -> begun -> settled
    assert row["adopted_at_ms"] <= row["due_at_ms"] <= row["started_at_ms"] <= row["planned_at_ms"]
    assert row["planned_at_ms"] <= row["decided_at_ms"] <= row["card_started_at_ms"] <= row["card_finished_at_ms"]
    assert row["card_finished_at_ms"] <= row["ready_at_ms"] <= row["attempted_at_ms"] <= row["settled_at_ms"]
    assert min(row["snapshot_ms"], row["judgment_ms"], row["send_slot_wait_ms"]) >= 0
    assert row["compared"] == 0


def test_new_program_reuses_extraction_and_keeps_legacy_observation_and_frozen_intent() -> None:
    """An old program's saved observation cannot collide with the next program's immutable result."""

    clock = Clock()
    _adopt(clock)
    rig = Rig(Provider(), clock=clock)
    prepared = asyncio.run(rig.notifications.prepare(EVENT, "news"))
    assert prepared.status == "ready" and prepared.card is not None and prepared.lease is not None
    frozen_before = sql("SELECT * FROM news_delivery_queue WHERE intent_id=%s", (prepared.lease.intent_id,))[0]

    retry_event = "event-program-retry"
    seed_event(
        retry_event,
        text="Agency lifts the aluminium export ban.",
        title="Agency lifts aluminium export ban",
        fingerprint="fp-program-retry",
    )
    pg = rig.semantic_store
    analyzer = StubAnalyzer()

    async def resume() -> tuple[str, str]:
        lease = await pg.claim_semantic_work(retry_event, lease_ms=180_000)
        assert lease is not None
        source = lease.source
        extracted = extraction_for(source)
        work_id = identity("semantic_work", source.event_id, source.revision, source.input_sha, analyzer.identity)
        await pg.save_extraction(work_id, extracted)
        # This is the deployed pre-759 formula, deliberately lacking program identity.
        legacy = SemanticObservation(
            result_id=identity("semantic_result", work_id, source.prior, extracted),
            work_id=work_id,
            event_id=source.event_id,
            input_revision=source.revision,
            input_sha256=source.input_sha,
            program_identity="program-before-refactor",
            completed_at_ms=clock.now_ms,
            understanding=extracted,
            read_refs=tuple(view.read_ref for view in reading_views(source)),
        )
        await pg.save_observation(legacy)
        legacy_before = sql("SELECT * FROM news_semantic_observations WHERE result_id=%s", (legacy.result_id,))[0]
        await pg.defer_semantic_event(lease, reason="resume_after_program_change")
        clock.now_ms = sql("SELECT next_attempt_at_ms FROM news_semantic_work WHERE event_id=%s", (retry_event,))[0][
            "next_attempt_at_ms"
        ]
        subject = agent(pg, clock, analyzer)
        subject.program_identity = "program-after-refactor"
        assert await run_agent(subject, retry_event) == "adopted"
        assert analyzer.extract_calls == 0, "the same analyzer/input work must reuse its saved extraction"
        assert (
            sql("SELECT * FROM news_semantic_observations WHERE result_id=%s", (legacy.result_id,))[0] == legacy_before
        )
        return legacy.result_id, work_id

    legacy_id, work_id = asyncio.run(resume())
    observations = sql("SELECT * FROM news_semantic_observations WHERE event_id=%s", (retry_event,))
    assert len(observations) == 2
    assert {row["work_id"] for row in observations} == {work_id}
    assert {row["program_identity"] for row in observations} == {"program-before-refactor", "program-after-refactor"}
    assert len({row["result_id"] for row in observations}) == 2
    adopted = next(row for row in observations if row["program_identity"] == "program-after-refactor")
    head = asyncio.run(pg.head(retry_event))
    assert head is not None
    adopted_row = sql(
        "SELECT observation_result_id FROM news_event_updates WHERE event_id=%s AND content_revision=%s",
        (retry_event, head.content_revision),
    )[0]
    assert adopted_row["observation_result_id"] == adopted["result_id"]
    assert adopted["result_id"] != legacy_id
    # A deployment identity is no reason to rewrite another Event's exact frozen pending body or intent.
    assert sql("SELECT * FROM news_delivery_queue WHERE intent_id=%s", (prepared.lease.intent_id,))[0] == frozen_before
    assert _ledger() == [], "resuming semantic work performs no provider send"
