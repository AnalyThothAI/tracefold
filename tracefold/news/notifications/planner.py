"""Select claims from one consistent reader snapshot and save the evidence used for each decision."""

from __future__ import annotations

import time

from ..updates.contracts import EventUpdate
from ..updates.identity import digest
from ..updates.judgment import Budget, JudgmentCache
from .contracts import (
    REASON_DECISIONS,
    ClaimDecision,
    ComparedReceipt,
    DeliveredText,
    NotificationPlan,
    PlanAction,
    PlanReason,
    PlanTimings,
    ReaderRecord,
    ReaderRepairContext,
    ReaderSnapshot,
)
from .novelty import reader_novelty
from .policy import NOTIFICATION_POLICY_IDENTITY, decide
from .reader import ReaderInput, ReaderJudge, cached_judgments


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
        linked = {row.intent_id: row for row in reader.receipts}
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
                    anchor_intent_id=anchor,
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
                {
                    "judge": self.judge.identity,
                    "policy": NOTIFICATION_POLICY_IDENTITY,
                    "inputs": sorted((ref, row.digest) for ref, row in inputs.items()),
                }
            ),
            compared_receipts=tuple(
                ComparedReceipt(intent_id=row.intent_id, payload_sha256=row.payload_sha256)
                for row in sorted(compared.values(), key=lambda row: row.intent_id)
            ),
            timings=PlanTimings(judgment_ms=judgment_ms),
        )
