"""News-owned bounded read statements shared by serving and query audit."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tracefold.platform.postgres.postgres_audit import ReadQuerySpec

PUBLIC_LIST_LIMIT = 100
_SLO_WINDOW_MS = 24 * 60 * 60 * 1000
SLO_SAMPLE_LIMIT = 5_000
_PUSH_RECONCILE_PAGE_SIZE = 1_000
_PUSH_RECONCILE_PROBE_LIMIT = _PUSH_RECONCILE_PAGE_SIZE + 1


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


def story_push_contexts_query(
    *,
    story_ids: Sequence[str],
) -> ReadQuerySpec:
    if len(story_ids) > PUBLIC_LIST_LIMIT:
        raise ValueError("news_push_context_story_ids_limit")
    values = list(story_ids)
    return ReadQuerySpec(
        name="news_feed_push_contexts",
        sql="""
            WITH requested AS (
              SELECT unnest(%s::text[]) AS story_id
            )
            SELECT requested.story_id,
                   selected.importance_score,
                   selected.item_count,
                   selected.source_count,
                   selected.first_published_at_ms,
                   selected.last_published_at_ms,
                   selected.item_id,
                   selected.canonical_url,
                   selected.provider_metadata,
                   selected.reporting_origin,
                   selected.title,
                   selected.description,
                   selected.lang,
                   selected.published_at_ms,
                   selected.eligibility_observed_at_ms,
                   selected.threshold_observed_at_ms,
                   selected.provider_score,
                   state.baseline_at_ms AS push_baseline_at_ms,
                   delivery.status AS push_delivery_status
              FROM requested
              LEFT JOIN LATERAL (
                SELECT story.importance_score,
                     story.item_count,
                     story.source_count,
                     story.first_published_at_ms,
                     story.last_published_at_ms,
                     item.item_id,
                     item.canonical_url,
                     item.provider_metadata,
                     item.reporting_origin,
                     item.title,
                     item.description,
                     item.lang,
                     item.published_at_ms,
                     item.push_eligibility_updated_at_ms AS eligibility_observed_at_ms,
                     coalesce(
                       item.provider_score_updated_at_ms,
                       item.updated_at_ms
                     ) AS threshold_observed_at_ms,
                     (item.provider_metadata ->> 'score')::numeric
                       AS provider_score
                FROM news_stories story
                JOIN news_story_members member
                  ON member.story_id = story.story_id
                JOIN news_items item ON item.item_id = member.item_id
               WHERE story.story_id = requested.story_id
                 AND jsonb_typeof(item.provider_metadata -> 'score') = 'number'
               ORDER BY (item.provider_metadata ->> 'score')::numeric DESC,
                        item.published_at_ms DESC,
                        item.item_id
               LIMIT 1
              ) selected ON true
              LEFT JOIN news_push_state state
                ON state.singleton_key = 'current'
              LEFT JOIN LATERAL (
                SELECT delivery.status
                  FROM (
                    SELECT requested.story_id, 0 AS priority
                  UNION ALL
                  SELECT selected_delivery.story_id, 1 AS priority
                    FROM news_push_deliveries selected_delivery
                   WHERE selected.item_id IS NOT NULL
                     AND selected_delivery.selected_item_id = selected.item_id
                  UNION ALL
                  SELECT member_delivery.story_id, 2 AS priority
                    FROM news_story_members ledger_member
                    JOIN news_push_deliveries member_delivery
                      ON member_delivery.selected_item_id = ledger_member.item_id
                   WHERE ledger_member.story_id = requested.story_id
                  ) matched
                  JOIN news_push_deliveries delivery
                    ON delivery.story_id = matched.story_id
                 ORDER BY matched.priority,
                          delivery.updated_at_ms DESC,
                          delivery.story_id
                 LIMIT 1
              ) delivery ON true
             ORDER BY requested.story_id
        """,
        params=(values,),
    )


def story_push_reconcile_page_query() -> ReadQuerySpec:
    """Read one exact Story-id page for the durable push discovery loop."""

    return ReadQuerySpec(
        name="news_push_reconcile_page",
        sql="""
            WITH push_state AS MATERIALIZED (
              SELECT baseline_at_ms, reconcile_cursor_story_id
                FROM news_push_state
               WHERE singleton_key = 'current'
            ),
            page_scan AS MATERIALIZED (
              SELECT story.story_id,
                     story.importance_score,
                     story.item_count,
                     story.source_count,
                     story.first_published_at_ms,
                     story.last_published_at_ms
                FROM news_stories story
               WHERE story.story_id > coalesce(
                       (SELECT reconcile_cursor_story_id FROM push_state),
                       ''
                     )
               ORDER BY story_id
               LIMIT %s
            ),
            page AS MATERIALIZED (
              SELECT page_scan.*
                FROM page_scan
               ORDER BY page_scan.story_id
               LIMIT %s
            ),
            selected AS (
              SELECT page.story_id,
                     page.importance_score,
                     page.item_count,
                     page.source_count,
                     page.first_published_at_ms,
                     page.last_published_at_ms,
                     evidence.*
                FROM page
                JOIN LATERAL (
                  SELECT item.item_id,
                         item.canonical_url,
                         item.provider_metadata,
                         item.reporting_origin,
                         item.title,
                         item.description,
                         item.lang,
                         item.published_at_ms,
                         item.push_eligibility_updated_at_ms AS eligibility_observed_at_ms,
                         coalesce(
                           item.provider_score_updated_at_ms,
                           item.updated_at_ms
                         ) AS threshold_observed_at_ms,
                         (item.provider_metadata ->> 'score')::numeric
                           AS provider_score
                    FROM news_story_members member
                    JOIN news_items item ON item.item_id = member.item_id
                   WHERE member.story_id = page.story_id
                     AND jsonb_typeof(item.provider_metadata -> 'score') = 'number'
                   ORDER BY (item.provider_metadata ->> 'score')::numeric DESC,
                            item.published_at_ms DESC,
                            item.item_id
                   LIMIT 1
                ) evidence ON true
            )
            SELECT page.story_id AS page_story_id,
                   EXISTS (
                     SELECT 1
                       FROM page_scan extra
                       LEFT JOIN page current
                         ON current.story_id = extra.story_id
                      WHERE current.story_id IS NULL
                   ) AS has_more,
                   selected.importance_score,
                   selected.item_count,
                   selected.source_count,
                   selected.first_published_at_ms,
                   selected.last_published_at_ms,
                   selected.item_id,
                   selected.canonical_url,
                   selected.provider_metadata,
                   selected.reporting_origin,
                   selected.title,
                   selected.description,
                   selected.lang,
                   selected.published_at_ms,
                   selected.eligibility_observed_at_ms,
                   selected.threshold_observed_at_ms,
                   selected.provider_score,
                   state.baseline_at_ms AS push_baseline_at_ms,
                   delivery.status AS push_delivery_status
              FROM page
              CROSS JOIN push_state state
              LEFT JOIN selected ON selected.story_id = page.story_id
              LEFT JOIN LATERAL (
                SELECT delivery.status
                  FROM (
                    SELECT page.story_id, 0 AS priority
                    UNION ALL
                    SELECT selected_delivery.story_id, 1 AS priority
                      FROM news_push_deliveries selected_delivery
                     WHERE selected.item_id IS NOT NULL
                       AND selected_delivery.selected_item_id = selected.item_id
                  ) matched
                  JOIN news_push_deliveries delivery
                    ON delivery.story_id = matched.story_id
                 ORDER BY matched.priority,
                          delivery.updated_at_ms DESC,
                          delivery.story_id
                 LIMIT 1
              ) delivery ON true
             ORDER BY page.story_id
        """,
        params=(_PUSH_RECONCILE_PROBE_LIMIT, _PUSH_RECONCILE_PAGE_SIZE),
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
                   s.live_connected, s.last_live_at_ms,
                   s.last_recovery_at_ms,
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
            SELECT source_id, name, live_connected, last_live_at_ms,
                   last_recovery_at_ms, last_error, last_outcome,
                   last_http_status, last_success_at_ms,
                   consecutive_failures, last_rejection_counts,
                   last_items_seen, last_items_accepted
              FROM news_sources
             WHERE source_kind = 'opennews' AND enabled
             ORDER BY source_id
             LIMIT 1
        """,
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


def push_oldest_due_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_push_oldest_due",
        sql="""
            SELECT next_attempt_at_ms
              FROM news_push_deliveries
             WHERE status IN (
                     'pending_translation', 'pending_delivery', 'retry_wait'
                   )
               AND next_attempt_at_ms IS NOT NULL
             ORDER BY next_attempt_at_ms, created_at_ms, story_id
             LIMIT 1
        """,
    )


def push_oldest_waiting_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_push_oldest_waiting",
        sql="""
            SELECT threshold_observed_at_ms
              FROM news_push_deliveries
             WHERE status IN (
                     'pending_translation', 'pending_delivery', 'retry_wait'
                   )
             ORDER BY threshold_observed_at_ms, story_id
             LIMIT 1
        """,
    )


def push_translation_samples_query(*, now_ms: int) -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_push_translation_24h",
        sql="""
            SELECT translation_status,
                   translation_fallback_code AS fallback_code,
                   translation_duration_ms AS duration_ms
              FROM news_push_deliveries
             WHERE translation_prompt_version = 'title_zh_v2'
               AND translation_attempted_at_ms BETWEEN %s AND %s
             ORDER BY translation_attempted_at_ms DESC, story_id DESC
             LIMIT %s
        """,
        params=(int(now_ms) - _SLO_WINDOW_MS, int(now_ms), SLO_SAMPLE_LIMIT + 1),
    )


def push_delivery_samples_query(*, now_ms: int) -> ReadQuerySpec:
    return ReadQuerySpec(
        name="news_push_delivery_24h",
        sql="""
            SELECT (
                     CASE
                       WHEN status = 'sent' THEN sent_at_ms
                       ELSE updated_at_ms
                     END
                   ) - threshold_observed_at_ms AS latency_ms
              FROM news_push_deliveries
             WHERE translation_prompt_version = 'title_zh_v2'
               AND status IN ('sent', 'terminal')
               AND CASE
                     WHEN status = 'sent' THEN sent_at_ms
                     ELSE updated_at_ms
                   END BETWEEN %s AND %s
             ORDER BY (
               CASE WHEN status = 'sent' THEN sent_at_ms ELSE updated_at_ms END
             ) DESC, story_id DESC
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
        story_push_contexts_query(story_ids=audit_story_ids),
        story_push_reconcile_page_query(),
        story_query(story_id="audit-missing-story"),
        story_members_query(story_id="audit-missing-story", limit=100),
        brief_query(),
        sources_query(limit=100),
        status_opennews_query(),
        status_rss_query(),
        status_projection_query(),
        push_state_query(),
        push_oldest_due_query(),
        push_oldest_waiting_query(),
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
    "push_oldest_due_query",
    "push_oldest_waiting_query",
    "push_state_query",
    "push_translation_samples_query",
    "sources_query",
    "status_opennews_query",
    "status_projection_query",
    "status_rss_query",
    "story_members_query",
    "story_push_contexts_query",
    "story_query",
]
