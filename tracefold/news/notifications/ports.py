"""Persistence and sender contracts consumed by the notification workflow.

Every atomic_* method is one short database transaction; provider/model I/O is
bounded by the caller outside the transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, Protocol

from pydantic import Field

from ..updates.contracts import EventUpdate, Exact
from .contracts import CardCopy, FrozenCard, NotificationPlan, ReaderSnapshot


class NotificationSnapshot(Exact):
    update: EventUpdate
    reader: ReaderSnapshot
    work_updated_at_ms: int | None = None
    # When the work became due: the start of the wait a planning turn's timings are measured from.
    work_due_at_ms: int | None = None
    recall_diagnostics: dict[str, dict[str, Any]] = Field(default_factory=dict)


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


class NotificationStore(Protocol):
    async def notification_snapshot(self, event_id: str, channel: str) -> NotificationSnapshot | None:
        """Consistent adopted head and actual-reader snapshot, not observed history.

        blocked_claim_refs comes from this Event's sends still in flight and
        ambiguous_claim_refs from its sends with no provable outcome. Neither is
        counted as received; the first makes the plan wait, the second is never
        sent again. The reader revision is the persisted sent-set and link-graph
        generation. CAS checks that generation without repeating candidate selection.
        """
        ...

    async def atomic_record_plan(
        self, plan: NotificationPlan, *, recall_diagnostics: Mapping[str, Mapping[str, Any]] | None = None
    ) -> PlanCommit:
        """Persist or reuse an immutable decision, check head/reader versions, reserve an intent.

        The decision is written before the reader check, so a plan that loses the
        race is on record; its reader judgments are already in the judgment cache for
        the next turn to reuse. An identical plan is the same decision row. Every
        claim_decisions row is persisted with its reason, novelty and judgment, so
        the Console can show why a claim was or was not notified. A
        no_notification plan completes the matching pending marker. An unresolved
        plan waits without spending an attempt for this Event's send, a linked
        send, or a temporarily unavailable reader judgment. The reader wait is
        bounded by the policy's ten-minute age limit. A notify result gets one
        stable intent/queue row
        bound to this decision; deferred claims keep the marker waiting even if
        other selected claims were reserved. A selection whose intent already
        ended is handled, not re-sent. A concurrent active lease or version race
        returns a recorded result without a lease.
        """
        ...

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

    async def pending_notification_events(self, channel: str, limit: int) -> tuple[str, ...]: ...
