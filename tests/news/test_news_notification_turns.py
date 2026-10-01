"""One core notification turn: what it records, what it returns, and what it never does (#706, #742).

The store here is a recording double of the port, so these tests state the turn's own sequencing: a
failed plan spends a notification attempt and is returned, not raised; a database that cannot answer
postpones the work without charging it; a failed card spends its intent's card attempt; a preflight that
proves nothing was sent never opens a `sending` row; nothing is composed for a plan that notifies nobody;
a changed selection is never sent; and a send whose outcome the turn cannot account for is settled
ambiguous. The last test is the Deliverer's fault isolation: one Event's failing turn never reaches another
Event's send in flight. The port's durable guarantees are asserted against PostgreSQL in
`tests/integration/test_news_event_update_store.py` and `test_news_update_delivery.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Mapping
from typing import Any

import pytest

from tests.support.news_update_cards import READER_REVISION, copy_for, nvda_update
from tracefold.news.bus import TransientError
from tracefold.news.models import ReaderDeliveryPresentation
from tracefold.news.notifications.card import freeze_card
from tracefold.news.notifications.contracts import (
    CardCopy,
    CardLine,
    ClaimDecision,
    FrozenCard,
    NotificationPlan,
    ReaderSnapshot,
)
from tracefold.news.notifications.ports import (
    DeliveryTimings,
    IntentLease,
    NotificationSnapshot,
    PlanCommit,
    SendOutcome,
)
from tracefold.news.notifications.service import Notifications
from tracefold.news.pipeline.delivery import DelivererLoop
from tracefold.news.pipeline.delivery_enrichment import DeliveryEnrichment
from tracefold.news.pipeline.notification_sender import NotificationSender
from tracefold.news.pipeline.send_entry import InitialSendEntry
from tracefold.news.reader_card import ReaderCard
from tracefold.news.updates.contracts import EventUpdate
from tracefold.news.updates.judgment import Budget, ContractFault, ProviderUnavailable


def _plan(update: EventUpdate, *, notify: bool = True) -> NotificationPlan:
    decision = (
        ClaimDecision(claim_ref=update.claims[0].ref, decision="notify", reason="reader_push")
        if notify
        else ClaimDecision(claim_ref=update.claims[0].ref, decision="not_notified", reason="reader_feed")
    )
    return NotificationPlan(
        action="notify" if notify else "no_notification",
        reason="uncovered_claims" if notify else "no_uncovered_actionable_claims",
        update_ref=update.ref,
        claim_decisions=(decision,),
        channel="news",
        reader_revision=READER_REVISION,
        reader_identity="fixture_reader",
        input_digest="fixture-input",
    )


class Store:
    """The port as a recording double: pending heads, one intent each, and every write it was asked for."""

    def __init__(self, *updates: EventUpdate, begin: bool = True) -> None:
        self.updates = {update.event_id: update for update in updates}
        self.update = updates[0]
        self.begin = begin
        self.calls: list[tuple[str, Any]] = []
        self.card: FrozenCard | None = None
        self.timings: list[DeliveryTimings | None] = []

    async def pending_notification_events(self, channel: str, limit: int) -> tuple[str, ...]:
        return tuple(self.updates)[:limit]

    async def notification_snapshot(self, event_id: str, channel: str) -> NotificationSnapshot | None:
        return NotificationSnapshot(
            update=self.updates[event_id],
            reader=ReaderSnapshot(channel=channel, revision=READER_REVISION, receipts=()),
            work_due_at_ms=1,
        )

    async def defer_notification(
        self,
        event_id: str,
        channel: str,
        expected_content_revision: str | None,
        expected_work_updated_at_ms: int | None = None,
        *,
        error_code: str,
    ) -> None:
        self.calls.append(("defer_notification", (event_id, error_code)))

    async def postpone_notification(self, event_id: str, channel: str, expected_content_revision: str | None) -> None:
        self.calls.append(("postpone_notification", event_id))

    async def lookup_notification_decision(self, event_id: str, channel: str, input_digest: str):
        return None

    async def lookup_card_copy(self, input_digest: str):
        return None

    async def atomic_record_plan(self, plan: NotificationPlan) -> PlanCommit:
        self.calls.append(("record_plan", plan.action))
        if plan.action != "notify":
            return PlanCommit(status="committed", effective_plan=plan)
        return PlanCommit(
            status="committed",
            effective_plan=plan,
            lease=IntentLease(intent_id=plan.intent_id, lease_token="lease", plan=plan, card=self.card),
        )

    async def record_unsent_failure(
        self, lease: IntentLease, *, error_code: str, retryable: bool, retry_after_ms: int | None = None
    ) -> None:
        self.calls.append(("unsent_failure", (error_code, retryable)))

    async def save_card(self, lease: IntentLease, card: FrozenCard, *, copy: CardCopy, input_digest: str) -> FrozenCard:
        self.calls.append(("save_card", card.intent_id))
        return card

    async def atomic_begin_send(
        self, lease: IntentLease, card: FrozenCard, *, timings: DeliveryTimings | None = None
    ) -> str:
        self.calls.append(("begin_send", card.intent_id))
        self.timings.append(timings)
        return "begun" if self.begin else "head_changed"

    async def release_unsent_intent(self, lease: IntentLease) -> None:
        self.calls.append(("release_unsent", lease.intent_id))

    async def settle_send(self, lease: IntentLease, card: FrozenCard, outcome: SendOutcome, *, settled_at_ms: int):
        self.calls.append(("settle", (outcome.state, outcome.error_code)))
        return outcome.state

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class Planner:
    def __init__(self, plan: NotificationPlan | None = None, *, error: BaseException | None = None, delay=0.0):
        self.plan_value = plan
        self.error = error
        self.delay = delay

    async def plan(self, update: EventUpdate, reader: ReaderSnapshot, budget: Budget, *, now_ms: int, reuse=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.plan_value if self.plan_value is not None else _plan(update)


class Composer:
    identity = "test_card_composer"

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    async def compose(self, claims: tuple[Any, ...], *, sources: Any, earlier: Any = None) -> CardCopy:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return CardCopy(
            headline_zh="英伟达向数据中心投资千亿美元",
            lines=tuple(CardLine(claim_ref=claim.ref, text_zh="英伟达宣布投资") for claim in claims),
        )


class Sender:
    def __init__(
        self,
        *,
        error: BaseException | None = None,
        payload_sha256: str | None = None,
        preflight: SendOutcome | None = None,
    ) -> None:
        self.error = error
        self.payload_sha256 = payload_sha256
        self.preflight_outcome = preflight
        self.cards: list[FrozenCard] = []

    @contextlib.asynccontextmanager
    async def send_slot(self):
        yield

    async def preflight(self, card: FrozenCard, *, plan: NotificationPlan, update: EventUpdate) -> SendOutcome | None:
        return self.preflight_outcome

    async def send(self, card: FrozenCard, *, plan: NotificationPlan, update: EventUpdate) -> SendOutcome:
        self.cards.append(card)
        if self.error is not None:
            raise self.error
        return SendOutcome(state="sent", payload_sha256=self.payload_sha256 or card.payload_sha256, message_id="1")


def _turn(store: Store, planner: Planner, composer: Composer, sender: Sender, *, stage_seconds: float = 5.0):
    notifications = Notifications(store, planner, composer, clock=lambda: 1, stage_seconds=stage_seconds)  # type: ignore[arg-type]
    return asyncio.run(notifications.process(store.update.event_id, "news", sender))


@pytest.mark.parametrize(
    ("error", "delay", "stage_seconds", "code"),
    [
        (ContractFault("news_judgment_option_invalid"), 0.0, 5.0, "news_judgment_option_invalid"),
        (None, 0.2, 0.05, "news_notification_plan:TimeoutError"),  # the stage deadline itself
        (KeyError("bug"), 0.0, 5.0, "news_notification_plan:KeyError"),  # a bug fails the work, visibly
    ],
)
def test_a_failed_plan_spends_one_notification_attempt_and_is_returned_not_raised(
    error: BaseException | None, delay: float, stage_seconds: float, code: str
) -> None:
    update = nvda_update()
    store = Store(update)
    composer = Composer()
    sender = Sender()

    turn = _turn(store, Planner(_plan(update), error=error, delay=delay), composer, sender, stage_seconds=stage_seconds)

    assert (turn.status, turn.error_code) == ("plan_failed", code)
    assert store.calls == [("defer_notification", (update.event_id, code))]
    assert composer.calls == 0 and sender.cards == []


def test_a_database_that_cannot_answer_postpones_the_work_without_charging_it() -> None:
    update = nvda_update()
    store = Store(update)

    with pytest.raises(TransientError):
        _turn(store, Planner(error=TransientError("db_overrun:news_judgment_cache_get")), Composer(), Sender())

    assert store.calls == [("postpone_notification", update.event_id)]


def test_a_plan_that_notifies_nobody_composes_no_card() -> None:
    update = nvda_update()
    store = Store(update)
    composer = Composer()

    turn = _turn(store, Planner(_plan(update, notify=False)), composer, Sender())

    assert turn.status == "no_notification"
    assert store.names() == ["record_plan"] and composer.calls == 0


def test_the_recorded_plan_carries_where_its_turn_spent_its_time() -> None:
    update = nvda_update()
    recorded: list[NotificationPlan] = []

    class Recording(Store):
        async def atomic_record_plan(self, plan: NotificationPlan) -> PlanCommit:
            recorded.append(plan)
            return await super().atomic_record_plan(plan)

    _turn(Recording(update), Planner(_plan(update, notify=False)), Composer(), Sender())

    (plan,) = recorded
    assert plan.timings is not None
    assert (plan.timings.due_at_ms, plan.timings.started_at_ms, plan.timings.planned_at_ms) == (1, 1, 1)
    assert plan.timings.snapshot_ms is not None and plan.timings.snapshot_ms >= 0


def test_a_failed_card_spends_only_its_intents_card_attempt() -> None:
    update = nvda_update()
    store = Store(update)
    sender = Sender()

    turn = _turn(store, Planner(_plan(update)), Composer(error=ProviderUnavailable("rate limited")), sender)

    assert (turn.status, turn.error_code) == ("card_failed", "news_card:ProviderUnavailable")
    assert store.calls == [("record_plan", "notify"), ("unsent_failure", ("news_card:ProviderUnavailable", True))]
    assert sender.cards == []


def test_cancelled_pacer_wait_releases_unsent_intent_without_ambiguous_receipt() -> None:
    update = nvda_update()
    store = Store(update)
    entered = asyncio.Event()

    class WaitingSender(Sender):
        @contextlib.asynccontextmanager
        async def send_slot(self):
            entered.set()
            await asyncio.Event().wait()
            yield

    async def run() -> None:
        task = asyncio.create_task(
            Notifications(store, Planner(_plan(update)), Composer()).process(update.event_id, "news", WaitingSender())
        )
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert store.names() == ["record_plan", "save_card", "release_unsent"]


def test_cancelled_target_preflight_never_starts_a_send() -> None:
    update = nvda_update()
    store = Store(update)
    entered = asyncio.Event()

    class WaitingPreflight(Sender):
        async def preflight(self, card: FrozenCard, *, plan: NotificationPlan, update: EventUpdate) -> None:
            entered.set()
            await asyncio.Event().wait()

    async def run() -> None:
        task = asyncio.create_task(
            Notifications(store, Planner(_plan(update)), Composer()).process(
                update.event_id, "news", WaitingPreflight()
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert store.names() == ["record_plan", "save_card", "release_unsent"]


def test_a_preflight_that_proves_nothing_was_sent_never_opens_a_sending_row() -> None:
    """#742 N10: a card that provably never left is an unsent failure of its intent, not a settled send."""

    update = nvda_update()
    store = Store(update)
    refused = SendOutcome(
        state="not_sent",
        payload_sha256="0" * 64,
        error_code="news_delivery_telegram_preflight_transport_failed",
        retryable=True,
    )
    sender = Sender(preflight=refused)

    turn = _turn(store, Planner(_plan(update)), Composer(), sender)

    assert turn.status == "not_sent" and sender.cards == []
    assert store.calls[-1] == ("unsent_failure", ("news_delivery_telegram_preflight_transport_failed", True))
    assert "begin_send" not in store.names() and "settle" not in store.names()


def test_copy_that_cannot_be_frozen_is_a_card_failure_too() -> None:
    update = nvda_update()
    store = Store(update)

    class UnsafeComposer(Composer):
        async def compose(self, claims: tuple[Any, ...], *, sources: Any, earlier: Any = None) -> CardCopy:
            self.calls += 1
            return copy_for(_plan(update), "看 https://evil.example 的标题")

    turn = _turn(store, Planner(_plan(update)), UnsafeComposer(), Sender())

    assert turn.status == "card_failed"
    assert store.calls[-1] == ("unsent_failure", ("news_card_copy_unsafe", True))


def test_a_frozen_card_is_reused_rather_than_composed_again() -> None:
    update = nvda_update()
    store = Store(update)
    plan = _plan(update)

    store.card = freeze_card(plan, update, copy_for(plan))
    composer = Composer()
    sender = Sender()

    turn = _turn(store, Planner(plan), composer, sender)

    assert turn.status == "sent" and composer.calls == 0 and sender.cards == [store.card]
    assert "save_card" not in store.names()
    # The send records that no card was composed this turn, and how long the ready card waited for its slot.
    assert store.timings == [
        DeliveryTimings(card_started_at_ms=None, card_finished_at_ms=None, ready_at_ms=1, send_slot_wait_ms=0)
    ]


def test_a_changed_selection_is_never_sent() -> None:
    update = nvda_update()
    store = Store(update, begin=False)
    sender = Sender()

    turn = _turn(store, Planner(_plan(update)), Composer(), sender)

    assert turn.status == "head_changed"
    assert sender.cards == [] and "settle" not in store.names()


@pytest.mark.parametrize(
    ("sender", "error"),
    [
        (Sender(error=RuntimeError("socket closed after the write")), RuntimeError),
        (Sender(payload_sha256="0" * 64), ContractFault),
    ],
)
def test_a_send_the_turn_cannot_account_for_is_settled_ambiguous(sender: Sender, error: type[Exception]) -> None:
    update = nvda_update()
    store = Store(update)

    with pytest.raises(error):
        _turn(store, Planner(_plan(update)), Composer(), sender)

    assert store.calls[-1][0] == "settle" and store.calls[-1][1][0] == "ambiguous"


class _ThreadedFinite:
    """The finite-operation runner's shape: the provider call runs in a thread the loop does not own."""

    async def run(self, _name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout_seconds", None)
        kwargs.pop("allow_shutdown", None)
        return await asyncio.to_thread(fn, *args, **kwargs)


class _NoQuotes:
    async def read(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        raise RuntimeError("no quote plane in this test")

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        raise RuntimeError("no database in this test")


class _BlockingProvider:
    """A Feishu-shaped provider whose one send blocks, in its thread, until the test releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.sent: list[ReaderCard] = []

    def prepare(self) -> None:
        return None

    def send_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        self.entered.set()
        assert self.release.wait(5)
        self.sent.append(card)
        return {"provider": "feishu", "status_code": 200, "code": 0}

    def close(self) -> None:
        return None


def test_one_events_database_failure_never_turns_another_events_delivered_card_ambiguous() -> None:
    """#742 N1 (#744 D1). Event B's turn fails with the database's TransientError while A's card is in
    the provider. Before, the failure left `advance()` and its cleanup cancelled A's send, which then
    settled `ambiguous` although the provider delivered it. A's send is its own owner until it settles."""

    first, second = nvda_update(event_id="event-a"), nvda_update(event_id="event-b")
    store = Store(first, second)
    b_may_fail = asyncio.Event()

    class SecondFails(Planner):
        async def plan(self, update: EventUpdate, reader: ReaderSnapshot, budget: Budget, *, now_ms: int, reuse=None):
            if update.event_id == "event-b":
                await b_may_fail.wait()
                raise TransientError("db_overrun:news_update_notification_snapshot")
            return _plan(update)

    provider = _BlockingProvider()
    notifications = Notifications(store, SecondFails(), Composer(), clock=lambda: 1)  # type: ignore[arg-type]
    db = _NoQuotes()
    entry = InitialSendEntry(sender=provider, finite_operations=_ThreadedFinite(), min_interval_seconds=0.0)
    enrichment = DeliveryEnrichment(db=db, send_entry=entry)  # type: ignore[arg-type]
    loop = DelivererLoop(
        db=db,  # type: ignore[arg-type]
        notification_sender=NotificationSender(entry, enrichment),
        enrichment=enrichment,
        notifications=notifications,
    )

    async def scenario() -> int:
        advance = asyncio.create_task(loop.advance())
        assert await asyncio.to_thread(provider.entered.wait, 5)
        b_may_fail.set()
        for _ in range(100):
            if ("postpone_notification", "event-b") in store.calls:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert not advance.done(), "the failed sibling turn must not end the window around A's send"
        provider.release.set()
        return await asyncio.wait_for(advance, 5)

    worked = asyncio.run(scenario())

    assert worked == 1
    assert len(provider.sent) == 1
    settled = [call for name, call in store.calls if name == "settle"]
    assert settled == [("sent", None)], "the delivered card is recorded sent, never ambiguous"
    assert ("postpone_notification", "event-b") in store.calls


def test_shared_provider_slot_is_held_until_editorial_receipt_settlement() -> None:
    """A market send cannot enter while the editorial workflow still settles provider evidence."""

    async def scenario() -> None:
        update = nvda_update()
        settling, release_settlement = asyncio.Event(), asyncio.Event()

        class SettlementWaits(Store):
            async def settle_send(self, lease, card, outcome, *, settled_at_ms):
                settling.set()
                await release_settlement.wait()
                return await super().settle_send(lease, card, outcome, settled_at_ms=settled_at_ms)

        provider = _BlockingProvider()
        provider.release.set()
        store = SettlementWaits(update)
        notifications = Notifications(store, Planner(), Composer(), clock=lambda: 1)
        entry = InitialSendEntry(sender=provider, finite_operations=_ThreadedFinite(), min_interval_seconds=0.0)
        enrichment = DeliveryEnrichment(db=_NoQuotes(), send_entry=entry)  # type: ignore[arg-type]
        adapter = NotificationSender(entry, enrichment)
        editorial = asyncio.create_task(notifications.process(update.event_id, "news", adapter))
        await asyncio.wait_for(settling.wait(), 1)
        assert len(provider.sent) == 1
        market = asyncio.create_task(entry.send_prepared_card(provider.sent[0], channel_payload={}))
        await asyncio.sleep(0.02)
        assert not market.done() and len(provider.sent) == 1
        release_settlement.set()
        turn = await asyncio.wait_for(editorial, 1)
        await asyncio.wait_for(market, 1)
        assert turn.status == "sent" and len(provider.sent) == 2
        await entry.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("part", ["plan", "card"])
def test_generation_admission_before_any_call_postpones_without_charging_work_or_intent(part: str) -> None:
    from tests.support.news_update_semantic import MemoryCache
    from tracefold.news.adapters.card_copy import DspyCardComposer
    from tracefold.news.adapters.reader_judge import DspyReaderJudge
    from tracefold.news.generation_capacity import NewsGenerationCapacity
    from tracefold.news.notifications.planner import NotificationPlanner

    async def run() -> None:
        update = nvda_update()
        store = Store(update)
        capacity = NewsGenerationCapacity(1)
        planner = (
            NotificationPlanner(
                DspyReaderJudge(lambda: "fixture", generated_model_identity="fixture", generation_capacity=capacity),
                MemoryCache(),
            )
            if part == "plan"
            else Planner()
        )
        composer = (
            DspyCardComposer(lambda: "fixture", model_identity="fixture", generation_capacity=capacity)
            if part == "card"
            else Composer()
        )
        notifications = Notifications(store, planner, composer, clock=lambda: 1, stage_seconds=0.02)
        async with capacity.acquire():
            result = await notifications.prepare(update.event_id, "news")
        assert result.status == ("plan_failed" if part == "plan" else "card_failed")
        assert result.error_code == "news_generation_capacity_wait"
        assert "defer_notification" not in store.names() and "unsent_failure" not in store.names()
        assert store.names() == (
            ["postpone_notification"] if part == "plan" else ["record_plan", "release_unsent", "postpone_notification"]
        )

    asyncio.run(run())


def test_native_call_before_generated_admission_wait_keeps_the_planning_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dspy

    from tests.support.news_update_semantic import MemoryCache
    from tracefold.news.adapters.reader_judge import DspyReaderJudge
    from tracefold.news.generation_capacity import NewsGenerationCapacity
    from tracefold.news.notifications.planner import NotificationPlanner

    calls: list[str] = []

    class Predict:
        def __init__(self, signature: Any) -> None:
            del signature

        async def acall(self, *, lm: str, **inputs: Any) -> None:
            calls.append(lm)
            raise ProviderUnavailable("news_reader_native_unavailable")

    monkeypatch.setattr(dspy, "Predict", Predict)

    async def run() -> None:
        update = nvda_update()
        store = Store(update)
        capacity = NewsGenerationCapacity(1)
        planner = NotificationPlanner(
            DspyReaderJudge(
                lambda: "generated",
                generated_model_identity="fixture",
                native_lm_factory=lambda: "native",
                native_model_identity="native-fixture",
                generation_capacity=capacity,
            ),
            MemoryCache(),
        )
        notifications = Notifications(store, planner, Composer(), clock=lambda: 1, stage_seconds=0.02)
        async with capacity.acquire():
            await notifications.prepare(update.event_id, "news")
        assert calls and set(calls) == {"native"}
        assert "postpone_notification" not in store.names()
        assert store.names() in (["record_plan"], ["defer_notification"])

    asyncio.run(run())
