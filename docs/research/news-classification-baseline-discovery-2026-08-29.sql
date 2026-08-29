\set ON_ERROR_STOP on
BEGIN READ ONLY;

-- News classification baseline discovery, 2026-08-29. Run as tracefold_serve against the
-- operator-owned database. Companion snapshot: news-classification-baseline-snapshot-2026-08-29.json.
-- Captured for docs/research/news-classification-audit-and-survey-2026-08-29.md, the day #117 was
-- reopened: what the 17-class mixed-axis `event_type` actually produces in production, before any
-- `news_taxonomy_v1` work lands.
--
-- Aggregates and identities only: no headline, card, prompt or provider text is selected.
-- Windows are relative to clock_timestamp(); the snapshot records the exact captured_at_ms.
-- The one absolute bound is the #314+#315 deployment moment (2026-08-28 20:26 UTC, from the
-- operator's deployment receipts), used to separate the old runtime's failure tail from the
-- currently deployed runtime's behavior.

WITH clock AS (
  SELECT (extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms
),
verdicts_7d AS (
  SELECT v.*
    FROM news_verdicts v CROSS JOIN clock c
   WHERE v.stage = 'triage'
     AND v.verdict <> '{}'::jsonb
     AND v.created_at_ms >= c.now_ms - 7 * 24 * 3_600_000
),
verdicts_24h AS (
  SELECT v.*
    FROM news_verdicts v CROSS JOIN clock c
   WHERE v.stage = 'triage'
     AND v.created_at_ms >= c.now_ms - 24 * 3_600_000
),
verdicts_post_315 AS (
  SELECT v.*
    FROM news_verdicts v
   WHERE v.stage = 'triage'
     AND v.created_at_ms >= (extract(epoch FROM timestamptz '2026-08-28 20:26:00+00') * 1000)::bigint
)
SELECT jsonb_pretty(jsonb_build_object(
  'artifact', 'tracefold_news_classification_baseline_v1',
  'captured_at_ms', (SELECT now_ms FROM clock),
  'source', jsonb_build_object(
    'database_role', 'tracefold_serve',
    'transaction', 'read_only',
    'stage', 'triage'
  ),
  'windows', jsonb_build_object(
    'distribution_ms', 7 * 24 * 3_600_000,
    'health_ms', 24 * 3_600_000,
    'post_315_deploy_since_ms', (extract(epoch FROM timestamptz '2026-08-28 20:26:00+00') * 1000)::bigint
  ),

  -- 1. What the 17-class enum produces: per-class volume and final policy outcome. `delivered`
  --    counts push+escalate; `decide()` owns that outcome, the model only proposes fields.
  'event_type_by_decision_7d', (
    SELECT jsonb_agg(to_jsonb(t) ORDER BY t.n DESC) FROM (
      SELECT v.verdict->>'event_type' AS event_type,
             count(*) AS n,
             count(*) FILTER (WHERE v.final_decision = 'push') AS push,
             count(*) FILTER (WHERE v.final_decision = 'escalate') AS escalate,
             count(*) FILTER (WHERE v.final_decision = 'drop') AS drop,
             count(*) FILTER (WHERE v.final_decision = 'throttled') AS throttled,
             round(100.0 * count(*) FILTER (WHERE v.final_decision IN ('push', 'escalate')) / count(*), 1)
               AS delivered_pct
        FROM verdicts_7d v
       GROUP BY 1
    ) t
  ),
  'totals_7d', (
    SELECT to_jsonb(t) FROM (
      SELECT count(*) AS n,
             count(*) FILTER (WHERE v.final_decision IN ('push', 'escalate')) AS delivered,
             round(100.0 * count(*) FILTER (WHERE v.final_decision IN ('push', 'escalate')) / count(*), 1)
               AS delivered_pct
        FROM verdicts_7d v
    ) t
  ),

  -- 2. The flat label cannot select the product population: rows whose TradeRelevance channels
  --    contain `product_progress` versus what `event_type` says. #117 quotes the same ~41% gap.
  'product_progress_channel_7d', (
    SELECT to_jsonb(t) FROM (
      SELECT count(*) AS pp_rows,
             count(*) FILTER (WHERE v.verdict->>'event_type' <> 'product') AS event_type_not_product,
             round(100.0 * count(*) FILTER (WHERE v.verdict->>'event_type' <> 'product') / count(*), 1)
               AS not_product_pct
        FROM verdicts_7d v
       WHERE v.editorial#>'{relevance,channels}' ? 'product_progress'
    ) t
  ),
  'product_progress_channel_event_types_7d', (
    SELECT jsonb_agg(to_jsonb(t) ORDER BY t.n DESC) FROM (
      SELECT v.verdict->>'event_type' AS event_type, count(*) AS n
        FROM verdicts_7d v
       WHERE v.editorial#>'{relevance,channels}' ? 'product_progress'
       GROUP BY 1
    ) t
  ),

  -- 3. Where product sits on the m1/m2 boundary #173 audited (its fixed-window audit reported
  --    ~55% of product candidates in m1; this is the live shape now).
  'product_magnitude_7d', (
    SELECT jsonb_agg(to_jsonb(t) ORDER BY t.m) FROM (
      SELECT v.verdict->>'magnitude' AS m,
             count(*) AS n,
             count(*) FILTER (WHERE v.final_decision IN ('push', 'escalate')) AS delivered
        FROM verdicts_7d v
       WHERE v.verdict->>'event_type' = 'product'
       GROUP BY 1
    ) t
  ),

  -- 4. Mixed-cohort caveat: distribution numbers above span every Program identity that ran in
  --    the window, so they are diagnostics, not single-cohort quality evidence (#117 Phase 0).
  'program_identity_mix_7d', (
    SELECT jsonb_agg(to_jsonb(t) ORDER BY t.n DESC) FROM (
      SELECT left(coalesce(v.program_sha256, '(null)'), 12) AS program_sha256_prefix,
             count(*) AS n,
             to_char(to_timestamp(min(v.created_at_ms) / 1000) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS first_seen_utc
        FROM verdicts_7d v
       GROUP BY 1
    ) t
  ),

  -- 5. Availability is not classification quality, but the essay reports both. The 24h window
  --    straddles the #314+#315 deployment; `health_post_315_deploy` isolates the current runtime.
  'health_24h', (
    SELECT to_jsonb(t) FROM (
      SELECT count(*) AS n,
             count(*) FILTER (WHERE v.degraded) AS degraded,
             count(*) FILTER (WHERE v.trace->>'model_fallback_from' IS NOT NULL) AS fallback_answered,
             round(avg(v.latency_ms)::numeric) AS avg_latency_ms,
             round((percentile_cont(0.95) WITHIN GROUP (ORDER BY v.latency_ms))::numeric) AS p95_latency_ms
        FROM verdicts_24h v
    ) t
  ),
  'health_24h_hourly', (
    SELECT jsonb_agg(to_jsonb(t) ORDER BY t.hour_utc) FROM (
      SELECT to_char(date_trunc('hour', to_timestamp(v.created_at_ms / 1000)) AT TIME ZONE 'UTC',
                     'YYYY-MM-DD"T"HH24:00"Z"') AS hour_utc,
             count(*) AS n,
             count(*) FILTER (WHERE v.degraded) AS degraded,
             count(*) FILTER (WHERE v.trace->>'model_fallback_from' IS NOT NULL) AS fallback
        FROM verdicts_24h v
       GROUP BY 1
    ) t
  ),
  'fallback_from_24h', (
    SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.n DESC), '[]'::jsonb) FROM (
      SELECT v.trace->>'model_fallback_from' AS fallback_from, count(*) AS n
        FROM verdicts_24h v
       WHERE v.trace->>'model_fallback_from' IS NOT NULL
       GROUP BY 1
    ) t
  ),
  'degraded_error_codes_24h', (
    SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.n DESC), '[]'::jsonb) FROM (
      SELECT v.error_code, count(*) AS n
        FROM verdicts_24h v
       WHERE v.degraded
       GROUP BY 1
    ) t
  ),
  'health_post_315_deploy', (
    SELECT to_jsonb(t) FROM (
      SELECT count(*) AS n,
             count(*) FILTER (WHERE v.degraded) AS degraded,
             count(*) FILTER (WHERE v.trace->>'model_fallback_from' IS NOT NULL) AS fallback_answered
        FROM verdicts_post_315 v
    ) t
  )
));

ROLLBACK;
