"""Pure SQL statement builders shared by the News feed runtime and its query audit."""

from __future__ import annotations

from typing import Final

# S608 exemptions below compose only the module's fixed feed predicate list; all request values stay bound.
from ..models import ADMITTED_ADMISSIONS, OUTBOX_MAX_AGE_MS

ADMITTED_SQL: Final = ", ".join(f"'{value}'" for value in sorted(ADMITTED_ADMISSIONS))
# Feed task tabs mirror OUTCOME_GROUP in outcome.py over the feed's joined rows. Keeping these predicates
# beside both statement builders makes the page and count query share one definition.
_EVENT_HANDOFF_LIVE_SQL: Final = (
    f"e.published_at_ms IS NOT NULL OR e.opened_at_ms >= clock.handoff_now_ms - {OUTBOX_MAX_AGE_MS}"
)
_VERDICT_HANDOFF_LIVE_SQL: Final = (
    f"t.published_at_ms IS NOT NULL OR t.created_at_ms >= clock.handoff_now_ms - {OUTBOX_MAX_AGE_MS}"
)
_PENDING_CORE_SQL: Final = (
    "COALESCE(d.state = 'sending', false) OR ("
    "d.state IS NULL"
    f" AND e.admission IN ({ADMITTED_SQL})"
    " AND ((t.final_decision IS NULL AND ("
    f"{_EVENT_HANDOFF_LIVE_SQL}"
    ")) OR (COALESCE(t.final_decision IN ('push', 'escalate'), false) AND ("
    f"{_VERDICT_HANDOFF_LIVE_SQL}"
    "))))"
)
OUTCOME_GROUP_SQL: Final = {
    "pushed": "d.state = 'sent'",
    "pending": f"COALESCE(d.state, '') <> 'sent' AND ({_PENDING_CORE_SQL})",
    "held": f"COALESCE(d.state, '') <> 'sent' AND NOT ({_PENDING_CORE_SQL})",
}
# Both feed laterals expose the judged OI rule under this alias. The page and count builders must spell it
# identically because downstream filters append a predicate over ``t.oi_rule`` to either statement.
OI_RULE_SQL: Final = "v.trace #>> '{judgment,rule}' AS oi_rule"
ASSET_SEARCH_PREDICATE: Final = (
    "EXISTS (SELECT 1 FROM news_event_assets a WHERE a.event_id = e.event_id AND a.symbol = ANY(%s))"
)
TEXT_SEARCH_PREDICATE: Final = "e.search_doc @@ websearch_to_tsquery('simple', %s)"
CURRENT_EVENT_CARD_SQL: Final = """
    SELECT e.event_id, e.leader_item_id, e.dedupe_family, e.comparison_fingerprint,
           e.comparison_title, e.leader_title, e.opened_at_ms, e.last_member_at_ms,
           e.expires_at_ms, e.member_count, e.admission, e.queue_priority, e.provider_score_max,
           e.engine_type, e.asset_class, e.grounded_assets, e.watchlist_hits, e.macro_lexicon,
           e.storyline_key, e.context_line, e.search_doc, e.published_at_ms, e.followup_of,
           e.ingest_mode, e.trace_id, e.created_at_ms, e.updated_at_ms, e.focus_fact_id,
           e.focus_fact_text, e.focus_fact_context, e.focus_fact_method, e.focus_span_start,
           e.focus_span_end, e.event_kind, e.source_contract_reason,
           e.current_contract_archive_only,
           i.description AS leader_description, i.canonical_url AS leader_url, i.reporting_origin,
           i.provider_metadata, i.provenance, i.published_at_ms AS leader_published_at_ms,
           i.raw_first_line
      FROM news_current_events_v1 e
      JOIN news_items i ON i.item_id = e.leader_item_id
     WHERE e.event_id = %s
"""
EVENT_VERDICTS_SQL: Final = """
    SELECT event_id, stage, policy_version, rule_baseline_decision, final_decision,
           override_rule, throttled_by, verdict, model, degraded, error_code, trace,
           published_at_ms, created_at_ms, evidence_version, evidence_sha256, focus_fact_id,
           program_version, program_sha256, editorial, scored_judgment_sha256,
           judgment_contract_version, judgment_origin
      FROM news_verdicts
     WHERE event_id = %s AND judgment_contract_version = 'news_judgment_v2'
     ORDER BY created_at_ms
"""
STATUS_INGEST_SQL: Final = """
    SELECT connected, last_frame_at_ms, last_publish_at_ms, last_error_code, broker_snapshot
      FROM news_ingest_state
     WHERE singleton_key = 'opennews'
"""
STATUS_LEARNING_RETENTION_SQL: Final = """
    SELECT last_run_at_ms, eligible_recordings, eligible_cases, eligible_artifacts,
           deleted_recordings, deleted_cases, deleted_artifacts, oldest_recording_age_ms,
           oldest_case_age_ms, oldest_artifact_age_ms, last_error_code, updated_at_ms
      FROM news_learning_retention_state
     WHERE singleton
"""


def feed_page_sql(where_sql: str) -> str:
    """Build the production page statement from one already-bound predicate list.

    The query audit calls this same builder with representative AssetSearch and TextSearch predicates,
    so its plans cannot silently drift back to a simplified SQL sketch.
    """

    return f"""
        WITH clock AS (SELECT %s::bigint AS handoff_now_ms)
        SELECT e.event_id, e.event_kind, e.source_contract_reason, e.leader_title,
               e.opened_at_ms, e.last_member_at_ms, e.member_count,
               e.admission, e.provider_score_max, e.engine_type, e.asset_class, e.grounded_assets,
               e.watchlist_hits, e.storyline_key, e.context_line, e.published_at_ms, e.ingest_mode,
               i.canonical_url AS leader_url, i.reporting_origin, i.provenance,
               t.final_decision, t.override_rule, t.throttled_by, t.degraded AS triage_degraded,
               t.error_code AS triage_error_code, t.created_at_ms AS verdict_created_at_ms,
               t.published_at_ms AS verdict_published_at_ms,
               t.verdict ->> 'direction' AS direction, (t.verdict ->> 'magnitude')::int AS magnitude,
               t.verdict ->> 'headline_zh' AS headline_zh, t.verdict ->> 'scope' AS scope,
               t.verdict AS triage_verdict, t.editorial AS model_editorial,
               t.trace -> 'judgment' AS oi_judgment,
               t.trace -> 'oi_signal' AS oi_metadata,
               d.state AS delivery_state, d.settled_at_ms AS delivered_at_ms, d.error_code AS delivery_error_code
          FROM clock CROSS JOIN news_current_events_v1 e
          JOIN news_items i ON i.item_id = e.leader_item_id
          JOIN LATERAL (
            SELECT s.provenance, s.snapshot
              FROM news_event_evidence_snapshots s
             WHERE s.event_id = e.event_id
             ORDER BY s.evidence_version DESC LIMIT 1
          ) current_evidence
            ON current_evidence.provenance = 'observed'
           AND current_evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
          LEFT JOIN LATERAL (
            SELECT v.final_decision, v.override_rule, v.throttled_by, v.degraded, v.error_code,
                   v.created_at_ms, v.published_at_ms, v.verdict, v.editorial, v.trace,
                   v.verdict ->> 'direction' AS direction, {OI_RULE_SQL}
              FROM news_verdicts v
             WHERE v.event_id = e.event_id AND v.stage = 'triage'
               AND v.judgment_contract_version = 'news_judgment_v2'
             ORDER BY v.created_at_ms DESC LIMIT 1
          ) t ON true
          LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
         WHERE {where_sql}
         ORDER BY e.opened_at_ms DESC, e.event_id DESC
         LIMIT %s
    """  # noqa: S608


def feed_counts_sql(where_sql: str) -> str:
    """Build the production first-page count statement for the same predicate list as the page."""

    return f"""
        WITH clock AS (SELECT %s::bigint AS handoff_now_ms)
        SELECT count(*) AS total,
               count(*) FILTER (WHERE {OUTCOME_GROUP_SQL["pushed"]}) AS pushed,
               count(*) FILTER (WHERE {OUTCOME_GROUP_SQL["held"]}) AS held,
               count(*) FILTER (WHERE {OUTCOME_GROUP_SQL["pending"]}) AS pending
          FROM clock CROSS JOIN news_current_events_v1 e
          JOIN news_items i ON i.item_id = e.leader_item_id
          JOIN LATERAL (
            SELECT s.provenance, s.snapshot
              FROM news_event_evidence_snapshots s
             WHERE s.event_id = e.event_id
             ORDER BY s.evidence_version DESC LIMIT 1
          ) current_evidence
            ON current_evidence.provenance = 'observed'
           AND current_evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
          LEFT JOIN LATERAL (
            SELECT v.final_decision, v.editorial, v.created_at_ms, v.published_at_ms,
                   v.verdict ->> 'direction' AS direction, {OI_RULE_SQL}
              FROM news_verdicts v
             WHERE v.event_id = e.event_id AND v.stage = 'triage'
               AND v.judgment_contract_version = 'news_judgment_v2'
             ORDER BY v.created_at_ms DESC LIMIT 1
          ) t ON true
          LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
         WHERE {where_sql}
    """  # noqa: S608


__all__ = [
    "ADMITTED_SQL",
    "ASSET_SEARCH_PREDICATE",
    "CURRENT_EVENT_CARD_SQL",
    "EVENT_VERDICTS_SQL",
    "OI_RULE_SQL",
    "OUTCOME_GROUP_SQL",
    "STATUS_INGEST_SQL",
    "STATUS_LEARNING_RETENTION_SQL",
    "TEXT_SEARCH_PREDICATE",
    "feed_counts_sql",
    "feed_page_sql",
]
