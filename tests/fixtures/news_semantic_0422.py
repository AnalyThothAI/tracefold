"""Frozen 0422 source helpers for historical migration proofs; no current storage calls."""

from typing import Any

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_update_pg import EVENT, STAMP, TEXT
from tracefold.news.updates.contracts import EventUpdate
from tracefold.news.updates.identity import canonical_json, digest


def latest_evidence(conn: Any, event_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT evidence_version,evidence_sha256,focus_fact_id,created_at_ms,snapshot,provenance,release_eligible "
        "FROM news_event_evidence_snapshots WHERE event_id=%s ORDER BY evidence_version DESC LIMIT 1",
        (event_id,),
    ).fetchone()


def seed_evidence(conn: Any, *, limit: int | None = None) -> None:
    """Bulk-create exact v3 evidence for current Event fixtures that do not exercise admission."""

    conn.execute(
        """
        WITH snapshots AS (
          SELECT event.event_id,
                 event.focus_fact_id,
                 event.created_at_ms,
                 jsonb_build_object(
                   'schema_version', 'news_event_evidence_v3',
                   'event_id', event.event_id,
                   'focus_fact', jsonb_build_object(
                     'fact_id', event.focus_fact_id,
                     'text', event.focus_fact_text,
                     'context', event.focus_fact_context,
                     'method', event.focus_fact_method,
                     'span_start', event.focus_span_start,
                     'span_end', event.focus_span_end
                   ),
                   'card',
                     (to_jsonb(event) - ARRAY[
                       'leader_title', 'context_line', 'search_doc', 'published_at_ms', 'followup_of',
                       'created_at_ms', 'updated_at_ms', 'focus_fact_text', 'focus_fact_context',
                       'focus_fact_method', 'focus_span_start', 'focus_span_end'
                     ]::text[])
                     || jsonb_build_object(
                       'leader_url', item.canonical_url,
                       'reporting_origin', item.reporting_origin,
                       'provider_metadata', item.provider_metadata,
                       'provenance', item.provenance,
                       'leader_published_at_ms', item.published_at_ms,
                       'raw_first_line', item.raw_first_line,
                       'leader_title', event.focus_fact_text,
                       'leader_description', event.focus_fact_context
                     ),
                   'members', '[]'::jsonb,
                   'provenance', 'observed'
                 ) AS snapshot
            FROM news_events event
            JOIN news_items item ON item.item_id = event.leader_item_id
           WHERE NOT EXISTS (
             SELECT 1 FROM news_event_evidence_snapshots evidence
              WHERE evidence.event_id = event.event_id
           )
           ORDER BY event.created_at_ms DESC, event.event_id
           LIMIT %s
        ), addressed AS (
          SELECT *, encode(sha256(
                   convert_to(news_canonical_jsonb(snapshot), 'UTF8')
                 ), 'hex') AS evidence_sha256
            FROM snapshots
        )
        INSERT INTO news_event_evidence_snapshots (
          event_id, evidence_version, focus_fact_id, evidence_sha256,
          provenance, release_eligible, snapshot, created_at_ms
        )
        SELECT event_id, 1, focus_fact_id, evidence_sha256,
               'observed', true, snapshot, created_at_ms
          FROM addressed
        """,
        (limit,),
    )


def seed_event(
    event_id: str = EVENT,
    *,
    text: str = TEXT,
    title: str = "Agency orders steel tariff",
    at_ms: int = STAMP,
    fingerprint: str = "fp-tariff",
) -> None:
    item_id = f"it-{event_id}"
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO news_items (
                  item_id, source_id, source_item_key, title, raw_first_line, description, canonical_url,
                  reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
                  first_ingest_mode, trace_id, created_at_ms, updated_at_ms, source_artifact_id,
                  evidence_text, evidence_text_sha256
                ) VALUES (%(item)s, 'opennews', %(item)s, %(title)s, %(title)s, '', 'https://www.reuters.com/a',
                          'Reuters', %(at)s, %(at)s, '{}'::jsonb, '[]'::jsonb, 'live', 'trace', %(at)s, %(at)s,
                          %(item)s, %(text)s, %(sha)s)
                """,
                {"item": item_id, "title": title, "at": at_ms, "text": text, "sha": digest(text)},
            )
            conn.execute(
                """
                INSERT INTO news_events (
                  event_id, leader_item_id, dedupe_family, comparison_fingerprint, comparison_title,
                  leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, admission, ingest_mode,
                  trace_id, created_at_ms, updated_at_ms, focus_fact_id, focus_fact_text,
                  focus_fact_context, focus_fact_method, focus_span_start, focus_span_end, event_kind
                ) VALUES (%(event)s, %(item)s, 'general', %(fp)s, %(title)s, %(title)s, %(at)s, %(at)s,
                          %(expires)s, 'candidate', 'live', 'trace', %(at)s, %(at)s, %(fact)s, %(title)s, '',
                          'whole_item', 0, 10, 'news')
                """,
                {
                    "event": event_id,
                    "item": item_id,
                    "fp": fingerprint,
                    "title": title,
                    "at": at_ms,
                    "expires": at_ms + 86_400_000,
                    "fact": f"fact-{item_id}",
                },
            )
            conn.execute(
                """
                INSERT INTO news_event_members (event_id, item_id, joined_at_ms, match_kind, fact_id, fact_text)
                VALUES (%s, %s, %s, 'leader', %s, %s)
                """,
                (event_id, item_id, at_ms, f"fact-{item_id}", title),
            )
            seed_evidence(conn)
            conn.execute(
                "INSERT INTO news_semantic_work(event_id,wanted_revision,lineage_id,next_attempt_at_ms,updated_at_ms) "
                "VALUES (%s,1,%s,%s,%s)",
                (event_id, f"lineage-{event_id}", at_ms, at_ms),
            )
    finally:
        conn.close()


def persist_update(conn: Any, update: EventUpdate, *, completed_at_ms: int | None = None) -> str:
    """Write one observation and its adopted revision, and move the head to it. Returns the result id."""

    result_id = f"result:{update.event_id}:{update.content_revision[:12]}"
    completed = int(completed_at_ms if completed_at_ms is not None else update.adopted_at_ms)
    conn.execute(
        """
        INSERT INTO news_semantic_observations (
          result_id, work_id, event_id, input_revision, input_sha256, program_identity, completed_at_ms,
          understanding
        ) VALUES (%s, %s, %s, %s, %s, 'news_updates:test-program', %s, '{}'::jsonb)
        """,
        (result_id, f"work:{update.event_id}", update.event_id, update.input_revision, "a" * 64, completed),
    )
    conn.execute(
        """
        INSERT INTO news_event_updates (
          event_id, content_revision, input_revision, previous_content_revision, adopted_at_ms,
          observation_result_id, document
        ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            update.event_id,
            update.content_revision,
            update.input_revision,
            update.previous_content_revision,
            update.adopted_at_ms,
            result_id,
            canonical_json(update),
        ),
    )
    conn.execute(
        """
        INSERT INTO news_event_update_heads (event_id, content_revision, input_revision, update_ref, adopted_at_ms)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO UPDATE
          SET content_revision = EXCLUDED.content_revision, input_revision = EXCLUDED.input_revision,
              update_ref = EXCLUDED.update_ref, adopted_at_ms = EXCLUDED.adopted_at_ms
        """,
        (update.event_id, update.content_revision, update.input_revision, update.ref, update.adopted_at_ms),
    )
    return result_id
