"""The sole PostgreSQL adapter for proposition retrieval and persistent embedding work."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..claim_recall import (
    CALIBRATION,
    PRIOR_WINDOW_MS,
    RECEIPT_WINDOW_MS,
    Candidate,
    Probe,
    embed_text,
    numbers,
    rank,
    structure_keys,
    text_sha,
)
from ..updates.contracts import Claim, EventUpdate, PriorClaim


class ClaimIndexStorage:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def index_update(self, update: EventUpdate) -> None:
        evidence = {e.ref: e.source for e in update.evidence}
        for claim in update.claims:
            sources = tuple(
                key
                for citation in claim.citations
                if citation.evidence_ref in evidence
                for key in source_keys(evidence[citation.evidence_ref])
            )
            self.index_claim(update.event_id, claim, sources=sources)

    def index_claim(self, event_id: str, claim: Claim, *, sources: Sequence[str] = ()) -> None:
        self.conn.execute(
            """INSERT INTO news_claim_index
                 (claim_ref,text_sha256,event_id,first_available_at_ms,embed_text,numbers,structure_keys)
               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (claim_ref,text_sha256) DO NOTHING""",
            (
                claim.ref,
                text_sha(claim),
                event_id,
                claim.first_available_at_ms,
                embed_text(claim),
                list(numbers(embed_text(claim))),
                sorted(set((*structure_keys(claim), *sources))),
            ),
        )

    def _query(self, text: str) -> Any:
        # PostgreSQL alone tokenizes FTS on both sides. Quote every lexeme before OR;
        # neither a model string nor a provider text can become tsquery syntax.
        return self.conn.execute(
            """SELECT COALESCE(string_agg(quote_literal(term),' | '),'')::tsquery AS query
                 FROM unnest(tsvector_to_array(to_tsvector('english',%s))) term""",
            (text,),
        ).fetchone()["query"]

    def claim_candidates(
        self, claims: Sequence[Claim], probe: Probe, *, sources: Sequence[str] = ()
    ) -> tuple[Candidate, ...]:
        """Frozen receipt claims stay frozen even if a later head changes their ref's text."""
        if not claims:
            return ()
        rows = self.conn.execute(
            """SELECT requested.ref,requested.sha,ci.vector,ci.embedder,
                      COALESCE(ci.structure_keys && %s::text[],false) AS same_source,
                      ts_rank_cd(to_tsvector('english',requested.text),%s::tsquery,32) AS lexical
                 FROM unnest(%s::text[],%s::text[],%s::text[]) AS requested(ref,sha,text)
                 LEFT JOIN news_claim_index ci ON ci.claim_ref=requested.ref AND ci.text_sha256=requested.sha""",
            (
                list(sources),
                self._query(probe.text),
                [c.ref for c in claims],
                [text_sha(c) for c in claims],
                [embed_text(c) for c in claims],
            ),
        ).fetchall()
        return tuple(
            Candidate(
                key=f"{r['ref']}:{r['sha']}",
                vector=None if r["vector"] is None else bytes(r["vector"]),
                embedder=r["embedder"],
                lexical=float(r["lexical"]),
                same_source=bool(r["same_source"]),
            )
            for r in rows
        )

    def prior(self, event_id: str, probe: Probe, *, now_ms: int, sources: Sequence[str]) -> tuple[PriorClaim, ...]:
        # Fetch one bounded vector window. Only current adopted propositions are
        # comparisons; historical versions remain available for frozen receipts.
        rows = self.conn.execute(
            """SELECT ci.claim_ref,ci.text_sha256,ci.event_id,ci.vector,ci.embedder,
                      ci.structure_keys && %s::text[] AS same_source,
                      ts_rank_cd(ci.lexical,%s::tsquery,32) AS lexical,
                      EXISTS (SELECT 1 FROM news_notifications n
                               WHERE n.state='sent' AND n.kind='update'
                                 AND n.settled_at_ms >= %s AND n.settled_at_ms < %s
                                 AND n.claim_refs ? ci.claim_ref) AS sent
                 FROM news_claim_index ci JOIN news_events e ON e.event_id=ci.event_id
                 JOIN news_analyses u ON u.analysis_id=e.current_analysis_id
                WHERE ci.event_id<>%s AND ci.first_available_at_ms >= %s
                  AND ci.first_available_at_ms < %s AND u.adopted_at_ms < %s
                  AND EXISTS (SELECT 1 FROM jsonb_array_elements(u.document->'claims') c
                               WHERE c->>'ref'=ci.claim_ref AND c->>'statement'=ci.embed_text
                                 AND NOT COALESCE(u.document->'retired_claim_refs','[]'::jsonb) ? ci.claim_ref
                                 AND NOT COALESCE(u.document->'superseded_claim_refs','[]'::jsonb) ? ci.claim_ref)""",
            (
                list(sources),
                self._query(probe.text),
                now_ms - RECEIPT_WINDOW_MS,
                now_ms,
                event_id,
                now_ms - PRIOR_WINDOW_MS,
                now_ms,
                now_ms,
            ),
        ).fetchall()
        candidates = []
        by_key = {}
        for row in rows:
            key = f"{row['claim_ref']}:{row['text_sha256']}"
            by_key[key] = row
            candidates.append(
                Candidate(
                    key=key,
                    vector=None if row["vector"] is None else bytes(row["vector"]),
                    embedder=row["embedder"],
                    lexical=float(row["lexical"]),
                    sent=bool(row["sent"]),
                    same_source=bool(row["same_source"]),
                )
            )
        ranked = rank(probe, candidates, "prior").hits
        selected_events = list({str(by_key[h.key]["event_id"]) for h in ranked})
        documents = (
            self.conn.execute(
                """SELECT e.event_id,u.document FROM news_events e JOIN news_analyses u
                 ON u.analysis_id=e.current_analysis_id WHERE e.event_id=ANY(%s::text[])""",
                (selected_events,),
            ).fetchall()
            if selected_events
            else ()
        )
        heads = {str(r["event_id"]): EventUpdate.model_validate(r["document"]) for r in documents}
        models = []
        for hit in ranked:
            row = by_key[hit.key]
            head = heads[str(row["event_id"])]
            claim = next(
                (c for c in head.current_claims if c.ref == row["claim_ref"] and text_sha(c) == row["text_sha256"]),
                None,
            )
            if claim is not None:
                models.append(PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim))
        return tuple(models)

    def pending(self, limit: int, *, now_ms: int) -> list[dict[str, Any]]:
        """Sent 48 h first, then 7 d, then 30 d; no volatile queue replaces these rows."""
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT ci.claim_ref,ci.text_sha256,ci.embed_text FROM news_claim_index ci
                WHERE (ci.vector IS NULL OR ci.embedder IS DISTINCT FROM %s)
                  AND ci.first_available_at_ms >= %s
                ORDER BY CASE WHEN EXISTS (
                    SELECT 1 FROM news_notifications n WHERE n.kind='update' AND n.state='sent'
                    AND n.settled_at_ms >= %s AND n.claim_refs ? ci.claim_ref) THEN 0
                  WHEN ci.first_available_at_ms >= %s THEN 1 ELSE 2 END,
                  ci.first_available_at_ms DESC,ci.claim_ref,ci.text_sha256 LIMIT %s""",
                (
                    CALIBRATION.embedder.key,
                    now_ms - 30 * 86400_000,
                    now_ms - RECEIPT_WINDOW_MS,
                    now_ms - PRIOR_WINDOW_MS,
                    limit,
                ),
            ).fetchall()
        ]

    def save_vectors(self, rows: Sequence[tuple[str, str, bytes]], *, embedder: str) -> None:
        for ref, sha, vector in rows:
            self.conn.execute(
                "UPDATE news_claim_index SET vector=%s,embedder=%s WHERE claim_ref=%s AND text_sha256=%s",
                (vector, embedder, ref, sha),
            )

    def status(self) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT count(*) FILTER (WHERE vector IS NULL OR embedder IS DISTINCT FROM %s) AS pending,
                      count(*) FILTER (WHERE vector IS NOT NULL AND embedder=%s) AS ready
                 FROM news_claim_index""",
            (CALIBRATION.embedder.key, CALIBRATION.embedder.key),
        ).fetchone()
        return {
            "recall_dense": "on" if row["ready"] and not row["pending"] else "degraded",
            "claim_index_pending": int(row["pending"]),
        }

    def backfill(self, *, limit: int, now_ms: int) -> int:
        """Bounded idempotent projection of adopted facts; never re-extract historical sources."""
        rows = self.conn.execute(
            """SELECT u.event_id,u.document FROM news_analyses u
                WHERE u.adopted_at_ms >= %s AND u.document IS NOT NULL
                  AND EXISTS (SELECT 1 FROM jsonb_array_elements(u.document->'claims') c
                       WHERE NOT EXISTS (SELECT 1 FROM news_claim_index ci
                                          WHERE ci.claim_ref=c->>'ref'
                                            AND ci.embed_text=c->>'statement'))
                ORDER BY u.adopted_at_ms DESC LIMIT %s""",
            (now_ms - 30 * 86400_000, limit),
        ).fetchall()
        for row in rows:
            self.index_update(EventUpdate.model_validate(row["document"]))
        return len(rows)


def source_keys(source: Any) -> tuple[str, ...]:
    return (
        f"artifact:{source.publisher_id}:{source.artifact_id}",
        *(() if source.url is None else (f"url:{source.url}",)),
    )
