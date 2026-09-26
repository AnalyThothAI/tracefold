"""One core notification turn: what it records before it raises, and what it never does (#706).

The store here is a recording double of the port, so these tests state the turn's own sequencing: a
failed plan spends a notification attempt, a failed card spends its intent's card attempt, nothing is
composed for a plan that notifies nobody, a changed selection is never sent, and a send whose outcome
the turn cannot account for is settled ambiguous. The port's durable guarantees are asserted against
PostgreSQL in `tests/integration/test_news_event_update_store.py` and `test_news_update_delivery.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.support.news_update_cards import READER_REVISION, copy_for, nvda_update
from tracefold.news.updates.contracts import EventUpdate
from tracefold.news.updates.judgment import Budget, ContractFault, ProviderUnavailable
from tracefold.news.updates.notification import (
    CardCopy,
    ClaimDecision,
    FrozenCard,
    NotificationPlan,
    ReaderSnapshot,
)
from tracefold.news.updates.ports import IntentLease, NotificationSnapshot, SendOutcome
from tracefold.news.updates.service import Notifications


def _plan(update: EventUpdate, *, notify: bool = True) -> NotificationPlan:
    decision = (
        ClaimDecision(claim_ref=update.claims[0].ref, decision="notify", reason="actionable_content")
        if notify
        else ClaimDecision(claim_ref=update.claims[0].ref, decision="not_notified", reason="mode_commentary")
    )
    return NotificationPlan(
        action="notify" if notify else "no_notification",
        reason="uncovered_claims" if notify else "no_uncovered_actionable_claims",
        update_ref=update.ref,
        claim_decisions=(decision,),
        channel="news",
        reader_revision=READER_REVISION,
    )


class Store:
    """The port as a recording double: one pending head, one intent, and every write it was asked for."""

    def __init__(self, update: EventUpdate, *, begin: bool = True) -> None:
        self.update = update
        self.begin = begin
        self.calls: list[tuple[str, Any]] = []
        self.card: FrozenCard | None = None

    async def notification_snapshot(self, event_id: str, channel: str) -> NotificationSnapshot | None:
        return NotificationSnapshot(
            update=self.update, reader=ReaderSnapshot(channel=channel, revision=READER_REVISION, receipts=())
        )

    async def defer_notification(self, event_id: str, channel: str) -> None:
        self.calls.append(("defer_notification", event_id))

    async def atomic_record_plan(self, plan: NotificationPlan) -> IntentLease | None:
        self.calls.append(("record_plan", plan.action))
        if plan.action != "notify":
            return None
        return IntentLease(intent_id=plan.intent_id, lease_token="lease", plan=plan, card=self.card)

    async def record_card_failure(self, lease: IntentLease, *, error_code: str) -> None:
        self.calls.append(("card_failure", error_code))

    async def save_card(self, lease: IntentLease, card: FrozenCard) -> FrozenCard:
        self.calls.append(("save_card", card.intent_id))
        return card

    async def atomic_begin_send(self, lease: IntentLease, card: FrozenCard) -> bool:
        self.calls.append(("begin_send", card.intent_id))
        return self.begin

    async def settle_send(self, lease: IntentLease, card: FrozenCard, outcome: SendOutcome, *, settled_at_ms: int):
        self.calls.append(("settle", (outcome.state, outcome.error_code)))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class Planner:
    def __init__(self, plan: NotificationPlan | None = None, *, error: BaseException | None = None, delay=0.0):
        self.plan_value = plan
        self.error = error
        self.delay = delay

    async def plan(self, update: EventUpdate, reader: ReaderSnapshot, budget: Budget, *, now_ms: int):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.plan_value


class Composer:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    async def compose(self, claims: tuple[Any, ...]) -> CardCopy:
        self.calls += 1
        if self.error is not None:
            raise self.error
        update = nvda_update()
        return copy_for(_plan(update))


class Sender:
    def __init__(self, *, error: BaseException | None = None, payload_sha256: str | None = None) -> None:
        self.error = error
        self.payload_sha256 = payload_sha256
        self.cards: list[FrozenCard] = []

    async def send(self, card: FrozenCard, *, plan: NotificationPlan, update: EventUpdate) -> SendOutcome:
        self.cards.append(card)
        if self.error is not None:
            raise self.error
        return SendOutcome(state="sent", payload_sha256=self.payload_sha256 or card.payload_sha256, message_id="1")


def _turn(store: Store, planner: Planner, composer: Composer, sender: Sender, *, stage_seconds: float = 5.0):
    notifications = Notifications(store, planner, composer, clock=lambda: 1, stage_seconds=stage_seconds)  # type: ignore[arg-type]
    return asyncio.run(notifications.process(store.update.event_id, "news", sender))


@pytest.mark.parametrize(
    ("error", "delay", "stage_seconds"),
    [
        (ContractFault("news_judgment_option_invalid"), 0.0, 5.0),
        (None, 0.2, 0.05),  # the stage deadline itself
    ],
)
def test_a_failed_plan_spends_one_notification_attempt_and_nothing_else(
    error: BaseException | None, delay: float, stage_seconds: float
) -> None:
    update = nvda_update()
    store = Store(update)
    composer = Composer()
    sender = Sender()

    with pytest.raises((ContractFault, TimeoutError)):
        _turn(store, Planner(_plan(update), error=error, delay=delay), composer, sender, stage_seconds=stage_seconds)

    assert store.names() == ["defer_notification"]
    assert composer.calls == 0 and sender.cards == []


def test_a_plan_that_notifies_nobody_composes_no_card() -> None:
    update = nvda_update()
    store = Store(update)
    composer = Composer()

    turn = _turn(store, Planner(_plan(update, notify=False)), composer, Sender())

    assert turn.status == "no_notification"
    assert store.names() == ["record_plan"] and composer.calls == 0


def test_a_failed_card_spends_only_its_intents_card_attempt() -> None:
    update = nvda_update()
    store = Store(update)
    sender = Sender()

    with pytest.raises(ProviderUnavailable):
        _turn(store, Planner(_plan(update)), Composer(error=ProviderUnavailable("rate limited")), sender)

    assert store.calls == [("record_plan", "notify"), ("card_failure", "ProviderUnavailable")]
    assert sender.cards == []


def test_copy_that_cannot_be_frozen_is_a_card_failure_too() -> None:
    update = nvda_update()
    store = Store(update)

    class UnsafeComposer(Composer):
        async def compose(self, claims: tuple[Any, ...]) -> CardCopy:
            self.calls += 1
            return copy_for(_plan(update), "看 https://evil.example 的标题")

    with pytest.raises(ValueError, match="news_card_copy_unsafe"):
        _turn(store, Planner(_plan(update)), UnsafeComposer(), Sender())

    assert store.calls[-1] == ("card_failure", "ValueError")


def test_a_frozen_card_is_reused_rather_than_composed_again() -> None:
    update = nvda_update()
    store = Store(update)
    plan = _plan(update)
    from tracefold.news.updates.notification import freeze_card

    store.card = freeze_card(plan, update, copy_for(plan))
    composer = Composer()
    sender = Sender()

    turn = _turn(store, Planner(plan), composer, sender)

    assert turn.status == "sent" and composer.calls == 0 and sender.cards == [store.card]
    assert "save_card" not in store.names()


def test_a_changed_selection_is_never_sent() -> None:
    update = nvda_update()
    store = Store(update, begin=False)
    sender = Sender()

    turn = _turn(store, Planner(_plan(update)), Composer(), sender)

    assert turn.status == "preflight_changed"
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
