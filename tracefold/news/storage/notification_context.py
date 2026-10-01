"""One consistent reader context for snapshots and both send-permission checks.

Commands use the caller's existing transaction; no external I/O or independent commit.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from ..entities import ADDRESS_PATTERN, CRYPTO_QUOTE_SUFFIXES, RELATED_ASSET_ALIASES, commodity_name_patterns
from ..notifications.contracts import NEWS_CHANNEL, DeliveredText
from ..notifications.novelty import ClaimLink, LinkedReceipt, current_links, reader_novelty
from ..notifications.recall import (
    LEXICAL_DF_MAX,
    LEXICAL_SHARED_MIN,
    LINKED_RECEIPT_WINDOW_MS,
    RECALL_WINDOW_MS,
    ROUTE_CANDIDATES_MAX,
    WORD_PATTERN,
    ClaimRecallQuery,
    RecallCandidate,
    RouteEvidence,
    query_for_claim,
    reader_context_revision,
    select_for_claim,
)
from ..source_contracts import classify_source_contracts
from ..updates.assembly import different_listing_assets
from ..updates.contracts import Claim, EventUpdate
from ..updates.identity import digest
from .notification_rows import NOTIFY_JOBS_SQL, UPDATE_RECEIPTS_SQL
from .semantic_updates import SemanticUpdateStorage
from .sql_values import _dumps

_RECEIPT_COLUMNS: Final = (
    "d.intent_id, d.event_id, d.kind, d.body, d.payload_sha256, d.settled_at_ms, "
    "d.receipt, d.card, d.history_context, d.claim_refs"
)


def linked_refs(update: EventUpdate, invalidated: Iterable[str] = ()) -> set[str]:
    """The active claims of an update and their antecedents: what a linked receipt carried."""

    inactive = set(update.retired_claim_refs) | set(update.superseded_claim_refs) | set(invalidated)
    return {ref for claim in update.claims if claim.ref not in inactive for ref in (claim.ref, *claim.antecedent_refs)}


def delivered_text(row: Mapping[str, Any]) -> DeliveredText | None:
    """One sent (or ambiguous) receipt with a provably exact frozen body the reader may have read."""

    body = row.get("body")
    payload_sha256 = row.get("payload_sha256")
    # Malformed or incomplete receipts cannot prove exact reader coverage.
    if not isinstance(body, str) or not body or not isinstance(payload_sha256, str) or digest(body) != payload_sha256:
        return None
    receipt = row.get("receipt") or {}
    message_id = None
    if isinstance(receipt, Mapping):
        value = receipt.get("provider_message_id", receipt.get("message_id"))
        message_id = None if value is None else str(value)
    state = str(row.get("state") or "sent")
    if state not in {"sent", "ambiguous"}:
        return None
    return DeliveredText(
        intent_id=str(row["intent_id"]),
        channel=NEWS_CHANNEL,
        state="sent" if state == "sent" else "ambiguous",
        body=body,
        payload_sha256=payload_sha256,
        received_at_ms=int(row["settled_at_ms"]) if state == "sent" else None,
        provider_message_id=message_id,
    )


def listing_compatible_links(links: Iterable[ClaimLink], claims: Mapping[str, Claim]) -> tuple[ClaimLink, ...]:
    """Keep latest assertions except links joining provably different quoted crypto listings.

    Missing either original claim is unknown. Select the latest assertion before checking its evidence:
    discarding a bad new link must not revive an older assertion about that pair. Corrections stay intact.
    """

    return tuple(
        link
        for link in current_links(links)
        if link.relation not in {"equivalent", "adds_information", "real_world_change"}
        or link.current_ref not in claims
        or link.previous_ref not in claims
        or not different_listing_assets(claims[link.current_ref], claims[link.previous_ref])
    )


log = logging.getLogger("tracefold.news")


class NotificationContextStorage:
    def __init__(self, conn: Any, *, updates: SemanticUpdateStorage) -> None:
        self.conn = conn
        self.updates = updates

    def invalidated_claim_refs(self, refs: Sequence[str], *, as_of_ms: int) -> list[str]:
        """Read invalidations of this exact adopted head at the reader's snapshot stamp."""
        if not refs:
            return []
        rows = self.conn.execute(
            """
            SELECT DISTINCT change->>'previous_ref' AS ref
              FROM news_event_updates u
              CROSS JOIN LATERAL jsonb_array_elements(u.document->'changes') change
             WHERE u.adopted_at_ms < %s
               AND jsonb_path_query_array(u.document, '$.changes[*].previous_ref') ?| %s::text[]
               AND change->>'previous_ref'=ANY(%s::text[])
               AND change->>'relation' IN ('corrects','real_world_change')
            """,
            (int(as_of_ms), list(refs), list(refs)),
        ).fetchall()
        return sorted(str(row["ref"]) for row in rows)

    def _unsettled_claim_refs(self, event_id: str) -> tuple[list[str], list[str]]:
        """This Event's claims in sends still in flight, and in sends whose outcome is ambiguous."""

        rows = self.conn.execute(
            f"""
            SELECT state, claim_refs FROM ({UPDATE_RECEIPTS_SQL})
             WHERE event_id = %s AND kind = 'update' AND state IN ('sending', 'ambiguous')
            """,  # noqa: S608 -- only code-owned SQL projections; values stay bound.
            (event_id,),
        ).fetchall()
        return (
            sorted({str(ref) for row in rows if row["state"] == "sending" for ref in row["claim_refs"] or ()}),
            sorted({str(ref) for row in rows if row["state"] == "ambiguous" for ref in row["claim_refs"] or ()}),
        )

    def _recall_receipt_rows(self, queries: Sequence[ClaimRecallQuery], *, now_ms: int) -> list[dict[str, Any]]:
        """Batch both bounded routes over the 48 h receipt window; keep the querying claim ref."""

        if not queries:
            return []
        query_rows = [
            {
                "ref": query.ref,
                # Retrieval spellings plus commodity name patterns; SQL uses the same owned features
                # as asset_retrieval_symbols, without promoting overlap to exact identity.
                "assets": [
                    {
                        "symbol": symbol,
                        "market_type": market,
                        "role": role,
                        "patterns": list(commodity_name_patterns(symbol)) if market == "commodity" else [],
                    }
                    for role, values in (("primary", query.primary_assets), ("mentioned", query.mentioned_assets))
                    for symbol, market in sorted(values)
                ],
                "subject": query.subject,
                "object": query.object,
                "known_identity": [{"key": key, "value": value} for key, value in sorted(query.known_identity)],
                "words": sorted(query.words),
                "han_bigrams": sorted(query.han_bigrams),
            }
            for query in queries
        ]
        # jsonb claim projection gives PostgreSQL a high estimated plan cost and otherwise
        # triggers JIT compilation on every short reader transaction. The plan takes much
        # longer to compile than to execute for the bounded 48 h receipt window.
        self.conn.execute("SET LOCAL jit = off")
        return [
            dict(row)
            for row in self.conn.execute(
                f"""
                WITH queries AS MATERIALIZED (
                    SELECT q.*
                      FROM jsonb_to_recordset(%s::jsonb) AS q(
                        ref text, assets jsonb, subject text, object text, known_identity jsonb,
                        words jsonb, han_bigrams jsonb)
                ), window_receipts AS MATERIALIZED (
                    SELECT {_RECEIPT_COLUMNS},
                           COALESCE(d.sent_claims, '[]'::jsonb) AS historical_claims,
                           (d.sent_claims IS NULL) AS missing_projection,
                           d.body || ' ' || array_to_string(ARRAY(
                               SELECT claim ->> 'statement'
                               FROM jsonb_array_elements(COALESCE(d.sent_claims, '[]'::jsonb)) claim
                           ), ' ') AS search_text
                      FROM ({UPDATE_RECEIPTS_SQL}) d
                     WHERE d.kind = 'update' AND d.state = 'sent'
                       AND d.settled_at_ms >= %s AND d.settled_at_ms < %s
                       AND d.body IS NOT NULL AND d.payload_sha256 IS NOT NULL
                ), structured AS (
                    SELECT q.ref AS current_ref, b.*, 'structure' AS route, matched.priority::real AS route_score,
                           NULL::text[] AS lexical_terms,
                           row_number() OVER (
                               PARTITION BY q.ref ORDER BY matched.priority DESC, b.settled_at_ms DESC, b.intent_id
                           ) AS rn
                      FROM queries q CROSS JOIN window_receipts b
                     CROSS JOIN LATERAL (
                          -- The same preference as select_for_claim, before this route's 32-row cap:
                          -- object/grounded identity, then typed primary, then actor/role-cross background.
                          SELECT CASE WHEN bool_or(names.identity_match OR (
                                               names.object_match AND NOT COALESCE(assets.typed_primary_disjoint, FALSE)
                                           )) THEN 2 + CASE WHEN bool_or(assets.primary_match) THEN 1 ELSE 0 END
                                      WHEN bool_or(assets.primary_match) THEN 1
                                      WHEN count(*) > 0 THEN 0 END AS priority
                            FROM jsonb_array_elements(b.historical_claims) hc
                           CROSS JOIN LATERAL (
                               SELECT (q.subject <> '' AND lower(btrim(hc -> 'fields' ->> 'subject')) = q.subject)
                                          AS subject_match,
                                      (q.object <> '' AND lower(btrim(hc -> 'fields' ->> 'object')) = q.object)
                                          AS object_match,
                                      EXISTS (
                                  SELECT 1
                                    FROM jsonb_array_elements(COALESCE(hc -> 'known_identity', '[]'::jsonb)) hi
                                    JOIN jsonb_array_elements(q.known_identity) qi
                                      ON hi ->> 'key' = qi ->> 'key'
                                     AND btrim(hi ->> 'value') = qi ->> 'value'
                                      ) AS identity_match
                           ) names
                            LEFT JOIN LATERAL (
                                  -- The shared entity features widen candidates only. Exact address spelling
                                  -- stays case-sensitive; catalogue/venue/quote-base features prove no identity.
                                  SELECT bool_or(qa IS NOT NULL) AS asset_match,
                                         bool_or(ha ->> 'role' = 'primary' AND qa ->> 'role' = 'primary'
                                                 AND hn.market_type <> 'unknown') FILTER (WHERE qa IS NOT NULL)
                                             AS primary_match,
                                         bool_or(ha ->> 'role' = 'primary' AND hn.market_type <> 'unknown')
                                         AND EXISTS (
                                             SELECT 1 FROM jsonb_array_elements(q.assets) current_asset
                                              WHERE current_asset ->> 'role' = 'primary'
                                                AND current_asset ->> 'market_type' <> 'unknown'
                                         ) AND NOT COALESCE(
                                             bool_or(ha ->> 'role' = 'primary' AND qa ->> 'role' = 'primary'
                                                     AND hn.market_type <> 'unknown') FILTER (WHERE qa IS NOT NULL),
                                             FALSE
                                         ) AS typed_primary_disjoint
                                    FROM jsonb_array_elements(
                                        COALESCE(hc -> 'fields' -> 'assets', '[]'::jsonb)
                                    ) ha
                                   CROSS JOIN LATERAL (
                                       SELECT regexp_replace(
                                                  ha ->> 'symbol', '^[[:space:]$]+|[[:space:]]+$', '', 'g'
                                              ) AS text
                                   ) ht
                                   CROSS JOIN LATERAL (
                                       SELECT CASE WHEN ht.text ~ %s THEN ht.text ELSE regexp_replace(
                                                  regexp_replace(upper(ht.text), '^XYZ-', ''), '^[^:]*:', ''
                                              ) END AS symbol,
                                              CASE ha ->> 'market_type'
                                                  WHEN 'forex' THEN 'fx' WHEN 'fund' THEN 'unknown'
                                                  ELSE ha ->> 'market_type' END AS market_type
                                   ) hn
                                   LEFT JOIN jsonb_array_elements(q.assets) qa
                                     ON hn.market_type = qa ->> 'market_type'
                                    AND (ha ->> 'role' = 'primary' OR qa ->> 'role' = 'primary')
                                    AND (
                                        hn.symbol = qa ->> 'symbol'
                                        OR COALESCE(%s::jsonb ->> hn.symbol, hn.symbol) = qa ->> 'symbol'
                                        OR (hn.market_type IN ('crypto','unknown') AND ht.text !~ %s
                                            AND (
                                                SELECT left(hn.symbol, length(hn.symbol)-length(quote))
                                                  FROM unnest(%s::text[]) WITH ORDINALITY quotes(quote, rank)
                                                 WHERE right(hn.symbol, length(quote)) = quote
                                                   AND length(hn.symbol) > length(quote)+1
                                                 ORDER BY rank LIMIT 1
                                            ) = qa ->> 'symbol')
                                        OR EXISTS (
                                            SELECT 1 FROM jsonb_array_elements_text(qa -> 'patterns') pattern
                                             WHERE ht.text ~* pattern
                                        )
                                    )
                            ) assets ON TRUE
                           WHERE names.subject_match OR names.object_match OR names.identity_match
                              OR assets.asset_match
                      ) matched
                     WHERE matched.priority IS NOT NULL
                ), query_terms AS MATERIALIZED (
                    SELECT q.ref, 'word' AS kind, term
                      FROM queries q CROSS JOIN LATERAL jsonb_array_elements_text(q.words) term
                    UNION ALL
                    SELECT q.ref, 'han' AS kind, term
                      FROM queries q CROSS JOIN LATERAL jsonb_array_elements_text(q.han_bigrams) term
                ), receipt_terms AS MATERIALIZED (
                    -- `lexical_evidence`: which query terms each window receipt's body and sent statements
                    -- carry, as `_words` (same pattern, lower-cased) and `_han_bigrams` (adjacent Han) read them.
                    SELECT DISTINCT b.intent_id, 'word' AS kind, lower(m[1]) AS term
                      FROM window_receipts b CROSS JOIN LATERAL regexp_matches(b.search_text, %s, 'g') m
                     WHERE lower(m[1]) IN (SELECT term FROM query_terms WHERE kind = 'word')
                    UNION ALL
                    SELECT DISTINCT b.intent_id, 'han' AS kind, t.term
                      FROM (SELECT DISTINCT term FROM query_terms WHERE kind = 'han') t
                      JOIN window_receipts b ON strpos(b.search_text, t.term) > 0
                ), rare_terms AS (
                    -- Document frequency over the same window: a term too many receipts carry is no evidence.
                    SELECT kind, term FROM receipt_terms GROUP BY kind, term
                    HAVING count(*) <= greatest(1, %s * (SELECT count(*) FROM window_receipts))
                ), shared_terms AS (
                    SELECT qt.ref AS current_ref, rt.intent_id, qt.term,
                           count(*) OVER (PARTITION BY qt.ref, rt.intent_id, qt.kind) AS kind_shared
                      FROM query_terms qt
                      JOIN rare_terms r ON r.kind = qt.kind AND r.term = qt.term
                      JOIN receipt_terms rt ON rt.kind = qt.kind AND rt.term = qt.term
                ), lexical_evidence AS (
                    SELECT current_ref, intent_id, max(kind_shared) AS shared, array_agg(term) AS terms
                      FROM shared_terms WHERE kind_shared >= %s
                     GROUP BY current_ref, intent_id
                ), lexical AS (
                    SELECT e.current_ref, b.*, 'lexical' AS route, e.shared::real AS route_score,
                           e.terms AS lexical_terms,
                           row_number() OVER (
                               PARTITION BY e.current_ref ORDER BY e.shared DESC, b.settled_at_ms DESC, b.intent_id
                           ) AS rn
                      FROM lexical_evidence e JOIN window_receipts b ON b.intent_id = e.intent_id
                )
                SELECT DISTINCT ON (current_ref, intent_id)
                       current_ref, intent_id, event_id, kind, body, payload_sha256,
                       settled_at_ms, receipt, card, history_context, claim_refs,
                       historical_claims, missing_projection,
                       min(rn) FILTER (WHERE route = 'structure')
                           OVER (PARTITION BY current_ref, intent_id) AS structure_rank,
                       min(rn) FILTER (WHERE route = 'lexical')
                           OVER (PARTITION BY current_ref, intent_id) AS lexical_rank,
                       max(lexical_terms) FILTER (WHERE route = 'lexical')
                           OVER (PARTITION BY current_ref, intent_id) AS lexical_terms
                  FROM (
                      SELECT * FROM structured WHERE rn <= %s
                      UNION ALL
                      SELECT * FROM lexical WHERE rn <= %s
                  ) routed
                 ORDER BY current_ref, intent_id, route
                """,  # noqa: S608 - a module-owned column list
                (
                    _dumps(query_rows),
                    int(now_ms) - RECALL_WINDOW_MS,
                    int(now_ms),
                    ADDRESS_PATTERN,
                    _dumps(RELATED_ASSET_ALIASES),
                    ADDRESS_PATTERN,
                    list(CRYPTO_QUOTE_SUFFIXES),
                    WORD_PATTERN,
                    LEXICAL_DF_MAX,
                    LEXICAL_SHARED_MIN,
                    ROUTE_CANDIDATES_MAX,
                    ROUTE_CANDIDATES_MAX,
                ),
            ).fetchall()
        ]

    def _claim_links(self, refs: Sequence[str], *, as_of_ms: int) -> list[dict[str, Any]]:
        """Persisted links within two hops of these claims, read from both ends (#742)."""

        if not refs:
            return []
        query = """
            SELECT update_ref, current_ref, previous_ref, relation, asserted_at_ms FROM news_claim_links
             WHERE (current_ref = ANY(%s) OR previous_ref = ANY(%s)) AND asserted_at_ms < %s
        """
        first = self.conn.execute(query, (list(refs), list(refs), as_of_ms)).fetchall()
        reached = sorted({str(row[key]) for row in first for key in ("current_ref", "previous_ref")} - set(refs))
        second = self.conn.execute(query, (reached, reached, as_of_ms)).fetchall() if reached else []
        rows = {
            (str(row["update_ref"]), str(row["current_ref"]), str(row["previous_ref"])): dict(row)
            for row in (*first, *second)
        }
        return [rows[key] for key in sorted(rows)]

    def _link_receipt_rows(self, refs: Sequence[str], *, now_ms: int, until_ms: int | None) -> list[dict[str, Any]]:
        """Receipts of any Event carrying a claim the links reach: delivered, ambiguous or still sending.

        Only a delivered receipt is bounded by the snapshot stamp; an unsettled one is read as it is now,
        like this Event's own unsettled claims, so the snapshot and the CAS read it the same way.
        """

        if not refs:
            return []
        return [
            dict(row)
            for row in self.conn.execute(
                f"""
                SELECT {_RECEIPT_COLUMNS}, d.state, COALESCE(d.sent_claims, '[]'::jsonb) AS historical_claims
                  FROM ({UPDATE_RECEIPTS_SQL}) d
                 WHERE d.kind = 'update'
                   AND d.claim_refs ?| %s::text[]
                   AND (d.state = 'sending'
                        OR (d.state = 'ambiguous' AND d.settled_at_ms >= %s AND d.settled_at_ms < %s)
                        OR (d.state = 'sent' AND d.settled_at_ms >= %s
                            AND (%s::bigint IS NULL OR d.settled_at_ms < %s)))
                 ORDER BY d.intent_id
                """,  # noqa: S608 - a module-owned column list
                (
                    list(refs),
                    int(now_ms) - LINKED_RECEIPT_WINDOW_MS,
                    int(now_ms),
                    int(now_ms) - LINKED_RECEIPT_WINDOW_MS,
                    until_ms,
                    until_ms,
                ),
            ).fetchall()
        ]

    def reader_state(self, *, event_id: str, head: EventUpdate, now_ms: int) -> dict[str, Any]:
        """Build the exact claim-scoped reader context for snapshot and both CAS sites."""

        sending, ambiguous = self._unsettled_claim_refs(event_id)
        invalidated = self.invalidated_claim_refs([claim.ref for claim in head.claims], as_of_ms=now_ms)
        event = self.conn.execute(
            "SELECT comparison_title, event_kind FROM news_events WHERE event_id = %s", (event_id,)
        ).fetchone()
        listing_members = (
            self.conn.execute(
                """SELECT m.item_id,m.fact_text,i.provider_metadata
                     FROM news_event_members m JOIN news_items i ON i.item_id=m.item_id
                    WHERE m.event_id=%s""",
                (event_id,),
            ).fetchall()
            if event is not None and event["event_kind"] == "listing"
            else []
        )
        listing_scopes = [
            row
            for row in listing_members
            if any(
                contract.source_contract_family == "listing_v1"
                for contract in classify_source_contracts(row.get("provider_metadata") or {})
            )
        ]
        evidence_items = {item.ref: item for item in head.evidence}
        protected_listing = tuple(
            claim.ref
            for claim in head.claims
            if any(
                citation.evidence_ref in evidence_items
                and evidence_items[citation.evidence_ref].source.record_id == str(scope["item_id"])
                and citation.quote in str(scope["fact_text"])
                for citation in claim.citations
                for scope in listing_scopes
            )
        )
        inactive = set(head.retired_claim_refs) | set(head.superseded_claim_refs) | set(invalidated)
        queries = {claim.ref: query_for_claim(claim) for claim in head.claims if claim.ref not in inactive}
        ordinary = self._recall_receipt_rows(tuple(queries.values()), now_ms=now_ms)
        active = linked_refs(head, invalidated)
        links = self._claim_links(sorted(active), as_of_ms=now_ms)
        reached = active | {str(row[key]) for row in links for key in ("current_ref", "previous_ref")}
        linked = self._link_receipt_rows(sorted(reached), now_ms=now_ms, until_ms=now_ms)
        original_claims = {
            claim.ref: claim
            for row in (*ordinary, *linked)
            for value in row.get("historical_claims") or ()
            for claim in (Claim.model_validate(value),)
        }
        original_claims.update((claim.ref, claim) for claim in head.claims)
        link_models = listing_compatible_links(
            (
                ClaimLink(
                    current_ref=str(row["current_ref"]),
                    previous_ref=str(row["previous_ref"]),
                    relation=row["relation"],
                    asserted_at_ms=int(row["asserted_at_ms"]),
                )
                for row in links
            ),
            original_claims,
        )
        active_assertions = {
            (link.current_ref, link.previous_ref, link.relation, link.asserted_at_ms) for link in link_models
        }
        links = [
            row
            for row in links
            if (row["current_ref"], row["previous_ref"], row["relation"], row["asserted_at_ms"]) in active_assertions
        ]
        receipt_models = tuple(
            LinkedReceipt(
                intent_id=str(row["intent_id"]),
                state=row["state"],
                claim_refs=tuple(str(ref) for ref in row["claim_refs"] or ()),
                settled_at_ms=None if row["settled_at_ms"] is None else int(row["settled_at_ms"]),
            )
            for row in linked
        )
        rows = {str(row["intent_id"]): row for row in (*linked, *ordinary)}
        candidates = tuple(
            RecallCandidate(
                intent_id=intent,
                payload_sha256=text.payload_sha256,
                body=text.body,
                settled_at_ms=int(text.received_at_ms),
                claims=tuple(Claim.model_validate(claim) for claim in row.get("historical_claims") or ()),
            )
            for intent, row in rows.items()
            if (text := delivered_text(row)) is not None
            and text.state == "sent"
            and text.received_at_ms is not None
            and text.received_at_ms < now_ms
        )
        if missing := sum(bool(row.get("missing_projection")) for row in ordinary):
            log.warning("news_reader_missing_receipt_projection", extra={"event_id": event_id, "count": missing})
        novelties = {
            claim.ref: reader_novelty(claim.ref, link_models, receipt_models)
            for claim in head.claims
            if claim.ref not in inactive
        }
        ordinary_ids = {
            ref: {str(row["intent_id"]) for row in ordinary if row["current_ref"] == ref} for ref in queries
        }
        routes = {
            ref: {
                str(row["intent_id"]): RouteEvidence(
                    structure_rank=row["structure_rank"],
                    lexical_rank=row["lexical_rank"],
                    lexical_terms=tuple(sorted(row["lexical_terms"] or ())),
                )
                for row in ordinary
                if row["current_ref"] == ref
            }
            for ref in queries
        }
        selections = {
            ref: select_for_claim(
                query,
                novelties[ref],
                tuple(
                    candidate
                    for candidate in candidates
                    if candidate.intent_id in ordinary_ids[ref] or candidate.intent_id in novelties[ref].linked_intents
                ),
                as_of_ms=now_ms,
                routes=routes[ref],
            )
            for ref, query in queries.items()
        }
        selected_ids = {intent for selection in selections.values() for intent in selection.intent_ids}
        return {
            "event": None if event is None else dict(event),
            "sending": sending,
            "ambiguous": ambiguous,
            "invalidated": invalidated,
            "protected_listing": protected_listing,
            "receipt_intents_by_claim": {ref: selection.intent_ids for ref, selection in selections.items()},
            "receipts": list(
                {
                    str(row["intent_id"]): row for row in (*[rows[intent] for intent in sorted(selected_ids)], *linked)
                }.values()
            ),
            "links": links,
            "linked": linked,
            "revision": reader_context_revision(
                head.ref,
                selections,
                novelties,
                candidates,
                receipt_models,
                blocked=tuple(sending),
                ambiguous=tuple(ambiguous),
                invalidated=tuple(invalidated),
                protected_listing=protected_listing,
            ),
        }

    def current_reader_revision(self, event_id: str, *, now_ms: int) -> str | None:
        document = self.updates.event_update_head_document(event_id)
        if document is None:
            return None
        head = EventUpdate.model_validate(document)
        state = self.reader_state(event_id=event_id, head=head, now_ms=now_ms)
        return str(state["revision"])

    def notification_snapshot_material(self, *, event_id: str, channel: str, now_ms: int) -> dict[str, Any] | None:
        """The pending head, the receipts the planner may compare, and the related-receipt reader revision."""

        # The port starts a repeatable-read transaction before session configuration or any query.
        # Several reads below must see one MVCC view for the model input and revision.
        work = self.conn.execute(
            f"SELECT content_revision, state, next_attempt_at_ms, updated_at_ms FROM ({NOTIFY_JOBS_SQL}) "  # noqa: S608
            "WHERE event_id = %s AND channel = %s",
            (event_id, channel),
        ).fetchone()
        if work is None or work["state"] != "pending":
            return None
        head = self.updates.event_update_head_document(event_id)
        if head is None or head.get("content_revision") != work["content_revision"]:
            return None
        reader = self.reader_state(event_id=event_id, head=EventUpdate.model_validate(head), now_ms=now_ms)
        return {
            "work_updated_at_ms": int(work["updated_at_ms"]),
            "work_due_at_ms": int(work["next_attempt_at_ms"]),
            "head": head,
            "blocked": reader["sending"],
            "ambiguous": reader["ambiguous"],
            "invalidated": reader["invalidated"],
            "revision": reader["revision"],
            "receipt_intents_by_claim": reader["receipt_intents_by_claim"],
            "receipt_rows": reader["receipts"],
            "links": reader["links"],
            "link_receipts": reader["linked"],
            "protected_listing": reader["protected_listing"],
        }
