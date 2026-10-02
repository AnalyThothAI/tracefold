"""Typed notification planning and sending over short PostgreSQL transactions."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ..bus import DeferError, TransientError
from ..claim_recall import EmbeddingPort, Probe, embed_text
from ..clock import clock_ms
from ..notifications.contracts import NEWS_CHANNEL, CardCopy, FrozenCard, NotificationPlan, ReaderSnapshot
from ..notifications.novelty import ClaimLink, LinkedReceipt
from ..notifications.ports import (
    BeginSendStatus,
    DeliveryTimings,
    IntentLease,
    NotificationSnapshot,
    PlanCommit,
    SendOutcome,
)
from ..updates.contracts import EventUpdate
from .errors import EventUpdateConflict
from .notification_context import delivered_text
from .notification_work import INTENT_LEASE_MS
from .reader_check import ReaderCheck

if TYPE_CHECKING:
    from tracefold.platform.observability import TelemetryRegistry

    from ..pipeline.runtime import NewsDatabasePort


def _lease_token() -> str:
    return secrets.token_hex(16)


class PgNotificationStore:
    def __init__(
        self,
        db: NewsDatabasePort,
        *,
        clock: Callable[[], int] = clock_ms,
        intent_lease_ms: int = INTENT_LEASE_MS,
        lease_token: Callable[[], str] = _lease_token,
        embedder: EmbeddingPort | None = None,
        telemetry: TelemetryRegistry | None = None,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.clock = clock
        self.intent_lease_ms = int(intent_lease_ms)
        self.lease_token = lease_token
        self.telemetry = telemetry

    def _reader_changed(self, stage: str) -> None:
        logging.getLogger("tracefold.news").info("news_reader_changed stage=%s count=1", stage)
        if self.telemetry is not None:
            self.telemetry.news_reader_changed_total.labels(stage=stage).inc()

    async def notification_snapshot(self, event_id: str, channel: str) -> NotificationSnapshot | None:
        if channel != NEWS_CHANNEL:
            raise ValueError("news_notification_channel_unknown")
        now_ms = self.clock()
        probes: dict[str, Probe] = {}
        if self.embedder is not None:
            document = await self.db.read(
                "news_reader_embedding_head", lambda r: r.news.semantic_updates.event_update_head_document(event_id)
            )
            if document is not None:
                head = EventUpdate.model_validate(document)
                encoded = await self.embedder.probes([embed_text(c) for c in head.current_claims])
                probes = {c.ref: p for c, p in zip(head.current_claims, encoded, strict=True)}
        material = await self.db.read(
            "news_update_notification_snapshot",
            lambda repos: repos.news.notification_context.notification_snapshot_material(
                event_id=event_id, channel=channel, now_ms=now_ms, probes=probes
            ),
            repeatable_read=True,
        )
        if material is None:
            return None
        update = EventUpdate.model_validate(material["head"])
        reader = ReaderSnapshot(
            channel=channel,
            revision=str(material["revision"]),
            receipts=tuple(text for row in material["receipt_rows"] if (text := delivered_text(row)) is not None),
            receipt_intents_by_claim={
                str(ref): tuple(str(intent) for intent in intents)
                for ref, intents in material["receipt_intents_by_claim"].items()
            },
            blocked_claim_refs=tuple(material["blocked"]),
            ambiguous_claim_refs=tuple(material["ambiguous"]),
            invalidated_claim_refs=tuple(material["invalidated"]),
            protected_listing_claim_refs=tuple(material["protected_listing"]),
            links=tuple(
                ClaimLink(
                    current_ref=str(row["current_ref"]),
                    previous_ref=str(row["previous_ref"]),
                    relation=row["relation"],
                    asserted_at_ms=int(row["asserted_at_ms"]),
                )
                for row in material["links"]
            ),
            link_receipts=tuple(
                LinkedReceipt(
                    intent_id=str(row["intent_id"]),
                    state=row["state"],
                    claim_refs=tuple(str(ref) for ref in row["claim_refs"] or ()),
                    settled_at_ms=None if row["settled_at_ms"] is None else int(row["settled_at_ms"]),
                )
                for row in material["link_receipts"]
            ),
        )
        return NotificationSnapshot(
            update=update,
            reader=reader,
            work_updated_at_ms=material["work_updated_at_ms"],
            work_due_at_ms=material["work_due_at_ms"],
            recall_diagnostics=material["recall_diagnostics"],
        )

    async def atomic_record_plan(
        self, plan: NotificationPlan, *, recall_diagnostics: Mapping[str, Mapping[str, Any]] | None = None
    ) -> PlanCommit:
        token = self.lease_token()
        plan_json = plan.model_dump_json()
        for _ in range(3):
            reserved, check = await self._record_plan_once(
                plan, token, plan_json, recall_diagnostics=recall_diagnostics
            )
            if reserved["status"] != "reader_changed" or check is None or check.revision != plan.reader_revision:
                break
        status = str(reserved["status"])
        if status == "reader_changed":
            self._reader_changed("plan")
        if status != "committed":
            return PlanCommit(status=status)
        effective = NotificationPlan.model_validate(reserved["plan"])
        if reserved["intent_id"] is None:
            return PlanCommit(status="committed", effective_plan=effective)
        frozen = reserved["frozen_card"]
        return PlanCommit(
            status="committed",
            effective_plan=effective,
            lease=IntentLease(
                intent_id=str(reserved["intent_id"]),
                lease_token=token,
                plan=effective,
                card=None if frozen is None else FrozenCard.model_validate(frozen),
            ),
        )

    async def _record_plan_once(
        self,
        plan: NotificationPlan,
        token: str,
        plan_json: str,
        *,
        recall_diagnostics: Mapping[str, Mapping[str, Any]] | None,
    ) -> tuple[dict[str, Any], ReaderCheck | None]:
        now_ms = self.clock()
        check = await self.db.read(
            "news_update_plan_permission",
            lambda repos: repos.news.notification_context.read_plan_permission(plan.update_ref, now_ms=now_ms),
            repeatable_read=True,
        )
        if check is None:
            return {"status": "head_changed"}, None
        reserved = await self.db.tx(
            "news_update_record_plan",
            lambda repos: repos.news.notification_work.record_notification_plan(
                plan=plan,
                plan_json=plan_json,
                lease_token=token,
                now_ms=now_ms,
                lease_ms=self.intent_lease_ms,
                check=check,
                recall_diagnostics=recall_diagnostics,
            ),
        )
        return reserved, check

    async def lookup_card_copy(self, input_digest: str) -> CardCopy | None:
        document = await self.db.read(
            "news_update_card_copy_lookup",
            lambda repos: repos.news.notification_work.lookup_card_copy(input_digest=input_digest),
        )
        return None if document is None else CardCopy.model_validate(document)

    async def save_card(self, lease: IntentLease, card: FrozenCard, *, copy: CardCopy, input_digest: str) -> FrozenCard:
        if card.intent_id != lease.intent_id or card.claim_refs != lease.plan.selected_claim_refs:
            raise ValueError("news_card_intent_mismatch")
        lines = {line.claim_ref: line.text_zh for line in copy.lines}
        if (
            len(lines) != len(copy.lines)
            or set(lines) != set(card.claim_refs)
            or card.body != "\n\n".join((copy.headline_zh, *(lines[ref] for ref in card.claim_refs)))
        ):
            raise ValueError("news_card_copy_mismatch")
        card_json = card.model_dump_json()
        now_ms = self.clock()
        stored = await self.db.tx(
            "news_update_save_card",
            lambda repos: repos.news.notification_work.save_intent_card(
                intent_id=lease.intent_id,
                lease_token=lease.lease_token,
                card_json=card_json,
                copy_json=copy.model_dump_json(),
                input_digest=input_digest,
                now_ms=now_ms,
            ),
        )
        return FrozenCard.model_validate(stored)

    async def release_unsent_intent(self, lease: IntentLease) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_release_unsent",
            lambda repos: repos.news.notification_work.release_unsent_intent(
                intent_id=lease.intent_id, lease_token=lease.lease_token, now_ms=now_ms
            ),
        )

    async def atomic_begin_send(
        self, lease: IntentLease, card: FrozenCard, *, timings: DeliveryTimings | None = None
    ) -> BeginSendStatus:
        timings_json = None if timings is None else timings.model_dump_json()
        for _ in range(3):
            status, check = await self._begin_send_once(lease, card, timings_json)
            if status != "reader_changed" or check is None or check.revision != lease.plan.reader_revision:
                break
        if status == "reader_changed":
            self._reader_changed("send")
        return status

    async def _begin_send_once(
        self, lease: IntentLease, card: FrozenCard, timings_json: str | None
    ) -> tuple[BeginSendStatus, ReaderCheck | None]:
        now_ms = self.clock()
        check = await self.db.read(
            "news_update_send_permission",
            lambda repos: repos.news.notification_context.read_intent_permission(lease.intent_id, now_ms=now_ms),
            repeatable_read=True,
        )
        if check is None:
            return "lease_lost", None
        status: BeginSendStatus = await self.db.tx(
            "news_update_begin_send",
            lambda repos: repos.news.notification_delivery.begin_intent_send(
                intent_id=lease.intent_id,
                lease_token=lease.lease_token,
                plan=lease.plan,
                card=card,
                now_ms=now_ms,
                timings_json=timings_json,
                check=check,
            ),
        )
        return status, check

    async def settle_send(
        self,
        lease: IntentLease,
        card: FrozenCard,
        outcome: SendOutcome,
        *,
        settled_at_ms: int,
    ) -> str:
        if outcome.payload_sha256 != card.payload_sha256:
            raise EventUpdateConflict("news_send_outcome_payload_mismatch")
        result = await self.db.tx(
            "news_update_settle_send",
            lambda repos: repos.news.notification_delivery.settle_intent_send(
                intent_id=lease.intent_id,
                lease_token=lease.lease_token,
                payload_sha256=card.payload_sha256,
                state=outcome.state,
                provider_message_id=outcome.message_id,
                error_code=outcome.error_code,
                retryable=outcome.retryable,
                retry_after_ms=outcome.retry_after_ms,
                settled_at_ms=settled_at_ms,
                provider_receipt=outcome.receipt,
            ),
        )
        if result == "conflict":
            raise RuntimeError("news_send_settlement_conflict")
        return str(result)

    async def record_unsent_failure(
        self,
        lease: IntentLease,
        *,
        error_code: str,
        retryable: bool,
        retry_after_ms: int | None = None,
    ) -> None:
        now_ms = self.clock()
        for attempt in range(3):
            try:
                await self.db.tx(
                    "news_update_unsent_failure",
                    lambda repos: repos.news.notification_work.record_unsent_intent_failure(
                        intent_id=lease.intent_id,
                        lease_token=lease.lease_token,
                        error_code=error_code,
                        retryable=retryable,
                        retry_after_ms=retry_after_ms,
                        now_ms=now_ms,
                    ),
                )
                return
            except (DeferError, TransientError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))

    async def defer_notification(
        self,
        event_id: str,
        channel: str,
        expected_content_revision: str | None,
        expected_work_updated_at_ms: int | None = None,
        *,
        error_code: str,
    ) -> None:
        now_ms = self.clock()
        for attempt in range(3):
            try:
                await self.db.tx(
                    "news_update_defer_notification",
                    lambda repos: repos.news.notification_work.defer_notification_work(
                        event_id=event_id,
                        channel=channel,
                        expected_content_revision=expected_content_revision,
                        expected_work_updated_at_ms=expected_work_updated_at_ms,
                        error_code=error_code,
                        now_ms=now_ms,
                    ),
                )
                return
            except (DeferError, TransientError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))

    async def postpone_notification(self, event_id: str, channel: str, expected_content_revision: str | None) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_postpone_notification",
            lambda repos: repos.news.notification_work.postpone_notification_work(
                event_id=event_id,
                channel=channel,
                expected_content_revision=expected_content_revision,
                now_ms=now_ms,
            ),
        )

    async def pending_notification_events(self, channel: str, limit: int) -> tuple[str, ...]:
        now_ms = self.clock()
        return tuple(
            await self.db.read(
                "news_update_pending_notification",
                lambda repos: repos.news.notification_work.pending_notification_event_ids(
                    channel=channel, now_ms=now_ms, limit=limit
                ),
            )
        )
