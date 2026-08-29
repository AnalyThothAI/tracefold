"""Pure SQL statement builders shared by the News feed runtime and its query audit."""

from __future__ import annotations

from typing import Final

from ..models import ADMITTED_ADMISSIONS

ADMITTED_SQL: Final = ", ".join(f"'{value}'" for value in sorted(ADMITTED_ADMISSIONS))
# Feed task tabs mirror OUTCOME_GROUP in outcome.py over the feed's joined rows. Keeping these predicates
# beside both statement builders makes the page and count query share one definition.
_PENDING_CORE_SQL: Final = (
    "COALESCE(d.state = 'sending', false) OR ("
    "d.state IS NULL"
    f" AND e.admission IN ({ADMITTED_SQL})"
    " AND (t.final_decision IS NULL OR t.final_decision IN ('push', 'escalate')))"
)
OUTCOME_GROUP_SQL: Final = {
    "pushed": "d.state = 'sent'",
    "pending": f"COALESCE(d.state, '') <> 'sent' AND ({_PENDING_CORE_SQL})",
    "held": f"COALESCE(d.state, '') <> 'sent' AND NOT ({_PENDING_CORE_SQL})",
}
# Both feed laterals expose the judged OI rule under this alias. The page and count builders must spell it
# identically because downstream filters append a predicate over ``t.oi_rule`` to either statement.
OI_RULE_SQL: Final = "v.trace -> 'oi_signal' ->> 'rule' AS oi_rule"
ASSET_SEARCH_PREDICATE: Final = (
    "EXISTS (SELECT 1 FROM news_event_assets a WHERE a.event_id = e.event_id AND a.symbol = ANY(%s))"
)
TEXT_SEARCH_PREDICATE: Final = "e.search_doc @@ websearch_to_tsquery('simple', %s)"


def feed_page_sql(where_sql: str) -> str:
    """Build the production page statement from one already-bound predicate list.

    The query audit calls this same builder with representative AssetSearch and TextSearch predicates,
    so its plans cannot silently drift back to a simplified SQL sketch.
    """

    return f"""
        SELECT e.event_id, e.family, e.event_kind, e.source_contract_reason, e.leader_title,
               e.opened_at_ms, e.last_member_at_ms, e.member_count,
               e.admission, e.provider_score_max, e.engine_type, e.asset_class, e.grounded_assets,
               e.watchlist_hits, e.storyline_key, e.context_line, e.published_at_ms, e.ingest_mode,
               i.canonical_url AS leader_url, i.reporting_origin, i.provenance,
               t.final_decision, t.override_rule, t.throttled_by, t.degraded AS triage_degraded,
               t.error_code AS triage_error_code,
               t.verdict ->> 'direction' AS direction, (t.verdict ->> 'magnitude')::int AS magnitude,
               t.verdict ->> 'event_type' AS event_type, t.verdict ->> 'headline_zh' AS headline_zh,
               t.verdict ->> 'scope' AS scope, t.verdict ->> 'title_zh' AS title_zh,
               t.model_decision, t.verdict AS triage_verdict, t.trace -> 'oi_signal' AS oi_signal,
               d.state AS delivery_state, d.settled_at_ms AS delivered_at_ms, d.error_code AS delivery_error_code
          FROM news_events e
          JOIN news_items i ON i.item_id = e.leader_item_id
          LEFT JOIN LATERAL (
            SELECT v.*, v.verdict ->> 'direction' AS direction, {OI_RULE_SQL}
              FROM news_verdicts v
             WHERE v.event_id = e.event_id AND v.stage = 'triage'
             ORDER BY v.created_at_ms DESC LIMIT 1
          ) t ON true
          LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
         WHERE {where_sql}
         ORDER BY e.opened_at_ms DESC, e.event_id DESC
         LIMIT %s
    """


def feed_counts_sql(where_sql: str) -> str:
    """Build the production first-page count statement for the same predicate list as the page."""

    return f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE {OUTCOME_GROUP_SQL["pushed"]}) AS pushed,
               count(*) FILTER (WHERE {OUTCOME_GROUP_SQL["held"]}) AS held,
               count(*) FILTER (WHERE {OUTCOME_GROUP_SQL["pending"]}) AS pending
          FROM news_events e
          JOIN news_items i ON i.item_id = e.leader_item_id
          LEFT JOIN LATERAL (
            SELECT v.final_decision, v.verdict ->> 'direction' AS direction, {OI_RULE_SQL}
              FROM news_verdicts v
             WHERE v.event_id = e.event_id AND v.stage = 'triage'
             ORDER BY v.created_at_ms DESC LIMIT 1
          ) t ON true
          LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
         WHERE {where_sql}
    """


__all__ = [
    "ADMITTED_SQL",
    "ASSET_SEARCH_PREDICATE",
    "OI_RULE_SQL",
    "OUTCOME_GROUP_SQL",
    "TEXT_SEARCH_PREDICATE",
    "feed_counts_sql",
    "feed_page_sql",
]
