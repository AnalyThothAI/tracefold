"""Editorial Sender adapter: preflight, rendering and classification of provider evidence."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import Any

from tracefold.platform.resource import ResourceAdmissionTimeout

from ..bus import now_ms
from ..delivery import (
    news_update_card,
    reader_market_movements,
    reader_trade_targets,
    update_card_assets,
    update_news_at_ms,
)
from ..delivery_contracts import (
    DELIVERY_FAILURE_RETRIABLE,
    DELIVERY_FAILURE_UNKNOWN,
    classify_delivery_failure,
    retry_after_ms,
)
from ..feishu_card import feishu_card
from ..models import ReaderDeliveryPresentation
from ..notifications.contracts import FrozenCard, NotificationPlan
from ..notifications.ports import SendOutcome
from ..reader_card import ReaderCard
from ..updates.contracts import EventUpdate
from .delivery_enrichment import DeliveryEnrichment
from .send_entry import InitialSendEntry


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}")[:160]


class NotificationSender:
    """Use the shared slot through preflight, durable begin, provider call and durable settlement.

    The workflow owns begin/settle; the adapter's prepared presentation is local to that held slot.
    It is never a second frozen ledger payload or a second notification decision.
    """

    def __init__(self, send_entry: InitialSendEntry, enrichment: DeliveryEnrichment) -> None:
        self.send_entry = send_entry
        self.enrichment = enrichment
        self._prepared_send: tuple[str, ReaderCard, Mapping[str, Any], ReaderDeliveryPresentation] | None = None

    @property
    def available(self) -> bool:
        return self.send_entry.available

    @contextlib.asynccontextmanager
    async def send_slot(self) -> AsyncIterator[None]:
        async with self.send_entry.slot():
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
        sha = card.payload_sha256
        if not self.send_entry.available:
            return SendOutcome(state="not_sent", payload_sha256=sha, error_code="news_delivery_unavailable")
        try:
            await self.send_entry.prepare()
        except Exception as exc:
            return SendOutcome(
                state="not_sent",
                payload_sha256=sha,
                error_code=_error_code(exc),
                retryable=True,
                retry_after_ms=retry_after_ms(exc) or None,
            )
        editable = self.send_entry.editable
        shown = update_card_assets(update, card.claim_refs)
        news_at_ms = update_news_at_ms(update, card.claim_refs)
        # A channel that cannot be edited gets its quotes now; an editable one is sent first and edited.
        quotes = [] if editable else await self.enrichment.quotes.market_data(shown, now_ms(), news_at_ms=news_at_ms)
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
            result = await self.send_entry.send_reserved_card(
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
