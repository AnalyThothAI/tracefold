"""At-most-once News update delivery: plan, freeze, send once, then enrich the receipt in place (#706).

One turn per pending `news_notification_work` marker: the core `Notifications` plans the adopted head
against what the reader actually received, reserves one stable intent, composes and freezes that
intent's Chinese card only then, rechecks it before the send and hands it to this loop, which is the
channel side -- the preflight, the paced provider call, the provider's own receipt, and the Telegram
enrichment edit that fills quotes and tradability into the message already sent. A card failure costs
that intent's card attempt and nothing else; an outcome the provider did not report is held ambiguous
and never sent again. One Event's failure is that Event's: it never cancels another Event's send.

Settled EventUpdate receipts keep their edit reconciliation by intent id.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Final, Protocol, runtime_checkable

from tracefold.platform.resource import ResourceAdmissionTimeout

from ..bus import DeferError, TransientError, now_ms
from ..delivery import (
    news_update_card,
    reader_market_movements,
    reader_trade_targets,
    update_card_assets,
    update_news_at_ms,
    update_primary_symbols,
)
from ..delivery_contracts import (
    DELIVERY_FAILURE_RETRIABLE,
    DELIVERY_FAILURE_UNKNOWN,
    classify_delivery_failure,
    retry_after_ms,
)
from ..feishu_card import feishu_card
from ..market_review.pricing import (
    QUOTE_READ_TIMEOUT_SECONDS,
    Candle,
    PriceInstrument,
    PricePoint,
    QuoteRequest,
    parse_change_pct,
    select_candle,
)
from ..models import (
    MarketAsset,
    ReaderDeliveryPresentation,
    TelegramDeliveryReceipt,
    base_symbol,
    market_type_of,
)
from ..reader_card import ReaderCard
from ..storage.event_updates import IntentLeaseLost
from ..telemetry import NewsWorkSemantics
from ..tradability import (
    TRADABILITY_REVIEW_TIMEOUT_SECONDS,
    TradabilityMatch,
    TradabilityReview,
    TradabilityVerifier,
)
from ..updates.contracts import EventUpdate
from ..updates.judgment import ContractFault, ProviderUnavailable
from ..updates.notification import NEWS_CHANNEL, FrozenCard, NotificationPlan
from ..updates.ports import SendOutcome
from ..updates.service import Notifications, NotificationTurn
from .runtime import NewsDatabasePort, _sleep_or_stop

_DELIVERY_CANDLE_TIMEOUT_SECONDS = 2.0
_DELIVERY_PRICE_SOURCE_TIMEOUT_SECONDS = 2.0
_DELIVERY_CANDLE_GAP_MS = 90_000
_ONE_HOUR_MS = 3_600_000
_DELIVERY_EDIT_TIMEOUT_SECONDS = 8.0
# How often sends and edits whose owner is gone are held ambiguous while the loop runs, not only at start.
_DELIVERY_RECONCILE_SECONDS = 30.0
_DELIVERY_STARTUP_RECONCILE_RETRY_SECONDS = 0.25
_DELIVERY_PREPARE_TIMEOUT_SECONDS = 8.0
# How long a stopping turn waits for a send already in flight to reach its settlement: the provider
# call's own bound plus the settlement's retries. A send is never cancelled to make a stop faster.
_FINALIZER_DRAIN_SECONDS = 20.0

# How many pending notification markers one turn takes before the loop goes back to the top. A burst
# drains in one turn instead of one per poll; an empty turn ends immediately.
_NOTIFICATIONS_PER_TURN = 20
# Idle poll. The semantic worker commits notification work in the adoption transaction; one second is
# the whole latency polling costs against a semantic stage that spends seconds in the model.
_DELIVERY_POLL_SECONDS = 1.0
# What a send may raise after the core has already settled it -- ambiguous, because the sender could not
# account for its own call -- and the loop survives. Anything else from a send is unclassified and faults
# `news_delivery`: a receipt this process could not record is not something to carry on past.
_SETTLED_SEND_FAILURES: Final = (TimeoutError, ProviderUnavailable, ContractFault, ValueError, IntentLeaseLost)

logger = logging.getLogger(__name__)


async def read_display_quotes(
    db: NewsDatabasePort,
    requests: Sequence[QuoteRequest],
    *,
    now_ms: int,
    name: str,
) -> list[dict[str, Any]]:
    """One `news_quote_snapshots` row per symbol, on one short session, or nothing at all.

    This is the whole of the quote rule shared by the News first card and the market card (#562 §3):
    one bounded read, no transaction held across it, the pricing domain's own budget, and any
    failure -- admission, overrun, timeout, a repository raising -- degrading to no quote rather than
    to a placeholder, a zero or a retry. What "fresh" and "24 h" mean is *not* restated here: the
    read model applies the freshness and reference-age rules in SQL, and `reader_card.quote_line`
    drops anything not fresh, so there is exactly one place each of those constants is read.
    """

    if not requests:
        return []
    try:
        rows = await db.read(
            name,
            lambda repos: repos.price.quotes_for_symbols(list(requests), now_ms=now_ms),
            timeout_seconds=QUOTE_READ_TIMEOUT_SECONDS,
        )
    except Exception:  # price is display-only; all failures degrade to no line
        return []
    return [dict(row) for row in rows or [] if isinstance(row, Mapping)]


async def read_pushed_news(db: NewsDatabasePort, symbol: str, *, now_ms: int, name: str) -> dict[str, Any]:
    """What News has already told this reader about one instrument, on one short session (#582 §3.3).

    The twin of `read_display_quotes`, deliberately: one bounded read on the same lane, the same
    pricing-domain budget, no transaction held across it, and every failure -- admission, overrun,
    timeout, a repository raising -- degrading to "no news to show" rather than to a placeholder or a
    retry. It lives beside the quote read rather than at the composition site because the budget is
    News's own: a Workers adapter deciding for itself how long a card may wait would be a second
    answer to a question this package has already answered.

    What "already told" and "48 h" mean is not restated here: the two statements in
    `storage/decisions.py` own the window, the delivered-card predicate and the alias resolution.

    One thing is enforced rather than assumed: an entry the read could not put a title on never
    reaches the card. The card prints one line per pushed entry and counts what it printed, so a
    titleless row would be either a line saying only a time or a count with nothing under it.
    """

    requested = str(symbol or "").strip()
    if not requested:
        return {"pushed": [], "total": 0}
    try:
        answer = await db.read(
            name,
            lambda repos: repos.news.pushed_news_for_symbol(requested, now_ms=now_ms),
            timeout_seconds=QUOTE_READ_TIMEOUT_SECONDS,
        )
    except Exception:  # display-only; all failures degrade to no line
        return {"pushed": [], "total": 0}
    if not isinstance(answer, Mapping):
        return {"pushed": [], "total": 0}
    rows = answer.get("pushed")
    return {
        "pushed": [
            dict(row)
            for row in (rows if isinstance(rows, Sequence) and not isinstance(rows, str | bytes) else ())
            if isinstance(row, Mapping) and str(row.get("headline_zh") or "").strip()
        ],
        "total": answer.get("total", 0),
    }


DeliveryCandleFetcher = Callable[[str, int, int], Awaitable[Sequence[Candle]]]
DeliveryCandleFetcherFor = Callable[[str], DeliveryCandleFetcher | None]
DeliveryPriceFetcher = Callable[[str, Sequence[int]], Awaitable[Mapping[int, PricePoint]]]
DeliveryPriceFetcherFor = Callable[[str], DeliveryPriceFetcher | None]


class NewsPushSender(Protocol):
    """Synchronous provider boundary executed by the finite-operation runner."""

    def prepare(self) -> None: ...

    def send_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


@runtime_checkable
class EditableNewsPushSender(Protocol):
    """Provider capability for replacing one already-receipted reader message in place."""

    def edit_card(
        self,
        receipt: Mapping[str, Any],
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _EnrichmentEditContext:
    """What the enrichment task needs. Not the sender: the shared entry owns the one it may edit."""

    intent_id: str
    event_id: str
    card: FrozenCard
    plan: NotificationPlan
    update: EventUpdate
    shown: tuple[MarketAsset, ...]
    receipt: TelegramDeliveryReceipt
    presentation: ReaderDeliveryPresentation
    tradability_symbols: tuple[str, ...]


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}")[:160]


# The initial-send deadline, unchanged, named once now that two owners share the entry.
_INITIAL_SEND_TIMEOUT_SECONDS = 8.0


class InitialSendEntry:
    """The one place a card leaves this process, whether it is a first send or an edit.

    Ordinary News, its enrichment edit, and market notifications all queue here (#553 §5.2). Sharing
    it is the point: the operator configured one `min_interval_seconds`, and two independent pacers
    would have meant the channel could be interrupted twice as often as the number they set. That was
    not hypothetical -- the Deliverer kept a second lock and a second stamp for its edit, so on
    Telegram, where every News card is edited once, the real outbound rate was twice the configured
    one and the promise in this docstring was false (#604 N3). The lock is also what stops two loops
    being inside the provider at the same time, which the previous shape -- a bare interval on the
    Deliverer, serialised only by its own `prefetch=1` -- did not do for anyone else.

    `asyncio.Lock` admits waiters in arrival order, so the queueing is fair by construction: a burst
    of market cards cannot starve a News card that was already waiting, and neither can hold the entry
    across anything but its own one provider call.
    """

    def __init__(
        self,
        *,
        sender: NewsPushSender | None,
        finite_operations: Any,
        min_interval_seconds: float,
        timeout_seconds: float = _INITIAL_SEND_TIMEOUT_SECONDS,
    ) -> None:
        self._sender = sender
        self._finite = finite_operations
        self.min_interval = float(min_interval_seconds)
        self._timeout_seconds = float(timeout_seconds)
        self._lock = asyncio.Lock()
        self._last_send_at = 0.0

    @property
    def available(self) -> bool:
        """Whether a sender was configured at all. Not a health check -- composition already decided."""

        return self._sender is not None

    @property
    def sender(self) -> NewsPushSender | None:
        return self._sender

    async def send_prepared_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
        operation: str = "news_delivery_send",
        prepare: bool = True,
    ) -> Mapping[str, Any]:
        """Send one already-built card. Nothing here enriches or settles it.

        Both shapes of the same card travel together: the `ReaderCard` a channel serializes for
        itself, and the channel payload the ledger froze, which the channel that owns that wire shape
        posts verbatim. Neither is derived from the other here.

        The caller owns idempotency and the receipt, exactly as the Deliverer always has: this is the
        target check, the pacing and the provider call, and no part of either domain's decision.

        `prepare=False` is for a caller that already validated the target *earlier on purpose*. The
        Deliverer prepares inside its reserved send slot before the durable `sending` transition,
        so a bad channel settles as not_sent without a provider call. The market loop takes the
        default, because Telegram refuses `send_card` outright on an unvalidated target and a card
        that skipped the check would fail on a channel that is in fact fine.
        """

        sender = self._sender
        if sender is None:
            raise RuntimeError("news_delivery_sender_unavailable")
        async with self._paced():
            return await self._send_reserved_card(
                card,
                channel_payload=channel_payload,
                presentation=presentation,
                operation=operation,
                prepare=prepare,
            )

    async def _send_reserved_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None,
        operation: str,
        prepare: bool,
    ) -> Mapping[str, Any]:
        """Called by the initial entry or by News while its send slot is held."""

        sender = self._sender
        if sender is None:
            raise RuntimeError("news_delivery_sender_unavailable")
        if prepare:
            await self._finite.run("news_delivery_prepare", sender.prepare, timeout_seconds=self._timeout_seconds)
        receipt: Mapping[str, Any] = await self._finite.run(
            operation,
            sender.send_card,
            card,
            channel_payload=dict(channel_payload),
            presentation=presentation,
            timeout_seconds=self._timeout_seconds,
        )
        return receipt

    async def send_prepared_edit(
        self,
        receipt: Mapping[str, Any],
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
        operation: str = "news_delivery_edit",
        timeout_seconds: float = _DELIVERY_EDIT_TIMEOUT_SECONDS,
    ) -> Mapping[str, Any]:
        """Replace one already-sent card in place, behind the same lock and the same interval.

        An edit is an outbound provider message like any other, and the channel counts it against the
        same per-chat rate a send is counted against. The Deliverer used to pace it separately, which
        made the operator's one interval mean two different things at once; the only thing the caller
        still owns is the durable `editing` intent it must hold before it gets here.

        `allow_shutdown` is the one asymmetry and it is the caller's contract, not this entry's: an
        edit that is still in flight when the process is asked to stop may finish, because the message
        it is replacing is already on the reader's screen either way.
        """

        sender = self._sender
        if not isinstance(sender, EditableNewsPushSender):
            raise RuntimeError("news_delivery_editable_sender_unavailable")
        async with self._paced():
            edited: Mapping[str, Any] = await self._finite.run(
                operation,
                sender.edit_card,
                dict(receipt),
                card,
                channel_payload=dict(channel_payload),
                presentation=presentation,
                timeout_seconds=timeout_seconds,
                allow_shutdown=True,
            )
            return edited

    @contextlib.asynccontextmanager
    async def _paced(self) -> AsyncIterator[None]:
        """Hold the entry for exactly one provider call, no sooner than the operator's interval.

        The stamp covers the whole held block, not just a successful call. A `prepare` that raises is
        still a provider call this process just made, and leaving the stamp stale would let the next
        caller compute `wait <= 0` -- so a turn draining its card budget against a broken target would
        hammer the preflight with no interval between attempts at all.
        """

        async with self._lock:
            try:
                wait = self.min_interval - (time.monotonic() - self._last_send_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                yield
            finally:
                self._last_send_at = time.monotonic()


class DelivererLoop:
    """The channel side of News notifications: one frozen send per intent, then its enrichment edit.

    Every turn walks the due `news_notification_work` markers and runs one core `Notifications` turn for
    each, with this loop as its `Sender`. The durable to-do list is PostgreSQL's: the marker, the intent
    row in `news_delivery_queue` and the `news_deliveries` ledger. `news_deliveries` is the only ledger of
    what a reader was sent, and an update row there keeps the exact frozen body and provider receipt.
    """

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        db: NewsDatabasePort,
        sender: NewsPushSender | None,
        finite_operations: Any,
        min_interval_seconds: float,
        notification_prepare_limit: int = 2,
        notifications: Notifications | None = None,
        candle_fetcher_for: DeliveryCandleFetcherFor | None = None,
        price_fetcher_for: DeliveryPriceFetcherFor | None = None,
        tradability_verifier: TradabilityVerifier | None = None,
    ) -> None:
        self.db = db
        self.sender = sender
        self.finite = finite_operations
        self.notifications = notifications
        if not 1 <= notification_prepare_limit <= 8:
            raise ValueError("news_notification_prepare_limit_invalid")
        self.notification_prepare_limit = notification_prepare_limit
        # The Deliverer owns the entry and composition hands the same object to the market loop, so
        # there is one pacer for the process rather than one per caller who remembered to share it.
        # The enrichment edit goes through it too (#604 N3).
        self.send_entry = InitialSendEntry(
            sender=sender, finite_operations=finite_operations, min_interval_seconds=min_interval_seconds
        )
        self._candle_fetcher_for = candle_fetcher_for
        self._price_fetcher_for = price_fetcher_for
        self._tradability_verifier = tradability_verifier
        self._prepared_send: tuple[str, ReaderCard, Mapping[str, Any], ReaderDeliveryPresentation] | None = None
        self._edit_tasks: set[asyncio.Task[None]] = set()
        # Sends a stopped turn handed off unfinished, and the intents of every send this process owns.
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
            lambda repos: repos.news.terminalize_interrupted_deliveries(now_ms=now_ms(), exclude_intent_ids=owned),
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
        if notifications is None or self.sender is None:
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
            notifications.finalize(prepared, self),
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
            self._enrich_sent(turn)
        return 0 if turn.status == "no_work" else 1

    # ------------------------------------------------------------------ the `Sender` port
    @contextlib.asynccontextmanager
    async def send_slot(self) -> AsyncIterator[None]:
        async with self.send_entry._paced():
            try:
                yield
            finally:
                self._prepared_send = None

    async def preflight(self, card: FrozenCard, *, plan: NotificationPlan, update: EventUpdate) -> SendOutcome | None:
        """Prepare target and render wire body before the durable sending transition.

        The target check only reads, so whatever it fails with -- a refusal, a timeout, a 5xx, a closed
        or saturated local capability -- proves the card was not sent, and waiting may cure it: it is a
        retryable not-sent, and the intent's own attempt bound is what stops a target that stays broken.
        """
        sender = self.sender
        sha = card.payload_sha256
        if sender is None:
            return SendOutcome(state="not_sent", payload_sha256=sha, error_code="news_delivery_unavailable")
        try:
            await self.finite.run(
                "news_delivery_prepare", sender.prepare, timeout_seconds=_DELIVERY_PREPARE_TIMEOUT_SECONDS
            )
        except Exception as exc:
            return SendOutcome(
                state="not_sent",
                payload_sha256=sha,
                error_code=_error_code(exc),
                retryable=True,
                retry_after_ms=retry_after_ms(exc) or None,
            )
        editable = isinstance(sender, EditableNewsPushSender)
        shown = update_card_assets(update, card.claim_refs)
        news_at_ms = update_news_at_ms(update, card.claim_refs)
        # A channel that cannot be edited gets its quotes now; an editable one is sent first and edited.
        quotes = [] if editable else await self._market_data(shown, now_ms(), news_at_ms=news_at_ms)
        try:
            reader_card = news_update_card(
                card, plan=plan, update=update, assets=[asset.symbol for asset in shown], quotes=quotes
            )
        except ValueError as exc:
            return SendOutcome(state="not_sent", payload_sha256=sha, error_code=f"news_delivery_render_failed:{exc}")
        presentation = (
            ReaderDeliveryPresentation(news_at_ms=news_at_ms, market_data_state="pending")
            if editable
            else ReaderDeliveryPresentation(
                news_at_ms=news_at_ms,
                trade_targets=reader_trade_targets(quotes),
                market_movements=reader_market_movements([asset.symbol for asset in shown], quotes),
            )
        )
        self._prepared_send = (sha, reader_card, feishu_card(reader_card), presentation)
        return None

    async def send(self, card: FrozenCard, *, plan: NotificationPlan, update: EventUpdate) -> SendOutcome:
        """Call the provider for the prepared frozen body and report its actual outcome."""

        prepared = self._prepared_send
        if prepared is None or prepared[0] != card.payload_sha256:
            raise RuntimeError("news_delivery_send_without_preflight")
        sha, reader_card, channel_payload, presentation = prepared
        try:
            result = await self.send_entry._send_reserved_card(
                reader_card,
                channel_payload=channel_payload,
                presentation=presentation,
                operation="news_delivery_send",
                prepare=False,
            )
        except Exception as exc:
            if isinstance(exc, ResourceAdmissionTimeout):
                # The call was never submitted: nothing left this process, and the capability may free up.
                return SendOutcome(state="not_sent", payload_sha256=sha, error_code=_error_code(exc), retryable=True)
            failure = classify_delivery_failure(exc)
            if failure == DELIVERY_FAILURE_UNKNOWN:
                return SendOutcome(state="ambiguous", payload_sha256=sha, error_code=_error_code(exc))
            return SendOutcome(
                state="not_sent",
                payload_sha256=sha,
                error_code=_error_code(exc),
                retryable=failure == DELIVERY_FAILURE_RETRIABLE,
                retry_after_ms=retry_after_ms(exc) or None,
            )
        receipt = dict(result)
        message_id = receipt.get("message_id")
        return SendOutcome(
            state="sent",
            payload_sha256=sha,
            # Telegram answers with its message id; a Feishu webhook answers with none, and says so.
            message_id=None if message_id is None else str(message_id),
            receipt=receipt,
        )

    # ------------------------------------------------------------------ Telegram enrichment
    def _enrich_sent(self, turn: NotificationTurn) -> None:
        """Start the in-place edit a sent card on an editable channel earns: quotes, then tradability."""

        sender = self.sender
        if (
            not isinstance(sender, EditableNewsPushSender)
            or turn.lease is None
            or turn.card is None
            or turn.update is None
            or turn.outcome is None
        ):
            return
        card, plan, update = turn.card, turn.lease.plan, turn.update
        shown = tuple(update_card_assets(update, card.claim_refs))
        symbols = update_primary_symbols(update, card.claim_refs)
        # One named instrument is what a catalogue check can answer about; a card about several, or
        # about none, has no single contract to find.
        tradability_symbols = symbols if self._tradability_verifier is not None and len(symbols) == 1 else ()
        if not shown and not tradability_symbols:
            return
        try:
            receipt = TelegramDeliveryReceipt.model_validate(turn.outcome.receipt or {})
        except ValueError:
            logger.warning("News delivery enrichment edit failed: news_delivery_edit_receipt_invalid")
            return
        context = _EnrichmentEditContext(
            intent_id=turn.lease.intent_id,
            event_id=update.event_id,
            card=card,
            plan=plan,
            update=update,
            shown=shown,
            receipt=receipt,
            presentation=ReaderDeliveryPresentation(news_at_ms=update_news_at_ms(update, card.claim_refs)),
            tradability_symbols=tradability_symbols,
        )
        task = asyncio.create_task(self._enrich_and_edit(context), name=f"news-delivery-edit-{update.event_id[:12]}")
        self._edit_tasks.add(task)
        task.add_done_callback(self._edit_tasks.discard)

    async def _enrich_and_edit(self, context: _EnrichmentEditContext) -> None:
        intent_started = False
        try:
            quotes, tradability_review = await asyncio.gather(
                self._market_data(
                    context.shown, context.receipt.pushed_at_ms, news_at_ms=context.presentation.news_at_ms
                ),
                self._tradability_review(context),
            )
            resolved_shown = tuple(context.shown)
            if (
                tradability_review is not None
                and tradability_review.state == "matched"
                and not reader_trade_targets(quotes)
            ):
                quotes = await self._market_data_for_matches(
                    tradability_review.matches,
                    context.receipt.pushed_at_ms,
                    news_at_ms=context.presentation.news_at_ms,
                )
                if not resolved_shown:
                    # A contract the catalogue verifier found by exact name: its own instrument class is
                    # the market, because the match *is* the instrument (#651 §6.2).
                    resolved_shown = tuple(
                        dict.fromkeys(
                            MarketAsset(base_symbol(match.requested_symbol), market_type_of(match.instrument_class))
                            for match in tradability_review.matches
                            if match.requested_symbol
                        )
                    )
            reader_card = news_update_card(
                context.card,
                plan=context.plan,
                update=context.update,
                assets=[asset.symbol for asset in resolved_shown],
                quotes=quotes,
                # The catalogue's authoritative "nothing here can be traded" is a fact about the card,
                # so it is set on the card and every channel prints it (#562 PR-C/PR-E). It is printed
                # only about a candidate specific enough to be a ticker, and never removes the card.
                untradeable=(
                    tradability_review is not None
                    and tradability_review.state == "absent"
                    and tradability_review.deletion_safe
                    and not reader_trade_targets(quotes)
                ),
            )
            card_payload = feishu_card(reader_card)
            if tradability_review is not None:
                card_payload["tradability_review"] = tradability_review.model_dump(mode="json", exclude_none=True)
            presentation = replace(
                context.presentation,
                trade_targets=reader_trade_targets(quotes),
                market_movements=reader_market_movements([a.symbol for a in resolved_shown], quotes),
            )
            # The durable `editing` intent is claimed before the entry is, not inside it: the CAS is
            # what makes this the only task editing this receipt, and holding the process-wide send
            # lock across a PostgreSQL round trip would put a market card behind a database instead of
            # behind one provider call.
            intent_started = bool(
                await self.db.tx(
                    "news_delivery_begin_edit",
                    lambda repos: repos.news.begin_delivery_edit(
                        intent_id=context.intent_id,
                        card=card_payload,
                        receipt=context.receipt.canonical(),
                        now_ms=now_ms(),
                    ),
                )
            )
            if not intent_started:
                logger.warning("News delivery enrichment edit failed: news_delivery_edit_intent_conflict")
                return
            result = await self.send_entry.send_prepared_edit(
                context.receipt.canonical(),
                reader_card,
                channel_payload=card_payload,
                presentation=presentation,
            )
            try:
                updated_receipt = TelegramDeliveryReceipt.model_validate(result)
            except ValueError as exc:
                raise RuntimeError("news_delivery_edit_receipt_invalid") from exc
            if updated_receipt.edited_at_ms is None:
                raise RuntimeError("news_delivery_edit_receipt_unsettled")
            recorded = bool(
                await self.db.tx(
                    "news_delivery_settle_edit",
                    lambda repos: repos.news.settle_delivery_edit(
                        intent_id=context.intent_id,
                        receipt=updated_receipt.canonical(),
                        now_ms=now_ms(),
                    ),
                )
            )
            if not recorded:
                await self._mark_edit_ambiguous(context, "news_delivery_edit_receipt_conflict")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_code = getattr(exc, "code", None) or f"{type(exc).__module__}.{type(exc).__name__}"
            if intent_started:
                await self._mark_edit_ambiguous(context, str(error_code))
            logger.warning("News delivery enrichment edit failed: %s", error_code)

    async def _tradability_review(self, context: _EnrichmentEditContext) -> TradabilityReview | None:
        verifier = self._tradability_verifier
        if verifier is None or not context.tradability_symbols:
            return None
        # The catalogue check reads identities out of text as well as the symbol: the selected claims'
        # own statements stand where the source title stood, and the frozen headline beside them.
        claims = {claim.ref: claim for claim in context.update.claims}
        statements = "\n".join(claims[ref].statement for ref in context.card.claim_refs if ref in claims)
        try:
            async with asyncio.timeout(TRADABILITY_REVIEW_TIMEOUT_SECONDS):
                raw = await verifier.review(
                    event={"leader_title": statements},
                    verdict={"headline_zh": context.card.headline_zh},
                    symbols=list(context.tradability_symbols),
                )
            return raw if isinstance(raw, TradabilityReview) else TradabilityReview.model_validate(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            return TradabilityReview(
                state="incomplete",
                candidates=context.tradability_symbols,
                checked_venues=(),
                failed_venues=(),
                matches=(),
                reason_zh="交易所目录核验超时或返回异常，按安全规则保留消息。",
            )

    async def _mark_edit_ambiguous(self, context: _EnrichmentEditContext, error_code: str) -> None:
        try:
            recorded = await self.db.tx(
                "news_delivery_ambiguous_edit",
                lambda repos: repos.news.mark_delivery_edit_ambiguous(
                    intent_id=context.intent_id,
                    receipt=context.receipt.canonical(),
                    error_code=error_code[:160],
                    now_ms=now_ms(),
                ),
            )
            if not recorded:
                logger.warning("News delivery enrichment edit failed: news_delivery_edit_ambiguous_conflict")
        except Exception as exc:
            bounded = getattr(exc, "code", None) or f"{type(exc).__module__}.{type(exc).__name__}"
            logger.warning("News delivery enrichment edit failed: %s", bounded)

    async def _market_data(
        self,
        shown: Sequence[MarketAsset],
        stamp: int,
        *,
        news_at_ms: int | None,
    ) -> list[dict[str, Any]]:
        """Fresh push prices plus the two historical anchors rendered on the card.

        The caller passes the same code-verified asset list to the renderer, so
        the facts and quote lines cannot describe different symbols. Resolution
        remains owned by PriceRepository. Every price-plane failure returns an
        empty display value and leaves the already-made send decision untouched.
        """

        if not shown:
            return []
        if self._price_fetcher_for is not None:
            return await self._point_market_data(shown, stamp, news_at_ms=news_at_ms)
        quotes = await read_display_quotes(
            self.db,
            [QuoteRequest(asset.symbol, asset.market_type) for asset in shown],
            now_ms=stamp,
            name="news_delivery_quotes",
        )
        if self._candle_fetcher_for is None:
            return quotes
        news_target_ms = (
            news_at_ms
            if isinstance(news_at_ms, int) and not isinstance(news_at_ms, bool) and 0 < news_at_ms <= stamp
            else None
        )
        tasks: list[Awaitable[tuple[int, Sequence[Candle]] | None]] = []
        for index, quote in enumerate(quotes):
            if quote.get("state") != "fresh":
                continue
            venue = str(quote.get("venue") or "").strip()
            venue_symbol = str(quote.get("venue_symbol") or "").strip()
            fetcher = self._candle_fetcher_for(venue) if venue and venue_symbol else None
            if fetcher is None:
                continue
            targets = [stamp - _ONE_HOUR_MS]
            if news_target_ms is not None:
                targets.append(news_target_ms)
            start_ms = min(targets) - _DELIVERY_CANDLE_GAP_MS
            tasks.append(self._delivery_candles(index, fetcher, venue_symbol, start_ms, stamp))
        if not tasks:
            return quotes
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) or result is None:
                continue
            index, candles = result
            hour = select_candle(candles, target_ms=stamp - _ONE_HOUR_MS, max_gap_ms=_DELIVERY_CANDLE_GAP_MS)
            if hour is not None:
                quotes[index]["price_one_hour_before_push"] = str(hour.close)
            if news_target_ms is not None:
                news = select_candle(candles, target_ms=news_target_ms, max_gap_ms=_DELIVERY_CANDLE_GAP_MS)
                if news is not None:
                    quotes[index]["price_at_news"] = str(news.close)
        return quotes

    async def _point_market_data(
        self,
        shown: Sequence[MarketAsset],
        stamp: int,
        *,
        news_at_ms: int | None,
    ) -> list[dict[str, Any]]:
        """Trade-first anchors with whole-calculation venue failover.

        A candidate is accepted only as one unit: current, news and one-hour prices all retain the same
        ``(venue, venue_symbol)``. Partial values are kept only if no later venue can provide the complete set.
        """

        requests = [QuoteRequest(asset.symbol, asset.market_type) for asset in shown]
        try:
            rows, candidates = await self.db.read(
                "news_delivery_price_sources",
                lambda repos: (
                    repos.price.quotes_for_symbols(requests, now_ms=stamp),
                    repos.price.instruments_for_symbols(requests),
                ),
                timeout_seconds=QUOTE_READ_TIMEOUT_SECONDS,
            )
        except Exception:
            return []
        originals = {
            str(row.get("requested_symbol") or ""): dict(row) for row in rows or [] if isinstance(row, Mapping)
        }
        news_target = (
            news_at_ms
            if isinstance(news_at_ms, int) and not isinstance(news_at_ms, bool) and 0 < news_at_ms <= stamp
            else None
        )
        tasks = [
            self._point_quote(
                request.symbol,
                originals.get(request.symbol, {}),
                tuple(candidates.get(request, ())),
                stamp=stamp,
                news_target_ms=news_target,
            )
            for request in requests
        ]
        resolved = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[dict[str, Any]] = []
        for symbol, result in zip((request.symbol for request in requests), resolved, strict=True):
            if isinstance(result, BaseException):
                fallback = originals.get(symbol)
                if fallback:
                    out.append(dict(fallback))
            elif result:
                out.append(result)
        return out

    async def _market_data_for_matches(
        self,
        matches: Sequence[TradabilityMatch],
        stamp: int,
        *,
        news_at_ms: int | None,
    ) -> list[dict[str, Any]]:
        """Price a freshly discovered exact contract without waiting for the periodic universe snapshot."""

        if not matches:
            return []
        news_target = (
            news_at_ms
            if isinstance(news_at_ms, int) and not isinstance(news_at_ms, bool) and 0 < news_at_ms <= stamp
            else None
        )
        targets = [stamp, stamp - _ONE_HOUR_MS, stamp - 24 * _ONE_HOUR_MS]
        if news_target is not None:
            targets.append(news_target)
        first_placeholder: dict[str, Any] | None = None
        first_partial: dict[str, Any] | None = None
        for match in matches:
            placeholder = {
                "requested_symbol": match.requested_symbol,
                "symbol": match.base_symbol,
                "base_symbol": match.base_symbol,
                "venue": match.venue,
                "venue_symbol": match.venue_symbol,
                "instrument_class": match.instrument_class,
                "quote_asset": match.quote_asset,
                "state": "unavailable",
                "state_zh": "暂无",
            }
            if first_placeholder is None:
                first_placeholder = placeholder
            fetcher = self._price_fetcher_for(match.venue) if self._price_fetcher_for else None
            if fetcher is None:
                continue
            try:
                points = await asyncio.wait_for(
                    fetcher(match.price_symbol, targets),
                    timeout=_DELIVERY_PRICE_SOURCE_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: S112 - one venue failure must fall through to the next exact match
                continue
            if stamp not in points:
                continue
            instrument = PriceInstrument(
                venue=match.venue,
                venue_symbol=match.price_symbol,
                base_symbol=match.base_symbol,
                instrument_class=match.instrument_class,
                quote_asset=match.quote_asset,
            )
            quote = self._quote_from_points(
                match.requested_symbol,
                {},
                instrument,
                points,
                stamp=stamp,
                news_target_ms=news_target,
            )
            quote["venue_symbol"] = match.venue_symbol
            if first_partial is None:
                first_partial = quote
            if all(target in points for target in targets):
                return [quote]
        fallback = first_partial or first_placeholder
        return [fallback] if fallback is not None else []

    async def _point_quote(
        self,
        symbol: str,
        original: Mapping[str, Any],
        instruments: Sequence[PriceInstrument],
        *,
        stamp: int,
        news_target_ms: int | None,
    ) -> dict[str, Any]:
        targets = [stamp, stamp - _ONE_HOUR_MS, stamp - 24 * _ONE_HOUR_MS]
        if news_target_ms is not None:
            targets.append(news_target_ms)
        expected_class = str(instruments[0].instrument_class) if instruments else ""
        candidates = [
            instrument
            for instrument in instruments
            if not expected_class
            or expected_class == "unknown"
            or instrument.instrument_class in {expected_class, "unknown"}
        ] or list(instruments)
        candidates = self._bounded_price_candidates(candidates)
        first_partial: dict[str, Any] | None = None
        seen_contracts: set[tuple[str, str]] = set()
        for instrument in candidates:
            contract = (instrument.venue, instrument.venue_symbol)
            if contract in seen_contracts:
                continue
            seen_contracts.add(contract)
            fetcher = self._price_fetcher_for(instrument.venue) if self._price_fetcher_for else None
            if fetcher is None:
                continue
            try:
                points = await asyncio.wait_for(
                    fetcher(instrument.venue_symbol, targets),
                    timeout=_DELIVERY_PRICE_SOURCE_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: S112 - one provider failure is the signal to try the next venue
                continue
            current = points.get(stamp)
            if current is None:
                continue
            quote = self._quote_from_points(
                symbol,
                original,
                instrument,
                points,
                stamp=stamp,
                news_target_ms=news_target_ms,
            )
            if first_partial is None:
                first_partial = quote
            if all(target in points for target in targets):
                return quote
        return first_partial or dict(original)

    @staticmethod
    def _bounded_price_candidates(instruments: Sequence[PriceInstrument]) -> list[PriceInstrument]:
        """At most two Binance contracts, then one Hyperliquid and one OKX contract."""

        limits = {"binance": 2, "hl": 1, "okx": 1}
        counts = {family: 0 for family in limits}
        out: list[PriceInstrument] = []
        for instrument in instruments:
            family = instrument.venue.split(".", 1)[0]
            if family not in limits or counts[family] >= limits[family]:
                continue
            counts[family] += 1
            out.append(instrument)
        return out

    @staticmethod
    def _quote_from_points(
        symbol: str,
        original: Mapping[str, Any],
        instrument: PriceInstrument,
        points: Mapping[int, PricePoint],
        *,
        stamp: int,
        news_target_ms: int | None,
    ) -> dict[str, Any]:
        current = points[stamp]
        same_snapshot = (
            str(original.get("venue") or "") == instrument.venue
            and str(original.get("venue_symbol") or "") == instrument.venue_symbol
            and original.get("state") == "fresh"
        )
        quote: dict[str, Any] = {
            "requested_symbol": symbol,
            "symbol": instrument.base_symbol,
            "base_symbol": instrument.base_symbol,
            "venue": instrument.venue,
            "venue_symbol": instrument.venue_symbol,
            "instrument_class": instrument.instrument_class,
            "quote_asset": instrument.quote_asset,
            "price": str(current.price),
            "price_kind": "last",
            "price_kind_zh": "成交价",
            "source_at_ms": current.at_ms,
            "received_at_ms": stamp,
            "age_ms": max(0, stamp - current.at_ms),
            "state": "fresh",
            "state_zh": "实时",
            "delivery_price_basis": current.basis,
            "change_pct": original.get("change_pct") if same_snapshot else None,
            "change_basis": original.get("change_basis") if same_snapshot else None,
            "change_basis_zh": original.get("change_basis_zh") if same_snapshot else None,
        }
        hour = points.get(stamp - _ONE_HOUR_MS)
        if hour is not None:
            quote["price_one_hour_before_push"] = str(hour.price)
            quote["price_one_hour_before_push_basis"] = hour.basis
        day = points.get(stamp - 24 * _ONE_HOUR_MS)
        if day is not None:
            # `pricing.parse_change_pct` is the only place two prices become a day change (#562).
            change_pct = parse_change_pct(current.price, day.price)
            if change_pct is not None:
                quote["change_pct"] = change_pct
                quote["change_basis"] = "rolling_24h"
                quote["change_basis_zh"] = "24 小时"
        if news_target_ms is not None:
            news = points.get(news_target_ms)
            if news is not None:
                quote["price_at_news"] = str(news.price)
                quote["price_at_news_basis"] = news.basis
        return quote

    async def _delivery_candles(
        self,
        index: int,
        fetcher: DeliveryCandleFetcher,
        venue_symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[int, Sequence[Candle]] | None:
        try:
            candles = await asyncio.wait_for(
                fetcher(venue_symbol, start_ms, end_ms),
                timeout=_DELIVERY_CANDLE_TIMEOUT_SECONDS,
            )
        except Exception:
            return None
        return index, candles

    async def drain(self) -> None:
        tasks = (*self._finalizers, *self._edit_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close_sender(self) -> None:
        if self.sender is not None:
            with contextlib.suppress(Exception):
                await self.finite.run(
                    "news_delivery_sender_close", self.sender.close, timeout_seconds=5.0, allow_shutdown=True
                )

    async def close(self) -> None:
        await self.drain()
        await self.close_sender()
