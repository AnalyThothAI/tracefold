"""One News Agent and independent notification/public-delivery continuations."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from .contracts import EventUpdate, Extraction, FrozenInput, PriorClaim
from .identity import canonical_json, identity
from .judgment import Budget, ContractFault, ProviderUnavailable, Question
from .notification import CardComposer, NotificationPlanner, freeze_card
from .ports import (
    ExistingSourceReader, NewsStore, SemanticObservation, Sender, SendOutcome, TradingReceiver,
)
from .public import public_updates
from .semantics import SemanticAnalyzer, assemble_update


def clock_ms() -> int:
    return time.time_ns() // 1_000_000


class NewsAgent:
    def __init__(self, store: NewsStore, analyzer: SemanticAnalyzer, *, program_identity: str,
                 source_reader: ExistingSourceReader | None = None, clock: Callable[[], int] = clock_ms) -> None:
        self.store, self.analyzer, self.program_identity = store, analyzer, program_identity
        self.source_reader, self.clock = source_reader, clock

    async def process(self, event_id: str, *, timeout: float = 20.0) -> str:
        async with asyncio.timeout(timeout):
            return await self._process(event_id, Budget.start(timeout))

    async def _process(self, event_id: str, budget: Budget) -> str:
        source = await self.store.input_for(event_id)
        work_id = identity("semantic_work", source.event_id, source.revision, source.evidence_sha,
                           self.program_identity, self.analyzer.identity)
        saved = await self.store.checkpoint(work_id)
        extracted = None if saved is None else saved.extraction
        if extracted is None:
            extracted = await self.analyzer.extract(source, budget)
            extracted = await self.store.save_extraction(work_id, extracted)
        understood = None if saved is None else saved.understanding
        if understood is None:
            understood = await self.analyzer.understand(source, extracted, budget)
            understood = await self.store.save_understanding(work_id, understood)

        completed_at_ms = self.clock()
        # A CAS collision retries only missing relationships/adoption, not extraction.
        # Persisted checkpoints/cache retain successful work if these retries are exhausted.
        for _ in range(2):
            budget.remaining()
            head = await self.store.head(event_id)
            if head is not None and head.input_revision > source.revision:
                observation = self._observation(work_id, source, understood, completed_at_ms)
                await self.store.save_observation(observation)
                await self.store.finish_semantic_work(work_id, reason="newer_head_already_adopted")
                return "newer_head"
            head_refs = set() if head is None else {c.ref for c in head.claims}
            prior_refs = {p.claim.ref for p in source.prior}
            if head is not None and head_refs - prior_refs:
                priors = {p.claim.ref: p for p in source.prior}
                priors.update({c.ref: PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=c) for c in head.claims})
                source = FrozenInput.model_validate({**source.model_dump(mode="json"), "prior": tuple(priors.values())})
                understood = await self.analyzer.understand(source, understood, budget, rebase_only=True)
            observation = self._observation(work_id, source, understood, completed_at_ms)
            observation = await self.store.save_observation(observation)
            update = assemble_update(source, understood, head, adopted_at_ms=self.clock())
            if update is None:
                await self.store.finish_semantic_work(work_id, reason="no_substantive_content_change")
                return "unchanged"
            if await self.store.atomic_adopt(expected_head_ref=None if head is None else head.ref,
                                            observation=observation, update=update, public=public_updates(update, semantic_completed_at_ms=observation.completed_at_ms)):
                await self.store.finish_semantic_work(work_id, reason="adopted")
                # The result, outbox and notification_pending are already committed.
                # This optional branch cannot retract them or reset its lineage budget.
                await self._extra_read(source, update, budget)
                return "adopted"
        await self.store.defer_semantic_work(work_id, reason="adopted_head_changed")
        return "deferred"

    def _observation(self, work_id: str, source: FrozenInput, understood: Extraction,
                     completed_at_ms: int) -> SemanticObservation:
        return SemanticObservation(result_id=identity("semantic_result", work_id, source.prior, understood),
            work_id=work_id, event_id=source.event_id, input_revision=source.revision,
            input_sha256=source.evidence_sha, program_identity=self.program_identity,
            completed_at_ms=completed_at_ms, understanding=understood)

    async def _extra_read(self, source: FrozenInput, update: EventUpdate, budget: Budget) -> None:
        if self.source_reader is None or not source.read_targets or not update.open_questions:
            return
        targets = {target.ref: target for target in source.read_targets}
        gaps = [gap for gap in update.open_questions if gap.target_ref in targets]
        if not gaps:
            return
        try:
            budget.remaining()
        except TimeoutError:
            return
        questions = tuple(Question(item_id=identity("read_candidate", gap.question, gap.target_ref),
            payload_json=canonical_json({"gap": gap, "target": targets[gap.target_ref]})) for gap in gaps)
        try:
            answers = await self.analyzer.judgments.judge("next_read", questions, budget)
            selected = next((index for index, answer in enumerate(answers) if answer.status == "available" and answer.value == "read"), None)
            if selected is None:
                return
            gap = gaps[selected]
            target = targets[gap.target_ref]
            if not await self.store.reserve_extra_read(source.lineage_id, target.ref):
                return
            remaining = budget.remaining()
            async with asyncio.timeout(remaining):
                evidence = await self.source_reader.read(target, timeout=remaining)
            if evidence:
                await self.store.attach_extra_evidence(source, target, evidence, gap.claim_refs)
                await self.store.record_read_outcome(source.lineage_id, outcome="attached")
            else:
                await self.store.record_read_outcome(source.lineage_id, outcome="no_material")
        except (ProviderUnavailable, TimeoutError):
            await self.store.record_read_outcome(source.lineage_id, outcome="unavailable_or_budget_exhausted")
        # Cancellation/configuration/programming errors remain visible. Adoption
        # remains durable even if the worker is stopped at this point.


class Notifications:
    def __init__(self, store: NewsStore, planner: NotificationPlanner, composer: CardComposer,
                 sender: Sender, *, clock: Callable[[], int] = clock_ms) -> None:
        self.store, self.planner, self.composer, self.sender, self.clock = store, planner, composer, sender, clock

    async def process(self, event_id: str, channel: str, *, timeout: float = 20.0) -> str:
        async with asyncio.timeout(timeout):
            return await self._process(event_id, channel, Budget.start(timeout))

    async def _process(self, event_id: str, channel: str, budget: Budget) -> str:
        snapshot = await self.store.notification_snapshot(event_id, channel)
        if snapshot is None:
            return "no_work"
        plan = await self.planner.plan(snapshot.update, snapshot.reader, budget, now_ms=self.clock())
        lease = await self.store.atomic_record_plan(plan)
        if plan.action != "notify":
            return plan.action
        if lease is None:
            return "deferred_or_already_owned"
        card = lease.card
        if card is None:
            try:
                selected = tuple(c for c in snapshot.update.claims if c.ref in plan.selected_claim_refs)
                copy = await self.composer.compose(selected, timeout=budget.remaining())
                card = await self.store.save_card(lease, freeze_card(plan, snapshot.update, copy))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.store.record_card_failure(lease, error_code=type(exc).__name__)
                raise
        budget.remaining()
        if not await self.store.atomic_begin_send(lease, card):
            return "preflight_changed"
        try:
            outcome = await self.sender.send(card, channel=channel, timeout=budget.remaining())
            if outcome.payload_sha256 != card.payload_sha256:
                raise ContractFault("news_sender_changed_frozen_payload")
        except BaseException as exc:
            # After entering sending, an unexpected error/cancellation says
            # nothing about whether the provider committed. Never blindly resend.
            await self.store.settle_send(lease, card, SendOutcome(state="ambiguous", payload_sha256=card.payload_sha256,
                error_code=type(exc).__name__), settled_at_ms=self.clock())
            raise
        await self.store.settle_send(lease, card, outcome, settled_at_ms=self.clock())
        return outcome.state


class PublicRelay:
    def __init__(self, store: NewsStore, receiver: TradingReceiver) -> None:
        self.store, self.receiver = store, receiver

    async def advance(self, *, limit: int = 64) -> int:
        rows = await self.store.pending_public_updates(limit)
        for update in rows:
            # Source corrections never even reach target selection/accept-trigger.
            if update.kind == "source_update":
                await self.receiver.receive_source_update(update)
            else:
                await self.receiver.receive_catalyst(update)
            # A receiver commits update_id idempotently. A crash before this ack
            # replays the same ID, not a new trigger/Case or refreshed freshness.
            await self.store.acknowledge_public_update(update.update_id)
        return len(rows)


class Repair:
    """A callable maintenance turn, scheduled by the existing worker, not a daemon."""
    def __init__(self, store: NewsStore, *, wake_semantic: Callable, wake_notification: Callable) -> None:
        self.store, self.wake_semantic, self.wake_notification = store, wake_semantic, wake_notification

    async def advance(self, channel: str, *, limit: int = 64) -> None:
        for event_id in await self.store.pending_semantic_events(limit):
            await self.wake_semantic(event_id)
        for event_id in await self.store.pending_notification_events(channel, limit):
            await self.wake_notification(event_id, channel)
