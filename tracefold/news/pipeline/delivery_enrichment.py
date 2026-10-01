"""Display-only quote enrichment and receipt-fenced edits after editorial settlement."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..bus import now_ms
from ..delivery import (
    news_update_card,
    reader_market_movements,
    reader_trade_targets,
    update_card_assets,
    update_news_at_ms,
    update_primary_symbols,
)
from ..feishu_card import feishu_card
from ..models import MarketAsset, ReaderDeliveryPresentation, TelegramDeliveryReceipt, base_symbol, market_type_of
from ..notifications.contracts import FrozenCard, NotificationPlan
from ..tradability import (
    TRADABILITY_REVIEW_TIMEOUT_SECONDS,
    TradabilityReview,
    TradabilityVerifier,
)
from ..updates.contracts import EventUpdate
from .delivery_quotes import DeliveryCandleFetcherFor, DeliveryPriceFetcherFor, DeliveryQuotes
from .runtime import NewsDatabasePort
from .send_entry import InitialSendEntry

if TYPE_CHECKING:
    from ..notifications.service import NotificationTurn

logger = logging.getLogger(__name__)


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


class DeliveryEnrichment:
    """Own display reads and edits; never decide whether a news claim should be sent."""

    def __init__(
        self,
        *,
        db: NewsDatabasePort,
        send_entry: InitialSendEntry,
        candle_fetcher_for: DeliveryCandleFetcherFor | None = None,
        price_fetcher_for: DeliveryPriceFetcherFor | None = None,
        tradability_verifier: TradabilityVerifier | None = None,
    ) -> None:
        self.db = db
        self.send_entry = send_entry
        self.quotes = DeliveryQuotes(db, candle_fetcher_for=candle_fetcher_for, price_fetcher_for=price_fetcher_for)
        self._tradability_verifier = tradability_verifier
        self._edit_tasks: set[asyncio.Task[None]] = set()

    def enrich_sent(self, turn: NotificationTurn) -> None:
        """Start the in-place edit a sent card on an editable channel earns: quotes, then tradability."""

        if (
            not self.send_entry.editable
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
                self.quotes.market_data(
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
                quotes = await self.quotes.for_matches(
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

    async def drain(self) -> None:
        if self._edit_tasks:
            await asyncio.gather(*tuple(self._edit_tasks), return_exceptions=True)
