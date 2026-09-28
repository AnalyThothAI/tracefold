-- Issue #733: read-only production evidence for the EventUpdate rollout audit.
-- Run with psql -X -A -F '|' -v ON_ERROR_STOP=1 -f this-file on the production DB.
-- Fixed event cohort: [2026-09-27 07:54, 2026-09-28 07:54) UTC.
-- Insert-only observations, updates, decisions and deliveries are bounded at audit_end_ms.
-- news_semantic_work and news_notification_work are mutable snapshots: reruns cannot
-- reconstruct their status at the original query time.
\set audit_end_ms 1790582040000
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '20s';

SELECT 'snapshot_utc' AS section, now() AS value, :audit_end_ms AS audit_end_ms;
SELECT 'first_observed_adoption' AS section,
       to_timestamp(min(adopted_at_ms)/1000.0) AS first_adoption_utc
FROM news_event_updates;

WITH e AS (
  SELECT * FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
)
SELECT 'population_24h' AS section, count(*) AS events,
       count(DISTINCT leader_item_id) AS leader_items,
       count(*) FILTER (WHERE focus_fact_method = 'explicit_numbered') AS split_events,
       count(DISTINCT leader_item_id) FILTER (WHERE focus_fact_method = 'explicit_numbered') AS split_leader_items,
       count(*) FILTER (WHERE focus_fact_method = 'whole_item') AS whole_events
FROM e;

WITH e AS (
  SELECT * FROM news_events
  WHERE created_at_ms >= (SELECT min(adopted_at_ms) FROM news_event_updates)
    AND created_at_ms < :audit_end_ms
)
SELECT 'post_first_adoption' AS section, count(*) AS events,
       count(DISTINCT leader_item_id) AS leader_items,
       count(*) FILTER (WHERE focus_fact_method = 'explicit_numbered') AS split_events,
       count(*) FILTER (WHERE focus_fact_method = 'whole_item') AS whole_events
FROM e;

WITH e AS (
  SELECT * FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
), o AS (
  SELECT event_id, min(completed_at_ms) AS t FROM news_semantic_observations
  WHERE completed_at_ms < :audit_end_ms GROUP BY event_id
), u AS (
  SELECT event_id, min(adopted_at_ms) AS t FROM news_event_updates
  WHERE adopted_at_ms < :audit_end_ms GROUP BY event_id
), d AS (
  SELECT event_id, min(created_at_ms) AS t FROM news_notification_decisions
  WHERE created_at_ms < :audit_end_ms GROUP BY event_id
), s AS (
  SELECT event_id, min(settled_at_ms) AS t FROM news_deliveries
  WHERE state = 'sent' AND settled_at_ms < :audit_end_ms GROUP BY event_id
)
SELECT 'latency_by_method' AS section, e.focus_fact_method AS method, count(*) AS events,
       count(o.t) AS observed, count(u.t) AS adopted, count(d.t) AS decided, count(s.t) AS sent,
       round(percentile_cont(.5) WITHIN GROUP (ORDER BY (o.t - e.opened_at_ms)/1000.0)::numeric, 2) AS opened_to_observation_p50_s,
       round(percentile_cont(.95) WITHIN GROUP (ORDER BY (o.t - e.opened_at_ms)/1000.0)::numeric, 2) AS opened_to_observation_p95_s,
       round(percentile_cont(.5) WITHIN GROUP (ORDER BY (o.t - e.created_at_ms)/1000.0)::numeric, 2) AS created_to_observation_p50_s,
       round(percentile_cont(.95) WITHIN GROUP (ORDER BY (o.t - e.created_at_ms)/1000.0)::numeric, 2) AS created_to_observation_p95_s,
       round(percentile_cont(.5) WITHIN GROUP (ORDER BY (d.t - u.t)/1000.0)::numeric, 2) AS adoption_to_decision_p50_s,
       round(percentile_cont(.95) WITHIN GROUP (ORDER BY (d.t - u.t)/1000.0)::numeric, 2) AS adoption_to_decision_p95_s,
       round(percentile_cont(.5) WITHIN GROUP (ORDER BY (s.t - u.t)/1000.0)::numeric, 2) AS adoption_to_sent_p50_s,
       round(percentile_cont(.95) WITHIN GROUP (ORDER BY (s.t - u.t)/1000.0)::numeric, 2) AS adoption_to_sent_p95_s
FROM e LEFT JOIN o USING (event_id) LEFT JOIN u USING (event_id)
       LEFT JOIN d USING (event_id) LEFT JOIN s USING (event_id)
GROUP BY e.focus_fact_method ORDER BY e.focus_fact_method;

WITH e AS (
  SELECT * FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
)
SELECT 'source_clock' AS section, focus_fact_method AS method,
       count(*) FILTER (WHERE created_at_ms < opened_at_ms) AS future_opened_at,
       round(percentile_cont(.5) WITHIN GROUP (ORDER BY (created_at_ms-opened_at_ms)/1000.0)::numeric,2) AS opened_to_created_p50_s,
       round(percentile_cont(.95) WITHIN GROUP (ORDER BY (created_at_ms-opened_at_ms)/1000.0)::numeric,2) AS opened_to_created_p95_s
FROM e GROUP BY focus_fact_method ORDER BY focus_fact_method;

WITH e AS (
  SELECT * FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
), s AS (
  SELECT DISTINCT ON (event_id) event_id, decision_ref, attempted_at_ms, settled_at_ms
  FROM news_deliveries WHERE state = 'sent' AND settled_at_ms < :audit_end_ms
  ORDER BY event_id, settled_at_ms
)
SELECT 'matched_send' AS section, e.focus_fact_method AS method,
       count(s.event_id) AS sent_events, count(d.decision_ref) AS matched_decisions,
       round(percentile_cont(.5) WITHIN GROUP (ORDER BY (s.settled_at_ms-d.created_at_ms)/1000.0)::numeric, 2) AS decision_to_sent_p50_s,
       round(percentile_cont(.95) WITHIN GROUP (ORDER BY (s.settled_at_ms-d.created_at_ms)/1000.0)::numeric, 2) AS decision_to_sent_p95_s,
       round(percentile_cont(.5) WITHIN GROUP (ORDER BY (s.settled_at_ms-s.attempted_at_ms)/1000.0)::numeric, 2) AS attempt_to_sent_p50_s
FROM e LEFT JOIN s USING (event_id)
       LEFT JOIN news_notification_decisions d ON d.decision_ref = s.decision_ref
GROUP BY e.focus_fact_method ORDER BY e.focus_fact_method;

WITH e AS (
  SELECT * FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000
    AND created_at_ms < :audit_end_ms - 600000
)
SELECT 'semantic_work_snapshot' AS section, e.focus_fact_method AS method,
       count(*) AS events, count(w.event_id) AS work_rows,
       round(avg(w.attempts)::numeric, 3) AS current_revision_attempts_mean,
       count(*) FILTER (WHERE w.last_outcome = 'failed' AND w.wanted_revision > coalesce(w.done_revision,0)) AS failed_current_revision,
       count(*) FILTER (WHERE w.wanted_revision > coalesce(w.done_revision,0)) AS pending_current_revision
FROM e LEFT JOIN news_semantic_work w USING (event_id)
GROUP BY e.focus_fact_method ORDER BY e.focus_fact_method;

WITH e AS (
  SELECT * FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
)
SELECT 'lineage_quality' AS section, count(*) AS events,
       count(*) FILTER (WHERE i.item_id IS NULL) AS missing_leader_item,
       count(*) FILTER (WHERE m.event_id IS NULL) AS missing_leader_fact_member,
       count(*) FILTER (WHERE e.focus_fact_method = 'explicit_numbered' AND e.focus_fact_text IS NULL) AS missing_split_text
FROM e LEFT JOIN news_items i ON i.item_id = e.leader_item_id
       LEFT JOIN news_event_members m ON m.event_id = e.event_id
         AND m.item_id = e.leader_item_id AND m.fact_id = e.focus_fact_id
         AND m.match_kind = 'leader';

WITH e AS (
  SELECT event_id, leader_item_id FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
    AND focus_fact_method = 'explicit_numbered'
)
SELECT 'split_membership' AS section, count(DISTINCT e.event_id) AS events,
       count(DISTINCT m.item_id) AS contributing_items,
       count(*) AS fact_member_rows,
       count(*) FILTER (WHERE m.match_kind = 'leader') AS leader_members,
       count(*) FILTER (WHERE m.match_kind = 'exact') AS exact_members,
       count(*) FILTER (WHERE m.match_kind = 'near') AS near_members
FROM e JOIN news_event_members m USING (event_id);

WITH e AS (
  SELECT * FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
    AND focus_fact_method = 'explicit_numbered'
), s AS (
  SELECT event_id, min(settled_at_ms) AS t FROM news_deliveries
  WHERE state = 'sent' AND settled_at_ms < :audit_end_ms GROUP BY event_id
)
SELECT 'split_leader_item' AS section, i.source_item_key, i.item_id,
       count(*) AS events, count(s.t) AS sent_events,
       round((max(s.t)-min(s.t))/1000.0,1) AS first_to_last_sent_s,
       count(*) FILTER (WHERE w.last_outcome='failed' AND w.wanted_revision>coalesce(w.done_revision,0)) AS failed_current_revision
FROM e JOIN news_items i ON i.item_id=e.leader_item_id
       LEFT JOIN s USING (event_id) LEFT JOIN news_semantic_work w USING (event_id)
GROUP BY i.source_item_key,i.item_id ORDER BY events DESC, i.source_item_key;

WITH e AS (
  SELECT event_id, leader_item_id FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
    AND focus_fact_method = 'explicit_numbered'
)
SELECT 'split_all_items' AS section, i.source_item_key, i.item_id,
       count(DISTINCT m.event_id) AS member_events,
       count(DISTINCT m.event_id) FILTER (WHERE e.leader_item_id=i.item_id) AS leader_events
FROM e JOIN news_event_members m USING (event_id) JOIN news_items i ON i.item_id=m.item_id
GROUP BY i.source_item_key,i.item_id ORDER BY member_events DESC,i.source_item_key;

WITH e AS (
  SELECT * FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
), first_decision AS (
  SELECT DISTINCT ON (event_id) event_id, plan FROM news_notification_decisions
  WHERE created_at_ms < :audit_end_ms ORDER BY event_id, created_at_ms
)
SELECT 'first_decision' AS section, e.focus_fact_method AS method,
       d.plan->>'action' AS action, d.plan->>'reason' AS reason, count(*) AS events
FROM e JOIN first_decision d USING (event_id)
GROUP BY e.focus_fact_method,d.plan->>'action',d.plan->>'reason'
ORDER BY method,events DESC;

WITH e AS (
  SELECT event_id, focus_fact_method FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
)
SELECT 'delivery_evidence' AS section, e.focus_fact_method AS method,
       d.kind, d.state, count(*) AS delivery_rows,
       count(*) FILTER (WHERE d.receipt IS NOT NULL) AS with_receipt,
       count(*) FILTER (WHERE d.body IS NOT NULL) AS with_delivered_body,
       count(*) FILTER (WHERE d.decision_ref IS NOT NULL) AS with_decision_ref
FROM e JOIN news_deliveries d USING (event_id)
WHERE d.settled_at_ms < :audit_end_ms
GROUP BY e.focus_fact_method,d.kind,d.state ORDER BY method,d.kind,d.state;

WITH e AS (
  SELECT * FROM news_events
  WHERE created_at_ms >= :audit_end_ms - 86400000 AND created_at_ms < :audit_end_ms
)
SELECT 'current_semantic_failure' AS section, e.focus_fact_method AS method,
       w.last_error_code, count(*) AS events
FROM e JOIN news_semantic_work w USING (event_id)
WHERE w.last_outcome = 'failed' AND w.wanted_revision > coalesce(w.done_revision,0)
GROUP BY e.focus_fact_method,w.last_error_code ORDER BY method,events DESC,w.last_error_code;

WITH items AS (
  SELECT DISTINCT i.source_item_key, i.evidence_text
  FROM news_events e JOIN news_items i ON i.item_id=e.leader_item_id
  WHERE e.created_at_ms >= :audit_end_ms - 86400000 AND e.created_at_ms < :audit_end_ms
    AND e.focus_fact_method='explicit_numbered'
)
SELECT 'split_source_non_numbered_line' AS section, source_item_key, ord, line
FROM items CROSS JOIN LATERAL regexp_split_to_table(evidence_text,E'\n') WITH ORDINALITY x(line,ord)
WHERE btrim(line)<>'' AND btrim(line) !~ '^\d{1,2}[.)、:：]'
ORDER BY source_item_key,ord;

SELECT 'scope_sample_source_revision' AS section, i.source_item_key,
       i.item_id, count(r.item_id) AS later_item_revisions
FROM news_items i LEFT JOIN news_item_revisions r ON r.item_id=i.item_id
WHERE i.source_item_key='4234240' AND i.source_id='news-opennews'
GROUP BY i.source_item_key,i.item_id;

SELECT 'later_notification_revision' AS section, d.event_id,
       to_timestamp(d.created_at_ms/1000.0) AS decision_utc,
       d.decision_ref, d.plan->>'action' AS action, d.plan->>'reason' AS reason,
       to_timestamp(s.settled_at_ms/1000.0) AS sent_utc, s.state AS delivery_state
FROM news_notification_decisions d
LEFT JOIN news_deliveries s ON s.decision_ref=d.decision_ref AND s.settled_at_ms<:audit_end_ms
WHERE d.event_id='936b9ec5775f188247e78e17792c7479467ef93b5bfa5037420f509eb556381b'
  AND d.created_at_ms<:audit_end_ms
ORDER BY d.created_at_ms;

COMMIT;
