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
from ..delivery import reader_assets, reader_market_movements, reader_trade_targets, render_first_card
from ..market_review.pricing import Candle, PriceInstrument, PricePoint, return_bps, select_candle
from ..models import Novelty, ReaderDeliveryPresentation, ReaderMarketScope, TelegramDeliveryReceipt
from ..oi_signals import DEFAULT_OI_POLICY, OiPolicy, program_sha256
from ..oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from ..oi_signals import PROGRAM_VERSION as OI_PROGRAM_VERSION
from ..progression_review import PROGRESSION_REVIEW_TIMEOUT_SECONDS, ProgressionReview, ProgressionVerifier
from ..telemetry import NewsWorkSemantics
from ..tradability import (
    TRADABILITY_REVIEW_TIMEOUT_SECONDS,
    TradabilityMatch,
    TradabilityReview,
    TradabilityVerifier,
)
from .runtime import NewsDatabasePort

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
_PROGRESSION_LINK_SIMILARITY_MIN = 0.5
_PROGRESSION_REVIEW_CANDIDATE_MAX = 8

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


def _progression_from_headline(triage_row: Mapping[str, Any], verdict: Mapping[str, Any]) -> str | None:
    """Name a prior card only when the stored retrieval evidence supports that relationship.

    ``progression`` currently does not carry an explicit told-ledger index. The selected ledger is relevance-ranked,
    but broad buckets such as ``macro:general`` may also contain unrelated zero-similarity cards. We therefore show
    a prior headline only for an exact-fact match or a non-zero semantic/title match; otherwise the reader sees the
    truthful ``新进展`` badge without a fabricated ``上一条`` relationship.
    """

    if verdict.get("novelty") != "progression":
        return None
    trace = triage_row.get("trace")
    told = trace.get("told") if isinstance(trace, Mapping) else None
    if not isinstance(told, Sequence) or isinstance(told, str | bytes):
        return None
    for entry in told:
        if not isinstance(entry, Mapping):
            continue
        tier = str(entry.get("tier") or "")
        similarity = entry.get("similarity")
        related = tier == "exact_fact" or (
            isinstance(similarity, int | float)
            and not isinstance(similarity, bool)
            and float(similarity) >= _PROGRESSION_LINK_SIMILARITY_MIN
        )
        headline = str(entry.get("headline_zh") or "").strip()
        if related and headline:
            return headline
    return None


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
                "headline_zh": headline[:120],
                "tier": str(entry.get("tier") or "recency")[:32],
                "similarity": similarity,
                "ago_min": max(0, int(entry.get("ago_min") or 0)),
                "event_type": str(entry.get("event_type") or entry.get("type") or "")[:32],
                "symbols": [str(value)[:32] for value in entry.get("symbols") or entry.get("sym") or ()][:6],
                "magnitude": max(0, min(3, int(entry.get("magnitude") or entry.get("m") or 0))),
                "direction": str(entry.get("direction") or entry.get("dir") or "")[:32],
            }
        )
        if len(candidates) >= _PROGRESSION_REVIEW_CANDIDATE_MAX:
            break
    return tuple(candidates)


def _progression_parent_age_minutes(
    candidates: Sequence[Mapping[str, Any]],
    review: ProgressionReview | None,
) -> int | None:
    if review is None or review.state != "confirmed" or review.candidate_i is None:
        return None
    candidate = next((item for item in candidates if item.get("i") == review.candidate_i), None)
    if candidate is None:
        return None
    value = candidate.get("ago_min")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


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


@runtime_checkable
class DeletableNewsPushSender(Protocol):
    """Provider capability for deleting one exactly receipted reader message."""

    def delete_card(self, receipt: Mapping[str, Any]) -> Mapping[str, Any]: ...


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
        oi_policy: OiPolicy = DEFAULT_OI_POLICY,
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
        self._oi_program_sha256 = program_sha256(oi_policy)
        self._candle_fetcher_for = candle_fetcher_for
        self._price_fetcher_for = price_fetcher_for
        self._progression_verifier = progression_verifier
        self._tradability_verifier = tradability_verifier
        self._last_send_at = 0.0
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
        await self.db.tx(
            "news_delivery_edit_reconcile",
            lambda repos: repos.news.terminalize_interrupted_delivery_edits(now_ms=now_ms()),
        )
        await self.db.tx(
            "news_delivery_delete_reconcile",
            lambda repos: repos.news.terminalize_interrupted_delivery_deletes(now_ms=now_ms()),
        )
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
        await asyncio.gather(
            self.db.tx(
                "news_delivery_edit_stale_reconcile",
                lambda repos: repos.news.terminalize_stale_delivery_edits(now_ms=now_ms()),
            ),
            self.db.tx(
                "news_delivery_delete_stale_reconcile",
                lambda repos: repos.news.terminalize_stale_delivery_deletes(now_ms=now_ms()),
            ),
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
        card, triage_row, oi_signal, _admission, event_kind, timing = bundle
        # A delivery message can outlive the source-contract migration that held its Event. Immutable
        # evidence and historical verdicts remain audit facts; current PostgreSQL routing still wins before
        # a delivery ledger row, quote read, or external send is attempted.
        if event_kind == "unsupported_market":
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
        shown = reader_assets(
            event_kind=event_kind,
            verdict=tv,
            grounded_assets=list(card.get("grounded_assets") or []),
            program_version=str(triage_row.get("program_version") or ""),
            verdict_program_sha256=str(triage_row.get("program_sha256") or ""),
            expected_program_sha256=self._oi_program_sha256,
            oi_signal=oi_signal,
        )
        news_at_ms = int(timing["news_at_ms"]) if timing and timing.get("news_at_ms") is not None else None
        observed_at_ms = int(timing["observed_at_ms"]) if timing and timing.get("observed_at_ms") is not None else None
        progression_candidates = _progression_review_candidates(triage_row, tv)
        progression_review_pending = self._progression_verifier is not None and bool(progression_candidates)
        tradability_pending = (
            self._tradability_verifier is not None and _reader_market_scope(tv) == "single_name" and len(shown) == 1
        )
        base_presentation = ReaderDeliveryPresentation(
            news_at_ms=news_at_ms,
            observed_at_ms=observed_at_ms,
            market_scope=_reader_market_scope(tv),
            novelty=_reader_novelty(tv),
            progression_from_headline=(
                None if progression_review_pending else _progression_from_headline(triage_row, tv)
            ),
            progression_review_state="pending" if progression_review_pending else None,
        )
        wait = self.min_interval - (time.monotonic() - self._last_send_at)
        if wait > 0:
            await asyncio.sleep(wait)
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
            result = await self.finite.run(
                "news_delivery_send",
                self.sender.send_card,
                card_payload,
                presentation=presentation,
                timeout_seconds=8.0,
            )
            receipt = dict(result)
        except Exception as exc:
            error_code = getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}"
        finally:
            self._last_send_at = time.monotonic()
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
            card_payload = render_first_card(
                event=context.event,
                verdict=context.verdict,
                decision=context.decision,
                grounded_assets=list(context.grounded_assets),
                assets=context.shown,
                degraded=context.degraded,
                quotes=quotes,
            )
            if progression_review is not None:
                card_payload["progression_review"] = progression_review.model_dump(mode="json", exclude_none=True)
            if tradability_review is not None:
                card_payload["tradability_review"] = tradability_review.model_dump(mode="json", exclude_none=True)
            if (
                tradability_review is not None
                and tradability_review.state == "absent"
                and not reader_trade_targets(quotes)
                and isinstance(context.sender, DeletableNewsPushSender)
            ):
                await self._delete_untradeable(context, tradability_review, progression_review)
                return
            presentation = replace(
                context.presentation,
                trade_targets=reader_trade_targets(quotes),
                market_movements=reader_market_movements(context.shown, quotes),
                progression_from_headline=(
                    progression_review.candidate_headline_zh
                    if progression_review is not None and progression_review.state == "confirmed"
                    else context.presentation.progression_from_headline
                ),
                progression_review_state=(
                    progression_review.state
                    if progression_review is not None
                    else context.presentation.progression_review_state
                ),
                progression_review_reason=(
                    progression_review.reason_zh
                    if progression_review is not None
                    else context.presentation.progression_review_reason
                ),
                progression_review_parent_age_minutes=_progression_parent_age_minutes(
                    context.progression_candidates,
                    progression_review,
                ),
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

    async def _delete_untradeable(
        self,
        context: _EnrichmentEditContext,
        review: TradabilityReview,
        progression_review: ProgressionReview | None,
    ) -> None:
        sender = context.sender
        if not isinstance(sender, DeletableNewsPushSender):
            return
        evidence: dict[str, Any] = {"tradability_review": review.model_dump(mode="json", exclude_none=True)}
        if progression_review is not None:
            evidence["progression_review"] = progression_review.model_dump(mode="json", exclude_none=True)
        intent_started = bool(
            await self.db.tx(
                "news_delivery_begin_delete",
                lambda repos: repos.news.begin_delivery_delete(
                    event_id=context.event_id,
                    kind=context.kind,
                    evidence=evidence,
                    reason=review.reason_zh,
                    receipt=context.receipt.canonical(),
                    now_ms=now_ms(),
                ),
            )
        )
        if not intent_started:
            logger.warning("News delivery delete failed: news_delivery_delete_intent_conflict")
            return
        try:
            result = await self.finite.run(
                "news_delivery_delete",
                sender.delete_card,
                context.receipt.canonical(),
                timeout_seconds=_DELIVERY_EDIT_TIMEOUT_SECONDS,
                allow_shutdown=True,
            )
            deleted_receipt = TelegramDeliveryReceipt.model_validate(result)
            if deleted_receipt.deleted_at_ms is None:
                raise RuntimeError("news_delivery_delete_receipt_unsettled")
            recorded = bool(
                await self.db.tx(
                    "news_delivery_settle_delete",
                    lambda repos: repos.news.settle_delivery_delete(
                        event_id=context.event_id,
                        kind=context.kind,
                        receipt=deleted_receipt.canonical(),
                        now_ms=now_ms(),
                    ),
                )
            )
            if not recorded:
                raise RuntimeError("news_delivery_delete_receipt_conflict")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_code = getattr(exc, "code", None) or f"{type(exc).__module__}.{type(exc).__name__}"
            await self.db.tx(
                "news_delivery_ambiguous_delete",
                lambda repos: repos.news.mark_delivery_delete_ambiguous(
                    event_id=context.event_id,
                    kind=context.kind,
                    receipt=context.receipt.canonical(),
                    error_code=str(error_code)[:160],
                    now_ms=now_ms(),
                ),
            )
            logger.warning("News delivery delete failed: %s", error_code)

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
            day = points.get(stamp - 24 * _ONE_HOUR_MS)
            if day is not None:
                day_bps = return_bps(day.price, points[stamp].price)
                if day_bps is not None:
                    quote["change_pct"] = float(day_bps) / 100.0
                    quote["change_basis"] = "rolling_24h"
                    quote["change_basis_zh"] = "24 小时"
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
        targets = [stamp, stamp - _ONE_HOUR_MS]
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
        if event_kind == "unsupported_market":
            return card, None, None, admission, event_kind, timing
        triage = repos.news.latest_verdict(event_id=event_id, stage="triage")
        if triage is None:
            return None
        oi_signal = None
        if (
            event_kind == "oi"
            and str(triage.get("program_version") or "") == OI_PROGRAM_VERSION
            and str(triage.get("program_sha256") or "") == self._oi_program_sha256
        ):
            oi_signal = repos.news.oi_signal(event_id=event_id, metric_version=OI_METRIC_VERSION)
        return card, triage, oi_signal, admission, event_kind, timing

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
