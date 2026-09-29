"""One reader selection owner: reader novelty, one two-question reader judgment per claim, a pure decision table.

The planner reads adopted EventUpdate content, the persisted semantic links between claims and the reader's
actual receipts. Every claim gets one named decision from `decide()`: retired, sending, ambiguous and stale
claims first; then what the reader already holds (`reader_novelty`, code over links); then corrections of
delivered claims, listing protection and whole-market daily moves; then the incremental importance the reader
judgment gave what the claim adds (`reader_judgments`). Cuts are code, per answering backend. A plan stays
pending only for a send still in flight or a reader judgment that cannot be had yet, and the latter only
for a bounded time.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, Protocol

from pydantic import Field, model_validator

from .contracts import Claim, ContentKind, EventUpdate, Exact, Source
from .identity import digest, identity
from .judgment import Budget, JudgmentCache
from .reader_judgments import (
    ClaimLink,
    LinkedReceipt,
    Novelty,
    ReaderDecision,
    ReaderInput,
    ReaderJudge,
    ReaderJudgment,
    ReaderNovelty,
    Render,
    cached_judgments,
    novelty_outcome,
    reader_decision,
    reader_novelty,
)

# An ordinary push reaches the reader within three hours of the claim first being visible; a correction of
# something the reader was told is still worth it for twelve.
SOURCE_MAX_AGE_MS: Final = 3 * 60 * 60_000
CORRECTION_MAX_AGE_MS: Final = 12 * 60 * 60_000
# A roundup or a background paragraph can arrive fresh and still report what happened long ago: such a claim
# is not pushed once the day it names is more than a week before it first became visible (#742 PR-4).
OCCURRENCE_MAX_AGE_DAYS: Final = 7
# How long an adopted update waits for a reader judgment nobody can give before its claims are recorded as
# unassessed. They are never pushed on a guess.
READER_WAIT_MAX_MS: Final = 10 * 60_000
# The one logical reader channel News notifies (#706). The configured provider is how it is reached.
NEWS_CHANNEL: Final = "news"

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
    # the incremental importance of what the claim adds, against the answering backend's cuts
    "reader_key",
    "reader_push",
    "reader_feed",
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
    "reader_unavailable": "deferred",
    "reader_unassessed": "not_notified",
}
# The owner's one exception (#675 §7), narrowed by #742: a same-day price move this large is itself the fact,
# but only where a whole market moved and only as a move, never a level, a year-on-year figure or a rate.
PRICE_MOVE_EXCEPTION_PERCENT: Final = Decimal(5)
PRICE_MOVE_EXCEPTION_MARKETS: Final[frozenset[str]] = frozenset({"commodity", "index"})
_PERCENT_UNITS: Final[frozenset[str]] = frozenset({"%", "pct", "percent"})
_MOVE_WORDS: Final = re.compile(r"change|move|rise|fall|drop|gain|loss|decline|jump|plunge|surge|涨|跌|升|降", re.I)
_SAME_DAY_WORDS: Final = re.compile(r"intraday|today|daily|session|日内|当日|今日|今天|盘中|收盘", re.I)
# A bare month dates an event; on a figure it names the statistical period, not when anything happened.
_OCCURRENCE_KINDS: Final[frozenset[ContentKind]] = frozenset({"state_change", "official_measure", "other"})
_ISO_DATE: Final = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_MONTH: Final = re.compile(r"^(?:early |mid-|late )?([a-z]+)\.?,?\s*(\d{4})?$")
_MONTHS: Final = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


NOTIFICATION_ATTEMPTS_MAX: Final = 3


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
    # Recalled sent receipts, never observed event heads or unsent drafts.
    receipts: tuple[DeliveredText, ...]
    # Ordered intent IDs selected independently for each active claim; receipts are shared body storage.
    receipt_intents_by_claim: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    # Claims of this Event's sends still in flight. Never counted as received; the plan waits for them.
    blocked_claim_refs: tuple[str, ...] = ()
    # Claims of this Event's sends with no provable outcome: possibly received, so never sent again.
    ambiguous_claim_refs: tuple[str, ...] = ()
    invalidated_claim_refs: tuple[str, ...] = ()
    # The reader's code-owned watchlist, as canonical upper-case base symbols supplied by the store.
    watch_symbols: tuple[str, ...] = ()
    protected_listing_claim_refs: tuple[str, ...] = ()
    # Persisted semantic links within two hops of this update's claims, the receipts that carry any claim
    # they reach (sent, ambiguous or still sending), and the delivered text of those receipts.
    links: tuple[ClaimLink, ...] = ()
    link_receipts: tuple[LinkedReceipt, ...] = ()
    linked: tuple[DeliveredText, ...] = ()


class ReaderRepairContext(Exact):
    """The earlier message a card line adds to or corrects: the reader already has its exact text."""

    render: Literal["increment", "correction"]
    intent_id: str
    body: str


class ReaderRecord(Exact):
    """What the reader rows decided from, for one claim: novelty, the judgment and the frozen input's shape."""

    novelty: Novelty
    link_path: tuple[ClaimLink, ...] = ()
    render: Render = "full"
    earlier: ReaderRepairContext | None = None
    input_digest: str | None = None
    message_intents: tuple[str, ...] = ()
    judgment: ReaderJudgment | None = None


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


class CardComposer(Protocol):
    identity: str

    async def compose(
        self,
        claims: tuple[Claim, ...],
        *,
        sources: Mapping[str, Source],
        earlier: Mapping[str, ReaderRepairContext] | None = None,
    ) -> CardCopy:
        """Chinese copy for exactly the selected claims. The caller bounds the call with asyncio.timeout."""
        ...


def card_copy_material(
    claims: tuple[Claim, ...],
    sources: Mapping[str, Source],
    earlier: Mapping[str, ReaderRepairContext] | None = None,
) -> list[dict[str, object]]:
    """Exactly the claim and provenance fields the Chinese composer receives.

    A claim rendered as an increment or a correction carries the earlier message the reader already has.
    """

    return [
        {
            "claim_ref": claim.ref,
            "statement": claim.statement,
            "fields": claim.fields.model_dump(mode="json"),
            "citations": [
                {
                    "evidence_ref": citation.evidence_ref,
                    "quote": citation.quote,
                    "source": None
                    if (source := sources.get(citation.evidence_ref)) is None
                    else {
                        "publisher_id": source.publisher_id,
                        "attribution": source.attribution,
                        "origin_id": source.origin_id,
                    },
                }
                for citation in claim.citations
            ],
            **(
                {}
                if (context := (earlier or {}).get(claim.ref)) is None
                else {"earlier": {"render": context.render, "delivered_text": context.body}}
            ),
        }
        for claim in claims
    ]


def large_daily_move(claim: Claim) -> bool:
    """A same-day percentage move of at least the exception size on a commodity or index primary.

    Only a moved level (`level_crossed`) counts, only a quantity named as a change, and only a same-day or
    unstated period: a yield level, a year-on-year figure or a crop-condition rate is not a daily move.
    """

    if claim.fields.content_kind != "level_crossed":
        return False
    markets = {asset.market_type for asset in claim.fields.assets if asset.role == "primary"}
    if not markets & PRICE_MOVE_EXCEPTION_MARKETS:
        return False
    for quantity in claim.fields.quantities:
        if quantity.unit.strip().casefold() not in _PERCENT_UNITS or not _MOVE_WORDS.search(quantity.name):
            continue
        period = quantity.period or claim.fields.statistical_period
        if period is not None and not _SAME_DAY_WORDS.search(period):
            continue
        try:
            if abs(Decimal(quantity.value)) >= PRICE_MOVE_EXCEPTION_PERCENT:
                return True
        except InvalidOperation:
            continue
    return False


def _occurred_on(claim: Claim, seen: date) -> date | None:
    """The latest day the claim's `occurred_at` can name, read conservatively; None when it names none.

    The extractor invents years, so an ISO date keeps its year only when a cited quote states it and otherwise
    takes the year that puts it nearest `seen`, the day the claim first became visible. A bare month, with an
    optional early / mid- / late, names its last day, within the past year unless a quote states its year; it
    counts only for an event (`_OCCURRENCE_KINDS`). Anything else, "Sept. 10" included, names no day.
    """

    text = (claim.fields.occurred_at or "").strip().lower()
    quotes = " ".join(citation.quote for citation in claim.citations)
    if iso := _ISO_DATE.match(text):
        days = []
        for year in (int(iso[1]),) if iso[1] in quotes else (seen.year - 1, seen.year, seen.year + 1):
            try:
                days.append(date(year, int(iso[2]), int(iso[3])))
            except ValueError:
                continue
        return min(days, key=lambda day: abs((day - seen).days), default=None)
    named = _MONTH.match(text)
    if named is None or len(named[1]) < 3 or claim.fields.content_kind not in _OCCURRENCE_KINDS:
        return None
    month = next((index for index, name in enumerate(_MONTHS, 1) if name.startswith(named[1])), None)
    if month is None:
        return None
    year = seen.year - 1 if month > seen.month else seen.year
    if named[2] and named[2] in quotes:
        year = int(named[2])
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return date.fromordinal(following.toordinal() - 1)


def stale_occurrence(claim: Claim) -> bool:
    """Whether the day the claim reports is more than `OCCURRENCE_MAX_AGE_DAYS` before it first became visible.

    A claim with a speaker is the statement itself, a new act whatever it recounts, so it is never stale here.
    """

    if claim.fields.speaker:
        return False
    seen = datetime.fromtimestamp(claim.first_available_at_ms / 1000, UTC).date()
    day = _occurred_on(claim, seen)
    return day is not None and (seen - day).days > OCCURRENCE_MAX_AGE_DAYS


READER_REASONS: Final[dict[str, ClaimReason]] = {
    "known": "known_to_reader",
    "in_flight": "linked_send_in_flight",
    "correction": "correction_of_sent",
    "key": "reader_key",
    "push": "reader_push",
    "feed": "reader_feed",
}


def decide(
    claim: Claim,
    update: EventUpdate,
    reader: ReaderSnapshot,
    *,
    now_ms: int,
    novelty: ReaderNovelty,
    judgment: ReaderJudgment | None,
    message_intents: Sequence[str] = (),
) -> tuple[ClaimReason, ReaderDecision | None]:
    """The decision table for one claim, in order. Pure: every input is already read.

    `judgment` is None when the claim was not asked; it is asked only when no earlier row decides it.
    """

    age = now_ms - claim.first_available_at_ms
    corrects = novelty.novelty == "development" and any(link.relation == "corrects" for link in novelty.path)
    corrective = corrects or any(
        change.current_ref == claim.ref and change.kind in {"correction", "conflict"} for change in update.changes
    )
    if claim.ref in set(update.retired_claim_refs) | set(update.superseded_claim_refs) | set(
        reader.invalidated_claim_refs
    ):
        return "retired", None
    if claim.ref in reader.blocked_claim_refs:
        return "send_outcome_unresolved", None
    if claim.ref in reader.ambiguous_claim_refs:
        return "send_outcome_ambiguous", None
    if age > (CORRECTION_MAX_AGE_MS if corrective else SOURCE_MAX_AGE_MS):
        return "stale_source", None
    if not corrective and stale_occurrence(claim):
        return "stale_occurrence", None
    early = novelty_outcome(novelty, first_available_at_ms=claim.first_available_at_ms)
    if early is not None:
        return READER_REASONS[early.outcome], early
    if claim.ref in reader.protected_listing_claim_refs:
        return "protected_listing", None
    if claim.fields.mode == "observation" and large_daily_move(claim):
        return "large_daily_move", None
    if judgment is None or judgment.status != "available":
        waited = now_ms - update.adopted_at_ms > READER_WAIT_MAX_MS
        return ("reader_unassessed" if waited else "reader_unavailable"), None
    judged = reader_decision(
        novelty, judgment, first_available_at_ms=claim.first_available_at_ms, message_intents=message_intents
    )
    return READER_REASONS[judged.outcome], judged


def reader_messages(claim_ref: str, reader: ReaderSnapshot) -> tuple[DeliveredText, ...]:
    """Resolve a claim's frozen selection to exact sent bodies, preserving the selection order."""

    by_id = {row.intent_id: row for row in reader.receipts}
    return tuple(
        by_id[intent]
        for intent in reader.receipt_intents_by_claim.get(claim_ref, ())
        if intent in by_id and by_id[intent].state == "sent" and by_id[intent].channel == reader.channel
    )


class NotificationPlanner:
    def __init__(self, judge: ReaderJudge, cache: JudgmentCache) -> None:
        self.judge = judge
        self.cache = cache

    async def plan(
        self, update: EventUpdate, reader: ReaderSnapshot, budget: Budget, *, now_ms: int
    ) -> NotificationPlan:
        """Novelty for every claim, one reader judgment for each claim no earlier row decides, then `decide()`.

        Judgments are reused per frozen input from the judgment cache and asked concurrently; an unavailable
        one is never stored, so the next turn asks again.
        """

        novelty = {claim.ref: reader_novelty(claim.ref, reader.links, reader.link_receipts) for claim in update.claims}
        pending = [
            claim
            for claim in update.claims
            if decide(claim, update, reader, now_ms=now_ms, novelty=novelty[claim.ref], judgment=None)[0]
            in {"reader_unavailable", "reader_unassessed"}
        ]
        messages = {claim.ref: reader_messages(claim.ref, reader) for claim in pending}
        inputs = {
            claim.ref: ReaderInput.of(claim, update, [row.body for row in messages[claim.ref]]) for claim in pending
        }
        started = time.monotonic()
        judgments = await cached_judgments(self.judge, self.cache, inputs, budget)
        judgment_ms = max(0, int((time.monotonic() - started) * 1000))
        linked = {row.intent_id: row for row in (*reader.linked, *reader.receipts)}
        rows = []
        for claim in update.claims:
            intents = tuple(row.intent_id for row in messages.get(claim.ref, ()))
            reason, decided = decide(
                claim,
                update,
                reader,
                now_ms=now_ms,
                novelty=novelty[claim.ref],
                judgment=judgments.get(claim.ref),
                message_intents=intents,
            )
            record = None
            if decided is not None or claim.ref in judgments:
                earlier = None
                anchor = None if decided is None else decided.anchor_intent_id
                if decided is not None and decided.render != "full" and anchor in linked:
                    earlier = ReaderRepairContext(render=decided.render, intent_id=anchor, body=linked[anchor].body)
                record = ReaderRecord(
                    novelty=novelty[claim.ref].novelty,
                    link_path=novelty[claim.ref].path,
                    render="full" if earlier is None else earlier.render,
                    earlier=earlier,
                    input_digest=None if claim.ref not in inputs else inputs[claim.ref].digest,
                    message_intents=intents,
                    judgment=judgments.get(claim.ref),
                )
            rows.append(
                ClaimDecision(claim_ref=claim.ref, decision=REASON_DECISIONS[reason], reason=reason, reader=record)
            )
        if any(row.decision == "notify" for row in rows):
            action: PlanAction = "notify"
            plan_reason: PlanReason = "uncovered_claims"
        elif any(row.decision == "deferred" for row in rows):
            action, plan_reason = "unresolved", "awaiting"
        else:
            action, plan_reason = "no_notification", "no_uncovered_actionable_claims"
        compared = {row.intent_id: row for group in messages.values() for row in group}
        return NotificationPlan(
            action=action,
            reason=plan_reason,
            update_ref=update.ref,
            claim_decisions=tuple(rows),
            key=any(row.reason == "reader_key" for row in rows),
            channel=reader.channel,
            reader_revision=reader.revision,
            reader_identity=self.judge.identity,
            input_digest=digest(
                {"judge": self.judge.identity, "inputs": sorted((ref, row.digest) for ref, row in inputs.items())}
            ),
            compared_receipts=tuple(
                ComparedReceipt(intent_id=row.intent_id, payload_sha256=row.payload_sha256)
                for row in sorted(compared.values(), key=lambda row: row.intent_id)
            ),
            timings=PlanTimings(judgment_ms=judgment_ms),
        )


def _has_han(text: str) -> bool:
    return any("㐀" <= char <= "鿿" for char in text)


# Reader copy is plain text that every channel shows exactly as frozen. A link or a control character
# in model copy is not something a channel may strip afterwards, so such copy is refused, not cleaned.
_COPY_LINK_RE: Final = re.compile(r"https?://|www\.", re.IGNORECASE)
_COPY_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _unsafe_copy(text: str) -> bool:
    return bool(_COPY_LINK_RE.search(text) or _COPY_CONTROL_RE.search(text))


def freeze_card(plan: NotificationPlan, update: EventUpdate, copy: CardCopy) -> FrozenCard:
    """Freeze actual reader copy; selected IDs are not proof of what the reader was told.

    Later reader judgments compare this exact delivered body, not the original
    article, the selected-ID set, or an unsent draft. Adapters may reject oversized
    copy, but may not silently truncate the frozen body.
    """
    refs = plan.selected_claim_refs
    if plan.update_ref != update.ref or not set(refs) <= {claim.ref for claim in update.claims}:
        raise ValueError("news_card_update_selection_mismatch")
    if {line.claim_ref for line in copy.lines} != set(refs) or len(copy.lines) != len(refs):
        raise ValueError("news_card_selected_claims_mismatch")
    lines = {line.claim_ref: line.text_zh for line in copy.lines}
    if not _has_han(copy.headline_zh) or any(not _has_han(text) for text in lines.values()):
        raise ValueError("news_card_chinese_copy_required")
    if "\n" in copy.headline_zh or any(_unsafe_copy(text) for text in (copy.headline_zh, *lines.values())):
        raise ValueError("news_card_copy_unsafe")
    body = "\n\n".join([copy.headline_zh, *(lines[ref] for ref in refs)])
    return FrozenCard(
        intent_id=plan.intent_id,
        claim_refs=refs,
        headline_zh=copy.headline_zh,
        body=body,
        payload_sha256=digest(body),
    )
