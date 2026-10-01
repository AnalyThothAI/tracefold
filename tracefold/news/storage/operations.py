"""OpenNews ingest incidents, broker state, and bounded retention operations."""

from __future__ import annotations

from typing import Any, cast

# S608 exemption below appends one fixed optional predicate; incident values remain bound parameters.
from .sql_values import _dumps

RAW_RETENTION_BATCH_MAX = 1_000

_RAW_RETENTION_PRESERVATION_SQL = """
  AND (
        %s::bigint IS NULL
        OR (
          NOT EXISTS (
            SELECT 1
              FROM news_event_members m
              JOIN news_events e ON e.event_id = m.event_id
             WHERE m.item_id = i.item_id
               AND e.opened_at_ms >= %s
               AND EXISTS (SELECT 1 FROM news_event_updates u WHERE u.event_id = e.event_id)
          )
          AND NOT EXISTS (
            SELECT 1
              FROM news_events e2
             WHERE e2.leader_item_id = i.item_id
               AND e2.opened_at_ms >= %s
               AND EXISTS (SELECT 1 FROM news_event_updates u WHERE u.event_id = e2.event_id)
          )
        )
      )
"""

RAW_RETENTION_CANDIDATE_SQL = f"""
    SELECT i.item_id, i.observed_at_ms
      FROM news_items i
     WHERE i.observed_at_ms < %s
       {_RAW_RETENTION_PRESERVATION_SQL}
     ORDER BY i.observed_at_ms, i.item_id
     LIMIT %s
"""  # noqa: S608

_RAW_RETENTION_DELETE_SQL = f"""
    DELETE FROM news_items i
     WHERE i.item_id = ANY(%s)
       AND i.observed_at_ms < %s
       {_RAW_RETENTION_PRESERVATION_SQL}
 RETURNING i.item_id, i.observed_at_ms
"""  # noqa: S608


class OperationsStorage:
    conn: Any

    def sweep_orphan_jobs(self, *, limit: int) -> int:
        """Remove one bounded batch of event jobs left behind by fact retention."""
        rows = self.conn.execute(
            """DELETE FROM news_jobs WHERE (job_kind,subject_id) IN (
                 SELECT job_kind,subject_id FROM news_jobs j
                 WHERE job_kind IN ('notify','semantic')
                   AND NOT EXISTS (SELECT 1 FROM news_events e WHERE e.event_id=j.subject_id)
                 ORDER BY job_kind,subject_id LIMIT %s FOR UPDATE SKIP LOCKED
               ) RETURNING subject_id""",
            (min(RAW_RETENTION_BATCH_MAX, max(1, limit)),),
        ).fetchall()
        return len(rows)

    def seed_restore_drill_facts(self, *, current_event_id: str) -> str:
        """Seed the bounded News truth used only by the isolated restore drill."""

        self.conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, raw_first_line, description,
              reporting_origin, published_at_ms, observed_at_ms, provider_metadata,
              provenance, first_ingest_mode, trace_id, created_at_ms, updated_at_ms
            ) VALUES
              ('restore-current', 'restore', 'restore-current', 'restore current',
               'restore current', 'current durable fact', 'restore', 10, 10,
               '{"strategies":["restore"]}'::jsonb, '["restore"]'::jsonb, 'live',
               'restore-current-trace', 10, 10)
            """
        )
        self.conn.execute(
            """
            INSERT INTO news_events (
              event_id, leader_item_id, dedupe_family, comparison_fingerprint, comparison_title,
              leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, admission,
              ingest_mode, trace_id, created_at_ms, updated_at_ms, focus_fact_id,
              focus_fact_text, focus_fact_context, focus_fact_method, focus_span_start,
              focus_span_end, event_kind
            ) VALUES
              (%s, 'restore-current', 'general', 'restore-current-fingerprint', 'restore current',
               'restore current', 10, 10, 100, 'candidate', 'live', 'restore-current-trace',
               10, 10, 'restore-current-fact', 'restore current', 'current durable fact',
               'whole_item', 0, 15, 'news')
            """,
            (current_event_id,),
        )
        self.conn.execute(
            """
            INSERT INTO news_event_members (
              event_id, item_id, joined_at_ms, match_kind, fact_id, fact_text
            ) VALUES (%s, 'restore-current', 10, 'leader', 'restore-current-fact', 'restore current')
            """,
            (current_event_id,),
        )
        # Build a current EventUpdate and decision, then a terminal update intent. This drill is a
        # synthetic schema/restore proof; it makes no provider or model call.
        from ..notifications.contracts import ClaimDecision, FrozenCard, NotificationPlan
        from ..updates.assembly import assemble_update
        from ..updates.contracts import (
            Citation,
            ClaimFields,
            DraftClaim,
            Evidence,
            Extraction,
            FrozenInput,
            Source,
            SupportDraft,
        )
        from ..updates.identity import digest

        repository = cast(Any, self)
        evidence_snapshot = repository.append_evidence_snapshot(event_id=current_event_id, now_ms=11)
        source = Source(
            publisher_id="restore",
            artifact_id="restore-current",
            artifact_revision="1",
            record_id="restore-current",
            first_available_at_ms=10,
        )
        material = Evidence.issue("restore current", source)
        frozen = FrozenInput(event_id=current_event_id, revision=1, lineage_id="restore-drill", evidence=(material,))
        extraction = Extraction(
            claims=(
                DraftClaim(
                    slot="a",
                    statement=material.text,
                    fields=ClaimFields(subject="restore", action="reported", mode="observation"),
                    citations=(Citation(evidence_ref=material.ref, quote=material.text),),
                ),
            ),
            supports=(SupportDraft(slot="a", evidence_ref=material.ref, relation="supports"),),
        )
        update = assemble_update(frozen, extraction, None, adopted_at_ms=12)
        if update is None:
            raise RuntimeError("postgres_restore_drill_update_missing")
        claim_ref = update.claims[0].ref
        self.conn.execute(
            """INSERT INTO news_semantic_observations
                 (result_id,work_id,event_id,input_revision,input_sha256,program_identity,
                  completed_at_ms,understanding,evidence_refs)
               VALUES (%s,%s,%s,1,%s,'restore_drill_v1',12,%s::jsonb,%s)""",
            (
                "restore-result",
                "restore-work",
                current_event_id,
                digest(frozen),
                _dumps(extraction.model_dump(mode="json")),
                [material.ref],
            ),
        )
        self.conn.execute(
            """INSERT INTO news_event_updates
                 (event_id,content_revision,input_revision,adopted_at_ms,observation_result_id,document)
               VALUES (%s,%s,1,12,'restore-result',%s::jsonb)""",
            (current_event_id, update.content_revision, update.model_dump_json()),
        )
        self.conn.execute(
            """INSERT INTO news_event_update_heads
                 (event_id,content_revision,input_revision,update_ref,adopted_at_ms)
               VALUES (%s,%s,1,%s,12)""",
            (current_event_id, update.content_revision, update.ref),
        )
        decision_input = {"reader_identity": "restore_drill_v1", "compared_receipts": [], "fixture": "restore_drill"}
        plan = NotificationPlan(
            action="notify",
            reason="uncovered_claims",
            update_ref=update.ref,
            claim_decisions=(ClaimDecision(claim_ref=claim_ref, decision="notify", reason="protected_listing"),),
            channel="news",
            reader_revision="restore-reader:12",
            reader_identity="restore_drill_v1",
            input_digest=digest(decision_input),
        )
        from .notification_jobs import NotificationJobDetail

        self.conn.execute(
            """INSERT INTO news_jobs(job_kind,subject_id,state,detail,next_attempt_at_ms,created_at_ms,updated_at_ms)
               VALUES ('notify',%s,'done',%s::jsonb,13,12,13)""",
            (
                current_event_id,
                NotificationJobDetail(
                    content_revision=update.content_revision,
                    decision_ref=plan.record_ref,
                    reader_revision=plan.reader_revision,
                ).model_dump_json(),
            ),
        )
        body = "恢复演练 · restore current"
        card = FrozenCard(
            intent_id=plan.intent_id,
            claim_refs=(claim_ref,),
            headline_zh="恢复演练",
            body=body,
            payload_sha256=digest(body),
        )
        self.conn.execute(
            """INSERT INTO news_notifications(notification_id,kind,origin,event_id,update_ref,input_digest,
                 input_snapshot,plan,decided_at_ms,intent_id,state,card,attempted_at_ms,settled_at_ms,created_at_ms,
                 updated_at_ms,content_revision,claim_refs,plan_key,error_code)
               VALUES (%s,'update','reader_v2',%s,%s,%s,%s::jsonb,%s::jsonb,12,%s,'terminal',%s::jsonb,
                       12,13,12,13,%s,%s::jsonb,false,'restore_drill')""",
            (
                plan.record_ref,
                current_event_id,
                update.ref,
                plan.input_digest,
                _dumps(decision_input),
                plan.model_dump_json(),
                plan.intent_id,
                card.model_dump_json(),
                update.content_revision,
                _dumps([claim_ref]),
            ),
        )
        return str(evidence_snapshot["evidence_sha256"])

    def expire_bands(self, *, now_ms: int, batch_size: int = 500) -> int:
        size = int(batch_size)
        if not 1 <= size <= RAW_RETENTION_BATCH_MAX:
            raise ValueError("news_band_expiry_batch_invalid")
        cursor = self.conn.execute(
            """
            WITH expired AS MATERIALIZED (
              SELECT band_index, band_key, event_id
                FROM news_event_bands
               WHERE expires_at_ms < %s
               ORDER BY expires_at_ms, band_index, band_key, event_id
               LIMIT %s
            )
            DELETE FROM news_event_bands band
             USING expired
             WHERE band.band_index = expired.band_index
               AND band.band_key = expired.band_key
               AND band.event_id = expired.event_id
            """,
            (int(now_ms), size),
        )
        return int(cursor.rowcount or 0)

    def purge_before(
        self,
        *,
        cutoff_ms: int,
        judged_cutoff_ms: int | None = None,
        batch_size: int = 500,
    ) -> dict[str, Any]:
        """Delete one stable batch of raw Items older than ``cutoff_ms``.

        Items that are evidence for an adopted EventUpdate newer than ``judged_cutoff_ms`` remain. The
        caller owns the transaction and repeats this method across transactions until ``backlog_capped`` is
        false or its turn budget is exhausted.

        Deleting `news_items` cascades to their Events and dependent evidence, updates, and deliveries.
        An Item is evidence when any Event
        it belongs to, as leader or later member, has an adopted update. Passing no ``judged_cutoff_ms`` uses the
            raw cutoff alone.
        """

        size = int(batch_size)
        if not 1 <= size <= RAW_RETENTION_BATCH_MAX:
            raise ValueError("news_raw_retention_batch_invalid")
        judged = None if judged_cutoff_ms is None else int(judged_cutoff_ms)
        candidate_params = (int(cutoff_ms), judged, judged, judged, size)
        candidates = self.conn.execute(RAW_RETENTION_CANDIDATE_SQL, candidate_params).fetchall()
        candidate_ids = [str(row["item_id"]) for row in candidates]
        deleted = []
        if candidate_ids:
            deleted = self.conn.execute(
                _RAW_RETENTION_DELETE_SQL,
                (candidate_ids, int(cutoff_ms), judged, judged, judged),
            ).fetchall()
        backlog = self.conn.execute(
            RAW_RETENTION_CANDIDATE_SQL,
            (int(cutoff_ms), judged, judged, judged, size + 1),
        ).fetchall()
        return {
            "candidate_items": len(candidate_ids),
            "deleted_items": len(deleted),
            "backlog_items": len(backlog),
            "backlog_capped": len(backlog) > size,
            "oldest_observed_at_ms": None if not backlog else int(backlog[0]["observed_at_ms"]),
        }

    def purge_market_observations(self, *, cutoff_ms: int, batch_size: int = 500) -> dict[str, Any]:
        size = int(batch_size)
        if not 1 <= size <= RAW_RETENTION_BATCH_MAX:
            raise ValueError("news_market_retention_batch_invalid")
        deleted = self.conn.execute(
            """
            DELETE FROM news_market_observations WHERE observation_id IN (
              SELECT observation_id FROM news_market_observations WHERE received_at_ms < %s
              ORDER BY received_at_ms,observation_id LIMIT %s)
            RETURNING observation_id
        """,
            (cutoff_ms, size),
        ).fetchall()
        backlog = self.conn.execute(
            """
            SELECT received_at_ms FROM news_market_observations WHERE received_at_ms < %s
            ORDER BY received_at_ms,observation_id LIMIT %s
        """,
            (cutoff_ms, size + 1),
        ).fetchall()
        return {
            "deleted_items": len(deleted),
            "backlog_items": len(backlog),
            "backlog_capped": len(backlog) > size,
            "oldest_observed_at_ms": None if not backlog else backlog[0]["received_at_ms"],
        }
