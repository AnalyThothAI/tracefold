"""Persistence and side-effect contracts of the new core, not legacy adapters.

The host implements these with the existing PG repository/worker/delivery
capabilities. Every method named atomic_* is one short database transaction;
none may execute a model, source fetch, or external send inside that transaction.
No implementation is allowed to encode revisions in a delivery `kind` string.
No port method takes a timeout: callers bound external calls with asyncio.timeout.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, Protocol

from pydantic import Field

from .contracts import EventUpdate, Evidence, Exact, Extraction, FrozenInput, PublicUpdate, ReadTarget, SemanticLease
from .notification import CardCopy, FrozenCard, NotificationPlan, ReaderSnapshot


class SemanticCheckpoint(Exact):
    work_id: str
    extraction: Extraction | None = None


class SemanticObservation(Exact):
    result_id: str
    work_id: str
    event_id: str
    input_revision: int
    input_sha256: str
    program_identity: str
    completed_at_ms: int
    understanding: Extraction
    read_refs: tuple[str, ...]
    reanalysis_reason: str | None = None
    reanalysis_head_ref: str | None = None


class NotificationSnapshot(Exact):
    update: EventUpdate
    reader: ReaderSnapshot
    work_updated_at_ms: int | None = None
    # When the work became due: the start of the wait a planning turn's timings are measured from.
    work_due_at_ms: int | None = None


class IntentLease(Exact):
    intent_id: str
    lease_token: str
    plan: NotificationPlan
    card: FrozenCard | None = None


class PlanCommit(Exact):
    status: Literal["committed", "head_changed", "reader_changed", "already_settled", "overlap"]
    effective_plan: NotificationPlan | None = None
    lease: IntentLease | None = None


BeginSendStatus = Literal["begun", "head_changed", "reader_changed", "lease_lost", "already_settled", "overlap"]


class DeliveryTimings(Exact):
    """Where one send spent its time before the provider call, kept beside its receipt. Audit only.

    The card clocks are absent when a frozen card was reused rather than composed by this turn.
    `send_slot_wait_ms` is from the card being ready to this turn holding the process's one send slot.
    """

    card_started_at_ms: int | None = Field(default=None, ge=0)
    card_finished_at_ms: int | None = Field(default=None, ge=0)
    ready_at_ms: int = Field(ge=0)
    send_slot_wait_ms: int = Field(ge=0)


class SendOutcome(Exact):
    state: Literal["sent", "not_sent", "ambiguous"]
    payload_sha256: str
    # The provider's own message identity. A channel that answers with none (a Feishu webhook) says so
    # with None rather than a made-up value.
    message_id: str | None = None
    # The provider's own receipt as the adapter returned it, e.g. what a later in-place edit is fenced by.
    receipt: dict[str, Any] | None = None
    error_code: str | None = None
    retryable: bool = False
    retry_after_ms: int | None = Field(default=None, ge=0)


class ExistingSourceReader(Protocol):
    async def read(self, target: ReadTarget) -> tuple[Evidence, ...]:
        """Read one code-prepared target with existing capabilities; no model-generated URL or tool."""
        ...


class Sender(Protocol):
    def send_slot(self) -> AbstractAsyncContextManager[None]:
        """Paced exclusive opportunity held through durable receipt settlement."""
        ...

    async def preflight(self, card: FrozenCard, *, plan: NotificationPlan, update: EventUpdate) -> SendOutcome | None:
        """Prepare the target and wire body before begin_send; return a proven unsent failure or None."""
        ...

    async def send(self, card: FrozenCard, *, plan: NotificationPlan, update: EventUpdate) -> SendOutcome:
        """Send the frozen body unchanged on `plan.channel` and report the actual outcome.

        `plan` and `update` are the adopted content the frozen card was selected from; an adapter may add
        code-owned facts around the body (assets, quotes, the key marker, the change label) but never
        rewrite, clip or re-generate it. The adapter bounds its own provider call. A provider answer it
        can prove never reached a reader is `not_sent`; anything it cannot account for is raised or
        reported `ambiguous`, never guessed as not sent.
        """
        ...


class NewsStore(Protocol):
    async def head(self, event_id: str) -> EventUpdate | None: ...

    async def checkpoint(self, work_id: str) -> SemanticCheckpoint | None: ...

    async def save_extraction(self, work_id: str, extracted: Extraction) -> Extraction:
        """Insert-only work stage; return the first stored winner on a race."""
        ...

    async def save_observation(self, observation: SemanticObservation) -> SemanticObservation:
        """Insert-only result_id; return the stored winner, including its original
        completion clock, on replay. Content mismatches are errors. This write is
        independent of adoption and cards.
        """
        ...

    async def atomic_adopt(
        self,
        *,
        expected_head_ref: str | None,
        lease: SemanticLease,
        observation: SemanticObservation,
        update: EventUpdate,
        public: tuple[PublicUpdate, ...],
    ) -> bool:
        """CAS the adopted head, save update + public outbox + notification_pending.

        Return False only for a changed adopted head, not simply newer arriving
        evidence. Never downgrade an adopted input revision. Unique public IDs
        retain the first payload; conflicting payload on an ID is an error.
        A `possible_new` change is adopted content and marks notification work,
        but it never has a public row: `public` already excludes it.
        """
        ...

    async def finish_semantic_work(self, work_id: str, *, lease: SemanticLease, reason: str) -> None: ...

    async def defer_semantic_event(self, lease: SemanticLease, *, reason: str, retry_after_ms: int = 0) -> None: ...

    async def notification_snapshot(self, event_id: str, channel: str) -> NotificationSnapshot | None:
        """Consistent adopted head and actual-reader snapshot, not observed history.

        blocked_claim_refs comes from this Event's sends still in flight and
        ambiguous_claim_refs from its sends with no provable outcome. Neither is
        counted as received; the first makes the plan wait, the second is never
        sent again. The reader revision is the digest of the receipts related to
        this Event and those claim sets, so only a related change races a plan.
        watch_symbols is the reader's code-owned watchlist as canonical upper-case
        base symbols.
        """
        ...

    async def atomic_record_plan(self, plan: NotificationPlan) -> PlanCommit:
        """Persist or reuse an immutable decision, check head/reader versions, reserve an intent.

        The decision is written before the reader check, so a plan that loses the
        race keeps its judgments for the next turn to reuse; an identical plan is
        the same decision row. Every claim_decisions row is persisted with its
        reason, so the Console can show why a claim was or was not notified. A
        no_notification plan completes the matching pending marker. An unresolved
        plan (only a send of this Event still in flight makes one) waits without
        spending an attempt. A notify result gets one stable intent/queue row
        bound to this decision; deferred claims keep the marker waiting even if
        other selected claims were reserved. A selection whose intent already
        ended is handled, not re-sent. A concurrent active lease or version race
        returns a recorded result without a lease.
        """
        ...

    async def lookup_notification_decision(
        self, event_id: str, channel: str, input_digest: str
    ) -> NotificationPlan | None: ...

    async def lookup_card_copy(self, input_digest: str) -> CardCopy | None: ...

    async def save_card(self, lease: IntentLease, card: FrozenCard, *, copy: CardCopy, input_digest: str) -> FrozenCard:
        """Fenced insert-only payload; an existing frozen payload wins."""
        ...

    async def release_unsent_intent(self, lease: IntentLease) -> None:
        """Release a pending owned intent cancelled before begin_send was called."""
        ...

    async def atomic_begin_send(
        self, lease: IntentLease, card: FrozenCard, *, timings: DeliveryTimings | None = None
    ) -> BeginSendStatus:
        """Recheck head, reader revision, lease and in-flight overlap; freeze sending.

        On a changed selection, retire only the unsent reservation and leave
        notification pending. Never mutate a sending payload or reset ambiguous.
        The timings are recorded beside the frozen send.
        """
        ...

    async def settle_send(
        self,
        lease: IntentLease,
        card: FrozenCard,
        outcome: SendOutcome,
        *,
        settled_at_ms: int,
    ) -> str:
        """Fenced actual receipt + queue outcome, atomically.

        Sent retains the exact body/hash, target, provider message ID and time.
        A retryable not-sent retries this SAME identity/payload under the intent's
        attempt bound and removes the `sending` row that never reached a reader;
        the last one ends the unsent intent and fails the work. A refused not-sent
        is terminal. Ambiguous is held, never retried, and its claims count as
        possibly sent.
        """
        ...

    async def record_unsent_failure(
        self,
        lease: IntentLease,
        *,
        error_code: str,
        retryable: bool,
        retry_after_ms: int | None = None,
    ) -> None:
        """An owned intent failed before any `sending` row: its card, or a proven unsent preflight.

        A retryable failure spends one intent attempt and backs off; the last one, or a refused failure,
        ends the unsent intent and fails the work with this error code. A lost lease is a no-op.
        """
        ...

    async def defer_notification(
        self,
        event_id: str,
        channel: str,
        expected_content_revision: str | None,
        expected_work_updated_at_ms: int | None = None,
        *,
        error_code: str,
    ) -> None:
        """A planning turn failed before a plan was recorded: spend one bounded attempt and back off.

        The last attempt fails the work with this error code. Semantics, the public outbox and any
        reserved intent are untouched. With no expected revision (the snapshot itself failed) the
        current pending work of the Event is the one charged.
        """
        ...

    async def postpone_notification(self, event_id: str, channel: str, expected_content_revision: str | None) -> None:
        """The database could not answer a planning turn: put the pending work off once, spending nothing."""
        ...

    async def reserve_extra_read(self, lineage_id: str, target_ref: str) -> bool:
        """Atomic one-read budget for the entire lineage, durable across retries."""
        ...

    async def attach_extra_evidence(
        self,
        source: FrozenInput,
        target: ReadTarget,
        evidence: tuple[Evidence, ...],
        affected_claim_refs: tuple[str, ...],
    ) -> None:
        """Append a new evidence revision and enqueue only the affected semantic work.

        Preserve lineage and source first-known clocks. The next frozen input has
        focus_claim_refs and only the changed/affected source material; unaffected
        head claims are carried by assembly rather than re-extracted.
        """
        ...

    async def record_read_outcome(self, lineage_id: str, *, outcome: str) -> None: ...

    async def pending_semantic_events(self, limit: int) -> tuple[str, ...]: ...

    async def pending_notification_events(self, channel: str, limit: int) -> tuple[str, ...]: ...
