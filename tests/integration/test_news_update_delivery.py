"""News update delivery against real PostgreSQL: plan, intent, card, send, settle and the crash windows (#706).

Every object on the path is the production one -- `PgNewsStore`, the core `Notifications` turn and the
`DelivererLoop` that is its `Sender` -- over one real database, with a fresh connection and transaction
per port call. Only the two outside parties are doubles: the card model (a composer that answers or
fails on cue) and the push provider (a sender that sends, refuses or goes silent on cue). Assertions
are durable rows and what reached the provider, never a private call sequence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from tests.integration.test_news_event_update_store import (
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
    seed_event,
    sql,
)
from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.bus import TransientError
from tracefold.news.delivery_contracts import COMMIT_PHASE_NOT_SENT, COMMIT_PHASE_UNKNOWN
from tracefold.news.models import ReaderDeliveryPresentation
from tracefold.news.pipeline.delivery import DelivererLoop
from tracefold.news.reader_card import ReaderCard
from tracefold.news.storage.decisions import LEGACY_INTENT_RETIRED, legacy_intent_id
from tracefold.news.storage.event_update_store import PgJudgmentCache, PgNewsStore
from tracefold.news.updates.contracts import (
    Asset,
    ClaimFields,
    DraftClaim,
    EventUpdate,
    Extraction,
    FrozenInput,
    PriorClaim,
)
from tracefold.news.updates.judgment import NewsJudgments, ProviderUnavailable
from tracefold.news.updates.notification import CardCopy, FrozenCard, NotificationPlanner
from tracefold.news.updates.service import Notifications

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
    ) -> None:
        self.clock = clock or Clock()
        self.db = db or FaultDb()
        self.store = PgNewsStore(self.db, clock=self.clock)
        self.composer = composer or Composer()
        judgments = NewsJudgments(
            generated=backend or TaskBackend({"coverage": "full"}), cache=PgJudgmentCache(self.db)
        )
        self.notifications = Notifications(self.store, NotificationPlanner(judgments), self.composer, clock=self.clock)
        self.provider = provider
        self.loop = DelivererLoop(
            db=self.db,
            sender=provider,
            finite_operations=InlineFinite(),
            min_interval_seconds=0.0,
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
    pg = PgNewsStore(ThreadedDb(), clock=clock)
    assert asyncio.run(agent(pg, clock, analyzer).process(EVENT)) == "adopted"
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
    return sql("SELECT state, attempts, next_attempt_at_ms, content_revision, plan FROM news_notification_work")[0]


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
    rig = Rig(provider, clock=clock)

    rig.advance()

    assert rig.composer.calls == 0
    assert provider.sent == [] and _ledger() == [] and _queue() == []
    work = _work()
    assert work["state"] == "done"
    assert [row["reason"] for row in work["plan"]["claim_decisions"]] == ["mode_commentary"]


def test_a_failed_plan_spends_one_bounded_attempt_and_touches_nothing_else() -> None:
    """A judgment answer outside its contract fails the plan: no intent, no card, the marker backs off."""

    clock = Clock()

    def unknown_mode(source: FrozenInput) -> Extraction:
        return Extraction(claims=(_claim(source.evidence[0], "a", mode="unknown"),))

    head = _adopt(clock, StubAnalyzer(unknown_mode))
    provider = Provider()
    rig = Rig(provider, clock=clock, backend=TaskBackend({"mode": "not-a-mode"}))

    for attempt in range(1, 4):
        rig.advance()
        work = _work()
        assert (work["state"], work["attempts"], work["plan"]) == ("pending", attempt, None)
        assert work["next_attempt_at_ms"] > clock.now_ms
        clock.now_ms = work["next_attempt_at_ms"]

    # Bounded: after the last attempt the marker stays visible and is no longer due.
    assert asyncio.run(rig.store.pending_notification_events("news", 10)) == ()
    assert rig.composer.calls == 0 and provider.sent == [] and _queue() == [] and _ledger() == []
    assert asyncio.run(rig.store.head(EVENT)) == head


class FlakyComposer(Composer):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def compose(self, claims: tuple[Any, ...]) -> CardCopy:
        if self.failures:
            self.calls += 1
            self.failures -= 1
            raise ProviderUnavailable("news_generation_LMRateLimitError")
        return await Composer.compose(self, claims)


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
    assert queued["error_code"] == "ProviderUnavailable" and queued["frozen_card"] is None
    assert _ledger() == [] and provider.sent == []
    assert asyncio.run(rig.store.head(EVENT)) == head
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

        async def compose(self, claims: tuple[Any, ...]) -> CardCopy:
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
                store = PgNewsStore(ThreadedDb(), clock=clock)
                adopted, _update = await adopt_next(store, head, source, extraction_for(source))
                assert adopted
            return await Composer.compose(self, claims)

    rig = Rig(provider, composer=AdoptingComposer(), clock=clock)

    rig.advance()

    # Nothing was sent for the stale selection, and no ledger row claims it was.
    assert provider.sent == [] and _ledger() == []
    stale_intent = _queue()[0]["intent_id"]
    assert _work()["state"] == "pending"

    rig.advance()

    # The next turn plans the new head: the stale reservation is retired and one card is sent for it.
    (ledger,) = _ledger()
    newer = asyncio.run(rig.store.head(EVENT))
    assert newer is not None and ledger["content_revision"] == newer.content_revision
    assert ledger["intent_id"] != stale_intent
    assert stale_intent not in {row["intent_id"] for row in _queue()}
    assert len(provider.sent) == 1


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
    adopted, update = asyncio.run(adopt_next(PgNewsStore(ThreadedDb(), clock=clock), head, source, aluminium))
    assert adopted and {claim.ref for claim in update.claims} > {ambiguous_claim}
    for _ in range(4):
        rig.advance()
        clock.now_ms += 15 * 60_000

    sent = [row for row in _ledger() if row["state"] == "sent"]
    assert len(provider.sent) == 1 and len(sent) == 1
    assert ambiguous_claim not in sent[0]["claim_refs"]
    held = next(row for row in _ledger() if row["state"] == "ambiguous")
    assert held["body"] == ledger["body"] and held["payload_sha256"] == ledger["payload_sha256"]
    decisions = {row["claim_ref"]: row["reason"] for row in _work()["plan"]["claim_decisions"]}
    assert decisions[ambiguous_claim] == "send_outcome_unresolved"


def test_a_crash_after_the_send_leaves_the_sending_payload_untouched_and_it_is_never_resent() -> None:
    """The settlement is lost after the provider took the card: `sending` stays, then becomes ambiguous."""

    clock = Clock()
    _adopt(clock)
    provider = Provider()
    rig = Rig(provider, clock=clock)
    rig.db.fail_operations = {"news_update_settle_send"}

    with pytest.raises(TransientError, match="news_update_settle_send"):
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
            repositories_for_connection(conn).news.terminalize_interrupted_deliveries(now_ms=clock.now_ms + 120_000)
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


def test_a_pending_legacy_intent_is_retired_with_its_reason_and_its_sent_row_still_edits() -> None:
    clock = Clock()
    seed_event()
    seed_event("ev-legacy-sent", fingerprint="fp-legacy")
    receipt = {"provider": "telegram", "message_id": 7, "pushed_at_ms": STAMP, "target_sha256": TELEGRAM_TARGET}
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            assert repos.news.enqueue_delivery(event_id=EVENT, kind="first", now_ms=STAMP)
            assert repos.news.begin_delivery(event_id="ev-legacy-sent", kind="first", card={"x": 1}, now_ms=STAMP)
            assert repos.news.settle_delivery(
                event_id="ev-legacy-sent", kind="first", state="sent", receipt=receipt, error_code=None, now_ms=STAMP
            )
    finally:
        conn.close()
    provider = Provider()
    rig = Rig(provider, clock=clock)
    stop = asyncio.Event()

    async def run_once() -> None:
        task = asyncio.create_task(rig.loop.run(stop_event=stop))
        await asyncio.sleep(0.2)
        stop.set()
        await task

    asyncio.run(run_once())

    assert provider.sent == []
    assert sql("SELECT kind, state, error_code FROM news_delivery_queue WHERE event_id = %s", (EVENT,)) == [
        {"kind": "first", "state": "dead", "error_code": LEGACY_INTENT_RETIRED}
    ]
    assert sql("SELECT count(*) AS n FROM news_deliveries WHERE event_id = %s", (EVENT,))[0]["n"] == 0
    # A settled legacy card keeps its edit reconciliation, keyed by its legacy intent id.
    legacy = legacy_intent_id("ev-legacy-sent", "first")
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            assert repos.news.begin_delivery_edit(intent_id=legacy, card={"x": 2}, receipt=receipt, now_ms=STAMP + 1)
            assert repos.news.settle_delivery_edit(
                intent_id=legacy, receipt={**receipt, "edited_at_ms": STAMP + 2}, now_ms=STAMP + 2
            )
    finally:
        conn.close()
    edited = sql("SELECT card, receipt, edit_state FROM news_deliveries WHERE intent_id = %s", (legacy,))[0]
    assert edited == {"card": {"x": 2}, "receipt": {**receipt, "edited_at_ms": STAMP + 2}, "edit_state": "edited"}
