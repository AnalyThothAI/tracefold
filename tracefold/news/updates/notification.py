"""One reader selection owner: editorial choice, stable intents and actual delivered-text coverage.

The planner reads adopted EventUpdate content and the reader's actual receipts. Every claim gets one named
decision. There is no statement drop, headline-similarity veto,
same-story count, ticker requirement or importance score. The only reason a plan stays pending is an
overlapping send whose outcome is not settled.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from .attention import BRIEF_IDENTITY, AttentionAssessor, Disposition, assessment_input
from .contracts import Claim, EventUpdate, Exact, Source
from .identity import canonical_json, digest, identity
from .judgment import Budget, ContractFault, NewsJudgments, ProviderUnavailable, Question
from .judgment import error_code as bounded_error_code

SOURCE_MAX_AGE_MS: Final = 12 * 60 * 60_000

PlanAction = Literal["notify", "no_notification", "unresolved"]
PlanReason = Literal["uncovered_claims", "send_outcome_unresolved", "no_uncovered_actionable_claims"]
ClaimDecisionValue = Literal["notify", "not_notified", "deferred"]
ClaimReason = Literal[
    "editor_notify",
    "editor_key",
    "editor_feed_only",
    "attention_unavailable_default_notify",
    "protected_listing",
    "large_daily_move",
    "retired",
    "stale_source",
    "covered_by_sent_receipt",
    # deferred: an overlapping sending/ambiguous intent must settle first
    "send_outcome_unresolved",
]
REASON_DECISIONS: Final[dict[ClaimReason, ClaimDecisionValue]] = {
    "editor_notify": "notify",
    "editor_key": "notify",
    "editor_feed_only": "not_notified",
    "attention_unavailable_default_notify": "notify",
    "protected_listing": "notify",
    "large_daily_move": "notify",
    "retired": "not_notified",
    "stale_source": "not_notified",
    "covered_by_sent_receipt": "not_notified",
    "send_outcome_unresolved": "deferred",
}
EDITOR_REASONS: Final[dict[Disposition, ClaimReason]] = {
    "notify": "editor_notify",
    "key": "editor_key",
    "feed_only": "editor_feed_only",
}
# The owner's one exception (#675 §7): a same-day move this large is itself the fact, but only where a whole
# market moved. A single stock is excluded by its market, never by the size of the move.
PRICE_MOVE_EXCEPTION_PERCENT: Final = Decimal(5)
PRICE_MOVE_EXCEPTION_MARKETS: Final[frozenset[str]] = frozenset({"commodity", "index"})
_PERCENT_UNITS: Final[frozenset[str]] = frozenset({"%", "pct", "percent"})


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
    # Retrieved receipt rows, never observed event heads or unsent drafts.
    receipts: tuple[DeliveredText, ...]
    # Claims of overlapping sending/ambiguous intents. Never counted as received.
    blocked_claim_refs: tuple[str, ...] = ()
    invalidated_claim_refs: tuple[str, ...] = ()
    # The reader's code-owned watchlist, as canonical upper-case base symbols supplied by the store.
    watch_symbols: tuple[str, ...] = ()
    protected_listing_claim_refs: tuple[str, ...] = ()


class ClaimDecision(Exact):
    claim_ref: str
    decision: ClaimDecisionValue
    reason: ClaimReason
    reason_zh: str | None = None

    @model_validator(mode="after")
    def check_reason(self) -> ClaimDecision:
        if REASON_DECISIONS[self.reason] != self.decision:
            raise ValueError("news_claim_decision_reason_mismatch")
        return self


class NotificationPlan(Exact):
    action: PlanAction
    reason: PlanReason
    update_ref: str
    # One decision per adopted claim, so the Console can show why each was or was not sent.
    claim_decisions: tuple[ClaimDecision, ...]
    # Replaces the retired escalate class: a louder presentation of this notification, never a gate.
    key: bool = False
    channel: str
    purpose: Literal["news_update"] = "news_update"
    reader_revision: str
    assessment_status: Literal["available", "unavailable", "skipped"] = "skipped"
    assessment_error_code: str | None = None
    assessment_identity: str | None = None
    assessment_input_digest: str | None = None
    assessment_input: dict[str, object] | None = None
    decision_ref: str | None = None

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
            self.assessment_input_digest,
        )

    @model_validator(mode="after")
    def check_action(self) -> NotificationPlan:
        refs = [row.claim_ref for row in self.claim_decisions]
        if len(refs) != len(set(refs)):
            raise ValueError("news_plan_duplicate_claim_decision")
        if self.selected_claim_refs:
            expected = ("notify", "uncovered_claims")
        elif self.deferred_claim_refs:
            expected = ("unresolved", "send_outcome_unresolved")
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

    async def compose(self, claims: tuple[Claim, ...], *, sources: Mapping[str, Source]) -> CardCopy:
        """Chinese copy for exactly the selected claims. The caller bounds the call with asyncio.timeout."""
        ...


def card_copy_material(claims: tuple[Claim, ...], sources: Mapping[str, Source]) -> list[dict[str, object]]:
    """Exactly the claim and provenance fields the Chinese composer receives."""

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
        }
        for claim in claims
    ]


def large_daily_move(claim: Claim) -> bool:
    """A structured percentage move of at least the exception size on a commodity or index primary."""

    markets = {asset.market_type for asset in claim.fields.assets if asset.role == "primary"}
    if not markets & PRICE_MOVE_EXCEPTION_MARKETS:
        return False
    for quantity in claim.fields.quantities:
        if quantity.unit.strip().casefold() not in _PERCENT_UNITS:
            continue
        try:
            if abs(Decimal(quantity.value)) >= PRICE_MOVE_EXCEPTION_PERCENT:
                return True
        except InvalidOperation:
            continue
    return False


class NotificationPlanner:
    def __init__(
        self, judgments: NewsJudgments, assessor: AttentionAssessor, *, source_max_age_ms: int = SOURCE_MAX_AGE_MS
    ) -> None:
        self.judgments = judgments
        self.assessor = assessor
        self.source_max_age_ms = source_max_age_ms

    async def plan(
        self,
        update: EventUpdate,
        reader: ReaderSnapshot,
        budget: Budget,
        *,
        now_ms: int,
        reuse: Callable[[str], Awaitable[NotificationPlan | None]] | None = None,
    ) -> NotificationPlan:
        """Filter fact/delivery constraints, then ask one editor about remaining ordinary claims."""

        reasons: dict[str, ClaimReason] = {}
        explanations: dict[str, str | None] = {}
        retired = (
            set(update.retired_claim_refs) | set(update.superseded_claim_refs) | set(reader.invalidated_claim_refs)
        )
        corrections = {change.current_ref for change in update.changes if change.kind in {"correction", "conflict"}}
        candidates: list[Claim] = []
        protected: set[str] = set(reader.protected_listing_claim_refs)
        for claim in update.claims:
            if claim.ref in retired:
                reasons[claim.ref] = "retired"
            elif (
                self.source_max_age_ms > 0
                and now_ms - claim.first_available_at_ms > self.source_max_age_ms
                and claim.ref not in corrections
            ):
                reasons[claim.ref] = "stale_source"
            elif claim.ref in reader.blocked_claim_refs:
                reasons[claim.ref] = "send_outcome_unresolved"
            else:
                candidates.append(claim)
        covered = await self._fully_covered(tuple(candidates), reader, budget)
        ordinary: list[Claim] = []
        for claim in candidates:
            if claim.ref in covered:
                reasons[claim.ref] = "covered_by_sent_receipt"
            elif claim.ref in protected:
                reasons[claim.ref] = "protected_listing"
            elif claim.fields.mode == "observation" and large_daily_move(claim):
                reasons[claim.ref] = "large_daily_move"
            else:
                ordinary.append(claim)

        evidence = {item.ref: item.source for item in update.evidence}
        material = json.loads(
            canonical_json(
                {
                    "candidate": assessment_input(
                        tuple(ordinary), sources=evidence, watch_symbols=reader.watch_symbols
                    ),
                    "assessor_identity": self.assessor.identity,
                }
            )
        )
        fingerprint = digest(material)
        reused = None
        if reuse is not None:
            previous = await reuse(fingerprint)
            if (
                previous is not None
                and previous.assessment_status == "available"
                and previous.assessment_identity == self.assessor.identity
                and previous.assessment_input_digest == fingerprint
            ):
                editorial = {
                    row.claim_ref: row
                    for row in previous.claim_decisions
                    if row.reason in {"editor_notify", "editor_key", "editor_feed_only"}
                }
                if set(editorial) == {claim.ref for claim in ordinary}:
                    reused = editorial
        status: Literal["available", "unavailable", "skipped"] = "skipped"
        error_code = None
        if ordinary:
            if reused is not None:
                for claim in ordinary:
                    row = reused[claim.ref]
                    reasons[claim.ref] = row.reason
                    explanations[claim.ref] = row.reason_zh
                status = "available"
            else:
                try:
                    async with asyncio.timeout(min(budget.remaining() / 3, 20.0)):
                        assessment = await self.assessor.assess(
                            tuple(ordinary), sources=evidence, watch_symbols=reader.watch_symbols
                        )
                    selected = {row.claim_ref: row for row in assessment.decisions}
                    if set(selected) != {claim.ref for claim in ordinary}:
                        raise ProviderUnavailable("news_attention_refs_invalid")
                    for claim in ordinary:
                        assessment_row = selected[claim.ref]
                        reasons[claim.ref] = EDITOR_REASONS[assessment_row.disposition]
                        explanations[claim.ref] = assessment_row.reason_zh
                    status = "available"
                except (ProviderUnavailable, ContractFault, ValidationError, TimeoutError) as exc:
                    if budget.remaining() <= 0:
                        raise TimeoutError("news_notification_stage_expired") from exc
                    status = "unavailable"
                    error_code = bounded_error_code(exc, default="news_attention")
                    for claim in ordinary:
                        reasons[claim.ref] = "attention_unavailable_default_notify"

        rows = tuple(
            ClaimDecision(
                claim_ref=claim.ref,
                decision=REASON_DECISIONS[reasons[claim.ref]],
                reason=reasons[claim.ref],
                reason_zh=explanations.get(claim.ref),
            )
            for claim in update.claims
        )
        if any(row.decision == "notify" for row in rows):
            action: PlanAction = "notify"
            reason: PlanReason = "uncovered_claims"
        elif any(row.decision == "deferred" for row in rows):
            action, reason = "unresolved", "send_outcome_unresolved"
        else:
            action, reason = "no_notification", "no_uncovered_actionable_claims"
        return NotificationPlan(
            action=action,
            reason=reason,
            update_ref=update.ref,
            claim_decisions=rows,
            key=any(row.reason == "editor_key" for row in rows),
            channel=reader.channel,
            reader_revision=reader.revision,
            assessment_status=status,
            assessment_error_code=error_code,
            assessment_identity=self.assessor.identity if ordinary else BRIEF_IDENTITY,
            assessment_input_digest=fingerprint,
            assessment_input=material,
        )

    async def _fully_covered(
        self,
        claims: tuple[Claim, ...],
        reader: ReaderSnapshot,
        budget: Budget,
    ) -> set[str]:
        """Claims an actually sent receipt on this channel fully covers.

        Partial, unresolved and unavailable answers are not full coverage. Unsent, not-sent and ambiguous
        copy is never reader coverage.
        """

        sent = tuple(row for row in reader.receipts if row.state == "sent" and row.channel == reader.channel)
        pairs: dict[str, str] = {}
        questions = []
        for claim in claims:
            for receipt in sent:
                item_id = identity("coverage", claim.ref, receipt.intent_id, receipt.payload_sha256)
                pairs[item_id] = claim.ref
                payload = {
                    "claim": claim,
                    "actual_delivered_text": receipt.body,
                    "receipt_id": receipt.intent_id,
                    "payload_sha256": receipt.payload_sha256,
                }
                questions.append(Question(item_id=item_id, payload_json=canonical_json(payload)))
        if not questions:
            return set()
        answers = await self.judgments.judge("coverage", tuple(questions), budget)
        return {pairs[row.item_id] for row in answers if row.status == "available" and row.value == "full"}


def _has_han(text: str) -> bool:
    return any("㐀" <= char <= "鿿" for char in text)


# Reader copy is plain text that every channel shows exactly as frozen. A link or a control character
# in model copy is not something a channel may strip afterwards, so such copy is refused, not cleaned.
_COPY_LINK_RE: Final = re.compile(r"https?://|www\.", re.IGNORECASE)
_COPY_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _unsafe_copy(text: str) -> bool:
    return bool(_COPY_LINK_RE.search(text) or _COPY_CONTROL_RE.search(text))


def freeze_card(plan: NotificationPlan, update: EventUpdate, copy: CardCopy) -> FrozenCard:
    """Freeze actual reader copy; selected IDs are not proof of full coverage.

    Future coverage decisions compare this exact delivered body, not the original
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
