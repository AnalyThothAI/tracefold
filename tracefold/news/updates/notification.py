"""One reader selection owner, stable intents and actual delivered-text coverage."""
from __future__ import annotations

from typing import Literal, Protocol
from pydantic import Field, model_validator

from .contracts import Claim, EventUpdate, Exact
from .identity import digest, identity, canonical_json
from .judgment import Budget, NewsJudgments, Question


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
    blocked_claim_refs: tuple[str, ...] = ()


class NotificationPlan(Exact):
    action: Literal["notify", "no_notification", "unresolved"]
    reason: str
    update_ref: str
    selected_claim_refs: tuple[str, ...]
    deferred_claim_refs: tuple[str, ...] = ()
    channel: str
    purpose: Literal["news_update"] = "news_update"
    reader_revision: str

    @property
    def intent_id(self) -> str:
        if self.action != "notify" or not self.selected_claim_refs:
            raise ValueError("news_non_notification_has_no_intent")
        return identity("intent", self.update_ref, sorted(self.selected_claim_refs), self.channel, self.purpose)


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
    async def compose(self, claims: tuple[Claim, ...], *, timeout: float) -> CardCopy: ...


class NotificationPlanner:
    def __init__(self, judgments: NewsJudgments, *, source_max_age_ms: int = 12 * 60 * 60_000) -> None:
        self.judgments, self.source_max_age_ms = judgments, source_max_age_ms

    async def plan(self, update: EventUpdate, reader: ReaderSnapshot, budget: Budget,
                   *, now_ms: int) -> NotificationPlan:
        retired = set(update.retired_claim_refs)
        candidates = [c for c in update.claims if c.ref not in retired and c.fields.mode not in {"commentary", "promotion"}]
        # Explicit corrections must not inherit a TTL refreshed by a model run.
        # They can nevertheless inform readers about an old report: not an entry signal.
        corrections = {change.current_ref for change in update.changes if change.kind in {"correction", "conflict"}}
        if self.source_max_age_ms > 0:
            candidates = [c for c in candidates if c.ref in corrections or now_ms - c.first_available_at_ms <= self.source_max_age_ms]
        selected: list[str] = []
        deferred: list[str] = []
        sent = tuple(r for r in reader.receipts if r.state == "sent" and r.channel == reader.channel)
        for claim in candidates:
            if claim.ref in reader.blocked_claim_refs:
                deferred.append(claim.ref)
                continue
            if claim.fields.mode == "unknown":
                deferred.append(claim.ref)
                continue
            questions = tuple(Question(item_id=identity("coverage", claim.ref, receipt.intent_id, receipt.payload_sha256),
                payload_json=canonical_json({"claim": claim, "actual_delivered_text": receipt.body,
                    "receipt_id": receipt.intent_id, "payload_sha256": receipt.payload_sha256})) for receipt in sent)
            answers = await self.judgments.judge("coverage", questions, budget)
            # Partial/unknown/provider failure is not full coverage. No additional
            # similarity veto, story count, ticker requirement or importance score.
            if not any(answer.status == "available" and answer.value == "full" for answer in answers):
                selected.append(claim.ref)
        action = "notify" if selected else "unresolved" if deferred else "no_notification"
        reason = "uncovered_claims" if selected else "claim_mode_or_send_outcome_unresolved" if deferred else "no_uncovered_actionable_claims"
        return NotificationPlan.model_validate({"action": action, "reason": reason, "update_ref": update.ref,
            "selected_claim_refs": tuple(sorted(selected)), "deferred_claim_refs": tuple(sorted(deferred)), "channel": reader.channel, "reader_revision": reader.revision})


def freeze_card(plan: NotificationPlan, update: EventUpdate, copy: CardCopy) -> FrozenCard:
    """Freeze actual reader copy; selected IDs are not proof of full coverage.

    Future coverage decisions compare this exact delivered body, not the original
    article, the selected-ID set, or an unsent draft. Adapters may reject oversized
    copy, but may not silently truncate the frozen body.
    """
    refs = plan.selected_claim_refs
    if plan.update_ref != update.ref or not set(refs) <= {c.ref for c in update.claims}:
        raise ValueError("news_card_update_selection_mismatch")
    if set(line.claim_ref for line in copy.lines) != set(refs) or len(copy.lines) != len(refs):
        raise ValueError("news_card_selected_claims_mismatch")
    lines = {line.claim_ref: line.text_zh for line in copy.lines}
    def has_han(text: str) -> bool:
        return any("\u3400" <= char <= "\u9fff" for char in text)
    if not has_han(copy.headline_zh) or any(not has_han(text) for text in lines.values()):
        raise ValueError("news_card_chinese_copy_required")
    body = "\n\n".join([copy.headline_zh, *(lines[ref] for ref in refs)])
    return FrozenCard(intent_id=plan.intent_id, claim_refs=refs, headline_zh=copy.headline_zh,
        body=body, payload_sha256=digest(body))
