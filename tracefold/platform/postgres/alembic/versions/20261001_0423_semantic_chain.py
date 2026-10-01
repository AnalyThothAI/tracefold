"""#764 P3: one semantic analysis chain and compact evidence state.

Migration evidence:
- category: forward-only projection-preserving consolidation.
- why_database_must_change: one immutable analysis with one adoption; compact evidence and shared semantic jobs.
- current_source_revision: 20261001_0422
- minimum_supported_source_revision: 20261001_0422
- lock_level_and_order: ACCESS EXCLUSIVE on source tables; stop Workers then Serve.
- statement_timeout: 1800s
- lock_timeout: 5s
- estimated_rows: semantic observations, updates and evidence versions; measure backup before rollout.
- estimated_bytes: source relation sizes; data-dependent.
- rewrite_or_index_build: full backfill and shared partial indexes.
- preflight_and_maintenance_boundary: export all ten retired tables, verify backup and sha256.
- archive_current_compatibility: analysis IDs, evidence digests, item revisions, leases and checkpoints preserved.
- role_and_grant_impact: none; existing single owner.
- failure_state: transaction rolls back on source mismatch.
- roll_forward_or_verified_backup_restore: verified backup with matching old image.
- production_postgres_image: postgres:18-bookworm
"""

import hashlib
import json

from alembic import op
from sqlalchemy import text

revision = "20261001_0423"
down_revision = "20261001_0422"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='1800s';
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
   AND (application_name LIKE 'tracefold_workers%' OR application_name LIKE 'tracefold_serve%'))
 THEN RAISE EXCEPTION 'p3_news_writers_connected'; END IF;
 IF EXISTS (SELECT 1 FROM news_jobs WHERE job_kind='semantic') THEN RAISE EXCEPTION 'p3_semantic_jobs_exist'; END IF;
 IF EXISTS (SELECT 1 FROM news_event_bands b LEFT JOIN news_events e USING(event_id)
   WHERE e.event_id IS NULL OR (b.dedupe_family,b.expires_at_ms) IS DISTINCT FROM (e.dedupe_family,e.expires_at_ms))
 THEN RAISE EXCEPTION 'p3_band_event_mismatch'; END IF;
 IF EXISTS (SELECT observation_result_id FROM news_event_updates WHERE observation_result_id IS NOT NULL
   GROUP BY 1 HAVING count(*)>1) OR EXISTS (
   SELECT 1 FROM news_event_updates u LEFT JOIN news_semantic_observations o ON o.result_id=u.observation_result_id
   WHERE u.observation_result_id IS NOT NULL AND
    (o.result_id IS NULL OR (o.event_id,o.input_revision) IS DISTINCT FROM (u.event_id,u.input_revision)))
 THEN RAISE EXCEPTION 'p3_observation_adoption_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM news_head_scope_repairs r FULL JOIN news_event_updates u ON u.scope_repair_id=r.repair_id
   WHERE (r.repair_id IS NOT NULL OR u.scope_repair_id IS NOT NULL) AND
    (r.repair_id IS NULL OR u.scope_repair_id IS NULL OR
     (r.event_id,r.content_revision,r.previous_content_revision,r.recorded_at_ms) IS DISTINCT FROM
     (u.event_id,u.content_revision,u.previous_content_revision,u.adopted_at_ms)))
 THEN RAISE EXCEPTION 'p3_scope_repair_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM news_event_update_heads h LEFT JOIN news_event_updates u
    ON (u.event_id,u.content_revision)=(h.event_id,h.content_revision)
   WHERE u.event_id IS NULL OR (h.input_revision,h.adopted_at_ms,h.update_ref) IS DISTINCT FROM
    (u.input_revision,u.adopted_at_ms,news_identity('update',jsonb_build_array(u.event_id,u.content_revision))))
 OR EXISTS (SELECT 1 FROM news_event_updates u WHERE NOT EXISTS
    (SELECT 1 FROM news_event_update_heads h WHERE h.event_id=u.event_id))
 THEN RAISE EXCEPTION 'p3_head_update_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM news_semantic_checkpoints c JOIN news_judgment_cache j
   ON j.cache_key='semantic_checkpoint:'||c.stage||':'||c.work_id)
 THEN RAISE EXCEPTION 'p3_checkpoint_key_collision'; END IF;
 IF EXISTS (SELECT 1 FROM news_event_evidence_snapshots s WHERE NOT EXISTS
   (SELECT 1 FROM news_events e WHERE e.event_id=s.event_id)) THEN RAISE NOTICE 'p3_orphan_snapshots'; END IF;
END $$;
ALTER TABLE news_events SET (fillfactor = 85);
ALTER TABLE news_items ADD COLUMN revisions jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD CONSTRAINT news_items_revisions_check CHECK ((jsonb_typeof(revisions) = 'array') IS TRUE);
ALTER TABLE news_events ADD COLUMN dedupe_bands text[] NOT NULL DEFAULT '{}'::text[],
  ADD COLUMN evidence_version integer, ADD COLUMN evidence jsonb, ADD COLUMN current_analysis_id text,
  ADD CONSTRAINT news_events_evidence_check CHECK (((evidence_version IS NULL AND evidence IS NULL)
    OR (evidence_version >= 1 AND jsonb_typeof(evidence) = 'object')) IS TRUE);
ALTER TABLE news_events DROP CONSTRAINT news_events_source_contract_consistency_check,
  DROP CONSTRAINT news_events_source_contract_reason_check, DROP COLUMN source_contract_reason;
CREATE TABLE news_analyses (
  analysis_id text PRIMARY KEY, event_id text NOT NULL REFERENCES news_events(event_id) ON DELETE CASCADE,
  origin text NOT NULL, input_revision integer NOT NULL, completed_at_ms bigint NOT NULL,
  work_id text, input_sha256 text, program_identity text, input_manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
  read_refs text[] NOT NULL DEFAULT '{}'::text[], reanalysis_reason text, reanalysis_head_ref text,
  understanding jsonb, repair jsonb, content_revision text, previous_content_revision text,
  update_ref text, adopted_at_ms bigint, document jsonb,
  CONSTRAINT news_analyses_origin_check CHECK (((origin = 'semantic' AND work_id IS NOT NULL AND input_sha256
 IS NOT NULL
      AND program_identity IS NOT NULL AND jsonb_typeof(understanding) = 'object' AND repair IS NULL)
    OR (origin = 'scope_repair' AND work_id IS NULL AND understanding IS NULL AND jsonb_typeof(repair) = 'object'
      AND document IS NOT NULL)) IS TRUE),
  CONSTRAINT news_analyses_adoption_check CHECK (((adopted_at_ms IS NULL AND content_revision IS NULL
      AND previous_content_revision IS NULL AND update_ref IS NULL AND document IS NULL)
    OR (adopted_at_ms >= 0 AND content_revision IS NOT NULL AND update_ref IS NOT NULL
      AND jsonb_typeof(document) = 'object')) IS TRUE),
  CONSTRAINT news_analyses_values_check CHECK ((input_revision >= 1 AND completed_at_ms >= 0
      AND jsonb_typeof(input_manifest) = 'object') IS TRUE),
  CONSTRAINT news_analyses_event_revision_key UNIQUE (event_id, content_revision));  -- 回执/链接按版本 join

INSERT INTO news_analyses(analysis_id,event_id,origin,input_revision,completed_at_ms,work_id,input_sha256,
 program_identity,read_refs,reanalysis_reason,reanalysis_head_ref,understanding,content_revision,
 previous_content_revision,update_ref,adopted_at_ms,document)
SELECT o.result_id,o.event_id,'semantic',o.input_revision,o.completed_at_ms,o.work_id,o.input_sha256,
 o.program_identity,o.read_refs,o.reanalysis_reason,o.reanalysis_head_ref,o.understanding,u.content_revision,
 u.previous_content_revision,CASE WHEN u.content_revision IS NOT NULL THEN
 news_identity('update',jsonb_build_array(u.event_id,u.content_revision)) END,u.adopted_at_ms,u.document
FROM news_semantic_observations o LEFT JOIN news_event_updates u ON u.observation_result_id=o.result_id;
INSERT INTO news_analyses(analysis_id,event_id,origin,input_revision,completed_at_ms,repair,content_revision,
 previous_content_revision,update_ref,adopted_at_ms,document)
SELECT r.repair_id,r.event_id,'scope_repair',u.input_revision,r.recorded_at_ms,
 jsonb_build_object('claim_refs',r.claim_refs,'proof',r.proof,'projection_version',r.projection_version),
 u.content_revision,u.previous_content_revision,news_identity('update',jsonb_build_array(u.event_id,u.content_revision)),
 u.adopted_at_ms,u.document FROM news_head_scope_repairs r JOIN news_event_updates u ON u.scope_repair_id=r.repair_id;
CREATE TEMP TABLE p3_material(event_id text PRIMARY KEY,material_sha256 text NOT NULL) ON COMMIT DROP;
""")
    conn = op.get_bind()
    rows = conn.execute(
        text("""
      SELECT DISTINCT ON (s.event_id) s.event_id,s.snapshot FROM news_event_evidence_snapshots s
      JOIN news_events e USING(event_id) ORDER BY s.event_id,s.evidence_version DESC
    """)
    )
    for event_id, snapshot in rows:
        card = snapshot.get("card") or {}
        material = {
            "focus_fact": snapshot.get("focus_fact"),
            "leader_item_id": card.get("leader_item_id"),
            "grounded_assets": card.get("grounded_assets"),
            "members": [
                {key: member.get(key) for key in ("item_id", "fact_id", "fact_text", "evidence_revisions")}
                for member in snapshot.get("members") or ()
            ],
        }
        serialized = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conn.execute(
            text("INSERT INTO p3_material VALUES (:event_id,:sha)"),
            {"event_id": event_id, "sha": hashlib.sha256(serialized.encode()).hexdigest()},
        )
    op.execute(r"""
UPDATE news_events e SET
 current_analysis_id=(SELECT a.analysis_id FROM news_event_update_heads h JOIN news_analyses a
   ON (a.event_id,a.content_revision)=(h.event_id,h.content_revision) WHERE h.event_id=e.event_id),
 dedupe_bands=COALESCE((SELECT array_agg(b.band_index::text||':'||b.band_key ORDER BY b.band_index,b.band_key)
   FROM news_event_bands b WHERE b.event_id=e.event_id),'{}'::text[]),
 evidence_version=(SELECT max(s.evidence_version) FROM news_event_evidence_snapshots s WHERE s.event_id=e.event_id),
 evidence=(SELECT jsonb_build_object('material_sha256',m.material_sha256,
   'focus_item_id',s.snapshot#>>'{card,leader_item_id}',
   'fact_scopes',COALESCE((SELECT jsonb_object_agg(f.focus_fact_id,f.fact) FROM (
     SELECT DISTINCT ON (v.focus_fact_id) v.focus_fact_id,v.snapshot->'focus_fact' AS fact
       FROM news_event_evidence_snapshots v WHERE v.event_id=e.event_id ORDER BY
 v.focus_fact_id,v.evidence_version) f),'{}'),
   'versions',(SELECT jsonb_agg(jsonb_build_object('evidence_version',v.evidence_version,
     'evidence_sha256',v.evidence_sha256,'focus_fact_id',v.focus_fact_id,'created_at_ms',v.created_at_ms)
     ORDER BY v.evidence_version) FROM news_event_evidence_snapshots v WHERE v.event_id=e.event_id))
   FROM news_event_evidence_snapshots s JOIN p3_material m USING(event_id)
   WHERE s.event_id=e.event_id ORDER BY s.evidence_version DESC LIMIT 1);
UPDATE news_items i SET revisions=COALESCE((SELECT jsonb_agg(to_jsonb(r)-'item_id' ORDER BY r.revision_sequence)
 FROM news_item_revisions r WHERE r.item_id=i.item_id),'[]');
INSERT INTO news_jobs(job_kind,subject_id,state,attempts,next_attempt_at_ms,lease_token,lease_until_ms,
 last_error_code,detail,created_at_ms,updated_at_ms)
SELECT 'semantic',w.event_id,CASE WHEN done_revision>=wanted_revision THEN 'done'
 WHEN last_outcome='failed' THEN 'failed' ELSE 'pending' END,w.attempts,w.next_attempt_at_ms,w.lease_token,
 w.leased_until_ms,w.last_error_code,to_jsonb(w)-ARRAY['event_id','attempts','next_attempt_at_ms','lease_token',
 'leased_until_ms','last_error_code','updated_at_ms'],w.updated_at_ms,w.updated_at_ms FROM news_semantic_work w;
INSERT INTO news_judgment_cache(cache_key,answer,created_at_ms)
SELECT 'semantic_checkpoint:'||stage||':'||work_id,document,created_at_ms FROM news_semantic_checkpoints;
CREATE FUNCTION pg_temp.p3_verify(label text, source text, target text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE mismatch boolean; src_count bigint; dst_count bigint; src_sha text; dst_sha text;
BEGIN
 EXECUTE
 format('SELECT count(*),md5(COALESCE(string_agg(row::text,E''\\n'' ORDER BY row::text),'''')) FROM (%s) q',source)
  INTO src_count,src_sha;
 EXECUTE
 format('SELECT count(*),md5(COALESCE(string_agg(row::text,E''\\n'' ORDER BY row::text),'''')) FROM (%s) q',target)
  INTO dst_count,dst_sha;
 EXECUTE format('SELECT EXISTS ((%s EXCEPT ALL %s) UNION ALL (%s EXCEPT ALL %s))',source,target,target,source)
  INTO mismatch;
 IF mismatch OR (src_count,src_sha) IS DISTINCT FROM (dst_count,dst_sha)
 THEN RAISE EXCEPTION 'p3_projection_mismatch: %',label; END IF;
 RAISE NOTICE 'p3_verify % rows=% md5=%',label,src_count,src_sha;
END $$;

SELECT pg_temp.p3_verify('updates',$src$SELECT to_jsonb(u) AS row FROM news_event_updates u$src$,$dst$SELECT
 to_jsonb(a)-'update_ref' AS row FROM (
SELECT event_id,content_revision,input_revision,previous_content_revision,adopted_at_ms,update_ref,
       CASE WHEN origin='semantic' THEN analysis_id END AS observation_result_id,
       CASE WHEN origin='scope_repair' THEN analysis_id END AS scope_repair_id,document
  FROM news_analyses WHERE adopted_at_ms IS NOT NULL
) a$dst$);

SELECT pg_temp.p3_verify('observations',$src$SELECT to_jsonb(o)-'evidence_refs' AS row FROM
 news_semantic_observations o$src$,$dst$SELECT to_jsonb(o)-'input_manifest' AS row FROM (
SELECT analysis_id AS result_id,work_id,event_id,input_revision,input_sha256,program_identity,
       completed_at_ms,understanding,read_refs,reanalysis_reason,reanalysis_head_ref,input_manifest
  FROM news_analyses WHERE origin='semantic'
) o$dst$);

SELECT pg_temp.p3_verify('heads',$src$SELECT to_jsonb(h) AS row FROM news_event_update_heads
 h$src$,$dst$SELECT to_jsonb(h) AS row FROM (
SELECT a.event_id,a.content_revision,a.input_revision,a.update_ref,a.adopted_at_ms
  FROM news_events e JOIN news_analyses a ON a.analysis_id=e.current_analysis_id
) h$dst$);

SELECT pg_temp.p3_verify('repairs',$src$SELECT to_jsonb(r) AS row FROM news_head_scope_repairs
 r$src$,$dst$SELECT
 jsonb_build_object('repair_id',analysis_id,'event_id',event_id,
 'previous_content_revision',previous_content_revision,'content_revision',content_revision,
 'claim_refs',repair->'claim_refs','proof',repair->'proof','projection_version',repair->'projection_version',
 'recorded_at_ms',completed_at_ms) AS row FROM news_analyses WHERE origin='scope_repair'$dst$);

SELECT pg_temp.p3_verify('jobs',$src$SELECT to_jsonb(w) AS row FROM news_semantic_work w$src$,$dst$SELECT
 to_jsonb(w) AS row FROM (
SELECT j.subject_id AS event_id,j.attempts,j.next_attempt_at_ms,j.lease_token,
       j.lease_until_ms AS leased_until_ms,j.last_error_code,j.updated_at_ms,d.*
  FROM news_jobs j CROSS JOIN LATERAL jsonb_to_record(j.detail) AS d(
    wanted_revision integer,done_revision integer,lineage_id text,published_at_ms bigint,last_outcome text,
    extra_read_state text,extra_read_target_ref text,attached_evidence jsonb,focus_claim_refs jsonb,
    processed_read_refs text[],failed_read_refs text[],attempt_read_refs text[],reanalysis_read_ref text,
    reanalysis_reason text,reanalysis_head_ref text)
 WHERE j.job_kind='semantic'
) w$dst$);

SELECT pg_temp.p3_verify('checkpoints',$src$SELECT to_jsonb(c) AS row FROM news_semantic_checkpoints
 c$src$,$dst$SELECT
 jsonb_build_object('work_id',substr(cache_key,
 length('semantic_checkpoint:')+length(split_part(substr(cache_key,21),':',1))+2),
 'stage',split_part(substr(cache_key,21),':',1),'document',answer,'created_at_ms',created_at_ms) AS row
 FROM news_judgment_cache WHERE cache_key LIKE 'semantic_checkpoint:%'$dst$);

SELECT pg_temp.p3_verify('revisions',$src$SELECT to_jsonb(r) AS row FROM news_item_revisions
 r$src$,$dst$SELECT to_jsonb(r) AS row FROM (
SELECT i.item_id,r.* FROM news_items i CROSS JOIN LATERAL jsonb_to_recordset(i.revisions) AS r(
  revision_sha256 text,content_sha256 text,previous_revision_sha256 text,revision_sequence bigint,
  evidence_text text,provider_params jsonb,reporting_origin text,canonical_url text,source_artifact_id text,
  published_at_ms bigint,received_at_ms bigint)
) r$dst$);

SELECT pg_temp.p3_verify('bands',$src$SELECT to_jsonb(b) AS row FROM news_event_bands b$src$,$dst$SELECT
 jsonb_build_object('event_id',event_id,'band_index',split_part(band,':',1)::smallint,
 'band_key',substr(band,strpos(band,':')+1),'dedupe_family',dedupe_family,'expires_at_ms',expires_at_ms) AS row
 FROM news_events CROSS JOIN LATERAL unnest(dedupe_bands) band$dst$);

SELECT pg_temp.p3_verify('evidence_versions',$src$SELECT to_jsonb(s)-'snapshot' AS row FROM
 news_event_evidence_snapshots s JOIN news_events e USING(event_id)$src$,$dst$SELECT to_jsonb(s)-'snapshot'
 AS row FROM (
SELECT e.event_id,v.*,'observed'::text AS provenance,true AS release_eligible,
       jsonb_build_object('schema_version','news_event_evidence_v3') AS snapshot
  FROM news_events e CROSS JOIN LATERAL jsonb_to_recordset(e.evidence->'versions') AS v(
    evidence_version integer,evidence_sha256 text,focus_fact_id text,created_at_ms bigint)
) s$dst$);

SELECT pg_temp.p3_verify('claim_links',$src$SELECT to_jsonb(l) AS row FROM news_claim_links
 l$src$,$dst$SELECT to_jsonb(l) AS row FROM (
SELECT DISTINCT a.update_ref,change->>'current_ref' AS current_ref,change->>'previous_ref' AS previous_ref,
       change->>'relation' AS relation,a.event_id AS current_event_id,p.event_id AS previous_event_id,
       a.adopted_at_ms AS asserted_at_ms
  FROM news_analyses a CROSS JOIN LATERAL jsonb_array_elements(a.document->'changes') change
  LEFT JOIN LATERAL (
    SELECT prior.event_id FROM news_analyses prior
     WHERE prior.adopted_at_ms<=a.adopted_at_ms
       AND EXISTS (SELECT 1 FROM jsonb_array_elements(prior.document->'claims') claim
                    WHERE claim->>'ref'=change->>'previous_ref')
     ORDER BY (prior.event_id=a.event_id) DESC,prior.adopted_at_ms DESC,prior.analysis_id LIMIT 1
  ) p ON true
 WHERE a.adopted_at_ms IS NOT NULL AND change->>'previous_ref' IS NOT NULL
   AND change->>'previous_ref'<>change->>'current_ref'
   AND change->>'relation' IN ('equivalent','adds_information','real_world_change','corrects','conflicts')
) l$dst$);

DO $$ BEGIN
 IF (SELECT count(*) FROM news_analyses)<>(SELECT count(*) FROM news_semantic_observations)+
  (SELECT count(*) FROM news_head_scope_repairs) THEN RAISE EXCEPTION 'p3_analysis_count_mismatch'; END IF;
 RAISE NOTICE 'p3_verify ok';
END $$;
DROP TABLE news_claim_links,news_event_update_heads,news_event_updates,news_head_scope_repairs,
 news_semantic_observations,news_semantic_checkpoints,news_semantic_work,news_event_evidence_snapshots,
 news_event_bands,news_item_revisions;
DROP FUNCTION IF EXISTS reject_news_event_evidence_mutation();
DROP FUNCTION IF EXISTS news_current_evidence_snapshot_valid(jsonb,text,text);
DROP FUNCTION IF EXISTS news_jsonb_required_optional_keys(jsonb,text[],text[]);
DROP FUNCTION IF EXISTS news_jsonb_exact_keys(jsonb,text[]);
DROP FUNCTION IF EXISTS news_jsonb_int64_valid(jsonb);
DROP FUNCTION IF EXISTS news_identity(text,jsonb);
DROP INDEX ix_news_events_storyline;    -- 无查询按 storyline_key 过滤/排序
DROP INDEX ix_news_events_unpublished;  -- 唯一谓词是主键 UPDATE
CREATE INDEX news_analyses_update_ref ON news_analyses (update_ref) WHERE update_ref IS NOT NULL;  -- 按 ref 取
 -- head/旧 claim
CREATE INDEX news_analyses_work ON news_analyses (work_id) WHERE work_id IS NOT NULL;  -- _observed_work
CREATE INDEX news_analyses_completed ON news_analyses (completed_at_ms);  -- 状态页 24 h 观察数
CREATE INDEX news_analyses_adopted ON news_analyses (adopted_at_ms) WHERE adopted_at_ms IS NOT NULL;  -- 24 h 采纳数
CREATE INDEX news_analyses_previous_refs ON news_analyses
  USING gin (jsonb_path_query_array(document, '$."changes"[*]."previous_ref"'));  -- 失效 claim、claim link
CREATE INDEX news_analyses_current_refs ON news_analyses
  USING gin (jsonb_path_query_array(document, '$."changes"[*]."current_ref"'));  -- claim link 两跳
CREATE INDEX news_events_dedupe_bands ON news_events USING gin (dedupe_bands) WITH (fastupdate = off);  -- band 候选
CREATE INDEX news_events_bands_expiry ON news_events (expires_at_ms) WHERE dedupe_bands <> '{}'::text[];  --
 -- janitor 清 band
CREATE INDEX news_events_current_analysis ON news_events (current_analysis_id)
  WHERE current_analysis_id IS NOT NULL;  -- FK 的 RI 查找
CREATE INDEX news_jobs_semantic_lineage ON news_jobs ((detail ->> 'lineage_id')) WHERE job_kind = 'semantic';
 -- 读取预算
ALTER TABLE news_events ADD CONSTRAINT news_events_current_analysis_fkey
  FOREIGN KEY (current_analysis_id) REFERENCES news_analyses(analysis_id);
CREATE FUNCTION news_analysis_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF OLD.adopted_at_ms IS NOT NULL
     OR (to_jsonb(NEW) - '{content_revision,previous_content_revision,update_ref,adopted_at_ms,document}'::text[])
        IS DISTINCT FROM (to_jsonb(OLD) -
 '{content_revision,previous_content_revision,update_ref,adopted_at_ms,document}'::text[])
  THEN RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'news_analysis_immutable'; END IF;
  RETURN NEW; END $$;
CREATE TRIGGER news_analyses_immutable BEFORE UPDATE ON news_analyses FOR EACH ROW EXECUTE FUNCTION
 news_analysis_immutable();
""")


def downgrade() -> None:
    raise RuntimeError("P3 is forward-only: restore a verified backup and the matching previous image")
