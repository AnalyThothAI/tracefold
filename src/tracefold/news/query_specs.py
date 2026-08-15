"""News-owned bounded read statements shared by serving and query audit."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tracefold.platform.postgres.postgres_audit import ReadQuerySpec

PUBLIC_LIST_LIMIT = 100
_SLO_WINDOW_MS = 24 * 60 * 60 * 1000
SLO_SAMPLE_LIMIT = 5_000
_REALTIME_WINDOW_MS = 60 * 60 * 1_000


def feed_rows_query(
    *,
    category: str | None,
    level: str | None,
    source_id: str | None,
    reporting_origin: str | None,
    provider_score_gt: float | None,
    q: str | None,
    sort: str,
    limit: int,
    cursor: tuple[int, int, str] | None = None,
) -> ReadQuerySpec:
    where, params = _feed_filter_where(
        category=category,
        level=level,
        source_id=source_id,
        reporting_origin=reporting_origin,
        provider_score_gt=provider_score_gt,
        q=q,
    )
    if cursor is not None:
        last_ms, score, story_id = cursor
        if sort == "latest":
            where.append(
                """
                (
                  st.last_published_at_ms < %s
                  OR (
                    st.last_published_at_ms = %s
                    AND st.importance_score < %s
                  )
                  OR (
                    st.last_published_at_ms = %s
                    AND st.importance_score = %s
                    AND st.story_id > %s
                  )
                )
                """
            )
            params.extend([last_ms, last_ms, score, last_ms, score, story_id])
        else:
            where.append(
                """
                (
                  st.importance_score < %s
                  OR (
                    st.importance_score = %s
                    AND st.last_published_at_ms < %s
                  )
                  OR (
                    st.importance_score = %s
                    AND st.last_published_at_ms = %s
                    AND st.story_id > %s
                  )
                )
                """
            )
            params.extend([score, score, last_ms, score, last_ms, story_id])
    order = (
        "st.last_published_at_ms DESC, st.importance_score DESC, st.story_id"
        if sort == "latest"
        else "st.importance_score DESC, st.last_published_at_ms DESC, st.story_id"
    )
    return ReadQuerySpec(
        name="news_feed_focus_rows",
        sql=f"""
            SELECT st.*, representative.reporting_origin
                         AS representative_source_name
              FROM news_stories st
              JOIN news_items representative
                ON representative.item_id = st.representative_item_id
             WHERE {" AND ".join(where)}
             ORDER BY {order}
             LIMIT %s
        """,
        params=(*params, int(limit) + 1),
    )


def feed_facets_query(
    *,
    category: str | None,
    level: str | None,
    source_id: str | None,
    reporting_origin: str | None,
    provider_score_gt: float | None,
    q: str | None,
) -> ReadQuerySpec:
    where, params = _feed_filter_where(
        category=category,
        level=level,
        source_id=source_id,
        reporting_origin=reporting_origin,
        provider_score_gt=provider_score_gt,
        q=q,
    )
    return ReadQuerySpec(
        name="news_feed_focus_facets",
        amplification_basis="aggregate_input",
        sql=f"""
            WITH filtered_stories AS MATERIALIZED (
              SELECT st.story_id, st.category, st.level, st.facet_facts
                FROM news_stories st
               WHERE {" AND ".join(where)}
            ),
            source_facts AS MATERIALIZED (
              SELECT filtered.story_id, source_id.value AS source_id
                FROM filtered_stories filtered
                CROSS JOIN LATERAL jsonb_array_elements_text(
                  filtered.facet_facts -> 'source_ids'
                ) source_id(value)
            ),
            reporting_origin_facts AS MATERIALIZED (
              SELECT filtered.story_id,
                     reporting_origin.value AS reporting_origin
                FROM filtered_stories filtered
                CROSS JOIN LATERAL jsonb_array_elements_text(
                  filtered.facet_facts -> 'reporting_origins'
                ) reporting_origin(value)
            ),
            facet_rows AS (
              SELECT 'category'::text AS facet_type,
                     filtered.category AS value,
                     filtered.category AS label,
                     count(*)::integer AS count
                FROM filtered_stories filtered
               GROUP BY filtered.category
              UNION ALL
              SELECT 'level'::text AS facet_type,
                     filtered.level AS value,
                     filtered.level AS label,
                     count(*)::integer AS count
                FROM filtered_stories filtered
               GROUP BY filtered.level
              UNION ALL
              SELECT 'source'::text AS facet_type,
                     source.source_id AS value,
                     source.name AS label,
                     count(DISTINCT source_facts.story_id)::integer AS count
                FROM source_facts
                JOIN news_sources source ON source.source_id = source_facts.source_id
               GROUP BY source.source_id, source.name
              UNION ALL
              SELECT 'reporting_origin'::text AS facet_type,
                     lower(btrim(reporting_origin_facts.reporting_origin)) AS value,
                     min(btrim(reporting_origin_facts.reporting_origin)) AS label,
                     count(DISTINCT reporting_origin_facts.story_id)::integer AS count
                FROM reporting_origin_facts
               WHERE nullif(btrim(reporting_origin_facts.reporting_origin), '') IS NOT NULL
               GROUP BY lower(btrim(reporting_origin_facts.reporting_origin))
            ),
            ranked AS (
              SELECT facet_rows.*,
                     row_number() OVER (
                       PARTITION BY facet_type
                       ORDER BY count DESC, value
                     ) AS position
                FROM facet_rows
            )
            SELECT facet_type, value, label, count, position
              FROM ranked
             WHERE position <= %s
             ORDER BY facet_type, position
        """,
        params=(*params, PUBLIC_LIST_LIMIT + 1),
    )


def story_provider_evidence_query(
    *,
    story_ids: Sequence[str],
) -> ReadQuerySpec:
    if len(story_ids) > PUBLIC_LIST_LIMIT:
        raise ValueError("news_provider_evidence_story_ids_limit")
    values = list(story_ids)
    return ReadQuerySpec(
        name="news_story_provider_evidence",
        sql="""
            WITH requested AS (
              SELECT unnest(%s::text[]) AS story_id
            )
            SELECT requested.story_id,
                   selected.item_id,
                   selected.canonical_url,
                   selected.provider_metadata,
                   selected.reporting_origin
              FROM requested
              LEFT JOIN LATERAL (
                SELECT item.item_id,
                       item.canonical_url,
                       item.provider_metadata,
                       item.reporting_origin
                FROM news_stories story
                LEFT JOIN LATERAL (
                  SELECT scored_item.item_id
                    FROM news_story_members scored_membership
                    JOIN news_items scored_item
                      ON scored_item.item_id = scored_membership.item_id
                   WHERE scored_membership.story_id = story.story_id
                     AND jsonb_typeof(
                           scored_item.provider_metadata -> 'score'
                         ) = 'number'
                   ORDER BY
                         (scored_item.provider_metadata ->> 'score')::numeric DESC,
                         scored_item.published_at_ms DESC,
                         scored_item.item_id ASC
                   LIMIT 1
                ) provider_scoring ON true
                JOIN news_items item
                  ON item.item_id = coalesce(
                       provider_scoring.item_id,
                       story.representative_item_id
                     )
               WHERE story.story_id = requested.story_id
              ) selected ON true
             ORDER BY requested.story_id
        """,
        params=(values,),
    )


def story_query(*, story_id: str) -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_story",
        sql="""
            SELECT st.*, representative.reporting_origin
                         AS representative_source_name
              FROM news_stories st
              JOIN news_items representative
                ON representative.item_id = st.representative_item_id
             WHERE st.story_id = %s
        """,
        params=(story_id,),
    )


def story_members_query(
    *,
    story_id: str,
    limit: int,
    cursor: tuple[int, str] | None = None,
) -> ReadQuerySpec:
    where = ["m.story_id = %s"]
    params: list[Any] = [story_id]
    if cursor is not None:
        published_at_ms, item_id = cursor
        where.append(
            """
            (
              i.published_at_ms < %s
              OR (
                i.published_at_ms = %s
                AND i.item_id > %s
              )
            )
            """
        )
        params.extend([published_at_ms, published_at_ms, item_id])
    params.append(int(limit) + 1)
    return ReadQuerySpec(
        name="news_story_members",
        sql=f"""
            SELECT i.*, i.reporting_origin AS source_name, src.tier
              FROM news_story_members m
              JOIN news_items i ON i.item_id = m.item_id
              JOIN news_sources src ON src.source_id = i.source_id
             WHERE {" AND ".join(where)}
             ORDER BY i.published_at_ms DESC, i.item_id
             LIMIT %s
        """,
        params=tuple(params),
    )


def brief_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_brief",
        sql="SELECT * FROM news_brief_current WHERE singleton_key = true",
    )


def sources_query(
    *,
    limit: int,
    cursor: tuple[int, int, str, str] | None = None,
) -> ReadQuerySpec:
    where = ["s.enabled"]
    params: list[Any] = []
    if cursor is not None:
        role_order, tier, name, source_id = cursor
        where.append(
            """
            (
              CASE WHEN s.source_kind = 'opennews' THEN 0 ELSE 1 END,
              s.tier, s.name, s.source_id
            ) > (%s, %s, %s, %s)
            """
        )
        params.extend([role_order, tier, name, source_id])
    params.append(int(limit) + 1)
    return ReadQuerySpec(
        name="news_sources",
        sql=f"""
            SELECT s.source_id, s.name, s.source_kind, s.tier,
                   s.enabled, s.feed_url, s.refresh_interval_seconds,
                   s.next_fetch_at_ms, s.claim_lease_expires_at_ms,
                   s.last_fetch_started_at_ms, s.last_fetch_finished_at_ms,
                   s.live_connected,
                   s.last_connected_at_ms, s.last_disconnected_at_ms,
                   s.last_accepted_strategy_trigger_at_ms,
                   s.strategy_history_status, s.last_history_check_at_ms,
                   jsonb_array_length(s.observed_strategy_provenance)
                     AS observed_strategy_count,
                   s.last_success_at_ms, s.last_http_status,
                   s.consecutive_failures, s.last_outcome, s.last_error,
                   s.last_rejection_counts, s.last_items_seen,
                   s.last_items_accepted
              FROM news_sources s
             WHERE {" AND ".join(where)}
             ORDER BY CASE WHEN s.source_kind = 'opennews' THEN 0 ELSE 1 END,
                      s.tier, s.name, s.source_id
             LIMIT %s
        """,
        params=tuple(params),
    )


def status_opennews_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_status_opennews",
        sql="""
            SELECT source_id, name, live_connected,
                   last_connected_at_ms, last_disconnected_at_ms,
                   last_accepted_strategy_trigger_at_ms,
                   strategy_history_status, last_history_check_at_ms,
                   jsonb_array_length(observed_strategy_provenance)
                     AS observed_strategy_count,
                   last_error, last_outcome, last_success_at_ms,
                   consecutive_failures, last_rejection_counts,
                   last_items_seen, last_items_accepted
              FROM news_sources
             WHERE source_kind = 'opennews' AND enabled
             ORDER BY source_id
             LIMIT 1
        """,
    )


def status_opennews_incidents_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_status_opennews_incidents",
        sql="""
            SELECT incident_id, cause_class, opened_at_ms, reconnected_at_ms,
                   closed_at_ms, planned, close_code, recovery_status,
                   recovery_from_at_ms, recovery_to_at_ms, recovered_count,
                   last_error_code
              FROM news_opennews_incidents
             ORDER BY opened_at_ms DESC, incident_id DESC
             LIMIT 50
        """,
    )


def status_inbound_latency_query(*, now_ms: int) -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_status_inbound_latency",
        sql="""
            SELECT first_observed_at_ms - published_at_ms AS latency_ms
              FROM news_items
             WHERE source_id = 'news-opennews'
               AND first_ingest_mode = 'live'
               AND first_observed_at_ms BETWEEN %s AND %s
               AND first_observed_at_ms >= published_at_ms
             ORDER BY first_observed_at_ms DESC, item_id DESC
             LIMIT %s
        """,
        params=(int(now_ms) - _REALTIME_WINDOW_MS, int(now_ms), SLO_SAMPLE_LIMIT + 1),
    )


def status_story_latency_query(*, now_ms: int) -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_status_story_latency",
        sql="""
            SELECT story.created_at_ms - min(item.first_observed_at_ms) AS latency_ms
              FROM news_stories story
              JOIN news_story_members member ON member.story_id = story.story_id
              JOIN news_items item ON item.item_id = member.item_id
             WHERE story.created_at_ms BETWEEN %s AND %s
               AND item.first_ingest_mode = 'live'
             GROUP BY story.story_id, story.created_at_ms
            HAVING story.created_at_ms >= min(item.first_observed_at_ms)
             ORDER BY story.created_at_ms DESC, story.story_id DESC
             LIMIT %s
        """,
        params=(int(now_ms) - _REALTIME_WINDOW_MS, int(now_ms), SLO_SAMPLE_LIMIT + 1),
    )


def status_rss_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_status_rss",
        sql="""
            SELECT last_success_at_ms, last_error, claim_token,
                   next_fetch_at_ms
              FROM news_sources
             WHERE source_kind = 'rss' AND enabled
             ORDER BY source_id
        """,
    )


def status_projection_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_status_projection",
        sql="""
            SELECT active_story_count AS active_count,
                   newest_story_at_ms,
                   last_material_change_at_ms,
                   active_item_count,
                   newest_item_at_ms,
                   invalid_owner_count,
                   invalid_story_aggregate_count,
                   last_attempt_at_ms,
                   last_success_at_ms,
                   last_error
              FROM news_projection_summary
             WHERE singleton_key = 'current'
        """,
    )


def push_state_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_push_state",
        sql="SELECT * FROM news_push_state WHERE singleton_key = 'current'",
    )


def push_oldest_pending_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_push_oldest_pending",
        sql="""
            SELECT live_observed_at_ms
              FROM news_push_deliveries
             WHERE status = 'pending'
               AND source_payload ->> 'schema_version' = 'news_item_push_v1'
             ORDER BY live_observed_at_ms, item_id
             LIMIT 1
        """,
    )


def push_translation_samples_query(*, now_ms: int) -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_push_translation_24h",
        sql="""
            SELECT presentation_snapshot ->> 'outcome' AS outcome,
                   presentation_snapshot ->> 'fallback_code' AS fallback_code,
                   CASE
                     WHEN jsonb_typeof(
                       presentation_snapshot -> 'translation_duration_ms'
                     ) = 'number'
                       THEN (
                         presentation_snapshot ->> 'translation_duration_ms'
                       )::bigint
                   END AS duration_ms
              FROM news_push_deliveries
             WHERE source_payload ->> 'schema_version' = 'news_item_push_v1'
               AND attempted_at_ms BETWEEN %s AND %s
             ORDER BY attempted_at_ms DESC, item_id DESC
             LIMIT %s
        """,
        params=(int(now_ms) - _SLO_WINDOW_MS, int(now_ms), SLO_SAMPLE_LIMIT + 1),
    )


def push_delivery_samples_query(*, now_ms: int) -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_push_delivery_24h",
        sql="""
            SELECT status,
                   CASE WHEN status = 'sent'
                     THEN sent_at_ms - live_observed_at_ms
                   END AS latency_ms
              FROM news_push_deliveries
             WHERE source_payload ->> 'schema_version' = 'news_item_push_v1'
               AND status IN ('sent', 'terminal')
               AND CASE
                     WHEN status = 'sent' THEN sent_at_ms
                     ELSE updated_at_ms
                   END BETWEEN %s AND %s
             ORDER BY (
               CASE WHEN status = 'sent' THEN sent_at_ms ELSE updated_at_ms END
             ) DESC, item_id DESC
             LIMIT %s
        """,
        params=(int(now_ms) - _SLO_WINDOW_MS, int(now_ms), SLO_SAMPLE_LIMIT + 1),
    )


def news_query_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
    audit_story_ids = tuple(f"audit-missing-story-{index}" for index in range(25))
    return (
        feed_rows_query(
            category=None,
            level=None,
            source_id=None,
            reporting_origin=None,
            provider_score_gt=70,
            q=None,
            sort="latest",
            limit=25,
        ),
        feed_facets_query(
            category=None,
            level=None,
            source_id=None,
            reporting_origin=None,
            provider_score_gt=70,
            q=None,
        ),
        story_provider_evidence_query(story_ids=audit_story_ids),
        story_query(story_id="audit-missing-story"),
        story_members_query(story_id="audit-missing-story", limit=100),
        brief_query(),
        sources_query(limit=100),
        status_opennews_query(),
        status_opennews_incidents_query(),
        status_inbound_latency_query(now_ms=now_ms),
        status_story_latency_query(now_ms=now_ms),
        status_rss_query(),
        status_projection_query(),
        push_state_query(),
        push_oldest_pending_query(),
        push_translation_samples_query(now_ms=now_ms),
        push_delivery_samples_query(now_ms=now_ms),
    )


def _feed_filter_where(
    *,
    category: str | None,
    level: str | None,
    source_id: str | None,
    reporting_origin: str | None,
    provider_score_gt: float | None,
    q: str | None,
) -> tuple[list[str], list[Any]]:
    where = ["true"]
    params: list[Any] = []
    if category:
        where.append("st.category = %s")
        params.append(category)
    if level:
        where.append("st.level = %s")
        params.append(level)
    if source_id:
        where.append(
            """
            EXISTS (
              SELECT 1
                FROM jsonb_array_elements_text(
                  st.facet_facts -> 'source_ids'
                ) source_facet(value)
               WHERE source_facet.value = %s
            )
            """
        )
        params.append(source_id)
    if reporting_origin:
        where.append(
            """
            EXISTS (
              SELECT 1
                FROM jsonb_array_elements_text(
                  st.facet_facts -> 'reporting_origins'
                ) origin_facet(value)
               WHERE lower(btrim(origin_facet.value)) = %s
            )
            """
        )
        params.append(reporting_origin)
    if provider_score_gt is not None:
        where.append(
            """
            st.story_id IN (
              SELECT current_member.story_id
                FROM (
                  SELECT fm.story_id
                    FROM news_story_members fm
                    CROSS JOIN LATERAL (
                      SELECT 1 AS matched
                        FROM news_items fi
                       WHERE fi.item_id = fm.item_id
                         AND jsonb_typeof(fi.provider_metadata -> 'score') = 'number'
                         AND (fi.provider_metadata ->> 'score')::numeric > %s
                       OFFSET 0
                    ) current_item
                ) current_member
            )
            """
        )
        params.append(provider_score_gt)
    if q:
        where.append(
            """
            (
              EXISTS (
                SELECT 1
                  FROM news_story_members fm
                  JOIN news_items fi ON fi.item_id = fm.item_id
                 WHERE fm.story_id = st.story_id
                   AND (
                     strpos(lower(fi.title), %s) > 0
                     OR strpos(lower(fi.description), %s) > 0
                     OR EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements_text(
                           st.facet_facts -> 'reporting_origins'
                         ) origin_facet(value)
                        WHERE strpos(lower(origin_facet.value), %s) > 0
                     )
                     OR strpos(
                       lower(coalesce(fi.provider_metadata ->> 'source', '')),
                       %s
                     ) > 0
                     OR EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(
                           CASE
                             WHEN jsonb_typeof(fi.provider_metadata -> 'coins') = 'array'
                               THEN fi.provider_metadata -> 'coins'
                             ELSE '[]'::jsonb
                           END
                         ) coin
                        WHERE strpos(lower(coalesce(coin ->> 'symbol', '')), %s) > 0
                     )
                   )
              )
            )
            """
        )
        params.extend([q, q, q, q, q])
    return where, params


__all__ = [
    "PUBLIC_LIST_LIMIT",
    "SLO_SAMPLE_LIMIT",
    "brief_query",
    "feed_facets_query",
    "feed_rows_query",
    "news_query_specs",
    "push_delivery_samples_query",
    "push_oldest_pending_query",
    "push_state_query",
    "push_translation_samples_query",
    "sources_query",
    "status_inbound_latency_query",
    "status_opennews_incidents_query",
    "status_opennews_query",
    "status_projection_query",
    "status_rss_query",
    "status_story_latency_query",
    "story_members_query",
    "story_provider_evidence_query",
    "story_query",
]
