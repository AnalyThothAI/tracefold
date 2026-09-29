"""Audit numbered Event heads and append provenance for proven sibling-fact retractions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..updates.contracts import EventUpdate
from ..updates.identity import digest, identity
from ..updates.projection import PROJECTION_VERSION, extraction_scopes, reading_view
from ..updates.public import public_updates
from ..updates.scope_repair import retract_out_of_scope
from .update_commit import PUBLIC_TRADE_KINDS, ScopeProofSource, commit_update, lock_event

HEAD_SCOPE_MATERIAL_SQL = """
SELECT h.event_id, u.document,
       COALESCE((
         SELECT jsonb_agg(jsonb_build_object(
           'item_id', m.item_id, 'fact_id', m.fact_id, 'fact_text', m.fact_text,
           'title', i.title, 'description', i.description, 'evidence_text', i.evidence_text
         ) ORDER BY m.item_id, m.fact_id)
           FROM news_event_members m JOIN news_items i ON i.item_id=m.item_id
          WHERE m.event_id=h.event_id
       ), '[]'::jsonb) AS members,
       COALESCE((
         SELECT jsonb_object_agg(f.focus_fact_id, f.fact) FROM (
           SELECT DISTINCT ON (s.focus_fact_id) s.focus_fact_id,
                  s.snapshot->'focus_fact' AS fact
             FROM news_event_evidence_snapshots s
            WHERE s.event_id=h.event_id AND s.provenance='observed'
            ORDER BY s.focus_fact_id, s.evidence_version DESC
         ) f WHERE f.focus_fact_id IS NOT NULL
       ), '{}'::jsonb) AS fact_scopes
  FROM news_event_update_heads h
  JOIN news_events e ON e.event_id=h.event_id
  JOIN news_event_updates u ON u.event_id=h.event_id AND u.content_revision=h.content_revision
 WHERE e.focus_fact_method='explicit_numbered'
 ORDER BY h.event_id
"""
HEAD_SCOPE_EVENT_SQL = HEAD_SCOPE_MATERIAL_SQL.replace(" ORDER BY h.event_id", " AND h.event_id=%s ORDER BY h.event_id")
HEAD_SCOPE_PAGE_SQL = HEAD_SCOPE_MATERIAL_SQL.replace(
    " ORDER BY h.event_id", " AND h.event_id > %s ORDER BY h.event_id LIMIT %s"
)


def audit_head_scope(row: Mapping[str, Any]) -> dict[str, Any]:
    head = EventUpdate.model_validate(row["document"])
    members = row["members"]
    scopes = extraction_scopes({"members": members, "items": members, "fact_scopes": row["fact_scopes"]}, head.evidence)
    member_ids = {str(member["item_id"]) for member in members}
    evidence = {item.ref: item for item in head.evidence}
    views = {item.ref: reading_view(head.event_id, item, scopes) for item in head.evidence}
    outside: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for claim in head.current_claims:
        bad: list[dict[str, Any]] = []
        good = 0
        uncertain: list[str] = []
        for citation in claim.citations:
            item = evidence[citation.evidence_ref]
            if item.source.record_id not in member_ids:
                uncertain.append("citation_source_not_current_member")
                continue
            view = views[item.ref]
            if view.mode != "scoped":
                uncertain.append(view.reason or "member_scope_missing")
            elif citation.quote not in item.text:
                uncertain.append("citation_quote_not_in_source")
            elif any(citation.quote in span.text for span in view.spans):
                good += 1
            else:
                bad.append(
                    {
                        "evidence_ref": item.ref,
                        "quote": citation.quote,
                        "read_ref": view.read_ref,
                        "view_material_sha": view.material_sha,
                        "source_record_id": item.source.record_id,
                        "scope_fact_ids": sorted(scope.fact_id for scope in scopes if scope.evidence_ref == item.ref),
                    }
                )
        if bad and not good and not uncertain and len(bad) == len(claim.citations):
            outside.append({"claim_ref": claim.ref, "citations": bad})
        elif bad or uncertain:
            unresolved.append({"claim_ref": claim.ref, "reasons": sorted(set(uncertain or ["mixed_scope_citations"]))})
    return {
        "event_id": head.event_id,
        "head_revision": head.content_revision,
        "input_revision": head.input_revision,
        "outside": outside,
        "unresolved": unresolved,
    }


def audit_scope_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    events = [audit_head_scope(row) for row in rows]
    material = {"projection_version": PROJECTION_VERSION, "events": events}
    return {
        **material,
        "digest": digest(material),
        "heads": len(events),
        "affected_heads": sum(bool(row["outside"]) for row in events),
        "outside_active_claims": sum(len(row["outside"]) for row in events),
        "unresolved_active_claims": sum(len(row["unresolved"]) for row in events),
    }


class HeadScopeRepairStorage:
    conn: Any

    def head_scope_material(self, *, after: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 501:
            raise ValueError("news_scope_repair_page_limit_invalid")
        return [dict(row) for row in self.conn.execute(HEAD_SCOPE_PAGE_SQL, (after, limit)).fetchall()]

    def head_scope_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(HEAD_SCOPE_EVENT_SQL, (event_id,)).fetchone()
        return None if row is None else dict(row)

    def adopt_head_scope_repair(self, *, expected_head: str, proof: dict[str, Any], now_ms: int) -> str:
        """CAS a single head, its immutable repair, public amendment and notification work."""

        event_id = str(proof["event_id"])
        lock_event(self.conn, event_id)
        current = self.conn.execute(HEAD_SCOPE_EVENT_SQL, (event_id,)).fetchone()
        if current is None or current["document"]["content_revision"] != expected_head:
            raise ValueError("news_scope_repair_head_changed")
        if audit_head_scope(dict(current)) != proof or not proof["outside"] or proof["unresolved"]:
            raise ValueError("news_scope_repair_proof_changed")
        head = EventUpdate.model_validate(current["document"])
        refs = tuple(sorted(str(row["claim_ref"]) for row in proof["outside"]))
        update = retract_out_of_scope(head, refs, adopted_at_ms=now_ms)
        repair_id = identity("news_head_scope_repair", event_id, head.content_revision, refs, PROJECTION_VERSION)
        public_rows = [
            (PUBLIC_TRADE_KINDS[row.kind], row.model_dump(mode="json"))
            for row in public_updates(update, semantic_completed_at_ms=now_ms)
        ]
        if not commit_update(
            self,
            expected_head_ref=head.ref,
            update=update,
            document_json=update.model_dump_json(),
            source=ScopeProofSource(repair_id, head.content_revision, refs, proof, PROJECTION_VERSION, int(now_ms)),
            public_rows=public_rows,
            now_ms=now_ms,
        ):
            raise ValueError("news_scope_repair_head_changed")
        return update.content_revision
