"""The core `NewsStore` and `JudgmentCache` ports over the News PostgreSQL repository (#706).

Each `atomic_*` method, and every other write, is exactly one `NewsDatabasePort.tx` call: one short
transaction with no model, provider, broker or send inside it. Model documents are serialized and
parsed outside the transaction. The SQL lives in `event_updates.EventUpdateStorage`.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from ..updates.contracts import EventUpdate, Evidence, Extraction, FrozenInput, PublicUpdate, ReadTarget
from ..updates.judgment import Answer
from ..updates.notification import FrozenCard, NotificationPlan, ReaderSnapshot
from ..updates.ports import IntentLease, NotificationSnapshot, SemanticCheckpoint, SemanticObservation, SendOutcome
from ..updates.service import clock_ms
from .event_updates import (
    INTENT_LEASE_MS,
    NEWS_CHANNEL,
    PUBLIC_TRADE_KINDS,
    EventUpdateConflict,
    SemanticLease,
    frozen_input,
    select_receipts,
)
from .sql_values import _dumps

if TYPE_CHECKING:
    from ..pipeline.runtime import NewsDatabasePort


def _lease_token() -> str:
    return secrets.token_hex(16)


class PgNewsStore:
    """`tracefold.news.updates.ports.NewsStore` over one News database port.

    `watch_symbols` is the reader's configured watchlist (canonical upper-case base symbols). The only
    notification channel is the logical `news` channel.
    """

    def __init__(
        self,
        db: NewsDatabasePort,
        *,
        watch_symbols: Iterable[str] = (),
        clock: Callable[[], int] = clock_ms,
        intent_lease_ms: int = INTENT_LEASE_MS,
        lease_token: Callable[[], str] = _lease_token,
    ) -> None:
        self.db = db
        self.watch_symbols = tuple(
            sorted({str(value).strip().upper() for value in watch_symbols if str(value).strip()})
        )
        self.clock = clock
        self.intent_lease_ms = int(intent_lease_ms)
        self.lease_token = lease_token

    # ------------------------------------------------------------------ semantic input and results
    async def input_for(self, event_id: str) -> FrozenInput:
        material = await self.db.read("news_update_input", lambda repos: repos.news.semantic_input_material(event_id))
        return frozen_input(event_id, material)

    async def head(self, event_id: str) -> EventUpdate | None:
        document = await self.db.read("news_update_head", lambda repos: repos.news.event_update_head_document(event_id))
        return None if document is None else EventUpdate.model_validate(document)

    async def checkpoint(self, work_id: str) -> SemanticCheckpoint | None:
        documents = await self.db.read(
            "news_update_checkpoint", lambda repos: repos.news.semantic_checkpoint_documents(work_id)
        )
        if not documents:
            return None
        extraction = documents.get("extraction")
        understanding = documents.get("understanding")
        return SemanticCheckpoint(
            work_id=work_id,
            extraction=None if extraction is None else Extraction.model_validate(extraction),
            understanding=None if understanding is None else Extraction.model_validate(understanding),
        )

    async def save_extraction(self, work_id: str, extracted: Extraction) -> Extraction:
        return await self._save_stage(work_id, "extraction", extracted)

    async def save_understanding(self, work_id: str, understood: Extraction) -> Extraction:
        return await self._save_stage(work_id, "understanding", understood)

    async def _save_stage(self, work_id: str, stage: str, value: Extraction) -> Extraction:
        document = value.model_dump_json()
        now_ms = self.clock()
        stored = await self.db.tx(
            "news_update_save_checkpoint",
            lambda repos: repos.news.insert_semantic_checkpoint(
                work_id=work_id, stage=stage, document_json=document, now_ms=now_ms
            ),
        )
        return Extraction.model_validate(stored)

    async def save_observation(self, observation: SemanticObservation) -> SemanticObservation:
        understanding = observation.understanding.model_dump_json()
        row = await self.db.tx(
            "news_update_save_observation",
            lambda repos: repos.news.insert_semantic_observation(
                result_id=observation.result_id,
                work_id=observation.work_id,
                event_id=observation.event_id,
                input_revision=observation.input_revision,
                input_sha256=observation.input_sha256,
                program_identity=observation.program_identity,
                completed_at_ms=observation.completed_at_ms,
                understanding_json=understanding,
            ),
        )
        stored = SemanticObservation(
            result_id=str(row["result_id"]),
            work_id=str(row["work_id"]),
            event_id=str(row["event_id"]),
            input_revision=int(row["input_revision"]),
            input_sha256=str(row["input_sha256"]),
            program_identity=str(row["program_identity"]),
            completed_at_ms=int(row["completed_at_ms"]),
            understanding=Extraction.model_validate(row["understanding"]),
        )
        # The completion clock is the stored winner's; every other field must be the same fact.
        if stored.model_copy(update={"completed_at_ms": observation.completed_at_ms}) != observation:
            raise EventUpdateConflict("news_semantic_observation_conflict")
        return stored

    async def atomic_adopt(
        self,
        *,
        expected_head_ref: str | None,
        observation: SemanticObservation,
        update: EventUpdate,
        public: tuple[PublicUpdate, ...],
    ) -> bool:
        if observation.event_id != update.event_id:
            raise ValueError("news_adopt_observation_event_mismatch")
        for row in public:
            if row.event_id != update.event_id or row.content_revision != update.content_revision:
                raise ValueError("news_adopt_public_update_mismatch")
        public_rows = [(PUBLIC_TRADE_KINDS[row.kind], row.model_dump(mode="json")) for row in public]
        document = update.model_dump_json()
        now_ms = self.clock()
        return bool(
            await self.db.tx(
                "news_update_adopt",
                lambda repos: repos.news.adopt_event_update(
                    expected_head_ref=expected_head_ref,
                    update=update,
                    document_json=document,
                    observation_result_id=observation.result_id,
                    public_rows=public_rows,
                    now_ms=now_ms,
                ),
            )
        )

    async def finish_semantic_work(self, work_id: str, *, reason: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_finish_work",
            lambda repos: repos.news.finish_semantic_work(work_id=work_id, reason=reason, now_ms=now_ms),
        )

    async def defer_semantic_work(self, work_id: str, *, reason: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_defer_work",
            lambda repos: repos.news.defer_semantic_work(work_id=work_id, reason=reason, now_ms=now_ms),
        )

    # ------------------------------------------------------------------ notifications
    async def notification_snapshot(self, event_id: str, channel: str) -> NotificationSnapshot | None:
        if channel != NEWS_CHANNEL:
            raise ValueError("news_notification_channel_unknown")
        now_ms = self.clock()
        material = await self.db.read(
            "news_update_notification_snapshot",
            lambda repos: repos.news.notification_snapshot_material(
                event_id=event_id, channel=channel, now_ms=now_ms, watch_symbols=self.watch_symbols
            ),
        )
        if material is None:
            return None
        update = EventUpdate.model_validate(material["head"])
        reader = ReaderSnapshot(
            channel=channel,
            revision=str(material["revision"]),
            receipts=select_receipts(event_id, material["band_event_ids"], material["receipt_rows"]),
            blocked_claim_refs=tuple(material["blocked"]),
            watch_symbols=self.watch_symbols,
        )
        return NotificationSnapshot(update=update, reader=reader)

    async def atomic_record_plan(self, plan: NotificationPlan) -> IntentLease | None:
        token = self.lease_token()
        plan_json = plan.model_dump_json()
        now_ms = self.clock()
        reserved = await self.db.tx(
            "news_update_record_plan",
            lambda repos: repos.news.record_notification_plan(
                plan=plan,
                plan_json=plan_json,
                lease_token=token,
                watch_symbols=self.watch_symbols,
                now_ms=now_ms,
                lease_ms=self.intent_lease_ms,
            ),
        )
        if reserved is None:
            return None
        frozen = reserved["frozen_card"]
        return IntentLease(
            intent_id=str(reserved["intent_id"]),
            lease_token=token,
            plan=plan,
            card=None if frozen is None else FrozenCard.model_validate(frozen),
        )

    async def save_card(self, lease: IntentLease, card: FrozenCard) -> FrozenCard:
        if card.intent_id != lease.intent_id or card.claim_refs != lease.plan.selected_claim_refs:
            raise ValueError("news_card_intent_mismatch")
        card_json = card.model_dump_json()
        now_ms = self.clock()
        stored = await self.db.tx(
            "news_update_save_card",
            lambda repos: repos.news.save_intent_card(
                intent_id=lease.intent_id, lease_token=lease.lease_token, card_json=card_json, now_ms=now_ms
            ),
        )
        return FrozenCard.model_validate(stored)

    async def atomic_begin_send(self, lease: IntentLease, card: FrozenCard) -> bool:
        now_ms = self.clock()
        return bool(
            await self.db.tx(
                "news_update_begin_send",
                lambda repos: repos.news.begin_intent_send(
                    intent_id=lease.intent_id,
                    lease_token=lease.lease_token,
                    plan=lease.plan,
                    card=card,
                    watch_symbols=self.watch_symbols,
                    now_ms=now_ms,
                ),
            )
        )

    async def settle_send(
        self,
        lease: IntentLease,
        card: FrozenCard,
        outcome: SendOutcome,
        *,
        settled_at_ms: int,
    ) -> None:
        if outcome.payload_sha256 != card.payload_sha256:
            raise EventUpdateConflict("news_send_outcome_payload_mismatch")
        await self.db.tx(
            "news_update_settle_send",
            lambda repos: repos.news.settle_intent_send(
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

    async def record_card_failure(self, lease: IntentLease, *, error_code: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_card_failure",
            lambda repos: repos.news.record_intent_card_failure(
                intent_id=lease.intent_id, lease_token=lease.lease_token, error_code=error_code, now_ms=now_ms
            ),
        )

    async def defer_notification(self, event_id: str, channel: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_defer_notification",
            lambda repos: repos.news.defer_notification_work(event_id=event_id, channel=channel, now_ms=now_ms),
        )

    # ------------------------------------------------------------------ optional read
    async def reserve_extra_read(self, lineage_id: str, target_ref: str) -> bool:
        now_ms = self.clock()
        return bool(
            await self.db.tx(
                "news_update_reserve_read",
                lambda repos: repos.news.reserve_extra_read(
                    lineage_id=lineage_id, target_ref=target_ref, now_ms=now_ms
                ),
            )
        )

    async def attach_extra_evidence(
        self,
        source: FrozenInput,
        target: ReadTarget,
        evidence: tuple[Evidence, ...],
        affected_claim_refs: tuple[str, ...],
    ) -> None:
        del target  # reserved by `reserve_extra_read`; the evidence carries its own provenance
        if not evidence:
            raise ValueError("news_extra_evidence_empty")
        evidence_json = _dumps([item.model_dump(mode="json") for item in {row.ref: row for row in evidence}.values()])
        now_ms = self.clock()
        await self.db.tx(
            "news_update_attach_evidence",
            lambda repos: repos.news.attach_extra_evidence(
                event_id=source.event_id,
                lineage_id=source.lineage_id,
                evidence_json=evidence_json,
                focus_claim_refs=affected_claim_refs,
                now_ms=now_ms,
            ),
        )

    async def record_read_outcome(self, lineage_id: str, *, outcome: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_read_outcome",
            lambda repos: repos.news.record_read_outcome(lineage_id=lineage_id, outcome=outcome, now_ms=now_ms),
        )

    # ------------------------------------------------------------------ repair and public relay
    async def pending_semantic_events(self, limit: int) -> tuple[str, ...]:
        now_ms = self.clock()
        return tuple(
            await self.db.read(
                "news_update_pending_semantic",
                lambda repos: repos.news.pending_semantic_event_ids(now_ms=now_ms, limit=limit),
            )
        )

    async def pending_notification_events(self, channel: str, limit: int) -> tuple[str, ...]:
        now_ms = self.clock()
        return tuple(
            await self.db.read(
                "news_update_pending_notification",
                lambda repos: repos.news.pending_notification_event_ids(channel=channel, now_ms=now_ms, limit=limit),
            )
        )

    async def pending_public_updates(self, limit: int) -> tuple[PublicUpdate, ...]:
        payloads = await self.db.read(
            "news_update_pending_public", lambda repos: repos.news.pending_public_update_payloads(limit=limit)
        )
        return tuple(PublicUpdate.model_validate(payload) for payload in payloads)

    async def acknowledge_public_update(self, update_id: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_ack_public",
            lambda repos: repos.news.acknowledge_public_update(update_id=update_id, now_ms=now_ms),
        )

    # ------------------------------------------------------------------ semantic worker lease (U2)
    async def claim_semantic_work(self, event_id: str, *, lease_ms: int) -> SemanticLease | None:
        token = self.lease_token()
        now_ms = self.clock()
        return await self.db.tx(
            "news_update_claim_work",
            lambda repos: repos.news.claim_semantic_work(
                event_id=event_id, lease_token=token, now_ms=now_ms, lease_ms=lease_ms
            ),
        )

    async def defer_semantic_event(self, lease: SemanticLease, *, reason: str, retry_after_ms: int = 0) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_defer_event",
            lambda repos: repos.news.defer_semantic_event(
                event_id=lease.event_id,
                lease_token=lease.lease_token,
                reason=reason,
                now_ms=now_ms,
                retry_after_ms=retry_after_ms,
            ),
        )

    async def fail_semantic_event(self, lease: SemanticLease, *, error_code: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_fail_event",
            lambda repos: repos.news.fail_semantic_event(
                event_id=lease.event_id, lease_token=lease.lease_token, error_code=error_code, now_ms=now_ms
            ),
        )

    async def mark_semantic_work_published(self, event_id: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_mark_published",
            lambda repos: repos.news.mark_semantic_work_published(event_id=event_id, now_ms=now_ms),
        )

    async def purge_semantic_caches(self, *, limit: int) -> int:
        now_ms = self.clock()
        return int(
            await self.db.tx(
                "news_update_purge_caches",
                lambda repos: repos.news.purge_semantic_caches(now_ms=now_ms, limit=limit),
            )
        )


class PgJudgmentCache:
    """`tracefold.news.updates.judgment.JudgmentCache` over `news_judgment_cache` (14-day retention)."""

    def __init__(self, db: NewsDatabasePort, *, clock: Callable[[], int] = clock_ms) -> None:
        self.db = db
        self.clock = clock

    async def get(self, key: str) -> Answer | None:
        answer = await self.db.read("news_judgment_cache_get", lambda repos: repos.news.judgment_cache_answer(key))
        return None if answer is None else Answer.model_validate(answer)

    async def put(self, key: str, answer: Answer) -> None:
        answer_json = answer.model_dump_json()
        now_ms = self.clock()
        await self.db.tx(
            "news_judgment_cache_put",
            lambda repos: repos.news.put_judgment_cache_answer(cache_key=key, answer_json=answer_json, now_ms=now_ms),
        )


__all__ = ["PgJudgmentCache", "PgNewsStore"]
