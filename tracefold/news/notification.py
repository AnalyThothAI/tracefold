"""Select concrete claims against actual delivered content, never generated titles.

Semantic history describes observations. Receipt history describes what a reader
received. The two are deliberately different inputs. Similarity may retrieve a
receipt but cannot answer whether it covered a proposition.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from .artifact_identity import canonical_sha
from .event_update import Claim, Digest, EventUpdate, Exact, Ref, notification_intent_id
from .judgment import (
    TASK_VERSIONS,
    JudgmentBatchResult,
    JudgmentCall,
    JudgmentDeadline,
    JudgmentItem,
    JudgmentItemResult,
    NewsJudgmentBackend,
)


class DeliveredContent(Exact):
    """Only storage's actual sent receipt projection may instantiate this input."""

    intent_id: Digest
    event_id: Ref
    channel: str = Field(min_length=1)
    sent_at_ms: int = Field(ge=0)
    content_sha256: Digest
    text: str = Field(min_length=1)
    selected_claim_ids: tuple[Digest, ...] = ()
    # Selected IDs do not by themselves prove the generated prose conveyed them.
    # Coverage is about this frozen text, including legacy cards with no IDs.


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    stale_source_max_age_s: int = 12 * 60 * 60

    def __post_init__(self) -> None:
        if self.stale_source_max_age_s < 0:
            raise ValueError("news_notification_age_invalid")

    def as_dict(self) -> dict[str, Any]:
        return {"stale_source_max_age_s": self.stale_source_max_age_s}


class NotificationSelection(Exact):
    content_id: Digest
    selected_claim_ids: tuple[Digest, ...]
    decision: Literal["notify", "no_notification"]
    reason: str
    intent_id: Digest | None
    coverage: tuple[JudgmentItemResult, ...] = ()
    calls: tuple[JudgmentCall, ...] = ()
    receipt_revision: Digest


def receipt_revision(receipts: Sequence[DeliveredContent]) -> str:
    return canonical_sha(sorted((row.intent_id, row.content_sha256, row.sent_at_ms) for row in receipts))


def _eligible(claim: Claim, *, policy: NotificationPolicy, now_ms: int, correction: bool) -> bool:
    if claim.mode in {"commentary", "promotion", "calendar"}:
        return False
    # A newly received correction of an old report is useful as a correction;
    # its underlying fact still cannot acquire a fresh trading TTL.
    if correction or not policy.stale_source_max_age_s:
        return True
    return now_ms - claim.first_available_at_ms <= policy.stale_source_max_age_s * 1000


def notification_candidates(update: EventUpdate, *, policy: NotificationPolicy, now_ms: int) -> tuple[Claim, ...]:
    changed_ids = {change.current_claim_id for change in update.changes}
    correction_ids = {
        change.current_claim_id
        for change in update.changes
        if change.cause == "source_correction" or {"correction", "retraction"}.intersection(change.kinds)
    }
    # An unchanged adoption still has pending notification work after a crash.
    # It is not the absence of new comparison results that proves a reader saw it.
    return tuple(
        claim
        for claim in update.claims
        if (not changed_ids or claim.claim_id in changed_ids)
        and _eligible(claim, policy=policy, now_ms=now_ms, correction=claim.claim_id in correction_ids)
    )


DEFAULT_NOTIFICATION_POLICY = NotificationPolicy()


class NotificationPlanner:
    def __init__(
        self, backend: NewsJudgmentBackend, *, policy: NotificationPolicy = DEFAULT_NOTIFICATION_POLICY
    ) -> None:
        self.backend = backend
        self.policy = policy

    async def select(
        self,
        update: EventUpdate,
        *,
        receipts: Sequence[DeliveredContent],
        channel: str,
        now_ms: int,
        deadline: JudgmentDeadline,
        cached: Mapping[str, JudgmentItemResult] | None = None,
        checkpoint: Callable[[JudgmentBatchResult], Awaitable[None]] | None = None,
    ) -> NotificationSelection:
        relevant = tuple(receipt for receipt in receipts if receipt.channel == channel)
        revision = receipt_revision(relevant)
        candidates = notification_candidates(update, policy=self.policy, now_ms=now_ms)
        pairs: list[JudgmentItem] = []
        pair_claims: dict[str, str] = {}
        for claim in candidates:
            for receipt in relevant:
                # Include actual body digest, not just an Event or receipt timestamp.
                item_id = canonical_sha((claim.claim_id, receipt.intent_id, receipt.content_sha256))
                pairs.append(
                    JudgmentItem(
                        item_id=item_id,
                        payload={
                            "claim": claim.model_dump(mode="json"),
                            "delivered_content": receipt.text,
                            "receipt_id": receipt.intent_id,
                        },
                        evidence_refs=tuple(quote.evidence_ref for quote in claim.evidence_quotes),
                    )
                )
                pair_claims[item_id] = claim.claim_id
        result = await self.backend.judge_batch(
            task_kind="coverage",
            task_version=TASK_VERSIONS["coverage"],
            frozen_evidence={evidence.evidence_ref: evidence.model_dump(mode="json") for evidence in update.evidence},
            ordered_items=pairs,
            deadline=deadline,
            cached=cached,
            checkpoint=checkpoint,
        )
        # Partial/unresolved/unavailable never become proof of full coverage.
        covered = {
            pair_claims[item.item_id]
            for item in result.results
            if item.status == "resolved" and item.value is not None and item.value.get("coverage") == "full"
        }
        selected = tuple(sorted(claim.claim_id for claim in candidates if claim.claim_id not in covered))
        return NotificationSelection(
            content_id=update.content_id,
            selected_claim_ids=selected,
            decision="notify" if selected else "no_notification",
            reason="uncovered_claims"
            if selected
            else "delivered_content_covers_all"
            if candidates
            else "no_concrete_fresh_claim",
            intent_id=notification_intent_id(update=update, claim_ids=selected, channel=channel) if selected else None,
            coverage=result.results,
            calls=result.calls,
            receipt_revision=revision,
        )
