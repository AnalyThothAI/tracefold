"""#764 P0: hot paths, durable pending inputs and retired structures.

Migration evidence:

- category: forward-only data compaction, constraints and structure retirement.
- why_database_must_change: remove unsafe input cursors, compact identical roster versions,
  freeze receipt Claims and retire four tables with no production readers.
- current_source_revision: 20261001_0419
- minimum_supported_source_revision: 20261001_0419
- lock_level_and_order: ACCESS EXCLUSIVE on altered/dropped tables; all writers stopped.
- statement_timeout: 600s
- lock_timeout: 5s
- estimated_rows: 63,502 roster rows and retained delivery receipts; cardinality is data-dependent.
- estimated_bytes: roster and receipt projections; sized from the verified backup before deployment.
- rewrite_or_index_build: roster compaction, sent_claims backfill and unmonitored partial index.
- preflight_and_maintenance_boundary: stop all processes, Analysis before Executor; verify backup,
  export wallet_archive, listing_events, roster, tape_state, wallet_fills, deliveries and
  notification_decisions with sha256. Reject live Trading writers and invalid stored contracts.
- archive_current_compatibility: stable external IDs; roster fills/cursor remapped to identical membership.
- role_and_grant_impact: none; migration runs as the single tracefold owner.
- failure_state: one transactional revision; any preflight or projection mismatch rolls back.
- roll_forward_or_verified_backup_restore: restore verified backup and matching old image.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20261001_0420
Revises: 20261001_0419
"""

from alembic import op

revision = "20261001_0420"
down_revision = "20261001_0419"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
-- #764 P0: one Alembic revision (down_revision = head at implementation time), one transaction.
-- Writers stopped (Serve, Workers, Analysis, Executor); verified backup; P0 exports done.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '600s';

-- ===== Trading + platform =====
-- #764 P0, Trading + platform part (items T1, T4; T2/T3 are code-only). FRAGMENT, not standalone:
-- runs inside the single P0 upgrade() transaction after its `SET LOCAL lock_timeout/statement_timeout`.
-- Each top-level statement = one op.execute(). Writers stopped (Analysis, then Executor).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_stat_activity
              WHERE pid <> pg_backend_pid()
                AND datname = current_database()
                AND application_name IN ('tracefold_executor', 'tracefold_analysis')) THEN
    RAISE EXCEPTION 'p0_trading_writers_connected: stop analysis and executor before upgrade';
  END IF;
  IF EXISTS (SELECT 1 FROM public.trading_trigger_conflicts) THEN
    RAISE EXCEPTION 'p0_trading_trigger_conflicts_not_empty: export the rows before upgrade';
  END IF;
END $$;
-- T1 live bug: the executor consumes inputs without a disposition; no seq high-water mark.
ALTER TABLE public.trading_executor_state
  DROP CONSTRAINT trading_executor_state_check,
  DROP COLUMN last_signal_seq,
  DROP COLUMN last_intent_seq;
-- T4 dead structure (0 rows, no reader).
DROP TABLE public.trading_trigger_conflicts;

-- ===== News notifications / market / wallet =====
-- #764 P0, News notify/market items N1-N6 (baseline main 2f5c6cd45, DB head 20261001_0419).
-- Alembic upgrade() body; one transaction (BEGIN/COMMIT = validation harness). Workers stopped before upgrade;
-- the operator exported (pg_dump -t) news_market_wallet_archive and news_market_instrument_listing_events first.

-- ===== N1: compact duplicate roster versions (keep the LAST id of each identical-membership run,
--             stamped with the run's FIRST known_at_ms), remap fills + tape state, index the hot UPDATE =====
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM news_market_wallet_roster GROUP BY roster_version HAVING count(DISTINCT known_at_ms) > 1)
  THEN
    RAISE EXCEPTION 'p0_roster_known_at_not_constant_per_version';
  END IF;
END $$;

CREATE TEMP TABLE p0_roster_versions ON COMMIT DROP AS
WITH v AS (
  SELECT roster_version, min(known_at_ms) AS known_at_ms,
         string_agg(wallet, ',' ORDER BY wallet) AS members
    FROM news_market_wallet_roster GROUP BY roster_version
), marked AS (
  SELECT v.*, (lag(members) OVER (ORDER BY roster_version) IS DISTINCT FROM members)::int AS starts FROM v
), runs AS (
  SELECT marked.*, sum(starts) OVER (ORDER BY roster_version) AS run_no FROM marked
)
SELECT roster_version, run_no, members,
       max(roster_version) OVER (PARTITION BY run_no) AS kept_version,
       min(known_at_ms)   OVER (PARTITION BY run_no) AS run_known_at_ms
  FROM runs;

CREATE TEMP TABLE p0_counts ON COMMIT DROP AS
SELECT (SELECT count(*) FROM news_market_wallet_fills) AS fills,
       (SELECT count(*) FROM news_market_wallet_fills f
         WHERE NOT EXISTS (SELECT 1 FROM p0_roster_versions v WHERE v.roster_version = f.roster_version)) AS
  orphan_fills,
       (SELECT count(*) FROM p0_roster_versions) AS versions_before,
       (SELECT count(DISTINCT run_no) FROM p0_roster_versions) AS runs;

UPDATE news_market_wallet_fills f SET roster_version = v.kept_version
  FROM p0_roster_versions v WHERE v.roster_version = f.roster_version AND v.kept_version <> v.roster_version;
UPDATE news_market_wallet_tape_state s SET roster_version = v.kept_version
  FROM p0_roster_versions v WHERE v.roster_version = s.roster_version AND v.kept_version <> v.roster_version;
DELETE FROM news_market_wallet_roster r USING p0_roster_versions v
 WHERE v.roster_version = r.roster_version AND v.kept_version <> v.roster_version;
UPDATE news_market_wallet_roster r SET known_at_ms = v.run_known_at_ms
  FROM p0_roster_versions v WHERE v.roster_version = r.roster_version AND r.known_at_ms <> v.run_known_at_ms;

DO $$
DECLARE c record; n bigint;
BEGIN
  SELECT * INTO c FROM p0_counts;
  -- every original version resolves to a kept version with exactly the same wallet set
  SELECT count(*) INTO n
    FROM p0_roster_versions v
   WHERE v.members IS DISTINCT FROM (SELECT string_agg(wallet, ',' ORDER BY wallet)
                                       FROM news_market_wallet_roster r WHERE r.roster_version = v.kept_version);
  IF n > 0 THEN RAISE EXCEPTION 'p0_roster_membership_changed: % versions', n; END IF;
  IF (SELECT count(DISTINCT roster_version) FROM news_market_wallet_roster) <> c.runs THEN
    RAISE EXCEPTION 'p0_roster_kept_versions_mismatch';
  END IF;
  IF (SELECT count(*) FROM news_market_wallet_fills) <> c.fills THEN RAISE EXCEPTION 'p0_fills_count_changed'; END IF;
  IF (SELECT count(*) FROM news_market_wallet_fills f WHERE NOT EXISTS (
        SELECT 1 FROM news_market_wallet_roster r WHERE r.roster_version = f.roster_version)) <> c.orphan_fills THEN
    RAISE EXCEPTION 'p0_fills_lost_their_version';
  END IF;
  IF EXISTS (SELECT 1 FROM news_market_wallet_tape_state s WHERE s.roster_version <> 0 AND NOT EXISTS (
        SELECT 1 FROM news_market_wallet_roster r WHERE r.roster_version = s.roster_version)) THEN
    RAISE EXCEPTION 'p0_tape_state_version_missing';
  END IF;
  RAISE NOTICE 'p0 roster: % versions -> % kept, % rows remain', c.versions_before, c.runs,
    (SELECT count(*) FROM news_market_wallet_roster);
END $$;

CREATE INDEX news_market_wallet_roster_unmonitored ON news_market_wallet_roster (wallet) WHERE monitoring_from_ms IS
  NULL;

-- ===== N2: delete_* columns on news_deliveries have no writer =====
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM news_deliveries WHERE
  num_nonnulls(delete_state,delete_evidence,delete_reason,delete_error_code,delete_attempted_at_ms,delete_settled_at_ms)
  > 0) THEN
    RAISE EXCEPTION 'p0_news_deliveries_delete_state_present';
  END IF;
END $$;
DROP INDEX ix_news_deliveries_deleting;
ALTER TABLE news_deliveries
  DROP CONSTRAINT news_deliveries_delete_shape_check,
  DROP CONSTRAINT news_deliveries_delete_state_check,
  DROP COLUMN delete_state, DROP COLUMN delete_evidence, DROP COLUMN delete_reason,
  DROP COLUMN delete_error_code, DROP COLUMN delete_attempted_at_ms, DROP COLUMN delete_settled_at_ms;

-- ===== N3: 0416 decision contract CHECK accepted empty plans (NULL predicate) =====
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(decision_ref, ',') INTO bad FROM news_notification_decisions
   WHERE NOT ((origin = 'legacy_work_plan' OR (
           input_digest IS NOT NULL AND plan->>'update_ref' = update_ref AND plan->>'channel' = channel
           AND plan->>(CASE origin WHEN 'reader_v2' THEN 'input_digest'
                         ELSE 'assessment_input_digest' END) = input_digest
           AND jsonb_typeof(plan->'claim_decisions') = 'array')) IS TRUE);
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'p0_decision_contract_violations: %', bad; END IF;
END $$;
ALTER TABLE news_notification_decisions DROP CONSTRAINT news_notification_decisions_contract_check;
ALTER TABLE news_notification_decisions ADD CONSTRAINT news_notification_decisions_contract_check CHECK ((
  origin = 'legacy_work_plan' OR (
    input_digest IS NOT NULL AND plan->>'update_ref' = update_ref AND plan->>'channel' = channel
    AND plan->>(CASE origin WHEN 'reader_v2' THEN 'input_digest' ELSE 'assessment_input_digest' END) = input_digest
    AND jsonb_typeof(plan->'claim_decisions') = 'array')) IS TRUE);

-- ===== N4: exported, reader-less history (operator ran pg_dump -t before upgrade) =====
DO $$ BEGIN
  RAISE NOTICE 'p0 dropping news_market_wallet_archive rows=% news_market_instrument_listing_events rows=%',
    (SELECT count(*) FROM news_market_wallet_archive), (SELECT count(*) FROM news_market_instrument_listing_events);
END $$;
DROP TABLE news_market_wallet_archive;            -- no CASCADE: fails if anything depends on it
DROP TABLE news_market_instrument_listing_events;

-- ===== N5: write-only columns =====
ALTER TABLE news_market_wallet_tape_state DROP COLUMN pre_0399_cursor;
ALTER TABLE news_market_wallet_roster DROP COLUMN archived_source_statistics;

-- ===== N6: freeze the sent claims on the receipt (recall no longer joins news_event_updates per receipt) =====
CREATE TEMP TABLE p0_receipt_projection ON COMMIT DROP AS
SELECT d.intent_id, md5(COALESCE((SELECT jsonb_agg(claim)
    FROM jsonb_array_elements(COALESCE(u.document->'claims','[]'::jsonb)) claim
    WHERE d.claim_refs ? (claim->>'ref')), '[]'::jsonb)::text) AS projection_md5
FROM news_deliveries d JOIN news_event_updates u
ON u.event_id=d.event_id AND u.content_revision=d.content_revision;
ALTER TABLE news_deliveries ADD COLUMN sent_claims jsonb
  CONSTRAINT news_deliveries_sent_claims_shape CHECK (sent_claims IS NULL OR jsonb_typeof(sent_claims) = 'array');
UPDATE news_deliveries d
   SET sent_claims = COALESCE((SELECT jsonb_agg(claim)
                                 FROM jsonb_array_elements(COALESCE(u.document -> 'claims', '[]'::jsonb)) claim
                                WHERE d.claim_refs ? (claim ->> 'ref')), '[]'::jsonb)
  FROM news_event_updates u
 WHERE u.event_id = d.event_id AND u.content_revision = d.content_revision;
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM news_deliveries d
   WHERE (d.sent_claims IS NULL) <> NOT EXISTS (
           SELECT 1 FROM news_event_updates u WHERE u.event_id = d.event_id AND u.content_revision =
  d.content_revision);
  IF n > 0 THEN RAISE EXCEPTION 'p0_sent_claims_backfill_mismatch: %', n; END IF;
END $$;

-- ===== News core =====
-- #764 P0 · News core part (spec A). Statements of the shared P0 upgrade(); down_revision = head at implementation
-- time (validated on 20261001_0419). Standalone it runs as one transaction; merged into the shared P0 revision the
-- BEGIN / SET LOCAL / COMMIT lines below are the shared revision's own.
-- 0419 already made related-Event recall index-driven (ix_news_event_members_fact_trgm), so P0 adds no index here.

-- Preconditions: nothing is dropped or tightened over a row that would be lost or rejected.
DO $$
DECLARE bad text;
BEGIN
  IF EXISTS (SELECT 1 FROM public.news_evidence_documents) THEN
    RAISE EXCEPTION 'p0_news_evidence_documents_not_empty: export and decide before dropping';
  END IF;
  SELECT string_agg(u.event_id || '/' || u.content_revision, ',') INTO bad
    FROM public.news_event_updates u
   WHERE NOT ((jsonb_typeof(u.document) = 'object'
               AND u.document->>'schema_version' = 'news_event_update_v2'
               AND u.document->>'event_id' = u.event_id
               AND u.document->>'content_revision' = u.content_revision
               AND u.document->'input_revision' = to_jsonb(u.input_revision)
               AND u.document->>'previous_content_revision' IS NOT DISTINCT FROM u.previous_content_revision) IS TRUE);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'p0_event_update_document_invalid: %', bad;
  END IF;
END $$;

-- 0411 EventUpdate document CHECK: a missing key made the predicate NULL, and CHECK accepts NULL.
ALTER TABLE public.news_event_updates DROP CONSTRAINT news_event_updates_document_check;
ALTER TABLE public.news_event_updates ADD CONSTRAINT news_event_updates_document_check CHECK ((
  jsonb_typeof(document) = 'object'
  AND document->>'schema_version' = 'news_event_update_v2'
  AND document->>'event_id' = event_id
  AND document->>'content_revision' = content_revision
  AND document->'input_revision' = to_jsonb(input_revision)
  AND document->>'previous_content_revision' IS NOT DISTINCT FROM previous_content_revision) IS TRUE);

-- Dead structure: 0 rows, no production reader or writer (only platform/postgres/audit.py:90 and tests).
DROP TABLE public.news_evidence_documents;            -- drops trg_news_document_append_only with it
DROP FUNCTION public.reject_news_document_mutation();
-- Orphan News functions: no constraint, trigger, view, function body or code references them.
DROP FUNCTION public.reject_news_canary_append_only_mutation();
DROP FUNCTION public.news_jsonb_bounded_text_list_valid(jsonb, integer);
DROP FUNCTION public.news_jsonb_ordered_string_set_valid(jsonb, text[], integer);
DROP FUNCTION public.news_strategy_provenance_valid(jsonb);
DROP FUNCTION public.news_jsonb_forbidden_keys_absent(jsonb, text[]);


DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM p0_receipt_projection p JOIN news_deliveries d USING(intent_id)
             WHERE p.projection_md5 IS DISTINCT FROM md5(d.sent_claims::text)) THEN
    RAISE EXCEPTION 'p0_sent_claims_projection_changed';
  END IF;
END $$;
    """)


def downgrade() -> None:
    raise RuntimeError("p0_forward_only: restore verified backup and matching image")
