"""Typed semantic work and adoption over short PostgreSQL transactions."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from ..clock import clock_ms
from ..updates.contracts import EventUpdate, Evidence, Extraction, FrozenInput, PublicUpdate, ReadTarget, SemanticLease
from ..updates.ports import SemanticCheckpoint, SemanticObservation
from .errors import EventUpdateConflict
from .semantic_input import frozen_input, item_evidence, read_target_item_id
from .sql_values import _dumps
from .update_commit import PUBLIC_TRADE_KINDS

if TYPE_CHECKING:
    from ..claim_recall import Probe
    from ..pipeline.runtime import NewsDatabasePort


def _lease_token() -> str:
    return secrets.token_hex(16)


class PgSemanticStore:
    def __init__(
        self,
        db: NewsDatabasePort,
        *,
        clock: Callable[[], int] = clock_ms,
        lease_token: Callable[[], str] = _lease_token,
    ) -> None:
        self.db = db
        self.clock = clock
        self.lease_token = lease_token

    async def input_for(self, event_id: str) -> FrozenInput:
        now_ms = self.clock()
        material = await self.db.read(
            "news_update_input",
            lambda repos: repos.news.semantic_input.semantic_input_material(event_id, now_ms=now_ms),
        )
        return frozen_input(event_id, material)

    async def head(self, event_id: str) -> EventUpdate | None:
        document = await self.db.read(
            "news_update_head", lambda repos: repos.news.semantic_updates.event_update_head_document(event_id)
        )
        return None if document is None else EventUpdate.model_validate(document)

    async def checkpoint(self, work_id: str) -> SemanticCheckpoint | None:
        documents = await self.db.read(
            "news_update_checkpoint", lambda repos: repos.news.semantic_updates.semantic_checkpoint_documents(work_id)
        )
        if not documents:
            return None
        extraction = documents.get("extraction")
        return SemanticCheckpoint(
            work_id=work_id, extraction=None if extraction is None else Extraction.model_validate(extraction)
        )

    async def save_extraction(self, work_id: str, extracted: Extraction) -> Extraction:
        document = extracted.model_dump_json()
        now_ms = self.clock()
        stored = await self.db.tx(
            "news_update_save_checkpoint",
            lambda repos: repos.news.semantic_updates.insert_semantic_checkpoint(
                work_id=work_id, stage="extraction", document_json=document, now_ms=now_ms
            ),
        )
        return Extraction.model_validate(stored)

    async def save_observation(self, observation: SemanticObservation) -> SemanticObservation:
        understanding = observation.understanding.model_dump_json()
        row = await self.db.tx(
            "news_update_save_observation",
            lambda repos: repos.news.semantic_updates.insert_semantic_observation(
                result_id=observation.result_id,
                work_id=observation.work_id,
                event_id=observation.event_id,
                input_revision=observation.input_revision,
                input_sha256=observation.input_sha256,
                program_identity=observation.program_identity,
                completed_at_ms=observation.completed_at_ms,
                understanding_json=understanding,
                input_manifest_json=json.dumps(observation.input_manifest),
                read_refs=observation.read_refs,
                reanalysis_reason=observation.reanalysis_reason,
                reanalysis_head_ref=observation.reanalysis_head_ref,
            ),
        )
        stored = SemanticObservation(
            result_id=str(row["result_id"]),
            work_id=str(row["work_id"]),
            event_id=str(row["event_id"]),
            input_revision=int(row["input_revision"]),
            input_sha256=str(row["input_sha256"]),
            program_identity=str(row["program_identity"]),
            completed_at_ms=int(row["completed_at_ms"]),
            understanding=Extraction.model_validate(row["understanding"]),
            input_manifest=dict(row["input_manifest"]),
            read_refs=tuple(row["read_refs"]),
            reanalysis_reason=row["reanalysis_reason"],
            reanalysis_head_ref=row["reanalysis_head_ref"],
        )
        # The completion clock is the stored winner's; every other field must be the same fact.
        if stored.model_copy(update={"completed_at_ms": observation.completed_at_ms}) != observation:
            raise EventUpdateConflict("news_semantic_observation_conflict")
        return stored

    async def atomic_adopt(
        self,
        *,
        expected_head_ref: str | None,
        lease: SemanticLease,
        observation: SemanticObservation,
        update: EventUpdate,
        public: tuple[PublicUpdate, ...],
        probes: Mapping[str, Probe] | None = None,
    ) -> bool:
        if observation.event_id != update.event_id:
            raise ValueError("news_adopt_observation_event_mismatch")
        for row in public:
            if row.event_id != update.event_id or row.content_revision != update.content_revision:
                raise ValueError("news_adopt_public_update_mismatch")
        public_rows = [(PUBLIC_TRADE_KINDS[row.kind], row.model_dump(mode="json")) for row in public]
        document = update.model_dump_json()
        now_ms = self.clock()
        return bool(
            await self.db.tx(
                "news_update_adopt",
                lambda repos: repos.news.semantic_updates.adopt_event_update(
                    expected_head_ref=expected_head_ref,
                    lease=lease,
                    update=update,
                    document_json=document,
                    observation_result_id=observation.result_id,
                    public_rows=public_rows,
                    now_ms=now_ms,
                    probes=probes,
                ),
            )
        )

    async def finish_semantic_work(self, work_id: str, *, lease: SemanticLease, reason: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_finish_work",
            lambda repos: repos.news.semantic_work.finish_semantic_work(
                work_id=work_id, lease=lease, reason=reason, now_ms=now_ms
            ),
        )

    async def reserve_extra_read(self, lineage_id: str, target_ref: str) -> bool:
        now_ms = self.clock()
        return bool(
            await self.db.tx(
                "news_update_reserve_read",
                lambda repos: repos.news.semantic_work.reserve_extra_read(
                    lineage_id=lineage_id, target_ref=target_ref, now_ms=now_ms
                ),
            )
        )

    async def attach_extra_evidence(
        self,
        source: FrozenInput,
        target: ReadTarget,
        evidence: tuple[Evidence, ...],
        affected_claim_refs: tuple[str, ...],
    ) -> None:
        del target  # reserved by `reserve_extra_read`; the evidence carries its own provenance
        if not evidence:
            raise ValueError("news_extra_evidence_empty")
        evidence_json = _dumps([item.model_dump(mode="json") for item in {row.ref: row for row in evidence}.values()])
        now_ms = self.clock()
        await self.db.tx(
            "news_update_attach_evidence",
            lambda repos: repos.news.semantic_work.attach_extra_evidence(
                event_id=source.event_id,
                lineage_id=source.lineage_id,
                evidence_json=evidence_json,
                focus_claim_refs=affected_claim_refs,
                now_ms=now_ms,
            ),
        )

    async def record_read_outcome(self, lineage_id: str, *, outcome: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_read_outcome",
            lambda repos: repos.news.semantic_work.record_read_outcome(
                lineage_id=lineage_id, outcome=outcome, now_ms=now_ms
            ),
        )

    async def pending_semantic_events(self, limit: int) -> tuple[str, ...]:
        now_ms = self.clock()
        return tuple(
            await self.db.read(
                "news_update_pending_semantic",
                lambda repos: repos.news.semantic_work.pending_semantic_event_ids(now_ms=now_ms, limit=limit),
            )
        )

    async def claim_semantic_work(self, event_id: str, *, lease_ms: int) -> SemanticLease | None:
        token = self.lease_token()
        now_ms = self.clock()
        read = await self.db.read(
            "news_update_read_claim",
            lambda repos: repos.news.semantic_work.read_semantic_claim(
                event_id=event_id, now_ms=now_ms, input=repos.news.semantic_input
            ),
            repeatable_read=True,
        )
        if read is None:
            return None
        return await self.db.tx(
            "news_update_claim_work",
            lambda repos: repos.news.semantic_work.claim_semantic_work(
                read=read, lease_token=token, now_ms=self.clock(), lease_ms=lease_ms
            ),
        )

    async def defer_semantic_event(self, lease: SemanticLease, *, reason: str, retry_after_ms: int = 0) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_defer_event",
            lambda repos: repos.news.semantic_work.defer_semantic_event(
                lease=lease,
                reason=reason,
                now_ms=now_ms,
                retry_after_ms=retry_after_ms,
            ),
        )

    async def fail_semantic_event(self, lease: SemanticLease, *, error_code: str) -> None:
        now_ms = self.clock()
        await self.db.tx(
            "news_update_fail_event",
            lambda repos: repos.news.semantic_work.fail_semantic_event(
                lease=lease, error_code=error_code, now_ms=now_ms
            ),
        )


class PgSourceReader:
    """`tracefold.news.updates.ports.ExistingSourceReader` over stored News Items only.

    A target is one a store prepared (`news_item:<item_id>`), never a model-generated URL or tool; the
    read returns that Item's stored text with its code-owned provenance, or nothing.
    """

    def __init__(self, db: NewsDatabasePort) -> None:
        self.db = db

    async def read(self, target: ReadTarget) -> tuple[Evidence, ...]:
        item_id = read_target_item_id(target.ref)
        if item_id is None:
            return ()
        item = await self.db.read(
            "news_update_read_target", lambda repos: repos.news.semantic_input.read_target_item(item_id)
        )
        value = None if item is None else item_evidence(item)
        return () if value is None else (value,)
