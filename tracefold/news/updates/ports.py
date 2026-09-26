"""Persistence and side-effect contracts of the new core, not legacy adapters.

The host implements these with the existing PG repository/worker/delivery
capabilities. Every method named atomic_* is one short database transaction;
none may execute a model, source fetch, or external send inside that transaction.
No implementation is allowed to encode revisions in a delivery `kind` string.
"""
from __future__ import annotations

from typing import Literal, Protocol
from pydantic import Field

from .contracts import EventUpdate, Evidence, Exact, Extraction, FrozenInput, PublicUpdate, ReadTarget
from .notification import DeliveredText, FrozenCard, NotificationPlan, ReaderSnapshot


class SemanticCheckpoint(Exact):
    work_id: str
    extraction: Extraction | None = None
    understanding: Extraction | None = None


class SemanticObservation(Exact):
    result_id: str
    work_id: str
    event_id: str
    input_revision: int
    input_sha256: str
    program_identity: str
    completed_at_ms: int
    understanding: Extraction


class NotificationSnapshot(Exact):
    update: EventUpdate
    reader: ReaderSnapshot


class IntentLease(Exact):
    intent_id: str
    lease_token: str
    plan: NotificationPlan
    card: FrozenCard | None = None


class SendOutcome(Exact):
    state: Literal["sent", "not_sent", "ambiguous"]
    payload_sha256: str
    message_id: str | None = None
    error_code: str | None = None
    retryable: bool = False
    retry_after_ms: int | None = Field(default=None, ge=0)


class ExistingSourceReader(Protocol):
    async def read(self, target: ReadTarget, *, timeout: float) -> tuple[Evidence, ...]: ...


class Sender(Protocol):
    async def send(self, card: FrozenCard, *, channel: str, timeout: float) -> SendOutcome: ...


class NewsStore(Protocol):
    async def input_for(self, event_id: str) -> FrozenInput: ...
    async def head(self, event_id: str) -> EventUpdate | None: ...
    async def checkpoint(self, work_id: str) -> SemanticCheckpoint | None: ...
    async def save_extraction(self, work_id: str, extracted: Extraction) -> Extraction:
        """Insert-only work stage; return the first stored winner on a race."""
        ...
    async def save_understanding(self, work_id: str, understood: Extraction) -> Extraction: ...
    async def save_observation(self, observation: SemanticObservation) -> SemanticObservation:
        """Insert-only result_id; return the stored winner, including its original
        completion clock, on replay. Content mismatches are errors. This write is
        independent of adoption and cards.
        """
        ...
    async def atomic_adopt(self, *, expected_head_ref: str | None, observation: SemanticObservation,
                           update: EventUpdate, public: tuple[PublicUpdate, ...]) -> bool:
        """CAS the adopted head, save update + public outbox + notification_pending.

        Return False only for a changed adopted head, not simply newer arriving
        evidence. Never downgrade an adopted input revision. Unique public IDs
        retain the first payload; conflicting payload on an ID is an error.
        """
        ...
    async def finish_semantic_work(self, work_id: str, *, reason: str) -> None: ...
    async def defer_semantic_work(self, work_id: str, *, reason: str) -> None: ...
    async def notification_snapshot(self, event_id: str, channel: str) -> NotificationSnapshot | None:
        """Consistent adopted head and actual-reader snapshot, not observed history.

        blocked_claim_refs comes from overlapping sending/ambiguous intents. It
        prevents a new ID from blindly retrying an unresolved external send; it
        does not count those claims as received.
        """
        ...
    async def atomic_record_plan(self, plan: NotificationPlan) -> IntentLease | None:
        """Check head/reader versions; persist decision and reserve an intent.

        No-notification clears the matching pending marker. Unresolved stays
        retryable under the existing bounded work policy. A notify result gets
        one stable intent/queue row. deferred_claim_refs remain pending even if
        other selected claims were reserved successfully. A concurrent active lease, version race, or
        already sending/sent/ambiguous identity returns None without resetting it.
        """
        ...
    async def save_card(self, lease: IntentLease, card: FrozenCard) -> FrozenCard:
        """Fenced insert-only payload; an existing frozen payload wins."""
        ...
    async def atomic_begin_send(self, lease: IntentLease, card: FrozenCard) -> bool:
        """Recheck head, reader revision, lease and in-flight overlap; freeze sending.

        On a changed selection, retire only the unsent reservation and leave
        notification pending. Never mutate a sending payload or reset ambiguous.
        """
        ...
    async def settle_send(self, lease: IntentLease, card: FrozenCard, outcome: SendOutcome,
                          *, settled_at_ms: int) -> None:
        """Fenced actual receipt + queue outcome, atomically.

        Sent retains the exact body/hash, target, provider message ID and time.
        Not-sent may retry this SAME identity/payload, respecting existing retry
        limits/backoff. Ambiguous is held for existing reconciliation, not retried.
        """
        ...
    async def record_card_failure(self, lease: IntentLease, *, error_code: str) -> None: ...
    async def reserve_extra_read(self, lineage_id: str, target_ref: str) -> bool:
        """Atomic one-read budget for the entire lineage, durable across retries."""
        ...
    async def attach_extra_evidence(self, source: FrozenInput, target: ReadTarget,
                                    evidence: tuple[Evidence, ...], affected_claim_refs: tuple[str, ...]) -> None:
        """Append a new evidence revision and enqueue only the affected semantic work.

        Preserve lineage and source first-known clocks. The next frozen input has
        focus_claim_refs and only the changed/affected source material; unaffected
        head claims are carried by assembly rather than re-extracted.
        """
        ...
    async def record_read_outcome(self, lineage_id: str, *, outcome: str) -> None: ...
    async def pending_semantic_events(self, limit: int) -> tuple[str, ...]: ...
    async def pending_notification_events(self, channel: str, limit: int) -> tuple[str, ...]: ...
    async def pending_public_updates(self, limit: int) -> tuple[PublicUpdate, ...]: ...
    async def acknowledge_public_update(self, update_id: str) -> None: ...


class TradingReceiver(Protocol):
    async def receive_catalyst(self, update: PublicUpdate) -> None:
        """App maps to existing target selection/accept-trigger, once per update_id."""
        ...
    async def receive_source_update(self, update: PublicUpdate) -> None:
        """App maps to Trading-owned claim-scoped research amendments.

        Atomically receive update_id and amend only research referencing
        affected_claim_refs/previous_content_refs. Do not accept a trigger,
        create a Case, refresh TTL, cancel orders or expand execution authority.
        """
        ...
