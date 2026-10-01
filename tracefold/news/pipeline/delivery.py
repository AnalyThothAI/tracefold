"""Bounded polling, preparation, finalization and reconciliation of durable News notifications."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, ClassVar, Final

from ..bus import DeferError, TransientError, now_ms
from ..notifications.contracts import NEWS_CHANNEL
from ..storage.errors import IntentLeaseLost
from ..telemetry import NewsWorkSemantics
from ..updates.judgment import ContractFault, ProviderUnavailable
from .delivery_enrichment import DeliveryEnrichment
from .notification_sender import NotificationSender
from .runtime import NewsDatabasePort, _sleep_or_stop

if TYPE_CHECKING:
    from ..notifications.service import Notifications, NotificationTurn

_DELIVERY_RECONCILE_SECONDS = 30.0
_DELIVERY_STARTUP_RECONCILE_RETRY_SECONDS = 0.25
_FINALIZER_DRAIN_SECONDS = 20.0
_NOTIFICATIONS_PER_TURN = 20
_DELIVERY_POLL_SECONDS = 1.0
_SETTLED_SEND_FAILURES: Final = (TimeoutError, ProviderUnavailable, ContractFault, ValueError, IntentLeaseLost)
logger = logging.getLogger(__name__)


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}")[:160]


class DelivererLoop:
    """Schedule bounded notification turns and reconcile owners whose process is gone.

    PostgreSQL owns the work and send ledger. Notifications owns planning and settlement;
    NotificationSender owns the provider boundary, and DeliveryEnrichment owns receipt edits.
    """

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        db: NewsDatabasePort,
        notification_sender: NotificationSender,
        enrichment: DeliveryEnrichment,
        notification_prepare_limit: int = 2,
        notifications: Notifications | None = None,
    ) -> None:
        self.db = db
        self.notification_sender = notification_sender
        self.enrichment = enrichment
        self.notifications = notifications
        if not 1 <= notification_prepare_limit <= 8:
            raise ValueError("news_notification_prepare_limit_invalid")
        self.notification_prepare_limit = notification_prepare_limit
        self._finalizers: set[asyncio.Task[NotificationTurn]] = set()
        self._sending_intents: set[str] = set()

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._reconcile_orphan_sends()
            except (TransientError, DeferError):
                await _sleep_or_stop(stop_event, _DELIVERY_STARTUP_RECONCILE_RETRY_SECONDS)
                continue
            break
        if stop_event.is_set():
            return
        # Unlike an initial-send ambiguity, an inherited edit intent cannot be left in a pretend in-flight state:
        # this process owns no edit task yet. Refuse to claim until PostgreSQL records that truth.
        startup_reconciliations = (
            (
                "news_delivery_edit_reconcile",
                lambda repos: repos.news.terminalize_interrupted_delivery_edits(now_ms=now_ms()),
            ),
        )
        for name, reconcile in startup_reconciliations:
            while not stop_event.is_set():
                try:
                    await self.db.tx(name, reconcile)
                except DeferError:
                    await _sleep_or_stop(stop_event, _DELIVERY_STARTUP_RECONCILE_RETRY_SECONDS)
                    continue
                break
            if stop_event.is_set():
                return
        claim_task = asyncio.create_task(
            self._claim_loop(stop_event=stop_event),
            name="news-delivery-claim",
        )
        reconcile_task = asyncio.create_task(
            self._reconcile_loop(stop_event=stop_event),
            name="news-delivery-reconcile",
        )
        tasks = {claim_task, reconcile_task}
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _reconcile_loop(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=_DELIVERY_RECONCILE_SECONDS)
            if stop_event.is_set():
                return
            with contextlib.suppress(TransientError, DeferError):
                await self._reconcile_orphan_sends()
            with contextlib.suppress(TransientError, DeferError):
                await self._reconcile_stale_delivery_edits()

    async def _reconcile_orphan_sends(self) -> None:
        """Hold ambiguous a `sending` row whose owner is gone -- never one this process is still settling."""

        owned = tuple(sorted(self._sending_intents))
        await self.db.tx(
            "news_delivery_reconcile",
            lambda repos: repos.news.notification_delivery.terminalize_interrupted_deliveries(
                now_ms=now_ms(), exclude_intent_ids=owned
            ),
        )

    async def _reconcile_stale_delivery_edits(self) -> None:
        await self.db.tx(
            "news_delivery_edit_stale_reconcile",
            lambda repos: repos.news.terminalize_stale_delivery_edits(now_ms=now_ms()),
        )

    async def _claim_loop(self, *, stop_event: asyncio.Event) -> None:
        """Run what is due, and sleep only when nothing was owed.

        Every business outcome of a turn is already durable and never reaches here; what reaches here
        is a database that cannot answer this second, and that is answered by waiting one poll rather
        than by faulting a capability.
        """

        while not stop_event.is_set():
            try:
                worked = await self.advance(stop_event=stop_event)
            except (TransientError, DeferError):
                worked = 0
            if not worked:
                await _sleep_or_stop(stop_event, _DELIVERY_POLL_SECONDS)

    async def advance(self, *, stop_event: asyncio.Event | None = None) -> int:
        """Continuously fill a bounded prepare/ready window from durable due work, one send at a time.

        A prepare task is one Event's turn and fails alone: whatever it raises is logged against that
        Event, whose marker stays due, and never reaches the send in flight or the other turns. The send
        is its own owner until its settlement. When `stop_event` is set no new turn starts, the turns
        still preparing are cancelled (which releases what they reserved), the send in flight is given
        a bounded wait to settle rather than being cancelled, and ready turns are released.
        """

        notifications = self.notifications
        if notifications is None or not self.notification_sender.available:
            return 0
        preparing: dict[asyncio.Task[NotificationTurn], tuple[str, float]] = {}
        ready: list[tuple[NotificationTurn, float]] = []
        finalizer: asyncio.Task[NotificationTurn] | None = None
        seen: set[str] = set()
        worked = 0

        def stopping() -> bool:
            return stop_event is not None and stop_event.is_set()

        async def fill() -> None:
            # The send slot is the process's own and serial; it does not take a preparation place.
            capacity = self.notification_prepare_limit - len(preparing) - len(ready)
            if capacity <= 0 or len(seen) >= _NOTIFICATIONS_PER_TURN or stopping():
                return
            try:
                due = await notifications.store.pending_notification_events(
                    NEWS_CHANNEL, _NOTIFICATIONS_PER_TURN + len(seen)
                )
            except (TransientError, DeferError):
                return
            for event_id in due:
                if capacity <= 0 or len(seen) >= _NOTIFICATIONS_PER_TURN:
                    break
                if event_id in seen:
                    continue
                seen.add(event_id)
                task = asyncio.create_task(
                    notifications.prepare(event_id, NEWS_CHANNEL), name=f"news-notification-prepare:{event_id}"
                )
                preparing[task] = (event_id, time.monotonic())
                capacity -= 1
                logger.info("news notification prepare started event_id=%s in_flight=%s", event_id, len(preparing))

        try:
            await fill()
            while (preparing or ready or finalizer is not None) and not stopping():
                if finalizer is None and ready:
                    prepared, ready_at = ready.pop(0)
                    logger.info(
                        "news notification ready finalized event_id=%s wait_ms=%s",
                        prepared.update.event_id if prepared.update else "unknown",
                        int((time.monotonic() - ready_at) * 1000),
                    )
                    finalizer = self._start_finalizer(notifications, prepared)
                await fill()
                active: set[asyncio.Task[NotificationTurn]] = set(preparing)
                if finalizer is not None:
                    active.add(finalizer)
                if not active:
                    continue
                finished, _ = await asyncio.wait(
                    active, timeout=_DELIVERY_POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED
                )
                for task in finished:
                    if task is finalizer:
                        finalizer = None
                        worked += self._finalized(task)
                        continue
                    event_id, started_at = preparing.pop(task)
                    try:
                        prepared = task.result()
                    except Exception as exc:
                        # Unrecorded: a database that could not answer, or a bug. The marker stays due,
                        # and nothing else in this turn -- above all the send in flight -- is touched.
                        logger.warning("news notification turn failed event_id=%s error=%s", event_id, _error_code(exc))
                        continue
                    logger.info(
                        "news notification prepared event_id=%s status=%s elapsed_ms=%s",
                        event_id,
                        prepared.status,
                        int((time.monotonic() - started_at) * 1000),
                    )
                    if prepared.status in {"plan_failed", "card_failed"}:
                        logger.warning(
                            "news notification turn failed event_id=%s status=%s error=%s",
                            event_id,
                            prepared.status,
                            prepared.error_code,
                        )
                    if prepared.status == "ready":
                        ready.append((prepared, time.monotonic()))
                    elif prepared.status != "no_work":
                        worked += 1
            return worked
        finally:
            for task in preparing:
                task.cancel()
            if preparing:
                await asyncio.gather(*preparing, return_exceptions=True)
            if finalizer is not None:
                if not finalizer.done():
                    await asyncio.wait({finalizer}, timeout=_FINALIZER_DRAIN_SECONDS)
                if finalizer.done():
                    with contextlib.suppress(Exception):
                        self._finalized(finalizer)
                else:
                    # Still in the provider or its settlement: it keeps running as its own owner.
                    self._finalizers.add(finalizer)
                    finalizer.add_done_callback(self._finalizers.discard)
            for prepared, _ready_at in ready:
                await notifications.release_ready(prepared)

    def _start_finalizer(
        self, notifications: Notifications, prepared: NotificationTurn
    ) -> asyncio.Task[NotificationTurn]:
        intent_id = prepared.lease.intent_id if prepared.lease is not None else None
        task = asyncio.create_task(
            notifications.finalize(prepared, self.notification_sender),
            name=f"news-notification-finalize:{prepared.update.event_id if prepared.update else 'unknown'}",
        )
        if intent_id is not None:
            # The reconciliation never holds a send ambiguous while this process still owns it.
            self._sending_intents.add(intent_id)
            task.add_done_callback(lambda _task: self._sending_intents.discard(intent_id))
        return task

    def _finalized(self, task: asyncio.Task[NotificationTurn]) -> int:
        """Account for one finished send; an unsettled failure is raised to fault the capability."""

        try:
            turn = task.result()
        except _SETTLED_SEND_FAILURES as exc:
            logger.warning("news notification finalizer failed error=%s", _error_code(exc))
            return 1
        if turn.status == "sent":
            self.enrichment.enrich_sent(turn)
        return 0 if turn.status == "no_work" else 1

    async def drain(self) -> None:
        """Settle every owned send before finishing receipt-bound enrichment tasks."""
        if self._finalizers:
            await asyncio.gather(*tuple(self._finalizers), return_exceptions=True)
        await self.enrichment.drain()
