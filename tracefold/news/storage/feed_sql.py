"""Pure SQL statement builders shared by the News feed runtime and its query audit."""

from __future__ import annotations

from typing import Final

# S608 exemptions below compose only the module's fixed feed predicate list; all request values stay bound.
from ..models import ADMITTED_ADMISSIONS, OUTBOX_MAX_AGE_MS
from ..source_contracts import EVENT_KINDS

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
# The News feed is the editorial Event feed and nothing else (#553). Market observations are stored as
# facts beside their Item and read through `/api/news/market`; a feed row for one would be a second copy
# of live market data under an editorial vocabulary it never had. Events of a retired market kind stay in
# PostgreSQL as immutable evidence, and this predicate is what stops the live feed reading them back.
EVENT_KIND_SQL: Final = ", ".join(f"'{value}'" for value in EVENT_KINDS)
EDITORIAL_EVENT_SQL: Final = f"e.event_kind IN ({EVENT_KIND_SQL})"
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
           e.focus_span_end, e.event_kind,
           i.description AS leader_description, i.canonical_url AS leader_url, i.reporting_origin,
           i.provider_metadata, i.provenance, i.published_at_ms AS leader_published_at_ms,
           i.raw_first_line
      FROM news_events e
      JOIN news_items i ON i.item_id = e.leader_item_id
     WHERE e.event_id = %s
"""
# The public Event read. The statement above is the raw row, and internal callers that already know an
# Event's identity keep using it; this one is what a *reader* may be served, and the two differ by
# exactly the predicate the feed uses. The migration keeps every pre-cut market Event, `EventKind` no
# longer names their kinds, and a bookmarked or pushed link to one is an ordinary thing to still have
# -- so it has to resolve to "not an Event" rather than to a row the response envelope cannot
# validate. The observation that Event was built from is readable at `/api/news/market`.
EDITORIAL_EVENT_CARD_SQL: Final = f"{CURRENT_EVENT_CARD_SQL.rstrip()}\n       AND {EDITORIAL_EVENT_SQL}\n"
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

# Every statement `/api/news/status` executes, in one place. The query audit registers these exact
# constants, so the page and its plan evidence cannot be two different queries: the audit used to carry
# a `count(news_verdicts)` sketch while the route ran the correlated latest-Evidence subquery, the
# percentile aggregates and the funnel beside it, and a passing audit said nothing about either (#570 A2).
# One bounded Event cohort, projected into the two editorial source-contract funnels.
STATUS_SOURCE_CONTRACTS_SQL: Final = """
    SELECT e.event_kind, count(*) AS received,
           count(*) FILTER (WHERE COALESCE(v.has_verdict, false)) AS verdict
      FROM news_events e
      LEFT JOIN LATERAL (
        SELECT bool_or(true) AS has_verdict
         FROM news_verdicts
         WHERE event_id = e.event_id AND stage = 'triage'
           AND judgment_contract_version = 'news_judgment_v2'
      ) v ON true
     WHERE e.opened_at_ms >= %s
       AND EXISTS (
         SELECT 1 FROM news_event_evidence_snapshots evidence
          WHERE evidence.event_id = e.event_id
            AND evidence.evidence_version = (
              SELECT max(latest.evidence_version) FROM news_event_evidence_snapshots latest
               WHERE latest.event_id = e.event_id
            )
            AND evidence.provenance = 'observed'
            AND evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
       )
     GROUP BY e.event_kind
"""

STATUS_PIPELINE_SQL: Final = """
    WITH event_counts AS (
      SELECT
        count(*) FILTER (WHERE opened_at_ms >= %s) AS events_1h,
        count(*) AS events_24h,
        count(*) FILTER (WHERE admission = 'candidate') AS candidates_24h
        FROM news_events current_event
       WHERE current_event.opened_at_ms >= %s
         AND EXISTS (
           SELECT 1 FROM news_event_evidence_snapshots evidence
            WHERE evidence.event_id = current_event.event_id
              AND evidence.evidence_version = (
                SELECT max(latest.evidence_version) FROM news_event_evidence_snapshots latest
                 WHERE latest.event_id = current_event.event_id
              )
              AND evidence.provenance = 'observed'
              AND evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
         )
    ), verdict_counts AS (
      SELECT
        -- Two denominators on purpose. The funnel is the reader's view — 收到 ⊇ 送审 ⊇ 模型判断
        -- ⊇ 决定推送 ⊇ 已送达, subtracted band by band by the console — and a telemetry judgment
        -- is a judgment and its push is a card the reader received, so both count here or the
        -- containment breaks at one end or the other. Model health is a different question and
        -- gets its own denominator below: ~190 arithmetic judgments a day, never degraded,
        -- would otherwise dilute the degraded share and make the model look healthier than it is.
        count(*) AS triage_24h,
        count(*) FILTER (
          WHERE judgment_origin IN ('model', 'degraded')
        ) AS model_triage_24h,
        count(*) FILTER (WHERE degraded) AS triage_degraded_24h,
        count(*) FILTER (WHERE final_decision IN ('push','escalate')) AS decided_push_24h,
        count(*) FILTER (WHERE final_decision = 'throttled') AS throttled_24h,
        -- #221: generated stored columns preserve the status values without decompressing the 26 MB
        -- daily TOAST corpus. One aggregate pass then replaces the old ten verdict scans.
        percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
          FILTER (WHERE latency_ms IS NOT NULL) AS triage_p50_ms,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
          FILTER (WHERE latency_ms IS NOT NULL) AS triage_p95_ms,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY queue_lag_ms)
          FILTER (WHERE queue_lag_ms IS NOT NULL) AS queue_lag_p95_ms,
        count(*) FILTER (WHERE reasked_after_told_change) AS reasked_24h
        FROM news_verdicts
       WHERE stage = 'triage' AND created_at_ms >= %s
         AND judgment_contract_version = 'news_judgment_v2'
    )
    SELECT event_counts.events_1h, event_counts.events_24h, event_counts.candidates_24h,
           verdict_counts.triage_24h, verdict_counts.model_triage_24h,
           verdict_counts.triage_degraded_24h, verdict_counts.decided_push_24h,
           verdict_counts.throttled_24h, verdict_counts.triage_p50_ms,
           verdict_counts.triage_p95_ms, verdict_counts.queue_lag_p95_ms,
           verdict_counts.reasked_24h
      FROM event_counts CROSS JOIN verdict_counts
"""

STATUS_DELIVERY_SQL: Final = """
    SELECT
      (SELECT count(*) FROM news_deliveries d
         JOIN news_events e ON e.event_id = d.event_id
        WHERE d.state = 'sent' AND d.settled_at_ms >= %s) AS sent_24h,
      (SELECT count(*) FROM news_deliveries d
         JOIN news_events e ON e.event_id = d.event_id
        WHERE d.state = 'sent' AND d.settled_at_ms >= %s) AS sent_1h,
      (SELECT count(*) FROM news_deliveries d
         JOIN news_events e ON e.event_id = d.event_id
        WHERE d.state = 'terminal' AND d.settled_at_ms >= %s) AS terminal_24h,
      (SELECT d.error_code FROM news_deliveries d
         JOIN news_events e ON e.event_id = d.event_id
        WHERE d.state = 'terminal'
        ORDER BY d.settled_at_ms DESC NULLS LAST LIMIT 1) AS last_error_code,
      (SELECT percentile_cont(0.5)
         WITHIN GROUP (ORDER BY (d.settled_at_ms - i.observed_at_ms)::double precision)
         FROM news_deliveries d JOIN news_events e ON e.event_id = d.event_id
         JOIN news_items i ON i.item_id = e.leader_item_id
        WHERE d.state = 'sent' AND d.kind = 'first' AND d.settled_at_ms >= %s) AS e2e_p50_ms,
      (SELECT percentile_cont(0.95)
         WITHIN GROUP (ORDER BY (d.settled_at_ms - i.observed_at_ms)::double precision)
         FROM news_deliveries d JOIN news_events e ON e.event_id = d.event_id
         JOIN news_items i ON i.item_id = e.leader_item_id
        WHERE d.state = 'sent' AND d.kind = 'first' AND d.settled_at_ms >= %s) AS e2e_p95_ms
"""

# The four funnel statements. `_funnel_24h` folds their rows into the named reasons a reader sees.
STATUS_FUNNEL_SUPPRESSED_SQL: Final = f"""
    SELECT admission, count(*) AS n FROM news_events current_event
     WHERE current_event.opened_at_ms >= %s AND admission NOT IN ({ADMITTED_SQL})
       AND EXISTS (
         SELECT 1 FROM news_event_evidence_snapshots evidence
          WHERE evidence.event_id = current_event.event_id
            AND evidence.evidence_version = (
              SELECT max(latest.evidence_version) FROM news_event_evidence_snapshots latest
               WHERE latest.event_id = current_event.event_id
            )
            AND evidence.provenance = 'observed'
            AND evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
       )
     GROUP BY admission ORDER BY n DESC
"""  # noqa: S608

# One pass over the last 24 h of Triage verdicts; the four named maps are folded from it in Python.
STATUS_FUNNEL_VERDICTS_SQL: Final = """
    SELECT final_decision, COALESCE(override_rule, 'unknown') AS rule,
           COALESCE(throttled_by, 'unknown') AS key, degraded, COALESCE(error_code, 'unknown') AS code,
           count(*) AS n
     FROM news_verdicts
     WHERE stage = 'triage' AND created_at_ms >= %s
       AND judgment_contract_version = 'news_judgment_v2'
     GROUP BY 1, 2, 3, 4, 5
"""

STATUS_FUNNEL_REVIEWS_SQL: Final = """
    WITH current_epoch AS (
      SELECT epoch.starts_at_ms
        FROM news_review_active_agent_v1 active
        JOIN news_learning_epochs epoch ON epoch.bundle_sha = active.stable_sha
       ORDER BY active.created_at_ms DESC
       LIMIT 1
    )
    SELECT count(*) FILTER (WHERE j.should_push IN ('must_push', 'should_push')) AS n,
           count(*) FILTER (
             WHERE j.subject_kind = 'external_miss'
               AND j.should_push IN ('must_push', 'should_push')
           ) AS external
      FROM news_review_records_v1 acceptance
      JOIN news_review_records_v1 j ON j.review_id = acceptance.accepts_review_id
      JOIN current_epoch ON true
     WHERE acceptance.review_kind = 'acceptance'
       AND acceptance.release_eligible AND j.release_eligible
       AND acceptance.created_at_ms >= greatest(%s, current_epoch.starts_at_ms)
       AND j.created_at_ms >= current_epoch.starts_at_ms
"""

STATUS_FUNNEL_TOTALS_SQL: Final = f"""
    SELECT count(*) AS events,
           count(*) FILTER (WHERE admission IN ({ADMITTED_SQL})) AS admitted,
           count(*) FILTER (
             WHERE admission IN ({ADMITTED_SQL})
               AND EXISTS (
                 SELECT 1 FROM news_verdicts v
                  WHERE v.event_id = current_event.event_id AND v.stage = 'triage'
                    AND v.judgment_contract_version = 'news_judgment_v2'
               )
           ) AS triaged,
           count(*) FILTER (
             WHERE admission IN ({ADMITTED_SQL})
               AND EXISTS (
                 SELECT 1 FROM news_verdicts v
                  WHERE v.event_id = current_event.event_id AND v.stage = 'triage'
                    AND v.judgment_contract_version = 'news_judgment_v2'
               )
               AND EXISTS (
                 SELECT 1 FROM news_deliveries d
                  WHERE d.event_id = current_event.event_id AND d.kind = 'first' AND d.state = 'sent'
               )
           ) AS delivered
      FROM news_events current_event WHERE current_event.opened_at_ms >= %s
       AND EXISTS (
         SELECT 1 FROM news_event_evidence_snapshots evidence
          WHERE evidence.event_id = current_event.event_id
            AND evidence.evidence_version = (
              SELECT max(latest.evidence_version) FROM news_event_evidence_snapshots latest
               WHERE latest.event_id = current_event.event_id
            )
            AND evidence.provenance = 'observed'
            AND evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
       )
"""  # noqa: S608


def feed_page_sql(where_sql: str) -> str:
    """Build the production page statement from one already-bound predicate list.

    The query audit calls this same builder with representative AssetSearch and TextSearch predicates,
    so its plans cannot silently drift back to a simplified SQL sketch.
    """

    return f"""
        WITH clock AS (SELECT %s::bigint AS handoff_now_ms)
        SELECT e.event_id, e.event_kind, e.leader_title,
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
               d.state AS delivery_state, d.settled_at_ms AS delivered_at_ms, d.error_code AS delivery_error_code
          FROM clock CROSS JOIN news_events e
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
                   v.verdict ->> 'direction' AS direction
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
          FROM clock CROSS JOIN news_events e
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
                   v.verdict ->> 'direction' AS direction
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
    "EDITORIAL_EVENT_CARD_SQL",
    "EDITORIAL_EVENT_SQL",
    "EVENT_KIND_SQL",
    "EVENT_VERDICTS_SQL",
    "OUTCOME_GROUP_SQL",
    "STATUS_INGEST_SQL",
    "STATUS_LEARNING_RETENTION_SQL",
    "TEXT_SEARCH_PREDICATE",
    "feed_counts_sql",
    "feed_page_sql",
]
