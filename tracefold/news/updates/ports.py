"""Persistence and source-read contracts consumed by the semantic workflow.

Every atomic_* method is one short database transaction. Model/source I/O occurs
outside it, and the adopted PostgreSQL head remains the authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import Field

from .contracts import (
    EventUpdate,
    Evidence,
    Exact,
    Extraction,
    FrozenInput,
    PriorClaim,
    PublicUpdate,
    ReadTarget,
    SemanticLease,
)

if TYPE_CHECKING:
    from ..claim_recall import Probe


@dataclass(frozen=True, slots=True)
class PriorBatch:
    by_slot: Mapping[str, tuple[PriorClaim, ...]]
    diagnostics: Mapping[str, Mapping[str, Any]]
    probes: Mapping[str, Probe] = field(default_factory=dict)


class PriorRecall(Protocol):
    async def priors(self, source: FrozenInput, extracted: Extraction) -> PriorBatch: ...


class SemanticCheckpoint(Exact):
    work_id: str
    extraction: Extraction | None = None


class SemanticObservation(Exact):
    result_id: str
    work_id: str
    event_id: str
    input_revision: int
    input_sha256: str
    program_identity: str
    completed_at_ms: int
    input_manifest: dict[str, Any] = Field(default_factory=dict)
    understanding: Extraction
    read_refs: tuple[str, ...]
    reanalysis_reason: str | None = None
    reanalysis_head_ref: str | None = None


class ExistingSourceReader(Protocol):
    async def read(self, target: ReadTarget) -> tuple[Evidence, ...]:
        """Read one code-prepared target with existing capabilities; no model-generated URL or tool."""
        ...


class SemanticStore(Protocol):
    async def head(self, event_id: str) -> EventUpdate | None: ...

    async def checkpoint(self, work_id: str) -> SemanticCheckpoint | None: ...

    async def save_extraction(self, work_id: str, extracted: Extraction) -> Extraction:
        """Insert-only work stage; return the first stored winner on a race."""
        ...

    async def save_observation(self, observation: SemanticObservation) -> SemanticObservation:
        """Insert-only result_id; return the stored winner, including its original
        completion clock, on replay. Content mismatches are errors. This write is
        independent of adoption and cards.
        """
        ...

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
        """CAS the adopted head, save update + public outbox + notification_pending.

        Return False only for a changed adopted head, not simply newer arriving
        evidence. Never downgrade an adopted input revision. Unique public IDs
        retain the first payload; conflicting payload on an ID is an error.
        A `possible_new` change is adopted content and marks notification work,
        but it never has a public row: `public` already excludes it.
        Reuse probes only for the final adopted claim's exact statement and
        current embedder identity; retrieval slots are not durable claim refs.
        """
        ...

    async def finish_semantic_work(self, work_id: str, *, lease: SemanticLease, reason: str) -> None: ...

    async def defer_semantic_event(self, lease: SemanticLease, *, reason: str, retry_after_ms: int = 0) -> None: ...

    async def reserve_extra_read(self, lineage_id: str, target_ref: str) -> bool:
        """Atomic one-read budget for the entire lineage, durable across retries."""
        ...

    async def attach_extra_evidence(
        self,
        source: FrozenInput,
        target: ReadTarget,
        evidence: tuple[Evidence, ...],
        affected_claim_refs: tuple[str, ...],
    ) -> None:
        """Append a new evidence revision and enqueue only the affected semantic work.

        Preserve lineage and source first-known clocks. The next frozen input has
        focus_claim_refs and only the changed/affected source material; unaffected
        head claims are carried by assembly rather than re-extracted.
        """
        ...

    async def record_read_outcome(self, lineage_id: str, *, outcome: str) -> None: ...

    async def pending_semantic_events(self, limit: int) -> tuple[str, ...]: ...
