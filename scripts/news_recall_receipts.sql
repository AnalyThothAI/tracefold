-- Read-only daily facts. Bind %(from_ms)s and %(as_of_ms)s; unknown historical
-- diagnostics remain unknown, never counted as healthy calls.
WITH semantic AS (
  SELECT input_manifest,understanding FROM news_analyses
   WHERE origin='semantic' AND completed_at_ms >= %(from_ms)s AND completed_at_ms < %(as_of_ms)s
), prior_calls AS (
  SELECT q.value FROM semantic s
   CROSS JOIN LATERAL jsonb_each(COALESCE(s.input_manifest->'recall'->'queries','{}'::jsonb)) q
), decisions AS (
  SELECT input_snapshot,plan FROM news_notifications
   WHERE kind='update' AND decided_at_ms >= %(from_ms)s AND decided_at_ms < %(as_of_ms)s
), receipt_calls AS (
  SELECT q.value FROM decisions d
   CROSS JOIN LATERAL jsonb_each(COALESCE(d.input_snapshot->'recall','{}'::jsonb)) q
), relations AS (
  SELECT r.value FROM semantic s
   CROSS JOIN LATERAL jsonb_array_elements(COALESCE(s.understanding->'relations','[]'::jsonb)) r
)
SELECT jsonb_build_object(
  'semantic_observations',(SELECT count(*) FROM semantic),
  'semantic_observations_with_recall',(SELECT count(*) FROM semantic WHERE input_manifest ? 'recall'),
  'relation_pairs',(SELECT COALESCE(sum((input_manifest->'recall'->>'pair_count')::bigint),0) FROM semantic),
  'relation_outputs',(SELECT count(*) FROM relations),
  'useful_relation_outputs',(SELECT count(*) FROM relations WHERE value->>'relation' <> 'unrelated'),
  'prior_calls',(SELECT count(*) FROM prior_calls),
  'prior_degraded',(SELECT count(*) FROM prior_calls WHERE (value->>'degraded')::boolean),
  'receipt_calls',(SELECT count(*) FROM receipt_calls),
  'receipt_degraded',(SELECT count(*) FROM receipt_calls WHERE (value->>'degraded')::boolean)
) AS statistics;
