"""#764 P4: Trading entries, case documents, accounts and platform processes.

Migration evidence:
- category: stopped-writer, forward-only projection-preserving consolidation.
- why_database_must_change: seven Trading tables and one platform process ledger.
- current_source_revision: 20261001_0423
- minimum_supported_source_revision: 20261001_0423
- lock_level_and_order: ACCESS EXCLUSIVE; stop Analysis, drain pending, stop Executor and Workers.
- statement_timeout: 1800s; lock_timeout: 5s.
- estimated_rows/bytes: measure the fourteen source tables in the verified backup.
- rewrite_or_index_build: full backfill and partial indexes.
- preflight_and_maintenance_boundary: export retired tables, sha256 and pg_restore --list.
- archive_current_compatibility: public input, case, intent, entry, order and fill identities preserved.
- role_and_grant_impact: none; existing single owner.
- failure_state: transactional rollback on predecessor mismatch.
- roll_forward_or_verified_backup_restore: verified backup and matching old image; venue orders persist.
- production_postgres_image: postgres:18-bookworm
"""

import sys
from typing import Any

from alembic import op

revision = "20261001_0424"
down_revision = "20261001_0423"
branch_labels = None
depends_on = None


def _verify_notice(notice: Any) -> None:
    message = str(notice.message_primary or "")
    if message.startswith("p4_verify "):
        print(message, file=sys.stderr)


def _execute(sql: str) -> None:
    if op.get_context().as_sql:
        op.execute(sql)
        return
    driver = op.get_bind().connection.driver_connection
    if driver is None:
        raise RuntimeError("migration_driver_connection_missing")
    driver.add_notice_handler(_verify_notice)
    try:
        op.execute(sql)
    finally:
        driver.remove_notice_handler(_verify_notice)


def upgrade() -> None:
    _execute(r"""
SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='1800s';
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
   AND application_name ~ '^tracefold_(workers|serve|analysis|executor|trading_analysis)')
 THEN RAISE EXCEPTION 'p4_writers_connected'; END IF;
 IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trading_executor_state'
   AND column_name IN ('last_signal_seq','last_intent_seq'))
 THEN RAISE EXCEPTION 'p4_requires_p0'; END IF;
 IF EXISTS (SELECT case_id FROM trading_assessments GROUP BY case_id HAVING count(*)>1)
 THEN RAISE EXCEPTION 'p4_replay_assessments_present'; END IF;
 IF EXISTS (SELECT 1 FROM trading_policy_actions a LEFT JOIN trading_assessments f
   USING(case_id,program_sha) WHERE f.case_id IS NULL)
 THEN RAISE EXCEPTION 'p4_action_assessment_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM trading_paper_legs l JOIN trading_cases c USING(case_id)
   WHERE (c.geometry_version IS NOT NULL AND l.geometry_version IS DISTINCT FROM c.geometry_version)
      OR (c.geometry_version IS NULL AND (l.status<>'missing' OR c.state<>'failed')))
 OR EXISTS (SELECT case_id FROM trading_paper_legs GROUP BY case_id
   HAVING count(*)<>2 OR count(DISTINCT side)<>2 OR count(DISTINCT geometry_version)<>1)
 THEN RAISE EXCEPTION 'p4_paper_case_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM trading_source_amendments WHERE
   payload->'affected_claim_refs' IS DISTINCT FROM affected_claim_refs
   OR payload->'retired_claim_refs' IS DISTINCT FROM retired_claim_refs)
 THEN RAISE EXCEPTION 'p4_amendment_claim_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM trading_signals s WHERE
   (s.payload->>'signal_id',s.payload->>'case_id',s.payload->>'decision_id',s.payload->>'account_slot',
    s.payload->>'native_symbol',s.payload->>'decided_at_ns',s.payload->>'expires_at_ns') IS DISTINCT FROM
   (s.signal_id,s.case_id,s.decision_id,s.account_slot,s.native_symbol,s.decided_at_ns::text,s.expires_at_ns::text))
 OR EXISTS (SELECT 1 FROM trading_policy_actions a JOIN trading_signals s USING(signal_id)
   WHERE a.case_id<>s.case_id OR a.action IS DISTINCT FROM s.payload->>'side')
 THEN RAISE EXCEPTION 'p4_signal_case_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM trading_dispositions d LEFT JOIN trading_signals s
   ON d.input_kind='signal' AND s.signal_id=d.input_id LEFT JOIN trading_operator_intents i
   ON d.input_kind='intent' AND i.command_id=d.input_id
   WHERE COALESCE(s.account_slot,i.account_slot) IS DISTINCT FROM d.account_slot)
 THEN RAISE EXCEPTION 'p4_disposition_input_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM trading_plans p LEFT JOIN trading_signals s ON s.signal_id=p.signal_id
   LEFT JOIN trading_operator_intents i ON i.command_id=p.command_id
   LEFT JOIN trading_dispositions d ON d.input_id=COALESCE(p.signal_id,p.command_id)
    AND d.input_kind=CASE WHEN p.signal_id IS NULL THEN 'intent' ELSE 'signal' END
   WHERE p.plan_id IS DISTINCT FROM COALESCE(p.signal_id,p.command_id)
    OR COALESCE(s.account_slot,i.account_slot) IS DISTINCT FROM p.account_slot
    OR (p.signal_id IS NOT NULL AND (s.native_symbol,s.payload->>'side')
        IS DISTINCT FROM (p.native_symbol,p.side))
    OR (p.command_id IS NOT NULL AND (i.action<>'manual_entry'
        OR (i.payload->>'market_key',i.payload->>'direction') IS DISTINCT FROM
           ('crypto'||chr(58)||'perp'||chr(58)||regexp_replace(p.native_symbol,'USDT$','')||chr(58)||'USDT',p.side)))
    OR d.disposition IS DISTINCT FROM 'accepted' OR d.plan_id IS DISTINCT FROM p.plan_id
    OR p.status NOT IN ('accepted','open','closing','terminal'))
 OR EXISTS (SELECT 1 FROM trading_dispositions d LEFT JOIN trading_plans p ON p.plan_id=d.plan_id
   LEFT JOIN trading_operator_intents i ON i.command_id=d.input_id AND d.input_kind='intent'
   WHERE (d.plan_id IS NOT NULL AND p.plan_id IS NULL)
    OR (d.disposition='accepted' AND (d.input_kind='signal' OR i.action='manual_entry') AND d.plan_id IS NULL)
    OR (d.disposition<>'accepted' AND d.plan_id IS NOT NULL))
 THEN RAISE EXCEPTION 'p4_plan_disposition_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM trading_fill_attributions a JOIN trading_fills f
   USING(environment,native_symbol,trade_id) JOIN trading_orders o USING(client_order_id)
   WHERE (a.plan_id,a.command_id,a.environment,a.native_symbol,f.venue_order_id)
     IS DISTINCT FROM (o.plan_id,o.command_id,o.environment,o.native_symbol,o.venue_order_id))
 THEN RAISE EXCEPTION 'p4_fill_attribution_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM trading_cases WHERE
   (state='running') IS DISTINCT FROM (claim_token IS NOT NULL AND lease_until_ms IS NOT NULL)
   OR (claim_token IS NULL) IS DISTINCT FROM (lease_until_ms IS NULL))
 THEN RAISE EXCEPTION 'p4_case_claim_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM trading_control_state c LEFT JOIN trading_operator_intents i
   ON i.command_id=c.flatten_command_id WHERE c.flatten_command_id IS NOT NULL
   AND (i.action IS DISTINCT FROM 'flatten' OR i.account_slot IS DISTINCT FROM c.account_slot))
 THEN RAISE EXCEPTION 'p4_flatten_command_mismatch'; END IF;
END $$;
CREATE TEMP TABLE p4_intents ON COMMIT DROP AS SELECT to_jsonb(i) AS row FROM trading_operator_intents i;
CREATE FUNCTION trading_reject_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE name text; before jsonb; after jsonb;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='trading_fact_immutable'; END IF;
 before=to_jsonb(OLD); after=to_jsonb(NEW);
 FOREACH name IN ARRAY TG_ARGV LOOP
  IF before->name IS DISTINCT FROM 'null'::jsonb AND after->name IS DISTINCT FROM before->name
  THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='trading_fact_immutable: '||name; END IF;
 END LOOP;
 RETURN NEW;
END $$;
CREATE TABLE trading_accounts (
 account_slot text PRIMARY KEY CHECK ((account_slot ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$') IS TRUE),
 environment text NOT NULL CHECK (environment='DEMO'), entries_paused boolean NOT NULL DEFAULT true,
 emergency_halted boolean NOT NULL DEFAULT false, flatten_command_id text REFERENCES trading_operator_intents,
 control_updated_at_ns bigint, unexpected_exposure boolean NOT NULL DEFAULT false,
 last_full_reconcile_at_ns bigint, account_snapshot jsonb, trade_cursors jsonb NOT NULL DEFAULT '{}',
 CONSTRAINT trading_accounts_halt_implies_pause CHECK ((NOT emergency_halted OR entries_paused) IS TRUE),
 CHECK (account_snapshot IS NULL OR jsonb_typeof(account_snapshot)='object'),
 CHECK ((jsonb_typeof(trade_cursors)='object') IS TRUE));
CREATE TABLE trading_inputs (
 input_id text PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('oi','catalyst','source_update')),
 source_fact_key text NOT NULL CHECK (source_fact_key<>''), source_revision text NOT NULL CHECK (source_revision<>''),
 payload_sha256 text NOT NULL CHECK ((payload_sha256 ~ '^[0-9a-f]{64}$') IS TRUE), payload jsonb NOT NULL,
 received_at_ms bigint NOT NULL CHECK (received_at_ms>=0), first_visible_at_ms bigint, source_observed_at_ms bigint,
 target_selection jsonb, selected_asset_id text, exclusion_reason text,
 CONSTRAINT trading_inputs_natural_key UNIQUE (source_fact_key,kind,source_revision),
 CHECK ((jsonb_typeof(payload)='object') IS TRUE),
 CHECK (target_selection IS NULL OR jsonb_typeof(target_selection)='object'),
 CONSTRAINT trading_inputs_kind_shape CHECK ((CASE WHEN kind='source_update' THEN
  num_nonnulls(first_visible_at_ms,source_observed_at_ms,target_selection,selected_asset_id,exclusion_reason)=0
  AND input_id LIKE 'public:%' ELSE num_nonnulls(first_visible_at_ms,source_observed_at_ms,target_selection)=3
  AND (selected_asset_id IS NULL)=(exclusion_reason IS NOT NULL) AND input_id ~ '^[0-9a-f]{64}$' END) IS TRUE));
ALTER TABLE trading_cases ADD program_sha text CHECK (program_sha IS NULL OR program_sha ~ '^[0-9a-f]{64}$'),
 ADD assessment jsonb CHECK (assessment IS NULL OR jsonb_typeof(assessment)='object'),
 ADD policy_decisions jsonb CHECK (policy_decisions IS NULL OR jsonb_typeof(policy_decisions)='array'),
 ADD paper_legs jsonb CHECK (paper_legs IS NULL OR jsonb_typeof(paper_legs)='object'), ADD paper_labeled_at_ms bigint,
 ADD CONSTRAINT trading_cases_assessment_pair CHECK ((num_nonnulls(program_sha,assessment,policy_decisions)
  IN (0,3)) IS TRUE),
 ADD CONSTRAINT trading_cases_paper_pair CHECK (((paper_legs IS NULL)=(paper_labeled_at_ms IS NULL)) IS TRUE),
 ADD CONSTRAINT trading_cases_claim_pair CHECK (
  ((state='running')=(claim_token IS NOT NULL AND lease_until_ms IS NOT NULL)
   AND (claim_token IS NULL)=(lease_until_ms IS NULL)) IS TRUE);
ALTER TABLE trading_operator_intents ADD disposition text CHECK (disposition IN ('accepted','refused','expired')),
 ADD disposition_reason text, ADD decided_at_ns bigint,
 ADD CONSTRAINT trading_operator_intents_disposition_triple
 CHECK ((num_nonnulls(disposition,disposition_reason,decided_at_ns) IN (0,3)) IS TRUE);
CREATE TABLE trading_entries (
 entry_id text PRIMARY KEY CHECK ((entry_id ~ '^[0-9a-f]{64}$') IS TRUE),
 source text NOT NULL CHECK (source IN ('signal','manual')), case_id text REFERENCES trading_cases,
 command_id text UNIQUE REFERENCES trading_operator_intents, account_slot text NOT NULL REFERENCES trading_accounts,
 native_symbol text NOT NULL, side text NOT NULL CHECK (side IN ('long','short')), request jsonb,
 requested_at_ns bigint NOT NULL, expires_at_ns bigint NOT NULL, created_at_ns bigint NOT NULL,
 state text NOT NULL CHECK (state IN ('pending','refused','expired','accepted','open','closing','terminal')),
 reason text, disposed_at_ns bigint, quantity numeric CHECK (quantity>0),
 reference_price numeric CHECK (reference_price>0),
 reserved_notional numeric CHECK (reserved_notional>0), stop_bps int CHECK (stop_bps>0), tp_bps int CHECK (tp_bps>0),
 max_hold_s int CHECK (max_hold_s>0),
 pnl_status text CHECK (pnl_status IN ('pending','complete','evidence_incomplete')),
 opened_at_ns bigint, terminal_at_ns bigint, terminal_reason text, realized_pnl numeric, fees numeric, net_pnl numeric,
 pnl_deadline_ns bigint, updated_at_ns bigint NOT NULL,
 CHECK (request IS NULL OR jsonb_typeof(request)='object'),
 CONSTRAINT trading_entries_expiry CHECK (expires_at_ns>requested_at_ns),
 CONSTRAINT trading_entries_source_shape CHECK ((CASE source WHEN 'signal' THEN
  case_id IS NOT NULL AND command_id IS NULL AND request IS NOT NULL ELSE
  case_id IS NULL AND command_id=entry_id AND request IS NULL AND state<>'pending' END) IS TRUE),
 CONSTRAINT trading_entries_disposition_pair
 CHECK (num_nonnulls(reason,disposed_at_ns)=CASE WHEN state='pending' THEN 0 ELSE 2 END),
 CONSTRAINT trading_entries_plan_columns
 CHECK (num_nonnulls(quantity,reference_price,reserved_notional,stop_bps,tp_bps,max_hold_s,pnl_status)
  =CASE WHEN state IN ('accepted','open','closing','terminal') THEN 7 ELSE 0 END),
 CONSTRAINT trading_entries_terminal_pair CHECK ((state='terminal')=(terminal_at_ns IS NOT NULL)));
CREATE TABLE runtime_processes (
 process_kind text NOT NULL CHECK (process_kind IN ('workers','analysis','executor')),
 process_key text NOT NULL CHECK (process_key<>''), instance_id uuid NOT NULL,
 lifecycle_state text NOT NULL CHECK (lifecycle_state IN ('starting','running','stopping','stopped','failed')),
 started_at_ms bigint NOT NULL CHECK (started_at_ms>=0), heartbeat_at_ms bigint NOT NULL,
 fatal_code text, fault_code text,
 runtime_version text, runtime_revision text, image_digest text, detail jsonb NOT NULL DEFAULT '{}',
 PRIMARY KEY (process_kind,process_key), CHECK ((jsonb_typeof(detail)='object') IS TRUE),
 CONSTRAINT runtime_processes_heartbeat_order CHECK (heartbeat_at_ms>=started_at_ms),
 CONSTRAINT runtime_processes_fatal_code CHECK (fatal_code IS NULL OR fatal_code IN
  ('startup_failed','child_failed','control_failed','singleton_lost','resource_operation_overrun',
   'graceful_deadline_exceeded','cleanup_failed','runtime_invariant_failed')),
 CONSTRAINT runtime_processes_fatal_pair CHECK ((lifecycle_state='failed')=(fatal_code IS NOT NULL)),
 CONSTRAINT runtime_processes_workers_identity CHECK ((process_kind<>'workers'
  OR (btrim(runtime_version)<>'' AND runtime_revision<>'' AND image_digest<>'')) IS TRUE));

INSERT INTO trading_accounts(account_slot,environment,entries_paused,emergency_halted,flatten_command_id,
 control_updated_at_ns,unexpected_exposure,last_full_reconcile_at_ns,account_snapshot)
SELECT keys.account_slot,'DEMO',COALESCE(c.entries_paused,true),COALESCE(c.emergency_halted,false),c.flatten_command_id,
 c.updated_at_ns,COALESCE(e.unexpected_exposure,false),e.last_full_reconcile_at_ns,e.account_snapshot
FROM (SELECT account_slot FROM trading_executor_state UNION SELECT account_slot FROM trading_control_state
 UNION SELECT account_slot FROM trading_operator_intents UNION SELECT account_slot FROM trading_signals
 UNION SELECT account_slot FROM trading_plans) keys LEFT JOIN trading_executor_state e USING(account_slot)
 LEFT JOIN trading_control_state c USING(account_slot);
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM trading_trade_cursors) AND (SELECT count(*) FROM trading_accounts)<>1
 THEN RAISE EXCEPTION 'p4_trade_cursor_account_ambiguous'; END IF;
END $$;
UPDATE trading_accounts SET trade_cursors=COALESCE((SELECT jsonb_object_agg(native_symbol,
 jsonb_build_object('next_trade_id',next_trade_id,'checked_at_ns',checked_at_ns)) FROM trading_trade_cursors),'{}');
INSERT INTO trading_inputs(input_id,kind,source_fact_key,source_revision,payload_sha256,payload,received_at_ms,
 first_visible_at_ms,source_observed_at_ms,target_selection,selected_asset_id,exclusion_reason)
SELECT trigger_id,kind,source_fact_key,source_revision,payload_sha256,payload,created_at_ms,
 first_visible_at_ms,source_observed_at_ms,target_selection,selected_asset_id,exclusion_reason FROM trading_triggers;
INSERT INTO trading_inputs(input_id,kind,source_fact_key,source_revision,payload_sha256,payload,received_at_ms)
SELECT update_id,'source_update',source_fact_key,content_revision,payload_sha256,payload,received_at_ms
 FROM trading_source_amendments;
UPDATE trading_cases c SET program_sha=f.program_sha,assessment=to_jsonb(f)-ARRAY['case_id','program_sha'],
 policy_decisions=COALESCE((SELECT jsonb_agg((to_jsonb(a)-ARRAY['case_id','program_sha','expected_r'])
  ||jsonb_build_object('expected_r',a.expected_r::text) ORDER BY a.policy_id,a.policy_version)
  FROM trading_policy_actions a WHERE a.case_id=c.case_id),'[]')
 FROM trading_assessments f WHERE f.case_id=c.case_id;
UPDATE trading_cases c SET paper_legs=(SELECT jsonb_object_agg(l.side,
 (to_jsonb(l)-ARRAY['case_id','anchor_price','exit_price','gross_bps','cost_bps','net_r'])
 ||jsonb_build_object('anchor_price',l.anchor_price::text,'exit_price',l.exit_price::text,
 'gross_bps',l.gross_bps::text,'cost_bps',l.cost_bps::text,'net_r',l.net_r::text))
 FROM trading_paper_legs l WHERE l.case_id=c.case_id),
 paper_labeled_at_ms=(SELECT max(l.labeled_at_ms) FROM trading_paper_legs l WHERE l.case_id=c.case_id)
 WHERE EXISTS (SELECT 1 FROM trading_paper_legs l WHERE l.case_id=c.case_id);
DROP TRIGGER trg_trading_operator_intents_append_only ON trading_operator_intents;
UPDATE trading_operator_intents i SET disposition=d.disposition,disposition_reason=d.reason,
 decided_at_ns=d.decided_at_ns
 FROM trading_dispositions d WHERE d.input_kind='intent' AND d.input_id=i.command_id;
INSERT INTO trading_entries(entry_id,source,case_id,account_slot,native_symbol,side,request,
 requested_at_ns,expires_at_ns,
 created_at_ns,state,reason,disposed_at_ns,quantity,reference_price,reserved_notional,stop_bps,tp_bps,max_hold_s,pnl_status,
 opened_at_ns,terminal_at_ns,terminal_reason,realized_pnl,fees,net_pnl,pnl_deadline_ns,updated_at_ns)
SELECT s.signal_id,'signal',s.case_id,s.account_slot,s.native_symbol,s.payload->>'side',s.payload-'seq',
 s.decided_at_ns,
 s.expires_at_ns,s.created_at_ns,COALESCE(p.status,d.disposition,'pending'),d.reason,d.decided_at_ns,
 p.quantity,p.reference_price,p.reserved_notional,p.stop_bps,p.tp_bps,p.max_hold_s,p.pnl_status,p.opened_at_ns,
 p.terminal_at_ns,p.terminal_reason,p.realized_pnl,p.fees,p.net_pnl,p.pnl_deadline_ns,
 COALESCE(p.updated_at_ns,d.decided_at_ns,s.created_at_ns)
FROM trading_signals s LEFT JOIN trading_dispositions d ON d.input_kind='signal' AND d.input_id=s.signal_id
 LEFT JOIN trading_plans p ON p.signal_id=s.signal_id;
INSERT INTO trading_entries(entry_id,source,command_id,account_slot,native_symbol,side,requested_at_ns,expires_at_ns,
 created_at_ns,state,reason,disposed_at_ns,quantity,reference_price,reserved_notional,stop_bps,tp_bps,max_hold_s,pnl_status,
 opened_at_ns,terminal_at_ns,terminal_reason,realized_pnl,fees,net_pnl,pnl_deadline_ns,updated_at_ns)
SELECT p.plan_id,'manual',p.command_id,p.account_slot,p.native_symbol,p.side,i.requested_at_ns,i.expires_at_ns,
 d.decided_at_ns,p.status,d.reason,d.decided_at_ns,p.quantity,p.reference_price,p.reserved_notional,p.stop_bps,p.tp_bps,
 p.max_hold_s,p.pnl_status,p.opened_at_ns,p.terminal_at_ns,p.terminal_reason,p.realized_pnl,p.fees,p.net_pnl,
 p.pnl_deadline_ns,p.updated_at_ns FROM trading_plans p JOIN trading_operator_intents i USING(command_id)
 JOIN trading_dispositions d ON d.input_kind='intent' AND d.input_id=i.command_id WHERE p.signal_id IS NULL;
ALTER TABLE trading_fills ADD account_slot text REFERENCES trading_accounts,
 ADD client_order_id text REFERENCES trading_orders, ADD attributed_at_ns bigint,
 ADD CONSTRAINT trading_fills_attribution_pair CHECK (((client_order_id IS NULL)=(attributed_at_ns IS NULL)) IS TRUE);
DROP TRIGGER trg_trading_fills_append_only ON trading_fills;
UPDATE trading_fills f SET client_order_id=a.client_order_id,attributed_at_ns=a.attributed_at_ns,
 account_slot=COALESCE(p.account_slot,i.account_slot) FROM trading_fill_attributions a JOIN trading_orders o
 USING(client_order_id) LEFT JOIN trading_plans p ON p.plan_id=o.plan_id
 LEFT JOIN trading_operator_intents i ON i.command_id=o.command_id
 WHERE (f.environment,f.native_symbol,f.trade_id)=(a.environment,a.native_symbol,a.trade_id);
INSERT INTO runtime_processes(process_kind,process_key,instance_id,lifecycle_state,started_at_ms,heartbeat_at_ms,
 fatal_code,runtime_version,runtime_revision,image_digest,detail)
SELECT 'workers','singleton',runtime_id,lifecycle_state,started_at_ms,heartbeat_at_ms,fatal_code,runtime_version,
 runtime_revision,image_digest,jsonb_build_object('capabilities',capabilities) FROM workers_runtime;

CREATE FUNCTION pg_temp.p4_verify(label text, source text, target text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE mismatch boolean; src_n bigint; dst_n bigint; src_md5 text; dst_md5 text;
BEGIN
 EXECUTE
 format('SELECT count(*),md5(COALESCE(string_agg(row::text,E''\n'' ORDER BY row::text),'''')) FROM (%s) q',source)
  INTO src_n,src_md5;
 EXECUTE
 format('SELECT count(*),md5(COALESCE(string_agg(row::text,E''\n'' ORDER BY row::text),'''')) FROM (%s) q',target)
  INTO dst_n,dst_md5;
 EXECUTE format('SELECT EXISTS ((SELECT * FROM (%s) s EXCEPT ALL SELECT * FROM (%s) t) UNION ALL '
                '(SELECT * FROM (%s) t EXCEPT ALL SELECT * FROM (%s) s))',source,target,target,source)
  INTO mismatch;
 IF mismatch OR (src_n,src_md5) IS DISTINCT FROM (dst_n,dst_md5)
 THEN RAISE EXCEPTION 'p4_projection_mismatch: %',label; END IF;
 RAISE NOTICE 'p4_verify % rows=% md5=%',label,src_n,src_md5;
END $$;
SELECT pg_temp.p4_verify('triggers',$s$SELECT to_jsonb(t) AS row FROM trading_triggers t$s$,
 $t$SELECT jsonb_build_object('trigger_id',input_id,'kind',kind,'source_fact_key',source_fact_key,
 'source_revision',source_revision,'payload_sha256',payload_sha256,'payload',payload,'created_at_ms',received_at_ms,
 'first_visible_at_ms',first_visible_at_ms,'source_observed_at_ms',source_observed_at_ms,
 'selected_asset_id',selected_asset_id,'target_selection',target_selection,'exclusion_reason',exclusion_reason) AS row
 FROM trading_inputs WHERE kind IN ('oi','catalyst')$t$);
SELECT pg_temp.p4_verify('amendments',$s$SELECT to_jsonb(a) AS row FROM trading_source_amendments a$s$,
 $t$SELECT jsonb_build_object('update_id',input_id,'source_fact_key',source_fact_key,'content_revision',source_revision,
 'payload_sha256',payload_sha256,'payload',payload,'received_at_ms',received_at_ms,
 'affected_claim_refs',payload->'affected_claim_refs','retired_claim_refs',payload->'retired_claim_refs') AS row
 FROM trading_inputs WHERE kind='source_update'$t$);
SELECT pg_temp.p4_verify('assessments',$s$SELECT to_jsonb(a) AS row FROM trading_assessments a$s$,
 $t$SELECT assessment||jsonb_build_object('case_id',case_id,'program_sha',program_sha) AS row FROM trading_cases
 WHERE assessment IS NOT NULL$t$);
SELECT pg_temp.p4_verify('actions',$s$SELECT to_jsonb(a) AS row FROM trading_policy_actions a$s$,
 $t$SELECT (value-'expected_r')||jsonb_build_object('expected_r',(value->>'expected_r')::numeric,
 'case_id',case_id,'program_sha',program_sha) AS row FROM trading_cases
 CROSS JOIN LATERAL jsonb_array_elements(policy_decisions)$t$);
SELECT pg_temp.p4_verify('paper',$s$SELECT to_jsonb(l) AS row FROM trading_paper_legs l$s$,
 $t$SELECT (value-ARRAY['anchor_price','exit_price','gross_bps','cost_bps','net_r'])||
 jsonb_build_object('case_id',case_id,'anchor_price',(value->>'anchor_price')::numeric,
 'exit_price',(value->>'exit_price')::numeric,'gross_bps',(value->>'gross_bps')::numeric,
 'cost_bps',(value->>'cost_bps')::numeric,'net_r',(value->>'net_r')::numeric) AS row
 FROM trading_cases CROSS JOIN LATERAL jsonb_each(paper_legs)$t$);
SELECT pg_temp.p4_verify('signals',$s$SELECT to_jsonb(s)-'seq' AS row FROM trading_signals s$s$,
 $t$SELECT jsonb_build_object('signal_id',entry_id,'case_id',case_id,'decision_id',request->>'decision_id',
 'account_slot',account_slot,'native_symbol',native_symbol,'decided_at_ns',requested_at_ns,'expires_at_ns',expires_at_ns,
 'payload',request,'created_at_ns',created_at_ns) AS row FROM trading_entries WHERE source='signal'$t$);
SELECT pg_temp.p4_verify('dispositions',$s$SELECT to_jsonb(d) AS row FROM trading_dispositions d
 UNION ALL SELECT row FROM p4_intents$s$,
 $t$SELECT jsonb_build_object('input_kind','signal','input_id',entry_id,'account_slot',account_slot,
 'disposition',CASE WHEN quantity IS NOT NULL THEN 'accepted' ELSE state END,'reason',reason,
 'plan_id',CASE WHEN quantity IS NOT NULL THEN entry_id END,'decided_at_ns',disposed_at_ns) AS row
 FROM trading_entries WHERE source='signal' AND disposed_at_ns IS NOT NULL UNION ALL
 SELECT jsonb_build_object('input_kind','intent','input_id',command_id,'account_slot',account_slot,
 'disposition',disposition,'reason',disposition_reason,'decided_at_ns',decided_at_ns,
 'plan_id',CASE WHEN action='manual_entry' AND disposition='accepted' THEN command_id END) AS row
 FROM trading_operator_intents WHERE disposition IS NOT NULL UNION ALL
 SELECT to_jsonb(i)-ARRAY['disposition','disposition_reason','decided_at_ns'] AS row
 FROM trading_operator_intents i$t$);
SELECT pg_temp.p4_verify('plans',$s$SELECT to_jsonb(p) AS row FROM trading_plans p$s$,
 $t$SELECT jsonb_build_object('plan_id',entry_id,'signal_id',CASE WHEN source='signal' THEN entry_id END,
 'command_id',command_id,'account_slot',account_slot,'environment','DEMO','native_symbol',native_symbol,'side',side,
 'quantity',quantity,'reference_price',reference_price,'reserved_notional',reserved_notional,'stop_bps',stop_bps,
 'tp_bps',tp_bps,'max_hold_s',max_hold_s,'status',state,'opened_at_ns',opened_at_ns,'terminal_at_ns',terminal_at_ns,
 'terminal_reason',terminal_reason,'pnl_status',pnl_status,'realized_pnl',realized_pnl,'fees',fees,'net_pnl',net_pnl,
 'pnl_deadline_ns',pnl_deadline_ns,'updated_at_ns',updated_at_ns) AS row
 FROM trading_entries WHERE quantity IS NOT NULL$t$);
SELECT pg_temp.p4_verify('attributions',$s$SELECT to_jsonb(a) AS row FROM trading_fill_attributions a$s$,
 $t$SELECT jsonb_build_object('environment',f.environment,'native_symbol',f.native_symbol,'trade_id',f.trade_id,
 'plan_id',o.plan_id,'command_id',o.command_id,'client_order_id',f.client_order_id,
 'attributed_at_ns',f.attributed_at_ns) AS row FROM trading_fills f JOIN trading_orders o USING(client_order_id)$t$);
SELECT pg_temp.p4_verify('controls',$s$SELECT to_jsonb(c) AS row FROM trading_control_state c$s$,
 $t$SELECT jsonb_build_object('account_slot',a.account_slot,'entries_paused',a.entries_paused,
 'emergency_halted',a.emergency_halted,'flatten_command_id',a.flatten_command_id,
 'updated_at_ns',a.control_updated_at_ns) AS row FROM trading_accounts a
 JOIN trading_control_state c USING(account_slot)$t$);
SELECT pg_temp.p4_verify('accounts',$s$SELECT to_jsonb(e)-ARRAY['heartbeat_at_ns','last_error'] AS row
 FROM trading_executor_state e$s$,
 $t$SELECT jsonb_build_object('account_slot',a.account_slot,'environment',a.environment,
 'last_full_reconcile_at_ns',a.last_full_reconcile_at_ns,'account_snapshot',a.account_snapshot,
 'unexpected_exposure',a.unexpected_exposure) AS row FROM trading_accounts a
 JOIN trading_executor_state e USING(account_slot)$t$);
SELECT pg_temp.p4_verify('cursors',$s$SELECT to_jsonb(c) AS row FROM trading_trade_cursors c$s$,
 $t$SELECT value||jsonb_build_object('environment',environment,'native_symbol',key) AS row FROM trading_accounts
 CROSS JOIN LATERAL jsonb_each(trade_cursors)$t$);
SELECT pg_temp.p4_verify('workers',$s$SELECT to_jsonb(w)-'singleton_key' AS row FROM workers_runtime w$s$,
 $t$SELECT jsonb_build_object('runtime_id',instance_id,'lifecycle_state',lifecycle_state,
 'started_at_ms',started_at_ms,'heartbeat_at_ms',heartbeat_at_ms,'fatal_code',fatal_code,
 'runtime_version',runtime_version,'runtime_revision',runtime_revision,'image_digest',image_digest,
 'capabilities',detail->'capabilities') AS row FROM runtime_processes WHERE process_kind='workers'$t$);
ALTER TABLE trading_cases DROP CONSTRAINT trading_cases_trigger_id_fkey,
 ADD CONSTRAINT trading_cases_trigger_id_fkey FOREIGN KEY(trigger_id) REFERENCES trading_inputs(input_id);
ALTER TABLE trading_orders DROP CONSTRAINT trading_orders_plan_id_fkey;
ALTER TABLE trading_orders RENAME COLUMN plan_id TO entry_id;
ALTER TABLE trading_orders RENAME CONSTRAINT trading_orders_plan_id_leg_attempt_key
 TO trading_orders_entry_id_leg_attempt_key;
ALTER TABLE trading_orders ADD CONSTRAINT trading_orders_entry_id_fkey
 FOREIGN KEY(entry_id) REFERENCES trading_entries(entry_id);
DROP INDEX ix_trading_orders_plan; DROP INDEX ix_trading_operator_intents_cursor;
DROP TABLE trading_fill_attributions,trading_policy_actions,trading_paper_legs,trading_assessments,
 trading_dispositions,trading_plans,trading_signals,trading_source_amendments,
 trading_triggers,trading_trade_cursors,trading_executor_state,trading_control_state,trading_analysis_runtime,
 workers_runtime;
DROP FUNCTION IF EXISTS reject_trading_executor_append_mutation();
DROP FUNCTION IF EXISTS reject_trading_analysis_stream_mutation();
CREATE INDEX trading_inputs_asset_recent ON trading_inputs(selected_asset_id,first_visible_at_ms DESC)
 WHERE selected_asset_id IS NOT NULL;
CREATE INDEX trading_inputs_received ON trading_inputs(received_at_ms) WHERE kind IN ('oi','catalyst');
CREATE INDEX trading_inputs_oi_evidence_ref ON trading_inputs((payload->>'evidence_ref')) WHERE kind='oi';
CREATE INDEX trading_inputs_superseded_claims ON trading_inputs USING gin((payload->'superseded_claim_refs'))
 WHERE kind='catalyst';
CREATE INDEX trading_inputs_source_recent ON trading_inputs(source_fact_key,received_at_ms DESC);
CREATE INDEX trading_inputs_retired_claims ON trading_inputs USING gin((payload->'retired_claim_refs'))
 WHERE kind='source_update';
CREATE INDEX trading_cases_label_due ON trading_cases(decided_at_ms)
 WHERE paper_labeled_at_ms IS NULL AND geometry_version IS NOT NULL AND decided_at_ms IS NOT NULL;
CREATE INDEX trading_operator_intents_pending ON trading_operator_intents(account_slot,seq) WHERE disposition IS NULL;
CREATE INDEX trading_entries_pending ON trading_entries(account_slot,created_at_ns,entry_id) WHERE state='pending';
CREATE UNIQUE INDEX trading_entries_one_active_symbol ON trading_entries(account_slot,native_symbol)
 WHERE state IN ('accepted','open','closing');
CREATE INDEX trading_entries_pnl_due ON trading_entries(account_slot,terminal_at_ns)
 WHERE state='terminal' AND pnl_status='pending';
CREATE INDEX trading_entries_case ON trading_entries(case_id) WHERE case_id IS NOT NULL;
CREATE INDEX trading_entries_updated ON trading_entries(updated_at_ns);
CREATE INDEX trading_fills_unattributed ON trading_fills(native_symbol) WHERE client_order_id IS NULL;
CREATE INDEX trading_fills_order ON trading_fills(client_order_id) WHERE client_order_id IS NOT NULL;
CREATE TRIGGER trading_entries_write_once BEFORE UPDATE OR DELETE ON trading_entries FOR EACH ROW
 EXECUTE FUNCTION trading_reject_rewrite('entry_id','source','case_id','command_id','account_slot','native_symbol',
 'side','request','requested_at_ns','expires_at_ns','created_at_ns','reason','disposed_at_ns','quantity',
 'reference_price','reserved_notional','stop_bps','tp_bps','max_hold_s','opened_at_ns','terminal_at_ns',
 'terminal_reason','realized_pnl','fees','net_pnl','pnl_deadline_ns');
CREATE TRIGGER trading_operator_intents_write_once BEFORE UPDATE OR DELETE ON trading_operator_intents FOR EACH ROW
 EXECUTE FUNCTION trading_reject_rewrite('command_id','seq','account_slot','action','scope','reason',
 'operator_identity',
 'authentication_identity','requested_at_ns','expires_at_ns','payload','disposition','disposition_reason','decided_at_ns');
CREATE TRIGGER trading_fills_write_once BEFORE UPDATE OR DELETE ON trading_fills FOR EACH ROW
 EXECUTE FUNCTION trading_reject_rewrite('environment','native_symbol','trade_id','venue_order_id','quantity','price',
 'realized_pnl','fee','fee_asset','traded_at_ns','evidence','account_slot','client_order_id','attributed_at_ns');
DO $$ BEGIN RAISE NOTICE 'p4_verify ok'; END $$;
""")


def downgrade() -> None:
    raise RuntimeError("P4 is forward-only: restore a verified backup and matching image; venue orders persist")
