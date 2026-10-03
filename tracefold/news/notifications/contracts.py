"""Persistent notification contracts: reader receipts, per-claim decisions, plans and frozen copy."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, model_validator

from ..updates.contracts import Exact
from ..updates.identity import digest, identity
from .novelty import ClaimLink, LinkedReceipt, Novelty, Render
from .reader import ReaderJudgment

NEWS_CHANNEL: Final = "news"
NOTIFICATION_ATTEMPTS_MAX: Final = 3

PlanAction = Literal["notify", "no_notification", "unresolved"]
PlanReason = Literal["uncovered_claims", "awaiting", "no_uncovered_actionable_claims"]
ClaimDecisionValue = Literal["notify", "not_notified", "deferred"]
ClaimReason = Literal[
    "retired",
    # deferred: a send of this Event is still in flight and must settle first
    "send_outcome_unresolved",
    # not notified: an earlier send of this claim has no provable outcome, so it may already be read
    "send_outcome_ambiguous",
    "stale_source",
    # not notified: the day the claim reports is more than a week before it first became visible
    "stale_occurrence",
    # novelty: the reader already holds this claim, or a linked claim's send is still in flight
    "known_to_reader",
    "linked_send_in_flight",
    "correction_of_sent",
    "protected_listing",
    "large_daily_move",
    # calibrated probabilities over independent report kind, materiality and interrupt evidence
    "reader_key",
    "reader_push",
    "reader_feed",
    "reader_ineligible",
    # deferred while the reader judgment cannot be had; recorded unassessed after READER_WAIT_MAX_MS
    "reader_unavailable",
    "reader_unassessed",
]
REASON_DECISIONS: Final[dict[ClaimReason, ClaimDecisionValue]] = {
    "retired": "not_notified",
    "send_outcome_unresolved": "deferred",
    "send_outcome_ambiguous": "not_notified",
    "stale_source": "not_notified",
    "stale_occurrence": "not_notified",
    "known_to_reader": "not_notified",
    "linked_send_in_flight": "deferred",
    "correction_of_sent": "notify",
    "protected_listing": "notify",
    "large_daily_move": "notify",
    "reader_key": "notify",
    "reader_push": "notify",
    "reader_feed": "not_notified",
    "reader_ineligible": "not_notified",
    "reader_unavailable": "deferred",
    "reader_unassessed": "not_notified",
}


class DeliveredText(Exact):
    intent_id: str
    channel: str
    state: Literal["sent", "not_sent", "ambiguous"]
    body: str
    payload_sha256: str
    received_at_ms: int | None = Field(default=None, ge=0)
    provider_message_id: str | None = None

    @model_validator(mode="after")
    def actual_receipt(self) -> DeliveredText:
        if self.payload_sha256 != digest(self.body):
            raise ValueError("news_receipt_payload_mismatch")
        if self.state == "sent" and self.received_at_ms is None:
            raise ValueError("news_receipt_sent_time_missing")
        return self


class ReaderSnapshot(Exact):
    channel: str
    revision: str
    # One shared pool of recalled/linked frozen bodies. Linked ambiguous receipts
    # may retain exact copy; ordered per-claim IDs select only sent judgment context.
    # Never observed heads or unsent drafts.
    receipts: tuple[DeliveredText, ...]
    # Ordered intent IDs selected independently for each active claim; receipts are shared body storage.
    receipt_intents_by_claim: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    # Claims of this Event's sends still in flight. Never counted as received; the plan waits for them.
    blocked_claim_refs: tuple[str, ...] = ()
    # Claims of this Event's sends with no provable outcome: possibly received, so never sent again.
    ambiguous_claim_refs: tuple[str, ...] = ()
    invalidated_claim_refs: tuple[str, ...] = ()
    protected_listing_claim_refs: tuple[str, ...] = ()
    # Persisted semantic links within two hops and the independent delivery state
    # of claims they reach. Exact sent bodies share the receipts pool above.
    links: tuple[ClaimLink, ...] = ()
    link_receipts: tuple[LinkedReceipt, ...] = ()


class ReaderRepairContext(Exact):
    """The earlier message a card line adds to or corrects: the reader already has its exact text."""

    render: Literal["increment", "correction"]
    intent_id: str
    body: str


class ReaderPolicyScores(Exact):
    """Frozen policy features and probabilities, separate from the model's answer evidence."""

    e: float = Field(ge=0, le=1)
    m: float = Field(ge=0, le=1)
    i: float = Field(ge=0, le=1)
    p_push: float = Field(ge=0, le=1)
    p_key: float = Field(ge=0, le=1)
    held: bool
    certification_status: Literal["uncalibrated", "certified"]
    # Frozen at decision time. Missing on records produced before cut provenance was added.
    push_cut: float | None = Field(default=None, ge=0, le=1)
    key_cut: float | None = Field(default=None, ge=0, le=1)
    calibration_identity: str | None = None


class ReaderRecord(Exact):
    """What the reader rows decided from, for one claim: novelty, the judgment and the frozen input's shape."""

    novelty: Novelty
    link_path: tuple[ClaimLink, ...] = ()
    render: Render = "full"
    earlier: ReaderRepairContext | None = None
    # Keep the actual receipt identity also for known/full rows, which need no repair copy.
    anchor_intent_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    input_digest: str | None = None
    message_intents: tuple[str, ...] = ()
    judgment: ReaderJudgment | None = None
    scores: ReaderPolicyScores | None = None


class ClaimDecision(Exact):
    claim_ref: str
    decision: ClaimDecisionValue
    reason: ClaimReason
    reader: ReaderRecord | None = None

    @model_validator(mode="after")
    def check_reason(self) -> ClaimDecision:
        if REASON_DECISIONS[self.reason] != self.decision:
            raise ValueError("news_claim_decision_reason_mismatch")
        return self


class ComparedReceipt(Exact):
    """One actual receipt this plan compared its claims against: which intent, and exactly which body."""

    intent_id: str
    payload_sha256: str


class PlanTimings(Exact):
    """Where one planning turn spent its time. Audit only: nothing reads it back to decide anything.

    `due_at_ms` is when the work became due and `started_at_ms` when this turn took it, so the wait for a
    prepare slot is their difference; the two durations are the snapshot read and the reader judgments;
    `planned_at_ms` is when the plan was complete. The decision row's `created_at_ms` is the write, so every
    stage from adoption to the recorded decision can be read back with SQL alone.
    """

    due_at_ms: int | None = Field(default=None, ge=0)
    started_at_ms: int | None = Field(default=None, ge=0)
    snapshot_ms: int | None = Field(default=None, ge=0)
    judgment_ms: int | None = Field(default=None, ge=0)
    planned_at_ms: int | None = Field(default=None, ge=0)


class NotificationPlan(Exact):
    action: PlanAction
    reason: PlanReason
    update_ref: str
    # One decision per adopted claim, so the Console can show why each was or was not sent.
    claim_decisions: tuple[ClaimDecision, ...]
    # A louder presentation of this notification, never a gate.
    key: bool = False
    channel: str
    purpose: Literal["news_update"] = "news_update"
    reader_revision: str
    # The reader judge that answered and one digest over every claim's frozen input.
    reader_identity: str
    input_digest: str
    decision_ref: str | None = None
    # The receipts the reader judgments read, so a recorded decision says what "already sent" meant.
    compared_receipts: tuple[ComparedReceipt, ...] = ()
    timings: PlanTimings | None = None

    @property
    def selected_claim_refs(self) -> tuple[str, ...]:
        return tuple(sorted(row.claim_ref for row in self.claim_decisions if row.decision == "notify"))

    @property
    def deferred_claim_refs(self) -> tuple[str, ...]:
        return tuple(sorted(row.claim_ref for row in self.claim_decisions if row.decision == "deferred"))

    @property
    def intent_id(self) -> str:
        if self.action != "notify" or not self.selected_claim_refs:
            raise ValueError("news_non_notification_has_no_intent")
        return identity("intent", self.update_ref, sorted(self.selected_claim_refs), self.channel, self.purpose)

    @property
    def record_ref(self) -> str:
        return self.decision_ref or identity(
            "notification_decision",
            self.update_ref,
            self.channel,
            self.reader_revision,
            self.claim_decisions,
            self.input_digest,
        )

    def earlier(self, claim_ref: str) -> ReaderRepairContext | None:
        row = next((row for row in self.claim_decisions if row.claim_ref == claim_ref), None)
        return None if row is None or row.reader is None else row.reader.earlier

    @model_validator(mode="after")
    def check_action(self) -> NotificationPlan:
        refs = [row.claim_ref for row in self.claim_decisions]
        if len(refs) != len(set(refs)):
            raise ValueError("news_plan_duplicate_claim_decision")
        if self.selected_claim_refs:
            expected = ("notify", "uncovered_claims")
        elif self.deferred_claim_refs:
            expected = ("unresolved", "awaiting")
        else:
            expected = ("no_notification", "no_uncovered_actionable_claims")
        if (self.action, self.reason) != expected:
            raise ValueError("news_plan_action_mismatch")
        if self.key and self.action != "notify":
            raise ValueError("news_plan_key_without_notification")
        return self


class CardLine(Exact):
    claim_ref: str
    text_zh: str = Field(min_length=1)


class CardCopy(Exact):
    headline_zh: str = Field(min_length=1, max_length=80)
    lines: tuple[CardLine, ...] = Field(min_length=1)


class FrozenCard(Exact):
    intent_id: str
    claim_refs: tuple[str, ...]
    headline_zh: str
    body: str
    payload_sha256: str

    @model_validator(mode="after")
    def check_payload(self) -> FrozenCard:
        if self.payload_sha256 != digest(self.body):
            raise ValueError("news_card_payload_mismatch")
        return self
