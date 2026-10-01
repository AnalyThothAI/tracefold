"""One semantic workflow owner and its durable-work repair continuation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from ..clock import clock_ms
from .assembly import assemble_update
from .contracts import EventUpdate, Extraction, FrozenInput, PriorClaim, ReadTarget, SemanticLease
from .identity import canonical_json, identity
from .judgment import Budget, ProviderUnavailable, Question
from .ports import ExistingSourceReader, SemanticObservation, SemanticStore
from .projection import reading_views
from .public import public_updates
from .semantics import SemanticAnalyzer

# Measured on the production News endpoint (2026-09-26 replay): one extraction is 7-20 s of decode under load,
# and an Event with related priors asks several judgment batches after it. A 20 s stage timed out before the
# extraction could be checkpointed, so every retry started over. The stage bounds a whole attempt; one
# model call is bounded separately so a stalled call still leaves room for the declared fallback.
SEMANTIC_STAGE_SECONDS: Final = 120.0
GENERATION_CALL_SECONDS: Final = 60.0
# A CAS collision retries only missing relationships/adoption, not extraction.
ADOPTION_ATTEMPTS: Final = 2
logger = logging.getLogger(__name__)


class NewsAgent:
    def __init__(
        self,
        store: SemanticStore,
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
            result_id=identity("semantic_result", self.program_identity, work_id, source.prior, understood),
            work_id=work_id,
            event_id=source.event_id,
            input_revision=source.revision,
            input_sha256=source.input_sha,
            program_identity=self.program_identity,
            completed_at_ms=completed_at_ms,
            understanding=understood,
            input_manifest={
                "lineage_id": source.lineage_id,
                "evidence": [{"ref": e.ref, "source": e.source.model_dump(mode="json")} for e in source.evidence],
                "read_refs": [view.read_ref for view in reading_views(source)],
                "prior_claim_refs": [row.claim.ref for row in source.prior],
            },
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


class Repair:
    """A callable maintenance turn, scheduled by the existing worker, not a daemon.

    It re-wakes durable pending semantic work. Pending notification work needs no wake: the
    Deliverer polls it on its own turn.
    """

    def __init__(self, store: SemanticStore, *, wake_semantic: Callable[[str], Awaitable[object]]) -> None:
        self.store = store
        self.wake_semantic = wake_semantic

    async def advance(self, *, limit: int = 64) -> None:
        for event_id in await self.store.pending_semantic_events(limit):
            await self.wake_semantic(event_id)
