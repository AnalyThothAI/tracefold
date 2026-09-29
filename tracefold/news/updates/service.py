"""One News Agent and independent notification/public-delivery continuations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from ..bus import DeferError, TransientError
from .contracts import EventUpdate, Extraction, FrozenInput, PriorClaim, ReadTarget, SemanticLease
from .identity import canonical_json, identity
from .judgment import Budget, ContractFault, ProviderUnavailable, Question, error_code
from .notification import (
    CardComposer,
    FrozenCard,
    NotificationPlanner,
    PlanTimings,
    card_copy_material,
    freeze_card,
)
from .ports import (
    DeliveryTimings,
    ExistingSourceReader,
    IntentLease,
    NewsStore,
    SemanticObservation,
    Sender,
    SendOutcome,
)
from .projection import reading_views
from .public import public_updates
from .semantics import SemanticAnalyzer, assemble_update

# Measured on the production News endpoint (2026-09-26 replay): one extraction is 7-20 s of decode under load,
# and an Event with related priors asks several judgment batches after it. A 20 s stage timed out before the
# extraction could be checkpointed, so every retry started over. The stage bounds a whole attempt; one
# model call is bounded separately so a stalled call still leaves room for the declared fallback.
SEMANTIC_STAGE_SECONDS: Final = 120.0
NOTIFICATION_STAGE_SECONDS: Final = 60.0
GENERATION_CALL_SECONDS: Final = 60.0
# A CAS collision retries only missing relationships/adoption, not extraction.
ADOPTION_ATTEMPTS: Final = 2
logger = logging.getLogger(__name__)


def clock_ms() -> int:
    return time.time_ns() // 1_000_000


class NewsAgent:
    def __init__(
        self,
        store: NewsStore,
        analyzer: SemanticAnalyzer,
        *,
        program_identity: str,
        source_reader: ExistingSourceReader | None = None,
        clock: Callable[[], int] = clock_ms,
        stage_seconds: float = SEMANTIC_STAGE_SECONDS,
    ) -> None:
        self.store = store
        self.analyzer = analyzer
        self.program_identity = program_identity
        self.source_reader = source_reader
        self.clock = clock
        self.stage_seconds = stage_seconds

    async def process(self, lease: SemanticLease, *, final_attempt: bool = True) -> str:
        """One semantic turn. Every model call, retry and optional read shares one stage deadline.

        `final_attempt` is the worker's retry policy, not a content rule: before the last attempt of a
        wanted revision, a relation or source answer the provider could not give raises
        ProviderUnavailable so the turn is retried; on the last attempt it is adopted as unresolved
        (`possible_new`), never as "no news value".
        """

        budget = Budget.start(self.stage_seconds)
        async with asyncio.timeout(self.stage_seconds):
            return await self._process(lease, budget, final_attempt=final_attempt)

    async def _process(self, lease: SemanticLease, budget: Budget, *, final_attempt: bool) -> str:
        source = lease.source
        event_id = source.event_id
        work_id = identity(
            "semantic_work",
            source.event_id,
            source.revision,
            source.input_sha,
            self.analyzer.identity,
        )
        for view in reading_views(source):
            logger.info(
                "news semantic read event_id=%s input_revision=%s work_id=%s read_ref=%s "
                "material_sha=%s chars=%s mode=%s",
                event_id,
                source.revision,
                work_id,
                view.read_ref,
                view.material_sha,
                sum(len(span.text) for span in view.spans),
                view.mode,
            )
        if not source.evidence:
            # A snapshot can advance for metadata or for an already adopted source identity.
            # It still needs a durable observation so finish_semantic_work can settle exactly
            # this input revision, but it must not re-extract the Event's old members.
            observation = self._observation(work_id, source, Extraction(claims=()), self.clock())
            await self.store.save_observation(observation)
            await self.store.finish_semantic_work(work_id, lease=lease, reason="no_new_evidence")
            return "unchanged"
        saved = await self.store.checkpoint(work_id)
        extracted = None if saved is None else saved.extraction
        if extracted is None:
            extracted = await self.analyzer.extract(source, budget)
            extracted = await self.store.save_extraction(work_id, extracted)
        # Understanding is derived again on every attempt against the priors supplied now; each answer it
        # needs is cached by content, so a retry asks only what changed or failed.
        understood = await self.analyzer.understand(source, extracted, budget, final_attempt=final_attempt)

        completed_at_ms = self.clock()
        # Persisted checkpoints/cache retain successful work if these retries are exhausted.
        for _attempt in range(ADOPTION_ATTEMPTS):
            budget.remaining()
            head = await self.store.head(event_id)
            if head is not None and head.input_revision > source.revision:
                observation = self._observation(work_id, source, understood, completed_at_ms)
                await self.store.save_observation(observation)
                await self.store.finish_semantic_work(work_id, lease=lease, reason="newer_head_already_adopted")
                return "newer_head"
            head_refs = set() if head is None else {claim.ref for claim in head.current_claims}
            prior_refs = {row.claim.ref for row in source.prior}
            if head is not None and head_refs - prior_refs:
                priors = {row.claim.ref: row for row in source.prior}
                for claim in head.current_claims:
                    priors[claim.ref] = PriorClaim(
                        event_id=head.event_id, content_revision=head.content_revision, claim=claim
                    )
                source = FrozenInput.model_validate({**dict(source), "prior": tuple(priors.values())})
                understood = await self.analyzer.understand(
                    source, understood, budget, rebase_only=True, final_attempt=final_attempt
                )
            observation = self._observation(work_id, source, understood, completed_at_ms)
            observation = await self.store.save_observation(observation)
            update = assemble_update(source, understood, head, adopted_at_ms=self.clock())
            if update is None:
                await self.store.finish_semantic_work(work_id, lease=lease, reason="no_substantive_content_change")
                return "unchanged"
            public = public_updates(update, semantic_completed_at_ms=observation.completed_at_ms)
            adopted = await self.store.atomic_adopt(
                expected_head_ref=None if head is None else head.ref,
                lease=lease,
                observation=observation,
                update=update,
                public=public,
            )
            if adopted:
                await self.store.finish_semantic_work(work_id, lease=lease, reason="adopted")
                # The result, outbox and notification_pending are already committed.
                # This optional branch cannot retract them or reset its lineage budget.
                await self._extra_read(source, update, budget)
                return "adopted"
        await self.store.defer_semantic_event(lease, reason="adopted_head_changed")
        return "deferred"

    def _observation(
        self,
        work_id: str,
        source: FrozenInput,
        understood: Extraction,
        completed_at_ms: int,
    ) -> SemanticObservation:
        return SemanticObservation(
            result_id=identity("semantic_result", work_id, source.prior, understood),
            work_id=work_id,
            event_id=source.event_id,
            input_revision=source.revision,
            input_sha256=source.input_sha,
            program_identity=self.program_identity,
            completed_at_ms=completed_at_ms,
            understanding=understood,
            read_refs=tuple(view.read_ref for view in reading_views(source)),
            reanalysis_reason=source.reanalysis_reason,
            reanalysis_head_ref=source.reanalysis_head_ref,
        )

    async def _extra_read(self, source: FrozenInput, update: EventUpdate, budget: Budget) -> None:
        if self.source_reader is None or not source.read_targets or not update.open_questions:
            return
        targets = {target.ref: target for target in source.read_targets}
        candidates: list[tuple[tuple[str, ...], ReadTarget, Question]] = []
        for gap in update.open_questions:
            target = targets.get(gap.target_ref or "")
            if target is None:
                continue
            question = Question(
                item_id=identity("read_candidate", gap.question, target.ref),
                payload_json=canonical_json({"gap": gap, "target": target}),
            )
            candidates.append((gap.claim_refs, target, question))
        if not candidates:
            return
        try:
            budget.remaining()
        except TimeoutError:
            return
        try:
            questions = tuple(dict.fromkeys(question for _refs, _target, question in candidates))
            answers = {row.item_id: row for row in await self.analyzer.judgments.judge("next_read", questions, budget)}
            selected = next(
                (
                    (refs, target)
                    for refs, target, question in candidates
                    if answers[question.item_id].status == "available" and answers[question.item_id].value == "read"
                ),
                None,
            )
            if selected is None:
                return
            claim_refs, target = selected
            if not await self.store.reserve_extra_read(source.lineage_id, target.ref):
                return
            async with asyncio.timeout(budget.remaining()):
                evidence = await self.source_reader.read(target)
            if evidence:
                await self.store.attach_extra_evidence(source, target, evidence, claim_refs)
                await self.store.record_read_outcome(source.lineage_id, outcome="attached")
            else:
                await self.store.record_read_outcome(source.lineage_id, outcome="no_material")
        except (ProviderUnavailable, TimeoutError):
            await self.store.record_read_outcome(source.lineage_id, outcome="unavailable_or_budget_exhausted")
        # Cancellation/configuration/programming errors remain visible. Adoption
        # remains durable even if the worker is stopped at this point.


@dataclass(frozen=True, slots=True)
class NotificationTurn:
    """What one notification turn did.

    `status` is `no_work`, a plan action, a named CAS result, `plan_failed` or `card_failed` after that
    failure is recorded, or the settled send state. A settled send carries the intent it settled and the
    provider's outcome, so a delivery adapter can enrich exactly that receipt afterwards. A ready turn
    carries when its card was composed and when it became ready, which the send records beside its receipt.
    """

    status: str
    update: EventUpdate | None = None
    lease: IntentLease | None = None
    card: FrozenCard | None = None
    outcome: SendOutcome | None = None
    error_code: str | None = None
    card_started_at_ms: int | None = None
    card_finished_at_ms: int | None = None
    ready_at_ms: int | None = None


# What says the database could not answer this second, rather than that anything failed. It is never
# recorded against the work: the marker stays due and the next poll asks again.
_UNANSWERED: Final = (TransientError, DeferError)


class Notifications:
    """Plan a pending head, reserve its intent, compose and freeze its card, then send it once.

    Planning and card composition share one model stage deadline. The send does not: a paced provider
    entry may hold a card for longer than a model stage, and a wait that nothing sent is not an unknown
    outcome. The sender bounds its own provider call.
    """

    def __init__(
        self,
        store: NewsStore,
        planner: NotificationPlanner,
        composer: CardComposer,
        *,
        clock: Callable[[], int] = clock_ms,
        stage_seconds: float = NOTIFICATION_STAGE_SECONDS,
    ) -> None:
        self.store = store
        self.planner = planner
        self.composer = composer
        self.clock = clock
        self.stage_seconds = stage_seconds

    async def process(self, event_id: str, channel: str, sender: Sender) -> NotificationTurn:
        prepared = await self.prepare(event_id, channel)
        return await self.finalize(prepared, sender)

    async def prepare(self, event_id: str, channel: str) -> NotificationTurn:
        """One notification turn for one channel, up to a frozen card ready to send.

        A failed plan spends one attempt of the work and a failed card one attempt of its intent; either is
        recorded and returned as `plan_failed` / `card_failed`, never raised, and neither touches adopted
        semantics. A database that cannot answer is not a failure of this work and is raised unrecorded.
        """

        budget = Budget.start(self.stage_seconds)
        started_at_ms = self.clock()
        started = time.monotonic()
        snapshot = None
        try:
            snapshot = await self.store.notification_snapshot(event_id, channel)
            if snapshot is None:
                return NotificationTurn("no_work")
            snapshot_ms = _elapsed_ms(started)
            # The stage deadline surfaces here as TimeoutError, so an expired plan is recorded like any
            # other failed one instead of leaving its work due again at once.
            async with asyncio.timeout(budget.remaining()):
                plan = await self.planner.plan(
                    snapshot.update,
                    snapshot.reader,
                    budget,
                    now_ms=self.clock(),
                    reuse=lambda fingerprint: self.store.lookup_notification_decision(
                        snapshot.update.event_id, channel, fingerprint
                    ),
                )
            timings = plan.timings or PlanTimings()
            plan = plan.model_copy(
                update={
                    "timings": timings.model_copy(
                        update={
                            "due_at_ms": snapshot.work_due_at_ms,
                            "started_at_ms": started_at_ms,
                            "snapshot_ms": snapshot_ms,
                            "planned_at_ms": self.clock(),
                        }
                    )
                }
            )
            committed = await self.store.atomic_record_plan(plan)
        except asyncio.CancelledError:
            raise
        except _UNANSWERED:
            # Nothing about this work failed. It is put off once, uncharged, so a database that cannot
            # answer is not asked the same plan again every poll; if even that cannot be written, the
            # marker simply stays due.
            with contextlib.suppress(*_UNANSWERED):
                await self.store.postpone_notification(
                    event_id, channel, None if snapshot is None else snapshot.update.content_revision
                )
            raise
        except Exception as exc:
            code = error_code(exc, default="news_notification_plan")
            await self.store.defer_notification(
                event_id,
                channel,
                None if snapshot is None else snapshot.update.content_revision,
                None if snapshot is None else snapshot.work_updated_at_ms,
                error_code=code,
            )
            return NotificationTurn(
                "plan_failed", update=None if snapshot is None else snapshot.update, error_code=code
            )
        if committed.status != "committed" or committed.effective_plan is None:
            return NotificationTurn(committed.status, update=snapshot.update)
        plan = committed.effective_plan
        lease = committed.lease
        if plan.action != "notify" or lease is None:
            return NotificationTurn(plan.action, update=snapshot.update)
        card = lease.card
        card_started_at_ms = card_finished_at_ms = None
        if card is None:
            card_started_at_ms = self.clock()
            try:
                card = await self._card(lease, snapshot.update, budget)
            except asyncio.CancelledError:
                await self._release_unsent(lease)
                raise
            except _UNANSWERED:
                raise
            except Exception as exc:
                code = error_code(exc, default="news_card")
                await self.store.record_unsent_failure(lease, error_code=code, retryable=True)
                return NotificationTurn("card_failed", update=snapshot.update, lease=lease, error_code=code)
            card_finished_at_ms = self.clock()
        return NotificationTurn(
            "ready",
            update=snapshot.update,
            lease=lease,
            card=card,
            card_started_at_ms=card_started_at_ms,
            card_finished_at_ms=card_finished_at_ms,
            ready_at_ms=self.clock(),
        )

    async def finalize(self, prepared: NotificationTurn, sender: Sender) -> NotificationTurn:
        """Check current reader state and settle one send inside its paced opportunity."""

        if prepared.status != "ready":
            return prepared
        if prepared.lease is None or prepared.update is None or prepared.card is None:
            raise ValueError("news_prepared_notification_incomplete")
        return await self._send(prepared, prepared.lease, prepared.update, prepared.card, sender)

    async def release_ready(self, prepared: NotificationTurn) -> None:
        """Release a prepared owner that the coordinator will never finalize."""

        if prepared.status == "ready" and prepared.lease is not None:
            await self._release_unsent(prepared.lease)

    async def _release_unsent(self, lease: IntentLease) -> None:
        cleanup = asyncio.create_task(self.store.release_unsent_intent(lease))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=5)
            raise

    async def _settle_known(self, lease: IntentLease, card: FrozenCard, outcome: SendOutcome) -> None:
        settled_at_ms = self.clock()
        started_at = time.monotonic()

        async def commit() -> None:
            for attempt in range(3):
                try:
                    result = await self.store.settle_send(lease, card, outcome, settled_at_ms=settled_at_ms)
                    if result not in ("sent", "not_sent", "ambiguous", "terminal", "already_settled"):
                        raise RuntimeError("news_send_settlement_conflict")
                    logger.info(
                        "news send settlement intent_id=%s state=%s retries=%s elapsed_ms=%s",
                        lease.intent_id,
                        outcome.state,
                        attempt,
                        int((time.monotonic() - started_at) * 1000),
                    )
                    return
                except (DeferError, TransientError) as exc:
                    if attempt == 2:
                        raise RuntimeError("news_send_settlement_unavailable") from exc
                    await asyncio.sleep(0.25 * (attempt + 1))

        owner = asyncio.create_task(commit(), name=f"news-send-settle:{lease.intent_id}")
        try:
            await asyncio.shield(owner)
        except asyncio.CancelledError:
            # A provider result already exists. The owner retains that exact result while this
            # caller is cancelled; a bounded failure faults the sender instead of returning idle.
            try:
                await asyncio.wait_for(asyncio.shield(owner), timeout=12)
            except TimeoutError as exc:
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
                raise RuntimeError("news_send_settlement_unavailable") from exc
            raise

    async def _card(self, lease: IntentLease, update: EventUpdate, budget: Budget) -> FrozenCard:
        """Compose copy for exactly the selected claims and freeze it; the caller records a failure."""

        plan = lease.plan
        selected = tuple(claim for claim in update.claims if claim.ref in plan.selected_claim_refs)
        async with asyncio.timeout(budget.remaining()):
            cited = {citation.evidence_ref for claim in selected for citation in claim.citations}
            sources = {item.ref: item.source for item in update.evidence if item.ref in cited}
            input_digest = identity(
                "news_card_copy_input", self.composer.identity, card_copy_material(selected, sources)
            )
            copy = await self.store.lookup_card_copy(input_digest)
            logger.info(
                "news card copy event_id=%s intent_id=%s input_digest=%s reused=%s",
                update.event_id,
                lease.intent_id,
                input_digest,
                copy is not None,
            )
            if copy is None:
                copy = await self.composer.compose(selected, sources=sources)
        frozen = freeze_card(plan, update, copy)
        return await self.store.save_card(lease, frozen, copy=copy, input_digest=input_digest)

    async def _send(
        self,
        prepared: NotificationTurn,
        lease: IntentLease,
        update: EventUpdate,
        card: FrozenCard,
        sender: Sender,
    ) -> NotificationTurn:
        begin_started = False
        queued_at = time.monotonic()
        try:
            async with sender.send_slot():
                slot_at_ms = self.clock()
                logger.info(
                    "news send slot event_id=%s intent_id=%s wait_ms=%s",
                    update.event_id,
                    lease.intent_id,
                    int((time.monotonic() - queued_at) * 1000),
                )
                # All fallible local/target preparation precedes the durable sending boundary.
                preflight = await sender.preflight(card, plan=lease.plan, update=update)
                if preflight is not None:
                    # Provably never sent, and no `sending` row claims otherwise: a failure of the unsent
                    # intent, retried with the same identity and payload or ended under its attempt bound.
                    await self.store.record_unsent_failure(
                        lease,
                        error_code=preflight.error_code or "news_delivery_preflight_failed",
                        retryable=preflight.retryable,
                        retry_after_ms=preflight.retry_after_ms,
                    )
                    logger.info("news send not started intent_id=%s error=%s", lease.intent_id, preflight.error_code)
                    return NotificationTurn(preflight.state, update=update, lease=lease, card=card, outcome=preflight)
                # Pacer waiting and preflight are over. This short transaction checks
                # the current reader and head before the external side effect can begin.
                begin_started = True
                ready_at_ms = slot_at_ms if prepared.ready_at_ms is None else prepared.ready_at_ms
                begin_status = await self.store.atomic_begin_send(
                    lease,
                    card,
                    timings=DeliveryTimings(
                        card_started_at_ms=prepared.card_started_at_ms,
                        card_finished_at_ms=prepared.card_finished_at_ms,
                        ready_at_ms=ready_at_ms,
                        send_slot_wait_ms=max(0, slot_at_ms - ready_at_ms),
                    ),
                )
                if begin_status != "begun":
                    return NotificationTurn(begin_status, update=update, lease=lease, card=card)
                try:
                    provider_at = time.monotonic()
                    outcome = await sender.send(card, plan=lease.plan, update=update)
                    logger.info(
                        "news provider returned intent_id=%s state=%s elapsed_ms=%s",
                        lease.intent_id,
                        outcome.state,
                        int((time.monotonic() - provider_at) * 1000),
                    )
                    if outcome.payload_sha256 != card.payload_sha256:
                        raise ContractFault("news_sender_changed_frozen_payload")
                except BaseException as exc:
                    # A crash between begin_send and the provider boundary is not
                    # provably safe to replay. Keep that intent ambiguous.
                    ambiguous = SendOutcome(
                        state="ambiguous", payload_sha256=card.payload_sha256, error_code=type(exc).__name__
                    )
                    await self._settle_known(lease, card, ambiguous)
                    raise
                await self._settle_known(lease, card, outcome)
                logger.info("news send settled intent_id=%s state=%s", lease.intent_id, outcome.state)
                return NotificationTurn(outcome.state, update=update, lease=lease, card=card, outcome=outcome)
        except asyncio.CancelledError:
            if not begin_started:
                await self._release_unsent(lease)
            raise


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


class Repair:
    """A callable maintenance turn, scheduled by the existing worker, not a daemon.

    It re-wakes durable pending semantic work. Pending notification work needs no wake: the
    Deliverer polls it on its own turn.
    """

    def __init__(self, store: NewsStore, *, wake_semantic: Callable[[str], Awaitable[object]]) -> None:
        self.store = store
        self.wake_semantic = wake_semantic

    async def advance(self, *, limit: int = 64) -> None:
        for event_id in await self.store.pending_semantic_events(limit):
            await self.wake_semantic(event_id)
