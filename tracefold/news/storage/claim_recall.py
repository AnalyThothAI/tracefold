"""Embedding outside transactions and one short candidate read."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any

from ..claim_recall import EmbeddingPort, Probe, embed_text, lexical_text
from ..clock import clock_ms
from ..updates.contracts import Claim, DraftClaim, Extraction, FrozenInput, PriorClaim
from ..updates.ports import PriorBatch
from .claim_index import source_keys

if TYPE_CHECKING:
    from ..pipeline.runtime import NewsDatabasePort, NewsRepositories


def _project_rows(repos: NewsRepositories, *, rows: Sequence[Mapping[str, Any]]) -> None:
    repos.news.claim_index.project_batch(rows)


def _save_vectors(repos: NewsRepositories, *, rows: Sequence[tuple[str, str, bytes]], embedder: str) -> None:
    repos.news.claim_index.save_vectors(rows, embedder=embedder)


class PgClaimRecall:
    def __init__(
        self, db: NewsDatabasePort, *, embedder: EmbeddingPort | None = None, embedding_batch_size: int = 64
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.embedding_batch_size = embedding_batch_size

    async def priors(self, source: FrozenInput, extracted: Extraction) -> PriorBatch:
        texts = [embed_text(c) for c in extracted.claims]
        probes = tuple(Probe(t) for t in texts) if self.embedder is None else await self.embedder.probes(texts)
        sources = tuple(key for e in source.evidence for key in source_keys(e.source))
        now_ms = clock_ms()
        diagnostics: dict[str, dict[str, Any]] = {c.slot: {} for c in extracted.claims}
        pool = (
            await self.db.read(
                "news_claim_prior_pool",
                lambda repos: repos.news.claim_index.prior_pool(source.event_id, now_ms=now_ms, sources=sources),
                repeatable_read=True,
            )
            if extracted.claims
            else ()
        )

        def select_claim(repos: NewsRepositories, *, claim: DraftClaim, probe: Probe) -> tuple[PriorClaim, ...]:
            return repos.news.claim_index.prior(
                source.event_id,
                probe,
                lexical_query=lexical_text(claim),
                now_ms=now_ms,
                sources=sources,
                diagnostics=diagnostics[claim.slot],
                pool=pool,
            )

        selected = {}
        for c, p in zip(extracted.claims, probes, strict=True):
            # Claim count has no cap. Each query owns a short transaction while
            # sharing the exact immutable head/receipt pool captured above.
            selected[c.slot] = await self.db.read(
                "news_claim_prior_recall",
                partial(select_claim, claim=c, probe=p),
                repeatable_read=True,
            )
        return PriorBatch(
            selected, diagnostics, probes={c.slot: p for c, p in zip(extracted.claims, probes, strict=True)}
        )

    async def advance(self, *, limit: int = 64) -> bool:
        now_ms = clock_ms()
        if self.embedder is None:
            return False
        pending = await self.db.read(
            "news_claim_index_pending",
            lambda r: r.news.claim_index.pending(min(limit, self.embedding_batch_size), now_ms=now_ms),
        )
        if not pending:
            return False
        probes = await self.embedder.probes([str(row["embed_text"]) for row in pending])
        vectors = [
            (str(r["claim_ref"]), str(r["text_sha256"]), p.vector)
            for r, p in zip(pending, probes, strict=True)
            if p.vector is not None
            and p.text == r["embed_text"]
            and p.embedder == self.embedder.identity.key
            and len(p.vector) == self.embedder.identity.dimensions * 2
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
        return bool(vectors)

    async def bulk_backfill(
        self,
        *,
        batch_size: int = 64,
        now_ms: int | None = None,
        resume: Mapping[str, Any] | None = None,
        checkpoint: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """One resumable keyset pass; encoding never holds a database transaction.

        The checkpoint advances only after the batch's vector transaction commits.
        Replaying its last batch after cancellation is safe and never re-extracts
        historical material. Live adoption atomically creates its own index row.
        """
        if not 32 <= batch_size <= 512:
            raise ValueError("news_claim_backfill_batch_invalid")
        if self.embedder is None:
            raise ValueError("news_claim_backfill_embedder_required")
        started = time.perf_counter()
        state = dict(resume or {})
        state.setdefault("as_of_ms", clock_ms() if now_ms is None else now_ms)
        state.setdefault("phase", "sent")
        state.setdefault("after", [0, "", 0])
        state.setdefault("projected", 0)
        state.setdefault("embedded", 0)
        state.setdefault("embedder", self.embedder.identity.key)
        embedder_key = self.embedder.identity.key
        after = state["after"]
        if (
            state["phase"] not in {"sent", "adopted", "done"}
            or type(state["as_of_ms"]) is not int
            or state["as_of_ms"] < 0
            or state["embedder"] != self.embedder.identity.key
            or not isinstance(after, (list, tuple))
            or len(after) != 3
            or type(after[0]) is not int
            or after[0] < 0
            or not isinstance(after[1], str)
            or type(after[2]) is not int
            or after[2] < 0
            or type(state["projected"]) is not int
            or state["projected"] < 0
            or type(state["embedded"]) is not int
            or state["embedded"] < 0
        ):
            raise ValueError("news_claim_backfill_checkpoint_invalid")
        # Freeze the window before even the first batch, including if its model
        # work is cancelled before any vector transaction commits.
        if checkpoint is not None:
            await checkpoint(dict(state))
        db_seconds = 0.0

        async def measured(operation: Awaitable[Any]) -> Any:
            nonlocal db_seconds
            before = time.perf_counter()
            try:
                return await operation
            finally:
                db_seconds += time.perf_counter() - before

        while state["phase"] != "done":
            rows = await measured(
                self.db.read(
                    "news_claim_bulk_read",
                    lambda r: r.news.claim_index.historical_batch(
                        phase=str(state["phase"]), after=state["after"], limit=batch_size, now_ms=int(state["as_of_ms"])
                    ),
                )
            )
            if not rows:
                state.update(phase="adopted" if state["phase"] == "sent" else "done", after=[0, "", 0])
                if checkpoint is not None:
                    await checkpoint(dict(state))
                continue
            await measured(self.db.tx("news_claim_bulk_project", partial(_project_rows, rows=rows)))
            missing = [row for row in rows if row["vector"] is None or row["embedder"] != self.embedder.identity.key]
            probes = await self.embedder.probes([str(row["claim"]["statement"]) for row in missing])
            vectors = [
                (claim.ref, row["text_sha256"], probe.vector)
                for row, probe in zip(missing, probes, strict=True)
                for claim in (Claim.model_validate(row["claim"]),)
                if probe.vector is not None
                and probe.text == claim.statement
                and probe.embedder == self.embedder.identity.key
                and len(probe.vector) == self.embedder.identity.dimensions * 2
            ]
            if len(vectors) != len(missing):
                # A unavailable runtime leaves NULL-vector facts and the previous
                # checkpoint intact, so an operator can resume this exact batch.
                raise ValueError("news_claim_backfill_embedding_unavailable")
            if vectors:
                await measured(
                    self.db.tx(
                        "news_claim_bulk_embed",
                        partial(_save_vectors, rows=vectors, embedder=embedder_key),
                    )
                )
            last = rows[-1]
            state.update(after=[last["cursor_ms"], last["cursor_id"], last["cursor_claim"]])
            state["projected"] += len(rows)
            state["embedded"] += len(vectors)
            if checkpoint is not None:
                await checkpoint(dict(state))
        await measured(self.db.tx("news_claim_bulk_analyze", lambda r: r.news.claim_index.analyze()))
        return {**state, "database_seconds": db_seconds, "elapsed_seconds": time.perf_counter() - started}
