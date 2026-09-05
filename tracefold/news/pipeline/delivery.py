"""At-most-once reader-card delivery with best-effort in-place enrichment."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Protocol, runtime_checkable

from ..bus import Q_DELIVER, BusMessage, DeferError, PermanentError, TransientError, now_ms
from ..delivery import card_assets, reader_market_movements, reader_trade_targets, render_first_card
from ..market_review.pricing import Candle, PriceInstrument, PricePoint, parse_change_pct, select_candle
from ..models import Novelty, ReaderDeliveryPresentation, ReaderMarketScope, TelegramDeliveryReceipt
from ..progression_review import PROGRESSION_REVIEW_TIMEOUT_SECONDS, ProgressionReview, ProgressionVerifier
from ..source_contracts import EVENT_KINDS
from ..telemetry import NewsWorkSemantics
from ..tradability import (
    TRADABILITY_REVIEW_TIMEOUT_SECONDS,
    TradabilityMatch,
    TradabilityReview,
    TradabilityVerifier,
    tradability_candidates,
)
from .runtime import NewsDatabasePort, _sleep_or_stop

# The quote read gets its own short session. A price is display-only and must
# never delay, retry, or suppress a delivery; every failure degrades to no
# market line while the card proceeds normally (#113).
_QUOTE_READ_TIMEOUT_SECONDS = 1.5
_DELIVERY_CANDLE_TIMEOUT_SECONDS = 2.0
_DELIVERY_PRICE_SOURCE_TIMEOUT_SECONDS = 2.0
_DELIVERY_CANDLE_GAP_MS = 90_000
_ONE_HOUR_MS = 3_600_000
_DELIVERY_EDIT_TIMEOUT_SECONDS = 8.0
_DELIVERY_EDIT_RECONCILE_SECONDS = 30.0
_DELIVERY_STARTUP_RECONCILE_RETRY_SECONDS = 0.25
_PROGRESSION_REVIEW_CANDIDATE_MAX = 8
# What an authoritative five-venue absence puts on the card. It replaces deleting the card: the
# tradability review is the same review, run at the same moment, on the same evidence -- what changed
# is that a model-derived candidate list plus a text heuristic no longer takes a card the reader has
# already read out of their channel. A reader who saw a story about a name they cannot trade is better
# served by being told that than by watching the message vanish (#562 §5 row 5).
_UNTRADEABLE_NOTICE_ZH = "未找到可交易标的"

logger = logging.getLogger(__name__)

DeliveryCandleFetcher = Callable[[str, int, int], Awaitable[Sequence[Candle]]]
DeliveryCandleFetcherFor = Callable[[str], DeliveryCandleFetcher | None]
DeliveryPriceFetcher = Callable[[str, Sequence[int]], Awaitable[Mapping[int, PricePoint]]]
DeliveryPriceFetcherFor = Callable[[str], DeliveryPriceFetcher | None]


def _reader_market_scope(verdict: Mapping[str, Any]) -> ReaderMarketScope | None:
    value = str(verdict.get("scope") or "")
    if value == "macro":
        return "macro"
    if value == "sector":
        return "sector"
    if value == "single_name":
        return "single_name"
    return None


def _reader_novelty(verdict: Mapping[str, Any]) -> Novelty | None:
    value = str(verdict.get("novelty") or "")
    if value == "new_fact":
        return "new_fact"
    if value == "progression":
        return "progression"
    if value == "restatement":
        return "restatement"
    return None


def _with_untradeable_notice(card: dict[str, Any]) -> dict[str, Any]:
    """Lead the enrichment edit with the catalogue's answer, leaving every other block untouched.

    The notice goes at the top of the markdown block, above the copy and clear of the facts line the
    block ends with, so both configured adapters print it with no channel branch of their own.
    """

    elements = list(card.get("elements") or ())
    for index, element in enumerate(elements):
        if not isinstance(element, Mapping) or element.get("tag") != "markdown":
            continue
        content = str(element.get("content") or "")
        if content.startswith(_UNTRADEABLE_NOTICE_ZH):
            return card
        elements[index] = {**dict(element), "content": f"{_UNTRADEABLE_NOTICE_ZH}\n{content}".strip()}
        return {**card, "elements": elements}
    return card


def _progression_review_candidates(
    triage_row: Mapping[str, Any], verdict: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    if verdict.get("novelty") != "progression":
        return ()
    trace = triage_row.get("trace")
    told = trace.get("told") if isinstance(trace, Mapping) else None
    if not isinstance(told, Sequence) or isinstance(told, str | bytes):
        return ()
    candidates: list[dict[str, Any]] = []
    # Every field this builder reads is named below, one at a time, with its own type check and its own
    # bound. A key it does not read cannot change a candidate, so a told entry carrying one is not a
    # reason to refuse anything: the rejected shape used to stop *every* progression card on the Event,
    # and the only thing an added upstream field proves is that the ledger grew a column (#562 §5 row 4).
    for fallback_i, entry in enumerate(told):
        if not isinstance(entry, Mapping):
            continue
        headline = str(entry.get("headline_zh") or "").strip()
        if not headline:
            continue
        raw_i = entry.get("i", fallback_i)
        candidate_i = raw_i if isinstance(raw_i, int) and not isinstance(raw_i, bool) and raw_i >= 0 else fallback_i
        raw_similarity = entry.get("similarity")
        similarity = (
            min(1.0, max(0.0, float(raw_similarity)))
            if isinstance(raw_similarity, int | float) and not isinstance(raw_similarity, bool)
            else 0.0
        )
        candidates.append(
            {
                "i": candidate_i,
                "event_id": str(entry.get("event_id") or "")[:128],
                "storyline_key": str(entry.get("storyline_key") or "")[:160],
                "comparison_title": str(entry.get("comparison_title") or "")[:600],
                "comparison_fingerprint": str(entry.get("comparison_fingerprint") or "")[:128],
                "headline_zh": headline[:120],
                "why_zh": str(entry.get("why_zh") or "")[:320],
                "tier": str(entry.get("tier") or "recency")[:32],
                "similarity": similarity,
                "ago_min": (
                    max(0, int(entry["ago_min"]))
                    if isinstance(entry.get("ago_min"), int) and not isinstance(entry.get("ago_min"), bool)
                    else None
                ),
                "at_ms": (
                    int(entry["at_ms"])
                    if isinstance(entry.get("at_ms"), int) and not isinstance(entry.get("at_ms"), bool)
                    else None
                ),
                "symbols": [str(value)[:32] for value in entry.get("symbols") or ()][:6],
                "magnitude": max(0, min(3, int(entry.get("magnitude") or 0))),
                "direction": str(entry.get("direction") or "")[:32],
            }
        )
        if len(candidates) >= _PROGRESSION_REVIEW_CANDIDATE_MAX:
            break
    return tuple(candidates)


@dataclass(frozen=True, slots=True)
class _ProgressionParentReference:
    message_id: int | None = None
    age_minutes: int | None = None


class NewsPushSender(Protocol):
    """Synchronous provider boundary executed by the finite-operation runner."""

    def prepare(self) -> None: ...

    def send_card(
        self,
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


@runtime_checkable
class EditableNewsPushSender(Protocol):
    """Provider capability for replacing one already-receipted reader message in place."""

    def edit_card(
        self,
        receipt: Mapping[str, Any],
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _EnrichmentEditContext:
    sender: EditableNewsPushSender
    event_id: str
    kind: str
    event: Mapping[str, Any]
    verdict: Mapping[str, Any]
    decision: str
    grounded_assets: tuple[str, ...]
    shown: tuple[str, ...]
    degraded: bool
    receipt: TelegramDeliveryReceipt
    presentation: ReaderDeliveryPresentation
    progression_candidates: tuple[dict[str, Any], ...]
    tradability_pending: bool


# The initial-send deadline, unchanged, named once now that two owners share the entry.
_INITIAL_SEND_TIMEOUT_SECONDS = 8.0


class InitialSendEntry:
    """The one place an initial card leaves this process.

    Ordinary News and market notifications both queue here (#553 §5.2). Sharing it is the point: the
    operator configured one `min_interval_seconds`, and two independent pacers would have meant the
    channel could be interrupted twice as often as the number they set. The lock is also what stops
    two loops being inside the provider at the same time, which the previous shape -- a bare interval
    on the Deliverer, serialised only by its own `prefetch=1` -- did not do for anyone else.

    `asyncio.Lock` admits waiters in arrival order, so the queueing is fair by construction: a burst
    of market cards cannot starve a News card that was already waiting, and neither can hold the entry
    across anything but its own one send.
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
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
        operation: str = "news_delivery_send",
        prepare: bool = True,
    ) -> Mapping[str, Any]:
        """Send one already-rendered card. Nothing here enriches or settles it.

        The caller owns idempotency and the receipt, exactly as the Deliverer always has: this is the
        target check, the pacing and the provider call, and no part of either domain's decision.

        `prepare=False` is for a caller that already validated the target *earlier on purpose*. The
        Deliverer does: it prepares before it writes its durable `sending` row, so a bad channel
        settles the Event without one. A caller with no such moment -- the market loop -- takes the
        default, because Telegram refuses `send_card` outright on an unvalidated target and a card
        that skipped the check would fail on a channel that is in fact fine.
        """

        sender = self._sender
        if sender is None:
            raise RuntimeError("news_delivery_sender_unavailable")
        async with self._lock:
            # The stamp covers the whole held block, not just the send. A `prepare` that raises is
            # still a provider call this process just made, and leaving the stamp stale would let the
            # next caller compute `wait <= 0` -- so a turn draining its card budget against a broken
            # target would hammer the preflight with no interval between attempts at all.
            try:
                wait = self.min_interval - (time.monotonic() - self._last_send_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                if prepare:
                    # Idempotent -- a validated target returns immediately -- and the adapter
                    # invalidates it again the moment a send fails, so a rotated token is re-checked
                    # rather than cached for the life of the process.
                    await self._finite.run(
                        "news_delivery_prepare", sender.prepare, timeout_seconds=self._timeout_seconds
                    )
                receipt: Mapping[str, Any] = await self._finite.run(
                    operation,
                    sender.send_card,
                    dict(card),
                    presentation=presentation,
                    timeout_seconds=self._timeout_seconds,
                )
                return receipt
            finally:
                self._last_send_at = time.monotonic()


class DelivererConsumer:
    """SAC consumer: one initial send per identity; editable providers enrich that same receipt."""

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        bus: Any,
        db: NewsDatabasePort,
        sender: NewsPushSender | None,
        finite_operations: Any,
        min_interval_seconds: float,
        candle_fetcher_for: DeliveryCandleFetcherFor | None = None,
        price_fetcher_for: DeliveryPriceFetcherFor | None = None,
        progression_verifier: ProgressionVerifier | None = None,
        tradability_verifier: TradabilityVerifier | None = None,
    ) -> None:
        self.bus = bus
        self.db = db
        self.sender = sender
        self.finite = finite_operations
        self.min_interval = float(min_interval_seconds)
        # The Deliverer owns the entry and composition hands the same object to the market loop, so
        # there is one pacer for the process rather than one per caller who remembered to share it.
        self.send_entry = InitialSendEntry(
            sender=sender, finite_operations=finite_operations, min_interval_seconds=min_interval_seconds
        )
        self._candle_fetcher_for = candle_fetcher_for
        self._price_fetcher_for = price_fetcher_for
        self._progression_verifier = progression_verifier
        self._tradability_verifier = tradability_verifier
        self._last_edit_at = 0.0
        self._edit_lock = asyncio.Lock()
        self._edit_tasks: set[asyncio.Task[None]] = set()

    async def run(self, *, stop_event: asyncio.Event) -> None:
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_delivery_reconcile", lambda repos: repos.news.terminalize_interrupted_deliveries(now_ms=now_ms())
            )
        # Unlike an initial-send ambiguity, an inherited edit intent cannot be left in a pretend in-flight state:
        # this process owns no edit task yet. Refuse to consume until PostgreSQL records that truth.
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
        consume_task = asyncio.create_task(
            self.bus.consume(Q_DELIVER, self.handle, prefetch=1, stop_event=stop_event),
            name="news-delivery-consume",
        )
        reconcile_task = asyncio.create_task(
            self._edit_reconcile_loop(stop_event=stop_event),
            name="news-delivery-edit-reconcile",
        )
        tasks = {consume_task, reconcile_task}
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _edit_reconcile_loop(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=_DELIVERY_EDIT_RECONCILE_SECONDS)
            if stop_event.is_set():
                return
            with contextlib.suppress(TransientError, DeferError):
                await self._reconcile_stale_delivery_edits()

    async def _reconcile_stale_delivery_edits(self) -> None:
        await self.db.tx(
            "news_delivery_edit_stale_reconcile",
            lambda repos: repos.news.terminalize_stale_delivery_edits(now_ms=now_ms()),
        )

    async def handle(self, message: BusMessage) -> None:
        event_id = str(message.payload.get("event_id") or "")
        kind = "first"  # one Event, one card; there is no follow-up lane
        if not event_id:
            raise PermanentError("news_event_id_missing")
        stamp = now_ms()
        bundle = await self.db.read("news_delivery_load", lambda repos: self._load(repos, event_id, stamp))
        if bundle is None:
            raise PermanentError("news_delivery_inputs_missing")
        card, triage_row, _admission, timing = bundle
        # A delivery message can outlive the source-contract migration that held its Event. Immutable
        # evidence and historical verdicts remain audit facts; current PostgreSQL routing still wins before
        # a delivery ledger row, quote read, or external send is attempted. A retired market kind is one of
        # those held Events (#553): `_load` answers it with no verdict, which is what "readable evidence,
        # never delivered" is. Re-testing `EVENT_KINDS` here was that same rule written a second time.
        if triage_row is None:
            return
        tv = dict(triage_row.get("verdict") or {})
        if triage_row["final_decision"] not in {"push", "escalate"}:
            return
        if self.sender is None:
            await self._settle_direct(event_id, kind, "delivery_unavailable", stamp)
            return
        try:
            await self.finite.run(
                "news_delivery_prepare",
                self.sender.prepare,
                timeout_seconds=8.0,
            )
        except Exception as exc:
            prepare_error_code = getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}"
            await self._settle_direct(event_id, kind, prepare_error_code, stamp)
            return
        # Only query a quote after every policy return above. A quote failure
        # never changes the delivery decision.
        shown = card_assets(tv, list(card.get("grounded_assets") or []))
        news_at_ms = int(timing["news_at_ms"]) if timing and timing.get("news_at_ms") is not None else None
        observed_at_ms = int(timing["observed_at_ms"]) if timing and timing.get("observed_at_ms") is not None else None
        progression_candidates = _progression_review_candidates(triage_row, tv)
        progression_review_pending = self._progression_verifier is not None and bool(progression_candidates)
        _, title_identity_confident = tradability_candidates(event=card, verdict=tv, symbols=shown)
        tradability_pending = (
            self._tradability_verifier is not None
            and _reader_market_scope(tv) == "single_name"
            and (len(shown) == 1 or (not shown and title_identity_confident))
        )
        base_presentation = ReaderDeliveryPresentation(
            news_at_ms=news_at_ms,
            observed_at_ms=observed_at_ms,
            market_scope=_reader_market_scope(tv),
            novelty=_reader_novelty(tv),
            # `⏳ 关联确认中` is a promise that an edit is on its way. With no verifier -- none
            # configured, or the editorial Program faulted and left one unwired (#553 PR-3) -- no edit
            # is coming, and the badge stayed on the card forever: a permanent "confirming" is a worse
            # answer than the plain `新进展` the verdict already earned. It is shown only while a review
            # this delivery actually started is still running (#562 §5 rows 3 and 6).
            progression_review_state=("pending" if progression_review_pending else None),
        )
        progressive_sender = self.sender if isinstance(self.sender, EditableNewsPushSender) else None
        quotes = (
            [] if progressive_sender is not None else await self._market_data(shown, now_ms(), news_at_ms=news_at_ms)
        )
        card_payload = render_first_card(
            event=card,
            verdict=tv,
            decision=str(triage_row["final_decision"]),
            grounded_assets=list(card.get("grounded_assets") or []),
            assets=shown,
            degraded=bool(triage_row.get("degraded")),
            quotes=quotes,
        )
        presentation = (
            replace(base_presentation, market_data_state="pending")
            if progressive_sender is not None
            else replace(
                base_presentation,
                trade_targets=reader_trade_targets(quotes),
                market_movements=reader_market_movements(shown, quotes),
            )
        )
        state = await self.db.tx(
            "news_delivery_begin",
            lambda repos: repos.news.begin_delivery(event_id=event_id, kind=kind, card=card_payload, now_ms=stamp),
        )
        if state != "new":
            if state == "sending":
                await self.db.tx(
                    "news_delivery_ambiguous",
                    lambda repos: repos.news.settle_delivery(
                        event_id=event_id,
                        kind=kind,
                        state="terminal",
                        receipt=None,
                        error_code="ambiguous_after_crash",
                        now_ms=now_ms(),
                    ),
                )
            return
        error_code: str | None = None
        receipt: dict[str, Any] | None = None
        try:
            # The shared entry paces this send and serialises it against the market loop's. News
            # keeps reading `code` and nothing else about the failure, exactly as before.
            # `prepare=False`: the target was validated above, before `begin_delivery`, which is
            # where this consumer wants a bad channel to fail. Preparing again here would be a second
            # no-op call per delivery.
            result = await self.send_entry.send_prepared_card(card_payload, presentation=presentation, prepare=False)
            receipt = dict(result)
        except Exception as exc:
            error_code = getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}"
        settled_state = "sent" if error_code is None else "terminal"
        try:
            settlement_recorded = await self.db.tx(
                "news_delivery_settle",
                lambda repos: repos.news.settle_delivery(
                    event_id=event_id,
                    kind=kind,
                    state=settled_state,
                    receipt=receipt,
                    error_code=error_code,
                    now_ms=now_ms(),
                ),
            )
        except (TransientError, DeferError) as exc:
            raise RuntimeError("news_delivery_settlement_unavailable") from exc
        if not settlement_recorded:
            logger.warning("News delivery settlement failed: news_delivery_settlement_conflict")
            return
        if (
            progressive_sender is None
            or settled_state != "sent"
            or receipt is None
            or (not shown and not progression_review_pending and not tradability_pending)
        ):
            return
        try:
            parsed_receipt = TelegramDeliveryReceipt.model_validate(receipt)
        except ValueError:
            logger.warning("News delivery enrichment edit failed: news_delivery_edit_receipt_invalid")
            return
        self._start_enrichment_edit(
            _EnrichmentEditContext(
                sender=progressive_sender,
                event_id=event_id,
                kind=kind,
                event=dict(card),
                verdict=dict(tv),
                decision=str(triage_row["final_decision"]),
                grounded_assets=tuple(card.get("grounded_assets") or ()),
                shown=tuple(shown),
                degraded=bool(triage_row.get("degraded")),
                receipt=parsed_receipt,
                presentation=base_presentation,
                progression_candidates=progression_candidates,
                tradability_pending=tradability_pending,
            )
        )

    def _start_enrichment_edit(self, context: _EnrichmentEditContext) -> None:
        task = asyncio.create_task(
            self._enrich_and_edit(context),
            name=f"news-delivery-edit-{context.event_id[:12]}",
        )
        self._edit_tasks.add(task)
        task.add_done_callback(self._edit_tasks.discard)

    async def _enrich_and_edit(self, context: _EnrichmentEditContext) -> None:
        intent_started = False
        try:
            quotes_task = self._market_data(
                context.shown, context.receipt.pushed_at_ms, news_at_ms=context.presentation.news_at_ms
            )
            review_task = self._progression_review(context)
            tradability_task = self._tradability_review(context)
            quotes, progression_review, tradability_review = await asyncio.gather(
                quotes_task, review_task, tradability_task
            )
            parent_reference = await self._progression_parent_reference(context, progression_review)
            displayed_progression_review = progression_review
            if (
                progression_review is not None
                and progression_review.state == "confirmed"
                and parent_reference.message_id is None
            ):
                displayed_progression_review = ProgressionReview(
                    state="unavailable",
                    reason_zh="未找到可引用的历史推送。",
                    verifier_id=progression_review.verifier_id,
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
                    resolved_shown = tuple(
                        dict.fromkeys(
                            match.requested_symbol for match in tradability_review.matches if match.requested_symbol
                        )
                    )
            card_payload = render_first_card(
                event=context.event,
                verdict=context.verdict,
                decision=context.decision,
                grounded_assets=list(context.grounded_assets),
                assets=resolved_shown,
                degraded=context.degraded,
                quotes=quotes,
            )
            if displayed_progression_review is not None:
                card_payload["progression_review"] = displayed_progression_review.model_dump(
                    mode="json", exclude_none=True
                )
            if tradability_review is not None:
                card_payload["tradability_review"] = tradability_review.model_dump(mode="json", exclude_none=True)
            if (
                tradability_review is not None
                and tradability_review.state == "absent"
                # Still the identity confidence the deletion path was gated on: an authoritative
                # absence is only worth printing about a candidate specific enough to be a ticker.
                # What changed is what it authorises -- a line on the card, not its removal.
                and tradability_review.deletion_safe
                and not reader_trade_targets(quotes)
            ):
                card_payload = _with_untradeable_notice(card_payload)
            presentation = replace(
                context.presentation,
                trade_targets=reader_trade_targets(quotes),
                market_movements=reader_market_movements(resolved_shown, quotes),
                novelty=(
                    "new_fact"
                    if displayed_progression_review is not None and displayed_progression_review.state != "confirmed"
                    else context.presentation.novelty
                ),
                progression_from_headline=(
                    displayed_progression_review.candidate_headline_zh
                    if displayed_progression_review is not None and displayed_progression_review.state == "confirmed"
                    else context.presentation.progression_from_headline
                ),
                progression_review_state=(
                    displayed_progression_review.state
                    if displayed_progression_review is not None
                    else context.presentation.progression_review_state
                ),
                progression_review_reason=(
                    displayed_progression_review.reason_zh
                    if displayed_progression_review is not None
                    else context.presentation.progression_review_reason
                ),
                progression_review_parent_age_minutes=parent_reference.age_minutes,
                progression_review_parent_message_id=parent_reference.message_id,
            )
            async with self._edit_lock:
                wait = self.min_interval - (time.monotonic() - self._last_edit_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                intent_started = bool(
                    await self.db.tx(
                        "news_delivery_begin_edit",
                        lambda repos: repos.news.begin_delivery_edit(
                            event_id=context.event_id,
                            kind=context.kind,
                            card=card_payload,
                            receipt=context.receipt.canonical(),
                            now_ms=now_ms(),
                        ),
                    )
                )
                if not intent_started:
                    logger.warning("News delivery enrichment edit failed: news_delivery_edit_intent_conflict")
                    return
                try:
                    result = await self.finite.run(
                        "news_delivery_edit",
                        context.sender.edit_card,
                        context.receipt.canonical(),
                        card_payload,
                        presentation=presentation,
                        timeout_seconds=_DELIVERY_EDIT_TIMEOUT_SECONDS,
                        allow_shutdown=True,
                    )
                finally:
                    self._last_edit_at = time.monotonic()
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
                        event_id=context.event_id,
                        kind=context.kind,
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
        if verifier is None or not context.tradability_pending:
            return None
        try:
            async with asyncio.timeout(TRADABILITY_REVIEW_TIMEOUT_SECONDS):
                raw = await verifier.review(
                    event=context.event,
                    verdict=context.verdict,
                    symbols=context.shown,
                )
            return raw if isinstance(raw, TradabilityReview) else TradabilityReview.model_validate(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            return TradabilityReview(
                state="incomplete",
                candidates=tuple(context.shown),
                checked_venues=(),
                failed_venues=(),
                matches=(),
                reason_zh="交易所目录核验超时或返回异常，按安全规则保留消息。",
            )

    async def _progression_review(self, context: _EnrichmentEditContext) -> ProgressionReview | None:
        verifier = self._progression_verifier
        if verifier is None or not context.progression_candidates:
            return None
        try:
            async with asyncio.timeout(PROGRESSION_REVIEW_TIMEOUT_SECONDS):
                raw = await verifier.review(
                    event=context.event,
                    verdict=context.verdict,
                    candidates=context.progression_candidates,
                )
            review = raw if isinstance(raw, ProgressionReview) else ProgressionReview.model_validate(raw)
            if review.state != "confirmed":
                return review
            candidate = next(
                (item for item in context.progression_candidates if item["i"] == review.candidate_i),
                None,
            )
            if candidate is None:
                raise ValueError("news_progression_review_candidate_missing")
            return review.model_copy(update={"candidate_headline_zh": candidate["headline_zh"]})
        except asyncio.CancelledError:
            raise
        except Exception:
            return ProgressionReview(
                state="unavailable",
                reason_zh="模型未返回可验证的关联结论。",
                verifier_id=str(getattr(verifier, "verifier_id", type(verifier).__name__))[:120],
            )

    async def _progression_parent_reference(
        self,
        context: _EnrichmentEditContext,
        review: ProgressionReview | None,
    ) -> _ProgressionParentReference:
        if review is None or review.state != "confirmed" or review.candidate_i is None:
            return _ProgressionParentReference()
        candidate = next(
            (item for item in context.progression_candidates if item.get("i") == review.candidate_i),
            None,
        )
        if candidate is None:
            return _ProgressionParentReference()
        parent_event_id = str(candidate.get("event_id") or "").strip()
        if not parent_event_id:
            return _ProgressionParentReference()
        try:
            row = await self.db.read(
                "news_delivery_progression_parent",
                lambda repos: repos.news.delivery(event_id=parent_event_id, kind="first"),
                timeout_seconds=_QUOTE_READ_TIMEOUT_SECONDS,
            )
            if (
                not isinstance(row, Mapping)
                or row.get("state") != "sent"
                or row.get("delete_state") is not None
                or not isinstance(row.get("receipt"), Mapping)
            ):
                return _ProgressionParentReference()
            parent_receipt = TelegramDeliveryReceipt.model_validate(row["receipt"])
            if (
                parent_receipt.target_sha256 != context.receipt.target_sha256
                or parent_receipt.deleted_at_ms is not None
                or parent_receipt.pushed_at_ms > context.receipt.pushed_at_ms
            ):
                return _ProgressionParentReference()
            return _ProgressionParentReference(
                message_id=parent_receipt.message_id,
                age_minutes=(context.receipt.pushed_at_ms - parent_receipt.pushed_at_ms) // 60_000,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return _ProgressionParentReference()

    async def _mark_edit_ambiguous(self, context: _EnrichmentEditContext, error_code: str) -> None:
        try:
            recorded = await self.db.tx(
                "news_delivery_ambiguous_edit",
                lambda repos: repos.news.mark_delivery_edit_ambiguous(
                    event_id=context.event_id,
                    kind=context.kind,
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

    async def _settle_direct(self, event_id: str, kind: str, error_code: str, stamp: int) -> None:
        def _fn(repos: Any) -> None:
            state = repos.news.begin_delivery(event_id=event_id, kind=kind, card={}, now_ms=stamp)
            if state == "new":
                repos.news.settle_delivery(
                    event_id=event_id, kind=kind, state="terminal", receipt=None, error_code=error_code, now_ms=stamp
                )

        await self.db.tx("news_delivery_settle_direct", _fn)

    async def _market_data(
        self,
        shown: Sequence[str],
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
        try:
            rows = await self.db.read(
                "news_delivery_quotes",
                lambda repos: repos.price.quotes_for_symbols(shown, now_ms=stamp),
                timeout_seconds=_QUOTE_READ_TIMEOUT_SECONDS,
            )
        except Exception:  # price is display-only; all failures degrade to no line
            return []
        quotes = [dict(row) for row in rows or [] if isinstance(row, Mapping)]
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
        shown: Sequence[str],
        stamp: int,
        *,
        news_at_ms: int | None,
    ) -> list[dict[str, Any]]:
        """Trade-first anchors with whole-calculation venue failover.

        A candidate is accepted only as one unit: current, news and one-hour prices all retain the same
        ``(venue, venue_symbol)``. Partial values are kept only if no later venue can provide the complete set.
        """

        try:
            rows, candidates = await self.db.read(
                "news_delivery_price_sources",
                lambda repos: (
                    repos.price.quotes_for_symbols(shown, now_ms=stamp),
                    repos.price.instruments_for_symbols(shown),
                ),
                timeout_seconds=_QUOTE_READ_TIMEOUT_SECONDS,
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
                symbol,
                originals.get(symbol, {}),
                tuple(candidates.get(symbol, ())),
                stamp=stamp,
                news_target_ms=news_target,
            )
            for symbol in shown
        ]
        resolved = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[dict[str, Any]] = []
        for symbol, result in zip(shown, resolved, strict=True):
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

    def _load(self, repos: Any, event_id: str, stamp: int) -> tuple[Any, ...] | None:
        del stamp
        card = repos.news.event_card(event_id)
        routing = repos.news.event_admission(event_id)
        timing = repos.news.event_delivery_timing(event_id)
        if card is None or routing is None:
            return None
        admission = str(routing.get("admission") or "")
        event_kind = str(routing.get("event_kind") or "")
        if event_kind not in EVENT_KINDS:
            return card, None, admission, timing
        triage = repos.news.latest_verdict(event_id=event_id, stage="triage")
        if triage is None:
            return None
        # No OI frame row travels in this bundle any more (#458). It grounded the symbol on a pushed OI
        # card, and Triage publishes to this consumer only on `push`, which the OI lane no longer produces.
        return card, triage, admission, timing

    async def drain(self) -> None:
        tasks = tuple(self._edit_tasks)
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
