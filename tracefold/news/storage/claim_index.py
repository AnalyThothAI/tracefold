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
    PreparedRecall,
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

# The frozen, possibly unindexed receipt uses the same fields and order as lexical_text.
# This expression only consumes persisted JSON; no inferred aliases enter the FTS route.
_FROZEN_LEXICAL = """concat_ws(' ',c->>'statement',c->'fields'->>'subject',c->'fields'->>'action',
    c->'fields'->>'object',c->'fields'->>'speaker',
    (SELECT string_agg(concat_ws(' ',q->>'name',q->>'unit',q->>'value',q->>'period'),' ' ORDER BY ordinal)
       FROM jsonb_array_elements(COALESCE(c->'fields'->'quantities','[]'::jsonb))
            WITH ORDINALITY quantities(q,ordinal)))"""


def _requested(claims: Mapping[str, Claim]) -> list[dict[str, str]]:
    return [{"ref": c.ref, "sha": key.rsplit(":", 1)[1], "text": embed_text(c)} for key, c in claims.items()]


class ClaimIndexStorage:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def index_update(self, update: EventUpdate, *, probes: Mapping[str, Probe] | None = None) -> None:
        evidence = {e.ref: e.source for e in update.evidence}
        for claim in update.claims:
            sources = tuple(
                key
                for citation in claim.citations
                if citation.evidence_ref in evidence
                for key in source_keys(evidence[citation.evidence_ref])
            )
            self.index_claim(update.event_id, claim, sources=sources, probe=(probes or {}).get(embed_text(claim)))

    def index_claim(
        self, event_id: str, claim: Claim, *, sources: Sequence[str] = (), probe: Probe | None = None
    ) -> None:
        usable = (
            probe is not None
            and probe.text == embed_text(claim)
            and probe.embedder == CALIBRATION.embedder.key
            and probe.vector is not None
            and len(probe.vector) == CALIBRATION.embedder.dimensions * 2
        )
        self.conn.execute(
            """INSERT INTO news_claim_index
                 (claim_ref,text_sha256,event_id,first_available_at_ms,embed_text,lexical_text,numbers,structure_keys,
                  vector,embedder)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (claim_ref,text_sha256) DO UPDATE
                 SET vector=CASE WHEN EXCLUDED.vector IS NOT NULL AND (news_claim_index.vector IS NULL
                                      OR news_claim_index.embedder IS DISTINCT FROM EXCLUDED.embedder)
                                 THEN EXCLUDED.vector ELSE news_claim_index.vector END,
                     embedder=CASE WHEN EXCLUDED.vector IS NOT NULL AND (news_claim_index.vector IS NULL
                                        OR news_claim_index.embedder IS DISTINCT FROM EXCLUDED.embedder)
                                   THEN EXCLUDED.embedder ELSE news_claim_index.embedder END,
                     structure_keys=ARRAY(SELECT DISTINCT key FROM unnest(
                         news_claim_index.structure_keys||EXCLUDED.structure_keys) keys(key) ORDER BY key)
               WHERE (EXCLUDED.vector IS NOT NULL AND (news_claim_index.vector IS NULL
                         OR news_claim_index.embedder IS DISTINCT FROM EXCLUDED.embedder))
                  OR NOT news_claim_index.structure_keys @> EXCLUDED.structure_keys""",
            (
                claim.ref,
                text_sha(claim),
                event_id,
                claim.first_available_at_ms,
                embed_text(claim),
                lexical_text(claim),
                list(numbers(embed_text(claim))),
                sorted(set((*structure_keys(claim), *sources))),
                probe.vector if usable and probe is not None else None,
                probe.embedder if usable and probe is not None else None,
            ),
        )

    def query_probes(self, claims: Sequence[Claim]) -> dict[str, Probe]:
        """The adopted exact wording owns the query vector, including on head changes."""
        if not claims:
            return {}
        requested = [{"ref": c.ref, "sha": text_sha(c), "text": embed_text(c)} for c in claims]
        rows = self.conn.execute(
            """SELECT requested.ref,requested.text,ci.vector,ci.embedder
                 FROM jsonb_to_recordset(%s::jsonb) requested(ref text,sha text,text text)
                 LEFT JOIN news_claim_index ci ON ci.claim_ref=requested.ref AND ci.text_sha256=requested.sha""",
            (json.dumps(requested),),
            binary=True,
        ).fetchall()
        return {
            str(row["ref"]): Probe(
                str(row["text"]),
                bytes(row["vector"])
                if row["vector"] is not None
                and row["embedder"] == CALIBRATION.embedder.key
                and len(row["vector"]) == CALIBRATION.embedder.dimensions * 2
                else None,
                CALIBRATION.embedder.key,
            )
            for row in rows
        }

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

    def lexical_pool_scores(
        self, query_text: str, rows: Mapping[str, Mapping[str, Any]], *, prepared: PreparedRecall
    ) -> dict[str, float]:
        """FTS filters exact eligible projections before loading any complete document.

        Eligibility was verified against the adopted head or frozen receipt before
        this route's top-n. Missing vectors use the calibrated per-candidate floor.
        """
        requested = [
            {"key": key, "ref": row["claim_ref"], "sha": row["text_sha256"], "text": row["lexical_text"]}
            for key, row in rows.items()
            if key in prepared.fts_eligible_keys
        ]
        if not requested:
            return {}
        cuts = CALIBRATION.prior if prepared.consumer == "prior" else CALIBRATION.receipt
        query = self._query(query_text)
        scored = self.conn.execute(
            """WITH requested AS MATERIALIZED (
                 SELECT * FROM jsonb_to_recordset(%s::jsonb) requested(key text,ref text,sha text,text text)
               ), scored AS (
                 SELECT r.key,ts_rank_cd(ci.lexical,%s::tsquery,32) AS lexical
                   FROM news_claim_index ci JOIN requested r ON ci.claim_ref=r.ref AND ci.text_sha256=r.sha
                  WHERE ci.lexical @@ %s::tsquery
                 UNION ALL
                 SELECT r.key,ts_rank_cd(to_tsvector('english',r.text),%s::tsquery,32)
                   FROM requested r LEFT JOIN news_claim_index ci ON ci.claim_ref=r.ref AND ci.text_sha256=r.sha
                  WHERE ci.claim_ref IS NULL AND to_tsvector('english',r.text) @@ %s::tsquery
               ) SELECT key,lexical FROM scored
                  WHERE lexical > CASE WHEN key=ANY(%s::text[]) THEN %s ELSE %s END
                  ORDER BY lexical DESC,key LIMIT %s""",
            (
                json.dumps(requested),
                query,
                query,
                query,
                query,
                list(prepared.dense),
                cuts.lexical_floor,
                cuts.degraded_lexical_floor,
                CALIBRATION.route_n,
            ),
        ).fetchall()
        return {str(row["key"]): float(row["lexical"]) for row in scored}

    def receipt_pool(self, intents: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Read exact frozen projections once for all claims in a notification snapshot."""
        if not intents:
            return {}
        self.conn.execute("SET LOCAL jit = off")
        rows = self.conn.execute(
            f"""SELECT n.intent_id,c->>'ref' AS claim_ref,c->>'statement' AS statement,
                      ci.text_sha256,ci.vector,ci.embedder,
                      COALESCE(ci.structure_keys,'{{}}'::text[]) AS structure_keys,
                      {_FROZEN_LEXICAL} AS lexical_text
                 FROM news_notifications n
                 CROSS JOIN LATERAL jsonb_array_elements(COALESCE(n.sent_claims,'[]'::jsonb)) c
                 LEFT JOIN LATERAL (
                   SELECT ci.text_sha256,ci.vector,ci.embedder,ci.structure_keys
                     FROM news_claim_index ci
                    WHERE ci.claim_ref=c->>'ref' AND ci.embed_text=c->>'statement' OFFSET 0
                 ) ci ON true
                WHERE n.intent_id=ANY(%s::text[])""",  # noqa: S608 - code-owned frozen projection
            (list(intents),),
            binary=True,
        ).fetchall()
        return {
            f"{row['intent_id']}:{row['claim_ref']}:{row['text_sha256'] or digest(row['statement'])}": {
                **dict(row),
                "text_sha256": row["text_sha256"] or digest(row["statement"]),
            }
            for row in rows
        }

    def prior_pool(self, event_id: str, *, now_ms: int, sources: Sequence[str]) -> tuple[dict[str, Any], ...]:
        # The ordinary dense window owns current claims. The sent reserve owns
        # the exact frozen proposition the reader saw, even beyond that window
        # or after its Event adopted a different version. Both use this ranker.
        # The set-returning frozen projection overestimates row counts and can
        # otherwise spend more time compiling a JIT plan than reading the pool.
        self.conn.execute("SET LOCAL jit = off")
        rows = self.conn.execute(
            f"""WITH sent AS MATERIALIZED (
                 SELECT DISTINCT ON (n.event_id,c->>'ref',c->>'statement')
                        n.event_id,n.content_revision,n.intent_id,c->>'ref' AS claim_ref,
                        c->>'statement' AS statement,{_FROZEN_LEXICAL} AS lexical_text
                   FROM news_notifications n
                   CROSS JOIN LATERAL jsonb_array_elements(COALESCE(n.sent_claims,'[]'::jsonb)) c
                  WHERE n.kind='update' AND n.state='sent' AND n.content_revision IS NOT NULL
                    AND n.settled_at_ms >= %s AND n.settled_at_ms < %s AND n.event_id<>%s
                    AND (c->>'first_available_at_ms')::bigint < %s
                  ORDER BY n.event_id,c->>'ref',c->>'statement',n.settled_at_ms DESC,n.intent_id
               )
               SELECT ci.claim_ref,ci.text_sha256,ci.event_id,ci.vector,ci.embedder,
                      ci.structure_keys && %s::text[] AS same_source,
                      u.analysis_id,u.content_revision,
                      ci.embed_text,ci.lexical_text,NULL::text AS frozen_intent
                 FROM news_claim_index ci
                 JOIN news_events e ON e.event_id=ci.event_id
                 JOIN news_analyses u ON u.analysis_id=e.current_analysis_id
                WHERE ci.event_id<>%s AND ci.first_available_at_ms >= %s
                  AND ci.first_available_at_ms < %s
                  AND u.adopted_at_ms < %s
                  AND u.document->'claims' @> jsonb_build_array(
                        jsonb_build_object('ref',ci.claim_ref,'statement',ci.embed_text))
                  AND NOT (COALESCE(u.document->'retired_claim_refs','[]'::jsonb) ? ci.claim_ref)
                  AND NOT (COALESCE(u.document->'superseded_claim_refs','[]'::jsonb) ? ci.claim_ref)
               UNION ALL
               SELECT s.claim_ref,ci.text_sha256,s.event_id,ci.vector,ci.embedder,
                      COALESCE(ci.structure_keys && %s::text[],false),
                      NULL::text,s.content_revision,s.statement,s.lexical_text,s.intent_id
                 FROM sent s LEFT JOIN LATERAL (
                   SELECT ci.text_sha256,ci.vector,ci.embedder,ci.structure_keys
                     FROM news_claim_index ci
                    WHERE ci.claim_ref=s.claim_ref AND ci.embed_text=s.statement OFFSET 0
                 ) ci ON true""",  # noqa: S608 - code-owned frozen projection
            (
                now_ms - RECEIPT_WINDOW_MS,
                now_ms,
                event_id,
                now_ms,
                list(sources),
                event_id,
                now_ms - PRIOR_WINDOW_MS,
                now_ms,
                now_ms,
                list(sources),
            ),
            binary=True,
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def prior(
        self,
        event_id: str,
        probe: Probe,
        *,
        lexical_query: str,
        now_ms: int,
        sources: Sequence[str],
        diagnostics: dict[str, Any] | None = None,
        pool: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[PriorClaim, ...]:
        rows = self.prior_pool(event_id, now_ms=now_ms, sources=sources) if pool is None else pool
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
        lexical = self.lexical_pool_scores(lexical_query, by_key, prepared=prepared)
        ranking = rank(prepared, tuple(replace(c, lexical=lexical.get(c.key, 0.0)) for c in candidates))
        # The pool already verified exact current/frozen versions before route_n/k.
        # Read only selected immutable adopted documents, including if the live head changed.
        selected_keys = tuple(hit.member_key or hit.key for hit in ranking.hits)
        eligible_rows = [by_key[key] for key in selected_keys]
        selected_analyses = list({str(row["analysis_id"]) for row in eligible_rows if row["frozen_intent"] is None})
        documents = (
            self.conn.execute(
                """SELECT u.event_id,u.document FROM news_analyses u
                   WHERE u.analysis_id=ANY(%s::text[]) AND u.adopted_at_ms < %s""",
                (selected_analyses, now_ms),
                binary=True,
            ).fetchall()
            if selected_analyses
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
        for key in selected_keys:
            row = by_key[key]
            occurrence = (
                str(row["frozen_intent"] or row["event_id"]),
                str(row["claim_ref"]),
                str(row["embed_text"]),
            )
            claim = (frozen if row["frozen_intent"] is not None else current).get(occurrence)
            if claim is not None:
                claims[key] = claim
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
            if key in claims
        )

    def pending(self, limit: int, *, now_ms: int) -> list[dict[str, Any]]:
        """Read durable missing rows directly; never rescan adopted historical JSON.

        The NULL-vector partial index handles ordinary repairs. A nonempty queue
        enables one frozen-receipt projection, including versions older than the
        normal window; never one complete receipt scan per missing claim.
        """
        return [
            dict(r)
            for r in self.conn.execute(
                """WITH pending_gate AS MATERIALIZED (
                     SELECT 1 FROM news_claim_index
                      WHERE (vector IS NULL OR embedder IS DISTINCT FROM %s) AND first_available_at_ms < %s LIMIT 1
                 ), sent AS MATERIALIZED (
                     SELECT DISTINCT ci.claim_ref,ci.text_sha256,ci.embed_text,ci.first_available_at_ms,0 AS priority
                       FROM news_notifications n
                       CROSS JOIN LATERAL jsonb_array_elements(COALESCE(n.sent_claims,'[]'::jsonb)) c
                       JOIN LATERAL (
                           SELECT ci.claim_ref,ci.text_sha256,ci.embed_text,ci.first_available_at_ms
                             FROM news_claim_index ci WHERE ci.claim_ref=c->>'ref' AND ci.embed_text=c->>'statement'
                              AND (ci.vector IS NULL OR ci.embedder IS DISTINCT FROM %s)
                              AND ci.first_available_at_ms < %s OFFSET 0
                       ) ci ON true
                      WHERE EXISTS (SELECT 1 FROM pending_gate)
                        AND n.kind='update' AND n.state='sent' AND n.content_revision IS NOT NULL
                        AND n.settled_at_ms >= %s AND n.settled_at_ms < %s
                      ORDER BY ci.first_available_at_ms DESC,ci.claim_ref,ci.text_sha256 LIMIT %s
                 ), recent AS MATERIALIZED (
                     SELECT ci.claim_ref,ci.text_sha256,ci.embed_text,ci.first_available_at_ms,
                            CASE WHEN ci.first_available_at_ms >= %s THEN 1 ELSE 2 END AS priority
                       FROM news_claim_index ci
                      WHERE (ci.vector IS NULL OR ci.embedder IS DISTINCT FROM %s)
                        AND ci.first_available_at_ms >= %s AND ci.first_available_at_ms < %s
                      ORDER BY ci.first_available_at_ms DESC,ci.claim_ref,ci.text_sha256 LIMIT %s
                 ), merged AS (
                     SELECT DISTINCT ON (claim_ref,text_sha256) * FROM (
                         SELECT * FROM sent UNION ALL SELECT * FROM recent
                     ) candidates ORDER BY claim_ref,text_sha256,priority
                 ) SELECT claim_ref,text_sha256,embed_text FROM merged
                   ORDER BY priority,first_available_at_ms DESC,claim_ref,text_sha256 LIMIT %s""",
                (
                    CALIBRATION.embedder.key,
                    now_ms,
                    CALIBRATION.embedder.key,
                    now_ms,
                    now_ms - RECEIPT_WINDOW_MS,
                    now_ms,
                    limit,
                    now_ms - PRIOR_WINDOW_MS,
                    CALIBRATION.embedder.key,
                    now_ms - 30 * 86400_000,
                    now_ms,
                    limit,
                    limit,
                ),
            ).fetchall()
        ]

    def historical_batch(self, *, phase: str, after: Sequence[Any], limit: int, now_ms: int) -> list[dict[str, Any]]:
        """Keyset over exact historical claims, including all claims in large documents."""
        if phase == "sent":
            relation = "news_notifications n"
            stamp, row_id, claims = "n.settled_at_ms", "n.intent_id", "n.sent_claims"
            conditions = "n.kind='update' AND n.state='sent' AND n.content_revision IS NOT NULL"
            window = RECEIPT_WINDOW_MS
            sources = "'{}'::text[]"
            event = "n.event_id"
        elif phase == "adopted":
            relation = "news_analyses n"
            stamp, row_id, claims = "n.adopted_at_ms", "n.analysis_id", "n.document->'claims'"
            conditions = "n.document IS NOT NULL"
            window = 30 * 86400_000
            sources = """ARRAY(
                SELECT key FROM jsonb_array_elements(n.document->'evidence') e
                CROSS JOIN LATERAL (VALUES
                    ('artifact:'||(e->'source'->>'publisher_id')||':'||(e->'source'->>'artifact_id')),
                    ('url:'||(e->'source'->>'url'))
                ) keys(key)
                WHERE key IS NOT NULL AND EXISTS (SELECT 1 FROM jsonb_array_elements(c->'citations') citation
                                                  WHERE citation->>'evidence_ref'=e->>'ref'))"""
            event = "n.event_id"
        else:
            raise ValueError("news_claim_backfill_phase_unknown")
        # Relation/column names above are code-owned; cursor and window are bound values.
        rows = self.conn.execute(
            f"""WITH documents AS MATERIALIZED (
                 SELECT n.* FROM {relation}
                  WHERE {conditions} AND {stamp} >= %s AND {stamp} < %s
                    AND ({stamp},{row_id}) >= (%s,%s)
                    AND EXISTS (
                        SELECT 1 FROM jsonb_array_elements(COALESCE({claims},'[]'::jsonb))
                             WITH ORDINALITY eligible(c,ordinal)
                         WHERE (c->>'first_available_at_ms')::bigint < %s
                           AND ordinal > CASE WHEN {stamp}=%s AND {row_id}=%s THEN %s ELSE 0 END)
                  ORDER BY {stamp},{row_id} LIMIT %s
               ) SELECT {stamp} AS cursor_ms,{row_id} AS cursor_id,ordinal AS cursor_claim,
                       {event} AS event_id,c AS claim,{sources} AS sources,ci.vector,ci.embedder
                  FROM documents n
                  CROSS JOIN LATERAL jsonb_array_elements(COALESCE({claims},'[]'::jsonb))
                       WITH ORDINALITY frozen(c,ordinal)
                  LEFT JOIN LATERAL (SELECT ci.vector,ci.embedder FROM news_claim_index ci
                                      WHERE ci.claim_ref=c->>'ref' AND ci.embed_text=c->>'statement'
                                      OFFSET 0) ci ON true
                 WHERE (c->>'first_available_at_ms')::bigint < %s
                   AND ({stamp},{row_id},ordinal) > (%s,%s,%s)
                 ORDER BY {stamp},{row_id},ordinal LIMIT %s""",  # noqa: S608 - code-owned identifiers only
            (now_ms - window, now_ms, *after[:2], now_ms, *after, limit, now_ms, *after, limit),
            binary=True,
        ).fetchall()
        return [{**dict(row), "text_sha256": digest(row["claim"]["statement"])} for row in rows]

    def project_batch(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            self.index_claim(str(row["event_id"]), Claim.model_validate(row["claim"]), sources=row["sources"])

    def analyze(self) -> None:
        self.conn.execute("ANALYZE news_claim_index")

    def save_vectors(self, rows: Sequence[tuple[str, str, bytes]], *, embedder: str) -> None:
        for ref, sha, vector in rows:
            self.conn.execute(
                "UPDATE news_claim_index SET vector=%s,embedder=%s WHERE claim_ref=%s AND text_sha256=%s "
                "AND (vector IS NULL OR embedder IS DISTINCT FROM %s)",
                (vector, embedder, ref, sha, embedder),
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
