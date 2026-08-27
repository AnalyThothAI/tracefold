-- Tracefold News fixed-window read-only audit
-- Event window: [2026-08-20 02:30:09.757Z, 2026-08-21 02:30:09.757Z)
-- psql variables are integers in milliseconds.
\set S 1787193009757
\set E 1787279409757
\set H1_MATURE 1787275809757

-- 1. Event funnel and latest Triage outcomes.
WITH w AS (
    SELECT *
    FROM news_events
    WHERE opened_at_ms >= :S AND opened_at_ms < :E
), lv AS (
    SELECT DISTINCT ON (v.event_id) v.*
    FROM news_verdicts v
    JOIN w USING (event_id)
    WHERE v.stage = 'triage'
    ORDER BY v.event_id, v.created_at_ms DESC
)
SELECT
    (SELECT count(*) FROM w) AS events,
    (SELECT count(*) FROM w WHERE ingest_mode = 'live') AS live,
    (SELECT count(*) FROM w WHERE ingest_mode = 'recovery') AS recovery,
    (SELECT count(*) FROM lv) AS triaged,
    count(*) FILTER (WHERE final_decision = 'push') AS push,
    count(*) FILTER (WHERE final_decision = 'escalate') AS escalate,
    count(*) FILTER (WHERE final_decision = 'drop') AS drop,
    count(*) FILTER (WHERE final_decision = 'throttled') AS throttled,
    count(*) FILTER (WHERE degraded) AS degraded
FROM lv;

-- Expected snapshot:
-- events=1628 live=1608 recovery=20 triaged=1572
-- push=391 escalate=28 drop=1065 throttled=88 degraded=1

-- 2. Admission distribution.
SELECT admission, count(*) AS n
FROM news_events
WHERE opened_at_ms >= :S AND opened_at_ms < :E
GROUP BY admission
ORDER BY n DESC;

-- Expected: candidate=1551, suppressed_pr_template=36,
-- listing_deterministic=21, recovery=20.

-- 3. Policy/prompt cohort distribution.
WITH w AS (
    SELECT event_id
    FROM news_events
    WHERE opened_at_ms >= :S AND opened_at_ms < :E
), lv AS (
    SELECT DISTINCT ON (v.event_id) v.*
    FROM news_verdicts v
    JOIN w USING (event_id)
    WHERE stage = 'triage'
    ORDER BY v.event_id, created_at_ms DESC
)
SELECT
    policy_version,
    prompt_version,
    count(*) AS n,
    count(*) FILTER (WHERE final_decision IN ('push', 'escalate')) AS pushed,
    count(*) FILTER (WHERE final_decision = 'throttled') AS throttled,
    min(created_at_ms) AS first_ms,
    max(created_at_ms) AS last_ms
FROM lv
GROUP BY policy_version, prompt_version
ORDER BY first_ms;

-- Expected:
-- v4/v8 220/57/19; v5/v8 702/181/42;
-- v6/v8 34/8/1; v6/v9 616/173/26 (n/pushed/throttled).

-- 4. Audience distribution.
WITH w AS (
    SELECT event_id
    FROM news_events
    WHERE opened_at_ms >= :S AND opened_at_ms < :E
), lv AS (
    SELECT DISTINCT ON (v.event_id) v.*
    FROM news_verdicts v
    JOIN w USING (event_id)
    WHERE stage = 'triage'
    ORDER BY v.event_id, created_at_ms DESC
)
SELECT
    coalesce(nullif(verdict ->> 'audience', ''), 'none') AS audience,
    count(*) AS triaged,
    count(*) FILTER (WHERE final_decision IN ('push', 'escalate')) AS pushed,
    count(*) FILTER (WHERE final_decision = 'throttled') AS throttled
FROM lv
GROUP BY audience
ORDER BY pushed DESC;

-- Expected: crypto=427/174/30; macro=475/147/45;
-- us_equity=191/98/13; none=479/0/0 (triaged/pushed/throttled).

-- 5. Storyline distribution.
WITH w AS (
    SELECT *
    FROM news_events
    WHERE opened_at_ms >= :S AND opened_at_ms < :E
), lv AS (
    SELECT DISTINCT ON (v.event_id) v.*
    FROM news_verdicts v
    JOIN w USING (event_id)
    WHERE stage = 'triage'
    ORDER BY v.event_id, created_at_ms DESC
)
SELECT
    w.storyline_key,
    count(*) AS events,
    count(*) FILTER (WHERE lv.final_decision IN ('push', 'escalate')) AS pushed,
    count(*) FILTER (WHERE lv.final_decision = 'throttled') AS throttled
FROM w
LEFT JOIN lv USING (event_id)
GROUP BY w.storyline_key
ORDER BY pushed DESC, events DESC;

-- Expected top rows: theme:mideast_energy=310/74/36;
-- macro:general=418/48/10; theme:rates=80/33/8; asset:BTC=100/30/10.

-- 6. Delivery truth for Events opened in the window. This is deliberately
-- not a delivery-settled-at window.
SELECT
    count(*) AS sent_for_opened_window,
    count(DISTINCT d.event_id) AS distinct_sent_events
FROM news_deliveries d
JOIN news_events e USING (event_id)
WHERE d.kind = 'first'
  AND d.state = 'sent'
  AND e.opened_at_ms >= :S
  AND e.opened_at_ms < :E;

-- Expected: 419 / 419.

-- 7. One-hour price maturity, coverage and direction-sign proxy.
-- This follows the product's reaction_v1 primary-asset event-level lower median.
WITH ev AS (
    SELECT DISTINCT ON (v.event_id)
        v.event_id,
        e.opened_at_ms,
        v.final_decision,
        v.degraded,
        v.verdict ->> 'direction' AS direction,
        d.state AS delivery_state
    FROM news_verdicts v
    JOIN news_events e USING (event_id)
    LEFT JOIN news_deliveries d
      ON d.event_id = v.event_id
     AND d.kind = 'first'
    WHERE v.stage = 'triage'
      AND e.opened_at_ms >= :S
      AND e.opened_at_ms < :E
    ORDER BY v.event_id, v.created_at_ms DESC
), agg AS (
    SELECT
        r.event_id,
        (array_agg(r.return_1h_bps ORDER BY r.return_1h_bps)
            FILTER (WHERE r.return_1h_bps IS NOT NULL))[
                (count(r.return_1h_bps) + 1) / 2
            ] AS bps_1h
    FROM news_event_reactions r
    WHERE r.metric_version = 'reaction_v1'
      AND r.is_primary
      AND r.anchor_at_ms >= :S
      AND r.anchor_at_ms < :E
    GROUP BY r.event_id
), fact AS (
    SELECT ev.*, agg.bps_1h
    FROM ev
    LEFT JOIN agg USING (event_id)
)
SELECT
    CASE WHEN delivery_state = 'sent' THEN 'delivered' ELSE 'held' END AS cohort,
    count(*) AS window_triaged,
    count(*) FILTER (WHERE opened_at_ms <= :H1_MATURE) AS eligible_1h,
    count(*) FILTER (
        WHERE opened_at_ms <= :H1_MATURE AND bps_1h IS NOT NULL
    ) AS priced_1h,
    count(*) FILTER (
        WHERE opened_at_ms <= :H1_MATURE
          AND bps_1h IS NOT NULL
          AND direction IN ('bullish', 'bearish')
    ) AS directional_1h,
    count(*) FILTER (
        WHERE opened_at_ms <= :H1_MATURE
          AND bps_1h IS NOT NULL
          AND (
              (direction = 'bullish' AND bps_1h > 0)
              OR (direction = 'bearish' AND bps_1h < 0)
          )
    ) AS hit_1h
FROM fact
GROUP BY cohort
ORDER BY cohort;

-- Expected:
-- delivered: window=419 eligible=402 priced=253 directional=241 hit=121
-- held:      window=1153 eligible=1109 priced=247 directional=185 hit=93

-- Near-duplicate proxy definition used outside SQL:
-- 1. Grain: one latest Triage verdict per Event in this Event-opened window.
-- 2. Text: verdict->>'headline_zh'; exclude degraded headlines.
-- 3. delivered: first delivery state='sent'.
-- 4. Remove whitespace; use unique character trigrams.
-- 5. containment(a,b)=|tri(a) intersect tri(b)|/min(|tri(a)|,|tri(b)|).
-- 6. Compare pairs no more than 4h apart; threshold=0.35.
-- Result: 94 delivered pairs. This is pair-grain, not unique facts.
--
-- Throttled uncovered-text proxy:
-- For every throttled Event, compare with delivered Events within +/-4h.
-- max containment <0.35 => uncovered wording. Result: 42 Events / 41
-- sequential clusters. This is not a gold missed-news label.
