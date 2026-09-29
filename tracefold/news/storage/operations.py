"""OpenNews ingest incidents, broker state, and bounded retention operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

# S608 exemption below appends one fixed optional predicate; incident values remain bound parameters.
from .sql_values import _dumps

RECOVERY_BACKLOG_LIMIT = 20
RAW_RETENTION_BATCH_MAX = 1_000
INGEST_LIVENESS_SQL = """
    SELECT connected, updated_at_ms
      FROM news_ingest_state
     WHERE singleton_key = 'opennews'
"""
_PENDING_RECOVERY_INCIDENTS_SQL = """
    SELECT incident_id, cause_class, opened_at_ms, closed_at_ms, recovery_from_at_ms,
           recovery_to_at_ms, last_error_code, updated_at_ms
      FROM news_opennews_incidents
     WHERE recovery_status = 'pending' AND closed_at_ms IS NOT NULL
     ORDER BY incident_id
     LIMIT %s
"""

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

# A market Item lives on the judged tier whatever happened to it (#553). It has no verdict, no review
# and no learning case, so the evidence predicate below can never preserve one -- under `raw_days`
# alone every OI frame, liquidation report and account report would expire in 30 days while the
# ordinary news it sits beside keeps a year. Which retention an observation gets is a decision about
# the observation, not a reward for having been judged.
_MARKET_RETENTION_SQL = """
  AND (
        %s::bigint IS NULL
        OR i.market_kind IS NULL
        OR i.observed_at_ms < %s
      )
"""

RAW_RETENTION_CANDIDATE_SQL = f"""
    SELECT i.item_id, i.observed_at_ms
      FROM news_items i
     WHERE i.observed_at_ms < %s
       {_MARKET_RETENTION_SQL}
       {_RAW_RETENTION_PRESERVATION_SQL}
     ORDER BY i.observed_at_ms, i.item_id
     LIMIT %s
"""  # noqa: S608

_RAW_RETENTION_DELETE_SQL = f"""
    DELETE FROM news_items i
     WHERE i.item_id = ANY(%s)
       AND i.observed_at_ms < %s
       {_MARKET_RETENTION_SQL}
       {_RAW_RETENTION_PRESERVATION_SQL}
 RETURNING i.item_id, i.observed_at_ms
"""  # noqa: S608


# The open-incident list `/api/news/status` renders, and the statement its query audit plans. The audit
# used to carry a copy that omitted `planned`, which is a different projection of the same rows (#570 A2).
OPEN_INCIDENTS_SQL = (
    "SELECT incident_id, cause_class, opened_at_ms, planned FROM news_opennews_incidents"
    " WHERE closed_at_ms IS NULL ORDER BY incident_id"
)


def pending_recovery_incidents_statement(*, limit: int) -> tuple[str, tuple[int]]:
    """Return the exact bounded statement shared by Recovery, status, and query audit."""

    return _PENDING_RECOVERY_INCIDENTS_SQL, (int(limit),)


class OperationsStorage:
    conn: Any

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
        from ..updates.notification import ClaimDecision, FrozenCard, NotificationPlan
        from ..updates.semantics import assemble_update

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
        self.conn.execute(
            """INSERT INTO news_notification_decisions
                 (decision_ref,event_id,update_ref,channel,input_digest,input_snapshot,plan,origin,created_at_ms)
               VALUES (%s,%s,%s,'news',%s,%s::jsonb,%s::jsonb,'reader_v2',12)""",
            (
                plan.record_ref,
                current_event_id,
                update.ref,
                plan.input_digest,
                _dumps(decision_input),
                plan.model_dump_json(),
            ),
        )
        self.conn.execute(
            """INSERT INTO news_notification_work
                 (event_id,channel,content_revision,state,decision_ref,reader_revision,
                  attempts,next_attempt_at_ms,updated_at_ms)
               VALUES (%s,'news',%s,'done',%s,%s,0,13,13)""",
            (current_event_id, update.content_revision, plan.record_ref, plan.reader_revision),
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
            """INSERT INTO news_deliveries
                 (intent_id,event_id,kind,state,card,attempted_at_ms,settled_at_ms,
                  created_at_ms,content_revision,claim_refs,body,payload_sha256,plan_key,decision_ref,error_code)
               VALUES (%s,%s,'update','terminal',%s::jsonb,12,13,12,%s,%s::jsonb,%s,%s,false,%s,'restore_drill')""",
            (
                plan.intent_id,
                current_event_id,
                card.model_dump_json(),
                update.content_revision,
                _dumps([claim_ref]),
                body,
                card.payload_sha256,
                plan.record_ref,
            ),
        )
        return str(evidence_snapshot["evidence_sha256"])

    def update_ingest_state(
        self,
        *,
        now_ms: int,
        connected: bool | None = None,
        last_frame_at_ms: int | None = None,
        last_publish_at_ms: int | None = None,
        last_error_code: str | None = None,
        clear_error: bool = False,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_ingest_state
               SET connected = COALESCE(%s, connected),
                   last_frame_at_ms = COALESCE(%s, last_frame_at_ms),
                   last_publish_at_ms = COALESCE(%s, last_publish_at_ms),
                   last_error_code = CASE WHEN %s THEN NULL ELSE COALESCE(%s, last_error_code) END,
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE singleton_key = 'opennews'
            """,
            (
                connected,
                last_frame_at_ms,
                last_publish_at_ms,
                bool(clear_error),
                last_error_code,
                int(now_ms),
            ),
        )

    def ingest_liveness(self) -> dict[str, Any] | None:
        """What the durable row still claims about the last Receiver process, and when it last wrote.

        `connected` is only ever set true by a live connection and false by a reported disconnect, so a
        process that starts and finds it still true is reading the trace of one that never reported one.
        `updated_at_ms` is the last write of any kind — a frame, a connection change, or the Janitor's
        minute snapshot — which makes it the last moment that process is known to have been running.
        """

        row = self.conn.execute(INGEST_LIVENESS_SQL).fetchone()
        if row is None:
            return None
        return {"connected": bool(row["connected"]), "updated_at_ms": int(row["updated_at_ms"])}

    def update_broker_snapshot(self, *, snapshot: Mapping[str, Any], now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_ingest_state SET broker_snapshot = %s::jsonb, updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE singleton_key = 'opennews'
            """,
            (_dumps({**dict(snapshot), "observed_at_ms": int(now_ms)}), int(now_ms)),
        )

    def open_incident(
        self, *, cause_class: str, now_ms: int, planned: bool = False, close_code: int | None = None
    ) -> int:
        """Open the one incident of this cause class, or return the one already open.

        This is a single idempotent statement resting on the partial unique index
        `ux_news_opennews_incidents_open_cause` (migration 0335), not a read-then-write. Two writers
        racing therefore converge in PostgreSQL rather than through application locking, and no caller
        needs to remember whether it already opened this incident.
        """

        row = self.conn.execute(
            """
            INSERT INTO news_opennews_incidents (
              cause_class, opened_at_ms, planned, close_code, recovery_status, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cause_class) WHERE closed_at_ms IS NULL
            DO UPDATE SET updated_at_ms = GREATEST(
              news_opennews_incidents.updated_at_ms, EXCLUDED.updated_at_ms
            )
            RETURNING incident_id
            """,
            (
                cause_class,
                int(now_ms),
                bool(planned),
                close_code,
                "not_applicable" if cause_class == "triage_circuit_open" else "pending",
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("news_incident_open_unresolved")
        return int(row["incident_id"])

    def close_open_incidents(self, *, cause_classes: Sequence[str] | None, now_ms: int) -> int:
        cause_filter = "" if cause_classes is None else " AND cause_class = ANY(%s)"
        params: tuple[Any, ...] = (int(now_ms), int(now_ms), int(now_ms))
        if cause_classes is not None:
            params = (*params, list(cause_classes))
        cursor = self.conn.execute(
            f"""
            UPDATE news_opennews_incidents
               SET closed_at_ms = %s, recovery_to_at_ms = COALESCE(recovery_to_at_ms, %s),
                   recovery_status = CASE
                     WHEN cause_class IN ('broker_backpressure', 'broker_unavailable') THEN 'pending'
                     ELSE recovery_status
                   END,
                   updated_at_ms = %s
             WHERE closed_at_ms IS NULL{cause_filter}
            """,  # noqa: S608
            params,
        )
        return int(cursor.rowcount or 0)

    def pending_recovery_incidents(self, *, limit: int = 20) -> list[dict[str, Any]]:
        sql, params = pending_recovery_incidents_statement(limit=limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def recovery_backlog(self) -> dict[str, Any]:
        rows = self.pending_recovery_incidents(limit=RECOVERY_BACKLOG_LIMIT)
        pending_count = len(rows)
        oldest_opened_at_ms = min((int(row["opened_at_ms"]) for row in rows), default=None)
        latest_error = max(
            (row for row in rows if row["last_error_code"] is not None),
            key=lambda row: (int(row["updated_at_ms"]), int(row["incident_id"])),
            default=None,
        )
        last_error_code = latest_error["last_error_code"] if latest_error is not None else None
        return {
            "pending_count": pending_count,
            "oldest_opened_at_ms": oldest_opened_at_ms,
            "last_error_code": last_error_code,
            "reason": (
                "recovery_transient"
                if pending_count and last_error_code is not None
                else ("recovery_pending" if pending_count else None)
            ),
        }

    def open_incident_summary(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT cause_class, count(*)::int AS count, min(opened_at_ms) AS oldest_opened_at_ms
              FROM news_opennews_incidents
             WHERE closed_at_ms IS NULL
             GROUP BY cause_class
             ORDER BY cause_class
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def record_recovery_error(self, *, incident_id: int, error_code: str, now_ms: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_opennews_incidents
               SET last_error_code = %s, updated_at_ms = %s
             WHERE incident_id = %s AND recovery_status = 'pending'
            """,
            (str(error_code)[:200], int(now_ms), int(incident_id)),
        )
        return bool(cursor.rowcount)

    def complete_recovery(
        self,
        *,
        incident_id: int,
        status: str,
        recovered_count: int,
        error_code: str | None,
        recovery_from_at_ms: int | None,
        recovery_to_at_ms: int | None,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_opennews_incidents
               SET recovery_status = %s, recovered_count = recovered_count + %s, last_error_code = %s,
                   recovery_from_at_ms = COALESCE(%s, recovery_from_at_ms),
                   recovery_to_at_ms = COALESCE(%s, recovery_to_at_ms), updated_at_ms = %s
             WHERE incident_id = %s AND recovery_status = 'pending'
            """,
            (
                status,
                int(recovered_count),
                error_code,
                recovery_from_at_ms,
                recovery_to_at_ms,
                int(now_ms),
                int(incident_id),
            ),
        )
        return bool(cursor.rowcount)

    def open_incidents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(OPEN_INCIDENTS_SQL).fetchall()
        return [dict(r) for r in rows]

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
        It also cascades to market facts whose source Item is removed. An Item is evidence when any Event
        it belongs to, as leader or later member, has an adopted update. A market Item is kept for the
        adopted period outright. Passing no ``judged_cutoff_ms`` uses the raw cutoff alone.
        """

        size = int(batch_size)
        if not 1 <= size <= RAW_RETENTION_BATCH_MAX:
            raise ValueError("news_raw_retention_batch_invalid")
        judged = None if judged_cutoff_ms is None else int(judged_cutoff_ms)
        candidate_params = (int(cutoff_ms), judged, judged, judged, judged, judged, size)
        candidates = self.conn.execute(RAW_RETENTION_CANDIDATE_SQL, candidate_params).fetchall()
        candidate_ids = [str(row["item_id"]) for row in candidates]
        deleted = []
        if candidate_ids:
            deleted = self.conn.execute(
                _RAW_RETENTION_DELETE_SQL,
                (candidate_ids, int(cutoff_ms), judged, judged, judged, judged, judged),
            ).fetchall()
        backlog = self.conn.execute(
            RAW_RETENTION_CANDIDATE_SQL,
            (int(cutoff_ms), judged, judged, judged, judged, judged, size + 1),
        ).fetchall()
        return {
            "candidate_items": len(candidate_ids),
            "deleted_items": len(deleted),
            "backlog_items": len(backlog),
            "backlog_capped": len(backlog) > size,
            "oldest_observed_at_ms": None if not backlog else int(backlog[0]["observed_at_ms"]),
        }
