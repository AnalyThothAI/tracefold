\set ON_ERROR_STOP on
BEGIN READ ONLY;

-- Issue #175 PR-A discovery. Run as tracefold_serve against the operator-owned database.
-- No provider/card prose is selected; the checked-in snapshot keeps only structural facts and digests.

WITH clock AS (
  SELECT (extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms
)
SELECT clock.now_ms,
       count(*) FILTER (WHERE d.settled_at_ms >= clock.now_ms - 4 * 3_600_000) AS sent_4h,
       count(*) FILTER (WHERE d.settled_at_ms >= clock.now_ms - 48 * 3_600_000) AS sent_48h,
       count(*) FILTER (WHERE d.settled_at_ms >= clock.now_ms - 7 * 24 * 3_600_000) AS sent_7d,
       count(*) FILTER (
         WHERE d.settled_at_ms >= clock.now_ms - 48 * 3_600_000
           AND d.settled_at_ms < clock.now_ms - 4 * 3_600_000
       ) AS targeted_4h_48h,
       count(*) FILTER (
         WHERE d.settled_at_ms >= clock.now_ms - 7 * 24 * 3_600_000
           AND d.settled_at_ms < clock.now_ms - 48 * 3_600_000
       ) AS extra_48h_7d
  FROM news_deliveries d CROSS JOIN clock
 WHERE d.kind = 'first' AND d.state = 'sent'
 GROUP BY clock.now_ms;

-- Frozen Alibaba pair: different fingerprints, 4 h 16 m apart, same canonical BABA base.
WITH prior AS (
  SELECT e.*, d.settled_at_ms
    FROM news_events e
    JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first' AND d.state = 'sent'
   WHERE e.event_id = 'b821c941776a00a56146894714f4511cc0c8cd2d2d91013124ef134a6121e904'
), current_event AS (
  SELECT e.*, d.settled_at_ms
    FROM news_events e
    JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first' AND d.state = 'sent'
   WHERE e.event_id = 'd621ac8ee02b8cf7b80be3124a4d98ec0d1bab51627de21028ea5ca6baeec4a6'
)
SELECT round((current_event.settled_at_ms - prior.settled_at_ms) / 60_000.0, 1) AS gap_min,
       current_event.family = prior.family AS same_family,
       current_event.comparison_fingerprint = prior.comparison_fingerprint AS same_fingerprint,
       current_event.grounded_assets AS current_assets,
       prior.grounded_assets AS prior_assets,
       EXISTS (
         SELECT 1
           FROM news_event_assets current_asset
           LEFT JOIN news_symbol_aliases current_alias ON current_alias.alias = current_asset.symbol
           JOIN news_event_assets prior_asset ON prior_asset.event_id = prior.event_id
           LEFT JOIN news_symbol_aliases prior_alias ON prior_alias.alias = prior_asset.symbol
          WHERE current_asset.event_id = current_event.event_id
            AND COALESCE(current_alias.base_symbol, current_asset.symbol)
              = COALESCE(prior_alias.base_symbol, prior_asset.symbol)
       ) AS canonical_asset_overlap
  FROM current_event CROSS JOIN prior;

-- The newest 200 sent Events, compared against prior sent Events that share a canonical asset. This is a
-- retrieval-load comparison rather than duplicate gold: it quantifies how often 48 h and 7 d saturate the
-- 24-row asset cap without treating every same-asset story as a restatement.
WITH sent_sample AS (
  SELECT d.event_id, d.settled_at_ms
    FROM news_deliveries d
   WHERE d.kind = 'first' AND d.state = 'sent'
   ORDER BY d.settled_at_ms DESC, d.event_id
   LIMIT 200
), sample_bases AS (
  SELECT sample.event_id, sample.settled_at_ms,
         COALESCE(alias.base_symbol, asset.symbol) AS base_symbol
    FROM sent_sample sample
    LEFT JOIN news_event_assets asset ON asset.event_id = sample.event_id
    LEFT JOIN news_symbol_aliases alias ON alias.alias = asset.symbol
), prior_bases AS (
  SELECT d.event_id, d.settled_at_ms,
         COALESCE(alias.base_symbol, asset.symbol) AS base_symbol
    FROM news_deliveries d
    JOIN news_event_assets asset ON asset.event_id = d.event_id
    LEFT JOIN news_symbol_aliases alias ON alias.alias = asset.symbol
   WHERE d.kind = 'first' AND d.state = 'sent'
     AND d.settled_at_ms >= (SELECT min(settled_at_ms) - 7 * 24 * 3_600_000 FROM sent_sample)
), candidate_counts AS (
  SELECT current.event_id,
         count(DISTINCT prior.event_id) FILTER (
           WHERE prior.settled_at_ms >= current.settled_at_ms - 48 * 3_600_000
         ) AS candidates_48h,
         count(DISTINCT prior.event_id) AS candidates_7d
    FROM sample_bases current
    LEFT JOIN prior_bases prior
      ON prior.base_symbol = current.base_symbol
     AND prior.event_id <> current.event_id
     AND prior.settled_at_ms >= current.settled_at_ms - 7 * 24 * 3_600_000
     AND prior.settled_at_ms < current.settled_at_ms - 4 * 3_600_000
   GROUP BY current.event_id
)
SELECT count(*) AS sample_n,
       count(*) FILTER (WHERE candidates_48h > 0) AS asset_hit_48h,
       count(*) FILTER (WHERE candidates_7d > 0) AS asset_hit_7d,
       count(*) FILTER (WHERE candidates_48h >= 24) AS asset_cap_24_saturated_48h,
       count(*) FILTER (WHERE candidates_7d >= 24) AS asset_cap_24_saturated_7d,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY candidates_48h) AS candidate_count_48h_p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY candidates_48h) AS candidate_count_48h_p95,
       max(candidates_48h) AS candidate_count_48h_max,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY candidates_7d) AS candidate_count_7d_p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY candidates_7d) AS candidate_count_7d_p95,
       max(candidates_7d) AS candidate_count_7d_max
  FROM candidate_counts;

-- Representative bounded canonical-asset plan. The production query must retain the finite symbol expansion,
-- sent-time bounds, stable order, and LIMIT 24; do not turn this into a 48 h Python scan.
EXPLAIN (ANALYZE, BUFFERS)
WITH current_event AS (
  SELECT event_id, opened_at_ms, grounded_assets
    FROM news_events
   WHERE event_id = 'd621ac8ee02b8cf7b80be3124a4d98ec0d1bab51627de21028ea5ca6baeec4a6'
), current_bases AS (
  SELECT DISTINCT COALESCE(a.base_symbol, replace(replace(tag.symbol, 'XYZ-', ''), 'XYZ:', '')) AS base
    FROM current_event
    CROSS JOIN LATERAL jsonb_array_elements_text(current_event.grounded_assets) tag(symbol)
    LEFT JOIN news_symbol_aliases a ON a.alias = tag.symbol
), equivalent_symbols AS (
  SELECT base AS symbol FROM current_bases
  UNION
  SELECT a.alias FROM news_symbol_aliases a JOIN current_bases b ON b.base = a.base_symbol
), candidate_events AS MATERIALIZED (
  SELECT DISTINCT candidate.event_id
    FROM equivalent_symbols equivalent
    CROSS JOIN LATERAL (
      -- Preserve a symbol-led lookup on news_event_assets(symbol,event_id). Without this subquery boundary,
      -- PostgreSQL can flatten the semi-join into one event-id probe per delivery in the 48 h window.
      SELECT ea.event_id FROM news_event_assets ea
       WHERE ea.symbol = equivalent.symbol
       OFFSET 0
    ) candidate
)
SELECT e.event_id, d.settled_at_ms
  FROM current_event
  JOIN candidate_events candidate ON true
  JOIN news_events e ON e.event_id = candidate.event_id
  JOIN news_deliveries d
    ON d.event_id = e.event_id
   AND d.kind = 'first'
   AND d.state = 'sent'
   AND d.settled_at_ms >= current_event.opened_at_ms - 48 * 3_600_000
   AND d.settled_at_ms < current_event.opened_at_ms - 4 * 3_600_000
 ORDER BY d.settled_at_ms DESC, e.event_id
 LIMIT 24;

ROLLBACK;
