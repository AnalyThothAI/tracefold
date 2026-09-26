"""Source-content idempotency and candidate recall, without near-match authority."""
from __future__ import annotations

from typing import Protocol

from .contracts import Exact, Source
from .identity import digest, identity


class SourceMessage(Exact):
    record_id: str
    text: str
    source: Source


class Admission(Exact):
    record_key: str
    revision_key: str
    event_id: str
    body_sha256: str
    message: SourceMessage
    related_event_ids: tuple[str, ...]


class AdmissionStore(Protocol):
    async def source_event(self, record_key: str) -> str | None: ...
    async def recall_candidates(self, message: SourceMessage) -> tuple[str, ...]: ...
    async def atomic_ingest(self, admission: Admission) -> bool:
        """In one short transaction append raw evidence revision and semantic work.

        Exact revision replay returns False without updating first-available time.
        Same provider record with a changed body is a new evidence revision, not
        a source-ID-only noop. Preserve its previous revision as original evidence.
        A source-event assignment race returns/uses its existing authoritative
        assignment, not a second Event. related_event_ids only seed recall; they
        cannot settle the item, mark it equivalent or suppress further processing.
        Durable pending work is committed here; the existing repair loop may wake
        the broker after a publish failure. Do not require an in-memory callback.
        """
        ...


class NewsAdmission:
    def __init__(self, store: AdmissionStore) -> None:
        self.store = store

    async def accept(self, message: SourceMessage) -> bool:
        record_key = identity("source_record", message.source.publisher_id, message.record_id)
        assigned = await self.store.source_event(record_key)
        candidates = await self.store.recall_candidates(message)
        body_sha = digest(message.text)
        revision_key = identity("source_revision", record_key, message.source.artifact_revision, body_sha,
            message.source.origin_id, message.source.attribution, message.source.published_at_ms)
        admission = Admission(record_key=record_key, revision_key=revision_key,
            event_id=assigned or identity("news_event", record_key), body_sha256=body_sha,
            message=message, related_event_ids=tuple(dict.fromkeys(candidates)))
        return await self.store.atomic_ingest(admission)
