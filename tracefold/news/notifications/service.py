"""One notification workflow owner: plan, freeze copy, send, and settle its durable intent."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from ..bus import DeferError, TransientError
from ..clock import clock_ms
from ..generation_capacity import (
    GENERATION_CAPACITY_WAIT,
    generation_capacity_wait_before_call,
    generation_stage,
)
from ..updates.contracts import EventUpdate
from ..updates.identity import identity
from ..updates.judgment import Budget, ContractFault, error_code
from .card import CardComposer, card_copy_material, freeze_card
from .contracts import FrozenCard, PlanTimings
from .planner import NotificationPlanner
from .ports import DeliveryTimings, IntentLease, NotificationStore, Sender, SendOutcome

NOTIFICATION_STAGE_SECONDS: Final = 60.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotificationTurn:
    """What one notification turn did.

    `status` is `no_work`, a plan action, a named CAS result, `plan_failed` or `card_failed` after that
    failure is recorded, or the settled send state. A settled send carries the intent it settled and the
    provider's outcome, so a delivery adapter can enrich exactly that receipt afterwards. A ready turn
    carries when its card was composed and when it became ready, which the send records beside its receipt.
    """

    status: str
    update: EventUpdate | None = None
    lease: IntentLease | None = None
    card: FrozenCard | None = None
    outcome: SendOutcome | None = None
    error_code: str | None = None
    card_started_at_ms: int | None = None
    card_finished_at_ms: int | None = None
    ready_at_ms: int | None = None


# What says the database could not answer this second, rather than that anything failed. It is never
# recorded against the work: the marker stays due and the next poll asks again.
_UNANSWERED: Final = (TransientError, DeferError)


class Notifications:
    """Plan a pending head, reserve its intent, compose and freeze its card, then send it once.

    Planning and card composition share one model stage deadline. The send does not: a paced provider
    entry may hold a card for longer than a model stage, and a wait that nothing sent is not an unknown
    outcome. The sender bounds its own provider call.
    """

    def __init__(
        self,
        store: NotificationStore,
        planner: NotificationPlanner,
        composer: CardComposer,
        *,
        clock: Callable[[], int] = clock_ms,
        stage_seconds: float = NOTIFICATION_STAGE_SECONDS,
    ) -> None:
        self.store = store
        self.planner = planner
        self.composer = composer
        self.clock = clock
        self.stage_seconds = stage_seconds

    async def process(self, event_id: str, channel: str, sender: Sender) -> NotificationTurn:
        prepared = await self.prepare(event_id, channel)
        return await self.finalize(prepared, sender)

    async def prepare(self, event_id: str, channel: str) -> NotificationTurn:
        """One notification turn for one channel, up to a frozen card ready to send.

        A failed plan spends one attempt of the work and a failed card one attempt of its intent; either is
        recorded and returned as `plan_failed` / `card_failed`, never raised, and neither touches adopted
        semantics. A database that cannot answer is not a failure of this work and is raised unrecorded.
        """

        budget = Budget.start(self.stage_seconds)
        started_at_ms = self.clock()
        started = time.monotonic()
        snapshot = None
        try:
            snapshot = await self.store.notification_snapshot(event_id, channel)
            if snapshot is None:
                return NotificationTurn("no_work")
            snapshot_ms = _elapsed_ms(started)
            # The stage deadline surfaces here as TimeoutError, so an expired plan is recorded like any
            # other failed one instead of leaving its work due again at once.
            with generation_stage() as planning:
                async with asyncio.timeout(budget.remaining()):
                    plan = await self.planner.plan(snapshot.update, snapshot.reader, budget, now_ms=self.clock())
            if planning.started_calls == 0 and any(
                row.reader is not None
                and row.reader.judgment is not None
                and row.reader.judgment.error_code == GENERATION_CAPACITY_WAIT
                for row in plan.claim_decisions
            ):
                await self.store.postpone_notification(event_id, channel, snapshot.update.content_revision)
                return NotificationTurn("plan_failed", update=snapshot.update, error_code=GENERATION_CAPACITY_WAIT)
            timings = plan.timings or PlanTimings()
            plan = plan.model_copy(
                update={
                    "timings": timings.model_copy(
                        update={
                            "due_at_ms": snapshot.work_due_at_ms,
                            "started_at_ms": started_at_ms,
                            "snapshot_ms": snapshot_ms,
                            "planned_at_ms": self.clock(),
                        }
                    )
                }
            )
            committed = await self.store.atomic_record_plan(plan)
        except asyncio.CancelledError:
            raise
        except _UNANSWERED:
            # Nothing about this work failed. It is put off once, uncharged, so a database that cannot
            # answer is not asked the same plan again every poll; if even that cannot be written, the
            # marker simply stays due.
            with contextlib.suppress(*_UNANSWERED):
                await self.store.postpone_notification(
                    event_id, channel, None if snapshot is None else snapshot.update.content_revision
                )
            raise
        except Exception as exc:
            code = error_code(exc, default="news_notification_plan")
            if generation_capacity_wait_before_call(exc):
                await self.store.postpone_notification(
                    event_id, channel, None if snapshot is None else snapshot.update.content_revision
                )
            else:
                await self.store.defer_notification(
                    event_id,
                    channel,
                    None if snapshot is None else snapshot.update.content_revision,
                    None if snapshot is None else snapshot.work_updated_at_ms,
                    error_code=code,
                )
            return NotificationTurn(
                "plan_failed", update=None if snapshot is None else snapshot.update, error_code=code
            )
        if committed.status != "committed" or committed.effective_plan is None:
            return NotificationTurn(committed.status, update=snapshot.update)
        plan = committed.effective_plan
        lease = committed.lease
        if plan.action != "notify" or lease is None:
            return NotificationTurn(plan.action, update=snapshot.update)
        card = lease.card
        card_started_at_ms = card_finished_at_ms = None
        if card is None:
            card_started_at_ms = self.clock()
            try:
                with generation_stage():
                    card = await self._card(lease, snapshot.update, budget)
            except asyncio.CancelledError:
                await self._release_unsent(lease)
                raise
            except _UNANSWERED:
                raise
            except Exception as exc:
                code = error_code(exc, default="news_card")
                if generation_capacity_wait_before_call(exc):
                    await self._release_unsent(lease)
                    await self.store.postpone_notification(event_id, channel, snapshot.update.content_revision)
                else:
                    await self.store.record_unsent_failure(lease, error_code=code, retryable=True)
                return NotificationTurn("card_failed", update=snapshot.update, lease=lease, error_code=code)
            card_finished_at_ms = self.clock()
        return NotificationTurn(
            "ready",
            update=snapshot.update,
            lease=lease,
            card=card,
            card_started_at_ms=card_started_at_ms,
            card_finished_at_ms=card_finished_at_ms,
            ready_at_ms=self.clock(),
        )

    async def finalize(self, prepared: NotificationTurn, sender: Sender) -> NotificationTurn:
        """Check current reader state and settle one send inside its paced opportunity."""

        if prepared.status != "ready":
            return prepared
        if prepared.lease is None or prepared.update is None or prepared.card is None:
            raise ValueError("news_prepared_notification_incomplete")
        return await self._send(prepared, prepared.lease, prepared.update, prepared.card, sender)

    async def release_ready(self, prepared: NotificationTurn) -> None:
        """Release a prepared owner that the coordinator will never finalize."""

        if prepared.status == "ready" and prepared.lease is not None:
            await self._release_unsent(prepared.lease)

    async def _release_unsent(self, lease: IntentLease) -> None:
        cleanup = asyncio.create_task(self.store.release_unsent_intent(lease))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=5)
            raise

    async def _settle_known(self, lease: IntentLease, card: FrozenCard, outcome: SendOutcome) -> None:
        settled_at_ms = self.clock()
        started_at = time.monotonic()

        async def commit() -> None:
            for attempt in range(3):
                try:
                    result = await self.store.settle_send(lease, card, outcome, settled_at_ms=settled_at_ms)
                    if result not in ("sent", "not_sent", "ambiguous", "terminal", "already_settled"):
                        raise RuntimeError("news_send_settlement_conflict")
                    logger.info(
                        "news send settlement intent_id=%s state=%s retries=%s elapsed_ms=%s",
                        lease.intent_id,
                        outcome.state,
                        attempt,
                        int((time.monotonic() - started_at) * 1000),
                    )
                    return
                except (DeferError, TransientError) as exc:
                    if attempt == 2:
                        raise RuntimeError("news_send_settlement_unavailable") from exc
                    await asyncio.sleep(0.25 * (attempt + 1))

        owner = asyncio.create_task(commit(), name=f"news-send-settle:{lease.intent_id}")
        try:
            await asyncio.shield(owner)
        except asyncio.CancelledError:
            # A provider result already exists. The owner retains that exact result while this
            # caller is cancelled; a bounded failure faults the sender instead of returning idle.
            try:
                await asyncio.wait_for(asyncio.shield(owner), timeout=12)
            except TimeoutError as exc:
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
                raise RuntimeError("news_send_settlement_unavailable") from exc
            raise

    async def _card(self, lease: IntentLease, update: EventUpdate, budget: Budget) -> FrozenCard:
        """Compose copy for exactly the selected claims and freeze it; the caller records a failure."""

        plan = lease.plan
        selected = tuple(claim for claim in update.claims if claim.ref in plan.selected_claim_refs)
        # An increment or a correction is written against the earlier message the reader already has.
        earlier = {claim.ref: context for claim in selected if (context := plan.earlier(claim.ref)) is not None}
        async with asyncio.timeout(budget.remaining()):
            cited = {citation.evidence_ref for claim in selected for citation in claim.citations}
            sources = {item.ref: item.source for item in update.evidence if item.ref in cited}
            input_digest = identity(
                "news_card_copy_input", self.composer.identity, card_copy_material(selected, sources, earlier)
            )
            copy = await self.store.lookup_card_copy(input_digest)
            logger.info(
                "news card copy event_id=%s intent_id=%s input_digest=%s reused=%s",
                update.event_id,
                lease.intent_id,
                input_digest,
                copy is not None,
            )
            if copy is None:
                copy = await self.composer.compose(selected, sources=sources, earlier=earlier)
        frozen = freeze_card(plan, update, copy)
        return await self.store.save_card(lease, frozen, copy=copy, input_digest=input_digest)

    async def _send(
        self,
        prepared: NotificationTurn,
        lease: IntentLease,
        update: EventUpdate,
        card: FrozenCard,
        sender: Sender,
    ) -> NotificationTurn:
        begin_started = False
        queued_at = time.monotonic()
        try:
            async with sender.send_slot():
                slot_at_ms = self.clock()
                logger.info(
                    "news send slot event_id=%s intent_id=%s wait_ms=%s",
                    update.event_id,
                    lease.intent_id,
                    int((time.monotonic() - queued_at) * 1000),
                )
                # All fallible local/target preparation precedes the durable sending boundary.
                preflight = await sender.preflight(card, plan=lease.plan, update=update)
                if preflight is not None:
                    # Provably never sent, and no `sending` row claims otherwise: a failure of the unsent
                    # intent, retried with the same identity and payload or ended under its attempt bound.
                    await self.store.record_unsent_failure(
                        lease,
                        error_code=preflight.error_code or "news_delivery_preflight_failed",
                        retryable=preflight.retryable,
                        retry_after_ms=preflight.retry_after_ms,
                    )
                    logger.info("news send not started intent_id=%s error=%s", lease.intent_id, preflight.error_code)
                    return NotificationTurn(preflight.state, update=update, lease=lease, card=card, outcome=preflight)
                # Pacer waiting and preflight are over. This short transaction checks
                # the current reader and head before the external side effect can begin.
                begin_started = True
                ready_at_ms = slot_at_ms if prepared.ready_at_ms is None else prepared.ready_at_ms
                begin_status = await self.store.atomic_begin_send(
                    lease,
                    card,
                    timings=DeliveryTimings(
                        card_started_at_ms=prepared.card_started_at_ms,
                        card_finished_at_ms=prepared.card_finished_at_ms,
                        ready_at_ms=ready_at_ms,
                        send_slot_wait_ms=max(0, slot_at_ms - ready_at_ms),
                    ),
                )
                if begin_status != "begun":
                    return NotificationTurn(begin_status, update=update, lease=lease, card=card)
                try:
                    provider_at = time.monotonic()
                    outcome = await sender.send(card, plan=lease.plan, update=update)
                    logger.info(
                        "news provider returned intent_id=%s state=%s elapsed_ms=%s",
                        lease.intent_id,
                        outcome.state,
                        int((time.monotonic() - provider_at) * 1000),
                    )
                    if outcome.payload_sha256 != card.payload_sha256:
                        raise ContractFault("news_sender_changed_frozen_payload")
                except BaseException as exc:
                    # A crash between begin_send and the provider boundary is not
                    # provably safe to replay. Keep that intent ambiguous.
                    ambiguous = SendOutcome(
                        state="ambiguous", payload_sha256=card.payload_sha256, error_code=type(exc).__name__
                    )
                    await self._settle_known(lease, card, ambiguous)
                    raise
                await self._settle_known(lease, card, outcome)
                logger.info("news send settled intent_id=%s state=%s", lease.intent_id, outcome.state)
                return NotificationTurn(outcome.state, update=update, lease=lease, card=card, outcome=outcome)
        except asyncio.CancelledError:
            if not begin_started:
                await self._release_unsent(lease)
            raise


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
