"""The sole PostgreSQL adapter for proposition retrieval and persistent embedding work."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..claim_recall import (
    CALIBRATION,
    PRIOR_WINDOW_MS,
    RECEIPT_WINDOW_MS,
    Candidate,
    Probe,
    embed_text,
    lexical_text,
    numbers,
    prepare_rank,
    rank,
    structure_keys,
    text_sha,
)
from ..updates.contracts import Claim, EventUpdate, PriorClaim
from ..updates.identity import digest


def _requested(claims: Mapping[str, Claim]) -> list[dict[str, str]]:
    return [{"ref": c.ref, "sha": key.rsplit(":", 1)[1], "text": embed_text(c)} for key, c in claims.items()]


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
                 (claim_ref,text_sha256,event_id,first_available_at_ms,embed_text,lexical_text,numbers,structure_keys)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (claim_ref,text_sha256) DO NOTHING""",
            (
                claim.ref,
                text_sha(claim),
                event_id,
                claim.first_available_at_ms,
                embed_text(claim),
                lexical_text(claim),
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

    def claim_candidates(self, claims: Mapping[str, Claim], *, sources: Sequence[str] = ()) -> tuple[Candidate, ...]:
        """Frozen receipt claims stay frozen even if a later head changes their ref's text."""
        if not claims:
            return ()
        rows = self.conn.execute(
            """SELECT requested.ref,requested.sha,ci.vector,ci.embedder,
                      COALESCE(ci.structure_keys && %s::text[],false) AS same_source
                 FROM jsonb_to_recordset(%s::jsonb) AS requested(ref text,sha text,text text)
                 LEFT JOIN news_claim_index ci ON ci.claim_ref=requested.ref AND ci.text_sha256=requested.sha""",
            (
                list(sources),
                json.dumps(_requested(claims)),
            ),
            binary=True,
        ).fetchall()
        return tuple(
            Candidate(
                key=f"{r['ref']}:{r['sha']}",
                vector=None if r["vector"] is None else bytes(r["vector"]),
                embedder=r["embedder"],
                same_source=bool(r["same_source"]),
            )
            for r in rows
        )

    def lexical_scores(self, query_text: str, claims: Sequence[tuple[str, Claim]]) -> dict[str, float]:
        """Score only keys the shared dense guard allows onto the FTS route."""
        if not claims:
            return {}
        requested = [
            {"key": key, "ref": claim.ref, "sha": text_sha(claim), "text": lexical_text(claim)} for key, claim in claims
        ]
        rows = self.conn.execute(
            """SELECT requested.key,
                      ts_rank_cd(COALESCE(ci.lexical,to_tsvector('english',requested.text)),%s::tsquery,32)
                        AS lexical
                 FROM jsonb_to_recordset(%s::jsonb) AS requested(key text,ref text,sha text,text text)
                 LEFT JOIN news_claim_index ci ON ci.claim_ref=requested.ref AND ci.text_sha256=requested.sha""",
            (self._query(query_text), json.dumps(requested)),
            binary=True,
        ).fetchall()
        return {str(row["key"]): float(row["lexical"]) for row in rows}

    def receipt_candidates(
        self, intents: Sequence[str], *, sources: Sequence[str]
    ) -> tuple[tuple[Candidate, tuple[str, str, str]], ...]:
        """Rank projection of the exact frozen versions, before complete eligible-claim reads."""
        if not intents:
            return ()
        self.conn.execute("SET LOCAL jit = off")
        rows = self.conn.execute(
            """SELECT n.intent_id,c->>'ref' AS claim_ref,c->>'statement' AS statement,
                      ci.text_sha256,ci.vector,ci.embedder,
                      COALESCE(ci.structure_keys && %s::text[],false) AS same_source
                 FROM news_notifications n
                 CROSS JOIN LATERAL jsonb_array_elements(COALESCE(n.sent_claims,'[]'::jsonb)) c
                 LEFT JOIN LATERAL (
                   SELECT ci.text_sha256,ci.vector,ci.embedder,ci.structure_keys
                     FROM news_claim_index ci
                    WHERE ci.claim_ref=c->>'ref' AND ci.embed_text=c->>'statement' OFFSET 0
                 ) ci ON true
                WHERE n.intent_id=ANY(%s::text[])""",
            (list(sources), list(intents)),
            binary=True,
        ).fetchall()
        return tuple(
            (
                Candidate(
                    f"{r['intent_id']}:{r['claim_ref']}:{r['text_sha256'] or digest(r['statement'])}",
                    r["vector"],
                    r["embedder"],
                    same_source=bool(r["same_source"]),
                    group=str(r["intent_id"]),
                ),
                (str(r["intent_id"]), str(r["claim_ref"]), str(r["statement"])),
            )
            for r in rows
        )

    def prior(
        self,
        event_id: str,
        probe: Probe,
        *,
        lexical_query: str,
        now_ms: int,
        sources: Sequence[str],
        diagnostics: dict[str, Any] | None = None,
    ) -> tuple[PriorClaim, ...]:
        # The ordinary dense window owns current claims. The sent reserve owns
        # the exact frozen proposition the reader saw, even beyond that window
        # or after its Event adopted a different version. Both use this ranker.
        # The set-returning frozen projection overestimates row counts and can
        # otherwise spend more time compiling a JIT plan than reading the pool.
        self.conn.execute("SET LOCAL jit = off")
        rows = self.conn.execute(
            """WITH sent AS MATERIALIZED (
                 SELECT DISTINCT ON (n.event_id,c->>'ref',c->>'statement')
                        n.event_id,n.content_revision,n.intent_id,c->>'ref' AS claim_ref,
                        c->>'statement' AS statement
                   FROM news_notifications n
                   CROSS JOIN LATERAL jsonb_array_elements(COALESCE(n.sent_claims,'[]'::jsonb)) c
                  WHERE n.kind='update' AND n.state='sent' AND n.content_revision IS NOT NULL
                    AND n.settled_at_ms >= %s AND n.settled_at_ms < %s AND n.event_id<>%s
                    AND (c->>'first_available_at_ms')::bigint < %s
                  ORDER BY n.event_id,c->>'ref',c->>'statement',n.settled_at_ms DESC,n.intent_id
               )
               SELECT ci.claim_ref,ci.text_sha256,ci.event_id,ci.vector,ci.embedder,
                      ci.structure_keys && %s::text[] AS same_source,
                      NULL::text AS analysis_id,NULL::text AS content_revision,
                      ci.embed_text,NULL::text AS frozen_intent
                 FROM news_claim_index ci
                WHERE ci.event_id<>%s AND ci.first_available_at_ms >= %s
                  AND ci.first_available_at_ms < %s
               UNION ALL
               SELECT s.claim_ref,ci.text_sha256,s.event_id,ci.vector,ci.embedder,
                      COALESCE(ci.structure_keys && %s::text[],false),
                      NULL::text,s.content_revision,s.statement,s.intent_id
                 FROM sent s LEFT JOIN LATERAL (
                   SELECT ci.text_sha256,ci.vector,ci.embedder,ci.structure_keys
                     FROM news_claim_index ci
                    WHERE ci.claim_ref=s.claim_ref AND ci.embed_text=s.statement OFFSET 0
                 ) ci ON true""",
            (
                now_ms - RECEIPT_WINDOW_MS,
                now_ms,
                event_id,
                now_ms,
                list(sources),
                event_id,
                now_ms - PRIOR_WINDOW_MS,
                now_ms,
                list(sources),
            ),
            binary=True,
        ).fetchall()
        by_key = {}
        for row in rows:
            sha = row["text_sha256"] or digest(row["embed_text"])
            key = f"{row['claim_ref']}:{sha}"
            # Frozen receipt evidence wins when both windows contain the exact
            # same version. Claim groups prevent wording variants using two slots.
            if key not in by_key or row["frozen_intent"] is not None:
                by_key[key] = {**row, "text_sha256": sha}
        candidates = tuple(
            Candidate(
                key,
                None if row["vector"] is None else bytes(row["vector"]),
                row["embedder"],
                same_source=bool(row["same_source"]),
                sent=row["frozen_intent"] is not None,
                group=str(row["claim_ref"]),
            )
            for key, row in by_key.items()
        )
        prepared = prepare_rank(probe, candidates, "prior")
        # Every key that could enter any route is validated before route_n/k.
        # Stale wording can neither consume a slot nor hide a current version.
        eligible_rows = [by_key[key] for key in prepared.eligible_keys]
        selected_events = list({str(row["event_id"]) for row in eligible_rows if row["frozen_intent"] is None})
        documents = (
            self.conn.execute(
                """SELECT e.event_id,u.document FROM news_events e
                   JOIN news_analyses u ON u.analysis_id=e.current_analysis_id
                   WHERE e.event_id=ANY(%s::text[]) AND u.adopted_at_ms < %s""",
                (selected_events, now_ms),
                binary=True,
            ).fetchall()
            if selected_events
            else ()
        )
        heads = {str(row["event_id"]): EventUpdate.model_validate(row["document"]) for row in documents}
        intents = sorted({str(row["frozen_intent"]) for row in eligible_rows if row["frozen_intent"] is not None})
        frozen_rows = (
            self.conn.execute(
                """SELECT n.intent_id,c AS claim FROM news_notifications n
                   CROSS JOIN LATERAL jsonb_array_elements(n.sent_claims) c
                   WHERE n.intent_id=ANY(%s::text[])""",
                (intents,),
                binary=True,
            ).fetchall()
            if intents
            else ()
        )
        frozen = {
            (str(row["intent_id"]), claim.ref, embed_text(claim)): claim
            for row in frozen_rows
            for claim in (Claim.model_validate(row["claim"]),)
        }
        current = {
            (analysis, claim.ref, embed_text(claim)): claim
            for analysis, head in heads.items()
            for claim in head.current_claims
        }
        claims = {}
        for key in prepared.eligible_keys:
            row = by_key[key]
            occurrence = (
                str(row["frozen_intent"] or row["event_id"]),
                str(row["claim_ref"]),
                str(row["embed_text"]),
            )
            claim = (frozen if row["frozen_intent"] is not None else current).get(occurrence)
            if claim is not None:
                claims[key] = claim
        lexical = self.lexical_scores(
            lexical_query, [(key, claim) for key, claim in claims.items() if key in prepared.fts_eligible_keys]
        )
        ranking = rank(
            prepared,
            tuple(replace(c, lexical=lexical.get(c.key, 0.0)) for c in candidates if c.key in claims),
        )
        if diagnostics is not None:
            diagnostics.update(ranking.diagnostics())
        return tuple(
            PriorClaim(
                event_id=str(by_key[key]["event_id"]),
                content_revision=(
                    str(by_key[key]["content_revision"])
                    if by_key[key]["frozen_intent"] is not None
                    else heads[str(by_key[key]["event_id"])].content_revision
                ),
                claim=claims[key],
            )
            for hit in ranking.hits
            for key in (hit.member_key or hit.key,)
        )

    def pending(self, limit: int, *, now_ms: int) -> list[dict[str, Any]]:
        """Sent 48 h first, then 7 d, then 30 d; no volatile queue replaces these rows."""
        sent_keys = [f"{c.ref}:{text_sha(c)}" for _, c in self._sent_versions(now_ms=now_ms)]
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT ci.claim_ref,ci.text_sha256,ci.embed_text FROM news_claim_index ci
                WHERE (ci.vector IS NULL OR ci.embedder IS DISTINCT FROM %s)
                  AND ci.first_available_at_ms < %s
                  AND (ci.first_available_at_ms >= %s OR ci.claim_ref||':'||ci.text_sha256=ANY(%s::text[]))
                ORDER BY CASE WHEN ci.claim_ref||':'||ci.text_sha256=ANY(%s::text[]) THEN 0
                  WHEN ci.first_available_at_ms >= %s THEN 1 ELSE 2 END,
                  ci.first_available_at_ms DESC,ci.claim_ref,ci.text_sha256 LIMIT %s""",
                (
                    CALIBRATION.embedder.key,
                    now_ms,
                    now_ms - 30 * 86400_000,
                    sent_keys,
                    sent_keys,
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

    def status(self, *, now_ms: int) -> dict[str, Any]:
        sent_keys = [f"{c.ref}:{text_sha(c)}" for _, c in self._sent_versions(now_ms=now_ms)]
        row = self.conn.execute(
            """SELECT count(*) FILTER (WHERE vector IS NULL OR embedder IS DISTINCT FROM %s) AS pending,
                      count(*) FILTER (WHERE vector IS NOT NULL AND embedder=%s) AS ready
                 FROM news_claim_index
                WHERE first_available_at_ms < %s
                  AND (first_available_at_ms >= %s OR claim_ref||':'||text_sha256=ANY(%s::text[]))""",
            (CALIBRATION.embedder.key, CALIBRATION.embedder.key, now_ms, now_ms - 30 * 86400_000, sent_keys),
        ).fetchone()
        return {
            "recall_dense": "on" if row["ready"] and not row["pending"] else "degraded",
            "claim_index_pending": int(row["pending"]),
        }

    def backfill(self, *, limit: int, now_ms: int) -> int:
        """Bounded idempotent projection of adopted facts; never re-extract historical sources."""
        sent = self._sent_versions(now_ms=now_ms)
        sent_keys = [f"{c.ref}:{text_sha(c)}" for _, c in sent]
        present = {
            str(r["key"])
            for r in self.conn.execute(
                "SELECT claim_ref||':'||text_sha256 AS key FROM news_claim_index "
                "WHERE claim_ref||':'||text_sha256=ANY(%s::text[])",
                (sent_keys,),
            ).fetchall()
        }
        filled = 0
        for event_id, claim in sent:
            key = f"{claim.ref}:{text_sha(claim)}"
            if key not in present:
                self.index_claim(event_id, claim)
                present.add(key)
                filled += 1
                if filled >= limit:
                    return filled
        # Keep the correlated existence check an indexed claim lookup; an anti-join
        # can otherwise scan the complete claim index for every adopted document.
        rows = self.conn.execute(
            """SELECT u.event_id,u.document FROM news_analyses u
                WHERE u.adopted_at_ms >= %s AND u.document IS NOT NULL
                  AND EXISTS (SELECT 1 FROM jsonb_array_elements(u.document->'claims') c
                       WHERE NOT EXISTS (SELECT 1 FROM news_claim_index ci
                                          WHERE ci.claim_ref=c->>'ref'
                                            AND ci.embed_text=c->>'statement' OFFSET 0))
                ORDER BY u.adopted_at_ms DESC,u.analysis_id LIMIT %s""",
            (now_ms - 30 * 86400_000, limit - filled),
        ).fetchall()
        for row in rows:
            self.index_update(EventUpdate.model_validate(row["document"]))
        return filled + len(rows)

    def _sent_versions(self, *, now_ms: int) -> list[tuple[str, Claim]]:
        """Exact frozen versions in the receipt window, regardless of their original age."""
        rows = self.conn.execute(
            """SELECT DISTINCT ON (n.event_id,c->>'ref',c->>'statement') n.event_id,c AS claim
                 FROM news_notifications n
                 CROSS JOIN LATERAL jsonb_array_elements(COALESCE(n.sent_claims,'[]'::jsonb)) c
                WHERE n.kind='update' AND n.state='sent' AND n.content_revision IS NOT NULL
                  AND n.settled_at_ms >= %s AND n.settled_at_ms < %s
                  AND (c->>'first_available_at_ms')::bigint < %s
                ORDER BY n.event_id,c->>'ref',c->>'statement',n.settled_at_ms DESC,n.intent_id""",
            (now_ms - RECEIPT_WINDOW_MS, now_ms, now_ms),
        ).fetchall()
        return [(str(r["event_id"]), Claim.model_validate(r["claim"])) for r in rows]


def source_keys(source: Any) -> tuple[str, ...]:
    return (
        f"artifact:{source.publisher_id}:{source.artifact_id}",
        *(() if source.url is None else (f"url:{source.url}",)),
    )
