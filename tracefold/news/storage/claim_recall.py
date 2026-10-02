"""Embedding outside transactions and one short candidate read."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..claim_recall import EmbeddingPort, Probe, embed_text
from ..clock import clock_ms
from ..updates.contracts import Extraction, FrozenInput
from ..updates.ports import PriorBatch
from .claim_index import source_keys

if TYPE_CHECKING:
    from ..pipeline.runtime import NewsDatabasePort


class PgClaimRecall:
    def __init__(self, db: NewsDatabasePort, *, embedder: EmbeddingPort | None = None) -> None:
        self.db = db
        self.embedder = embedder

    async def priors(self, source: FrozenInput, extracted: Extraction) -> PriorBatch:
        texts = [embed_text(c) for c in extracted.claims]
        probes = tuple(Probe(t) for t in texts) if self.embedder is None else await self.embedder.probes(texts)
        sources = tuple(key for e in source.evidence for key in source_keys(e.source))
        now_ms = clock_ms()
        diagnostics: dict[str, dict[str, Any]] = {c.slot: {} for c in extracted.claims}
        selected = await self.db.read(
            "news_claim_prior_recall",
            lambda repos: {
                c.slot: repos.news.claim_index.prior(
                    source.event_id, p, now_ms=now_ms, sources=sources, diagnostics=diagnostics[c.slot]
                )
                for c, p in zip(extracted.claims, probes, strict=True)
            },
        )
        return PriorBatch(selected, diagnostics)

    async def advance(self, *, limit: int = 64) -> None:
        now_ms = clock_ms()
        await self.db.tx("news_claim_index_backfill", lambda r: r.news.claim_index.backfill(limit=limit, now_ms=now_ms))
        if self.embedder is None:
            return
        pending = await self.db.read(
            "news_claim_index_pending", lambda r: r.news.claim_index.pending(limit, now_ms=now_ms)
        )
        probes = await self.embedder.probes([str(row["embed_text"]) for row in pending])
        vectors = [
            (str(r["claim_ref"]), str(r["text_sha256"]), p.vector)
            for r, p in zip(pending, probes, strict=True)
            if p.vector is not None
        ]
        if vectors:
            embedder_key = self.embedder.identity.key
            await self.db.tx(
                "news_claim_index_embed",
                lambda r: r.news.claim_index.save_vectors(
                    vectors,
                    embedder=embedder_key,
                ),
            )
