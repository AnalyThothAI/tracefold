"""One consistent reader context for snapshots and both send-permission checks.

Commands use the caller's existing transaction; no external I/O or independent commit.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Final

from ..claim_recall import (
    CALIBRATION,
    RECEIPT_WINDOW_MS,
    Candidate,
    Probe,
    embed_text,
    lexical_text,
    prepare_rank,
    rank,
)
from ..notifications.contracts import NEWS_CHANNEL, DeliveredText
from ..notifications.novelty import ClaimLink, LinkedReceipt, current_links, reader_novelty
from ..notifications.recall import select_for_claim
from ..source_contracts import classify_source_contracts
from ..updates.assembly import different_listing_assets
from ..updates.contracts import Claim, EventUpdate
from ..updates.identity import digest
from .claim_index import ClaimIndexStorage, source_keys
from .reader_check import ReaderCheck
from .semantic_updates import SemanticUpdateStorage

_RECEIPT_COLUMNS: Final = (
    "d.intent_id, d.event_id, d.kind, d.card->>'body' AS body, d.card->>'payload_sha256' AS "
    "payload_sha256, d.settled_at_ms, "
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
              FROM news_analyses u
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
            """
            SELECT state, claim_refs FROM news_notifications
             WHERE event_id = %s AND kind = 'update' AND state IN ('sending', 'ambiguous')
            """,
            (event_id,),
        ).fetchall()
        return (
            sorted({str(ref) for row in rows if row["state"] == "sending" for ref in row["claim_refs"] or ()}),
            sorted({str(ref) for row in rows if row["state"] == "ambiguous" for ref in row["claim_refs"] or ()}),
        )

    def _recall_receipt_rows(self, *, now_ms: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT d.intent_id,d.event_id,d.kind,d.settled_at_ms,d.state,
                       d.sent_claims IS NULL AS missing_projection,d.card->>'body' AS body,
                       d.card->>'payload_sha256' AS payload_sha256
                  FROM news_notifications d
                 WHERE d.kind='update' AND d.state='sent'
                   AND d.settled_at_ms >= %s AND d.settled_at_ms < %s
                 ORDER BY d.intent_id""",
                (now_ms - RECEIPT_WINDOW_MS, now_ms),
                binary=True,
            ).fetchall()
        ]

    def _complete_receipt_rows(self, intents: Sequence[str]) -> list[dict[str, Any]]:
        if not intents:
            return []
        return [
            dict(row)
            for row in self.conn.execute(
                f"""SELECT {_RECEIPT_COLUMNS},d.state,
                           COALESCE(d.sent_claims,'[]'::jsonb) AS historical_claims
                      FROM news_notifications d WHERE d.intent_id=ANY(%s::text[])""",  # noqa: S608
                (list(intents),),
                binary=True,
            ).fetchall()
        ]

    def _claim_links(self, refs: Sequence[str], *, as_of_ms: int) -> list[dict[str, Any]]:
        """Persisted links within two hops of these claims, read from both ends (#742)."""

        if not refs:
            return []
        query = """
            SELECT DISTINCT ON (a.update_ref,change->>'current_ref',change->>'previous_ref')
                   a.update_ref,change->>'current_ref' AS current_ref,
                   change->>'previous_ref' AS previous_ref,change->>'relation' AS relation,
                   a.adopted_at_ms AS asserted_at_ms
              FROM news_analyses a CROSS JOIN LATERAL jsonb_array_elements(a.document->'changes')
                   WITH ORDINALITY AS changes(change,position)
             WHERE a.adopted_at_ms < %s
               AND (jsonb_path_query_array(a.document,'$."changes"[*]."current_ref"') ?| %s
                 OR jsonb_path_query_array(a.document,'$."changes"[*]."previous_ref"') ?| %s)
               AND (change->>'current_ref'=ANY(%s) OR change->>'previous_ref'=ANY(%s))
               AND change->>'previous_ref'<>change->>'current_ref'
               AND change->>'relation' IN ('equivalent','adds_information','real_world_change','corrects','conflicts')
             ORDER BY a.update_ref,change->>'current_ref',change->>'previous_ref',position
        """
        first = self.conn.execute(query, (as_of_ms, *([list(refs)] * 4))).fetchall()
        reached = sorted({str(row[key]) for row in first for key in ("current_ref", "previous_ref")} - set(refs))
        second = self.conn.execute(query, (as_of_ms, *([reached] * 4))).fetchall() if reached else []
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
                  FROM news_notifications d
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
                    int(now_ms) - RECEIPT_WINDOW_MS,
                    int(now_ms),
                    int(now_ms) - RECEIPT_WINDOW_MS,
                    until_ms,
                    until_ms,
                ),
            ).fetchall()
        ]

    def reader_state(
        self, *, event_id: str, head: EventUpdate, now_ms: int, probes: Mapping[str, Probe] | None = None
    ) -> dict[str, Any]:
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
        queries = {claim.ref: claim for claim in head.claims if claim.ref not in inactive}
        ordinary = self._recall_receipt_rows(now_ms=now_ms) if queries else []
        active = linked_refs(head, invalidated)
        links = self._claim_links(sorted(active), as_of_ms=now_ms)
        reached = active | {str(row[key]) for row in links for key in ("current_ref", "previous_ref")}
        linked = self._link_receipt_rows(sorted(reached), now_ms=now_ms, until_ms=now_ms)
        rows = {str(row["intent_id"]): row for row in linked}
        frozen_by_intent = {
            intent: tuple(Claim.model_validate(value) for value in row.get("historical_claims") or ())
            for intent, row in rows.items()
        }
        original_claims = {claim.ref: claim for claims in frozen_by_intent.values() for claim in claims}
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
        available = frozenset(
            intent
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
        index = ClaimIndexStorage(self.conn)
        selections = {}
        recall_diagnostics = {}
        ordinary_available = frozenset(str(row["intent_id"]) for row in ordinary if delivered_text(row) is not None)
        # Validate receipt bodies before route_n; malformed receipts cannot take
        # a slot from valid reader evidence. Frozen vectors are shared by queries.
        pool = index.receipt_pool(sorted(ordinary_available))
        stored_probes = index.query_probes(tuple(queries.values()))
        for ref, claim in queries.items():
            probe = (probes or {}).get(ref, stored_probes.get(ref, Probe(embed_text(claim))))
            if (
                probe.text != embed_text(claim)
                or probe.embedder != CALIBRATION.embedder.key
                or probe.vector is None
                or len(probe.vector) != CALIBRATION.embedder.dimensions * 2
            ):
                probe = stored_probes.get(ref, Probe(embed_text(claim)))
            sources = tuple(
                key
                for citation in claim.citations
                if citation.evidence_ref in evidence_items
                for key in source_keys(evidence_items[citation.evidence_ref].source)
            )
            candidates = tuple(
                Candidate(
                    key,
                    row["vector"],
                    row["embedder"],
                    same_source=bool(set(row["structure_keys"]) & set(sources)),
                    group=str(row["intent_id"]),
                )
                for key, row in pool.items()
            )
            prepared = prepare_rank(probe, candidates, "receipt")
            lexical = index.lexical_pool_scores(lexical_text(claim), pool, prepared=prepared)
            ranking = rank(prepared, tuple(replace(c, lexical=lexical.get(c.key, 0.0)) for c in candidates))
            eligible_intents = {hit.key for hit in ranking.hits}
            rows.update(
                (str(row["intent_id"]), row)
                for row in self._complete_receipt_rows(sorted(eligible_intents - rows.keys()))
            )
            available = available | frozenset(
                intent
                for intent in eligible_intents
                if (text := delivered_text(rows[intent])) is not None
                and text.state == "sent"
                and text.received_at_ms is not None
                and text.received_at_ms < now_ms
            )
            selections[ref] = select_for_claim(novelties[ref], ranking, available=available)
            recall_diagnostics[ref] = ranking.diagnostics()
        selected_ids = {intent for selection in selections.values() for intent in selection.intent_ids}
        selected_rows = [rows[intent] for intent in sorted(selected_ids)]
        return {
            "event": None if event is None else dict(event),
            "sending": sending,
            "ambiguous": ambiguous,
            "invalidated": invalidated,
            "protected_listing": protected_listing,
            "receipt_intents_by_claim": {ref: selection.intent_ids for ref, selection in selections.items()},
            "receipts": list({str(row["intent_id"]): row for row in (*selected_rows, *linked)}.values()),
            "links": links,
            "linked": linked,
            "revision": self._generation_revision(),
            "recall_diagnostics": recall_diagnostics,
        }

    def _generation_revision(self) -> str:
        generation = self.conn.execute("SELECT revision FROM news_reader_clock WHERE singleton").fetchone()["revision"]
        return f"reader_generation:{generation}"

    def read_permission(self, event_id: str, *, now_ms: int) -> ReaderCheck:
        del now_ms
        generation = int(
            self.conn.execute("SELECT revision FROM news_reader_clock WHERE singleton").fetchone()["revision"]
        )
        return ReaderCheck(event_id, f"reader_generation:{generation}", generation)

    def read_plan_permission(self, update_ref: str, *, now_ms: int) -> ReaderCheck | None:
        row = self.conn.execute("SELECT event_id FROM news_analyses WHERE update_ref=%s", (update_ref,)).fetchone()
        return None if row is None else self.read_permission(str(row["event_id"]), now_ms=now_ms)

    def read_intent_permission(self, intent_id: str, *, now_ms: int) -> ReaderCheck | None:
        row = self.conn.execute("SELECT event_id FROM news_notifications WHERE intent_id=%s", (intent_id,)).fetchone()
        return None if row is None else self.read_permission(str(row["event_id"]), now_ms=now_ms)

    def notification_snapshot_material(
        self, *, event_id: str, channel: str, now_ms: int, probes: Mapping[str, Probe] | None = None
    ) -> dict[str, Any] | None:
        """The pending head, one selected reader context, and the sent-set/link generation."""

        # The port starts a repeatable-read transaction before session configuration or any query.
        # Several reads below must see one MVCC view for the model input and revision.
        work = self.conn.execute(
            "SELECT detail->>'content_revision' AS content_revision,state,next_attempt_at_ms,updated_at_ms "
            "FROM news_jobs "
            "WHERE job_kind='notify' AND subject_id=%s",
            (event_id,),
        ).fetchone()
        if work is None or work["state"] != "pending":
            return None
        head = self.updates.event_update_head_document(event_id)
        if head is None or head.get("content_revision") != work["content_revision"]:
            return None
        reader = self.reader_state(
            event_id=event_id, head=EventUpdate.model_validate(head), now_ms=now_ms, probes=probes
        )
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
            "recall_diagnostics": reader["recall_diagnostics"],
        }
