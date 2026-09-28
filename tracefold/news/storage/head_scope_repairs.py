"""Audit numbered Event heads and append provenance for proven sibling-fact retractions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from ..updates.contracts import EventUpdate
from ..updates.identity import digest, identity
from ..updates.projection import PROJECTION_VERSION, reading_view
from ..updates.public import public_updates
from ..updates.scope_repair import retract_out_of_scope
from .event_updates import _ADOPT_LOCK_NAMESPACE, NEWS_CHANNEL, PUBLIC_TRADE_KINDS, _extraction_scopes
from .sql_values import _dumps
from .trade_projection import TradeProjectionStorage

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


def audit_head_scope(row: Mapping[str, Any]) -> dict[str, Any]:
    head = EventUpdate.model_validate(row["document"])
    members = row["members"]
    scopes = _extraction_scopes(
        {"members": members, "items": members, "fact_scopes": row["fact_scopes"]}, head.evidence
    )
    member_ids = {str(member["item_id"]) for member in members}
    evidence = {item.ref: item for item in head.evidence}
    views = {item.ref: reading_view(head.event_id, item, scopes) for item in head.evidence}
    inactive = set(head.retired_claim_refs) | set(head.superseded_claim_refs)
    outside: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for claim in head.claims:
        if claim.ref in inactive:
            continue
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

    def head_scope_material(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(HEAD_SCOPE_MATERIAL_SQL).fetchall()]

    def adopt_head_scope_repair(self, *, expected_head: str, proof: dict[str, Any], now_ms: int) -> str:
        """CAS a single head, its immutable repair, public amendment and notification work."""

        event_id = str(proof["event_id"])
        self.conn.execute("SET LOCAL lock_timeout = '2500ms'")
        self.conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_ADOPT_LOCK_NAMESPACE, event_id))
        current = self.conn.execute(HEAD_SCOPE_EVENT_SQL, (event_id,)).fetchone()
        if current is None or current["document"]["content_revision"] != expected_head:
            raise ValueError("news_scope_repair_head_changed")
        if audit_head_scope(dict(current)) != proof or not proof["outside"] or proof["unresolved"]:
            raise ValueError("news_scope_repair_proof_changed")
        head = EventUpdate.model_validate(current["document"])
        refs = tuple(sorted(str(row["claim_ref"]) for row in proof["outside"]))
        in_flight = self.conn.execute(
            """SELECT 1 FROM news_deliveries WHERE event_id=%s AND kind='update'
                 AND state IN ('sending','ambiguous') AND claim_refs ?| %s::text[] LIMIT 1""",
            (event_id, list(refs)),
        ).fetchone()
        if in_flight is not None:
            raise ValueError("news_scope_repair_claim_delivery_in_flight")
        update = retract_out_of_scope(head, refs, adopted_at_ms=now_ms)
        repair_id = identity("news_head_scope_repair", event_id, head.content_revision, refs, PROJECTION_VERSION)
        self.conn.execute(
            """INSERT INTO news_head_scope_repairs
                 (repair_id,event_id,previous_content_revision,content_revision,claim_refs,
                  proof,projection_version,recorded_at_ms)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
            (
                repair_id,
                event_id,
                head.content_revision,
                update.content_revision,
                list(refs),
                _dumps(proof),
                PROJECTION_VERSION,
                int(now_ms),
            ),
        )
        self.conn.execute(
            """INSERT INTO news_event_updates
                 (event_id,content_revision,input_revision,previous_content_revision,
                  adopted_at_ms,scope_repair_id,document)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (
                event_id,
                update.content_revision,
                update.input_revision,
                head.content_revision,
                int(now_ms),
                repair_id,
                update.model_dump_json(),
            ),
        )
        changed = self.conn.execute(
            """UPDATE news_event_update_heads
                  SET content_revision=%s, update_ref=%s, adopted_at_ms=%s
                WHERE event_id=%s AND content_revision=%s RETURNING event_id""",
            (update.content_revision, update.ref, int(now_ms), event_id, head.content_revision),
        ).fetchone()
        if changed is None:
            raise ValueError("news_scope_repair_head_changed")
        for public in public_updates(update, semantic_completed_at_ms=now_ms):
            if not cast(TradeProjectionStorage, self).enqueue_trade_event(
                kind=PUBLIC_TRADE_KINDS[public.kind],
                source_fact_key=event_id,
                source_revision=update.content_revision,
                payload=public.model_dump(mode="json"),
                source_recorded_at_ms=now_ms,
            ):
                raise ValueError("news_scope_repair_public_conflict")
        # A repair has no new reader fact. Preserve completed notification decisions;
        # only work already owed may plan against the corrected active claims.
        self.conn.execute(
            """UPDATE news_notification_work
                  SET content_revision=%s,plan=NULL,decision_ref=NULL,reader_revision=NULL,
                      attempts=0,next_attempt_at_ms=%s,updated_at_ms=%s
                WHERE event_id=%s AND channel=%s AND state='pending'""",
            (update.content_revision, int(now_ms), int(now_ms), event_id, NEWS_CHANNEL),
        )
        return update.content_revision
