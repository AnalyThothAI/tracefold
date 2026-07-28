--
-- PostgreSQL database dump
--


-- Dumped from database version 18.4 (Debian 18.4-1.pgdg12+1)
-- Dumped by pg_dump version 18.4 (Debian 18.4-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--



--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: enforce_macro_research_run_lifecycle(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_macro_research_run_lifecycle() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'pending'
               OR NEW.attempt_count <> 0
               OR NEW.leased_until_ms IS NOT NULL
               OR NEW.lease_owner IS NOT NULL THEN
              RAISE EXCEPTION 'macro_research_run_initial_state_invalid';
            END IF;
            RETURN NEW;
          END IF;
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'macro_research_run_delete_forbidden';
          END IF;
          IF NEW.session_date IS DISTINCT FROM OLD.session_date
             OR NEW.market_cutoff_ms IS DISTINCT FROM OLD.market_cutoff_ms
             OR NEW.evidence_pack_id IS DISTINCT FROM OLD.evidence_pack_id
             OR NEW.sealed_at_ms IS DISTINCT FROM OLD.sealed_at_ms
             OR NEW.created_at_ms IS DISTINCT FROM OLD.created_at_ms THEN
            RAISE EXCEPTION 'macro_research_run_frozen_fields_immutable';
          END IF;
          IF OLD.status = 'failed' AND NEW.status = 'retryable' THEN
            RETURN NEW;
          END IF;
          IF OLD.status IN ('failed', 'published') THEN
            RAISE EXCEPTION 'macro_research_run_terminal';
          END IF;
          IF NOT (
            (OLD.status = 'pending' AND NEW.status = 'running')
            OR (OLD.status = 'retryable' AND NEW.status = 'running')
            OR (OLD.status = 'running' AND NEW.status IN (
              'running', 'retryable', 'failed', 'published'
            ))
          ) THEN
            RAISE EXCEPTION 'macro_research_run_transition_invalid:%->%', OLD.status, NEW.status;
          END IF;
          IF NEW.attempt_count < OLD.attempt_count THEN
            RAISE EXCEPTION 'macro_research_run_attempt_count_decrease';
          END IF;
          RETURN NEW;
        END
        $$;


--
-- Name: forbid_market_fact_update(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.forbid_market_fact_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF TG_TABLE_NAME = 'enriched_events' THEN
    IF OLD.capture_method = 'unavailable'
       AND OLD.capture_reason = 'pending_backfill'
       AND OLD.tick_observed_at_ms IS NULL
       AND OLD.tick_id IS NULL
       AND OLD.tick_lag_ms IS NULL
       AND NEW.event_id = OLD.event_id
       AND NEW.intent_id = OLD.intent_id
       AND NEW.resolution_id = OLD.resolution_id
       AND NEW.target_type = OLD.target_type
       AND NEW.target_id = OLD.target_id
       AND NEW.t_event_ms = OLD.t_event_ms
       AND NEW.created_at_ms = OLD.created_at_ms
       AND (
         (
           NEW.capture_method IN ('tier1_ws', 'tier2_poll', 'tier3_inline')
           AND NEW.capture_reason = 'async_backfill'
           AND NEW.tick_observed_at_ms IS NOT NULL
           AND NEW.tick_id IS NOT NULL
           AND NEW.tick_lag_ms IS NOT NULL
           AND NEW.tick_lag_ms >= 0
         )
         OR
         (
           NEW.capture_method = 'unavailable'
           AND NEW.capture_reason IN (
             'backfill_expired',
             'invalid_resolution',
             'missing_market_key',
             'missing_provider',
             'no_market_data',
             'provider_error',
             'provider_no_quote',
             'provider_timeout',
             'rate_limited',
             'unknown'
           )
           AND NEW.tick_observed_at_ms IS NULL
           AND NEW.tick_id IS NULL
           AND NEW.tick_lag_ms IS NULL
         )
       )
    THEN
      RETURN NEW;
    END IF;
  END IF;
  RAISE EXCEPTION 'market facts are append-only';
END;
$$;


--
-- Name: reject_macro_fact_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_macro_fact_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          RAISE EXCEPTION '%_append_only', TG_TABLE_NAME;
        END
        $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: asset_identity_current; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_identity_current (
    asset_id text NOT NULL,
    canonical_symbol text,
    canonical_name text,
    decimals bigint,
    identity_confidence text NOT NULL,
    selected_evidence_id text,
    selection_reason_codes_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    conflict_count bigint DEFAULT 0 NOT NULL,
    verified_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL
);


--
-- Name: asset_identity_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_identity_evidence (
    evidence_id text NOT NULL,
    asset_id text NOT NULL,
    evidence_kind text NOT NULL,
    provider text NOT NULL,
    lookup_mode text NOT NULL,
    chain_id text NOT NULL,
    address text NOT NULL,
    symbol text,
    name text,
    decimals bigint,
    confidence text NOT NULL,
    source_event_id text,
    source_intent_id text,
    source_resolution_id text,
    raw_payload_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    observed_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL
);


--
-- Name: asset_profile_refresh_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_profile_refresh_targets (
    provider text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    chain_id text NOT NULL,
    address text NOT NULL,
    symbol text,
    dirty_reason text NOT NULL,
    payload_hash text NOT NULL,
    source_watermark_ms bigint DEFAULT 0 NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    due_at_ms bigint NOT NULL,
    leased_until_ms bigint,
    lease_owner text,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_error text,
    first_dirty_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT asset_profile_refresh_targets_attempt_count_check CHECK ((attempt_count >= 0))
)
WITH (fillfactor='70', autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


--
-- Name: asset_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_profiles (
    asset_id text NOT NULL,
    provider text NOT NULL,
    status text NOT NULL,
    symbol text,
    name text,
    logo_url text,
    banner_url text,
    website_url text,
    twitter_username text,
    twitter_url text,
    telegram_url text,
    gmgn_url text,
    geckoterminal_url text,
    description text,
    raw_payload_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    observed_at_ms bigint,
    next_refresh_at_ms bigint NOT NULL,
    last_error text,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT asset_profiles_status_check CHECK ((status = ANY (ARRAY['ready'::text, 'missing'::text, 'unsupported'::text, 'error'::text])))
);


--
-- Name: cex_token_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cex_token_profiles (
    cex_token_id text NOT NULL,
    provider text NOT NULL,
    status text NOT NULL,
    symbol text,
    name text,
    logo_url text,
    source_ref text,
    raw_payload_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    observed_at_ms bigint,
    last_error text,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL
);


--
-- Name: cex_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cex_tokens (
    cex_token_id text NOT NULL,
    base_symbol text NOT NULL,
    status text NOT NULL,
    evidence_level text NOT NULL,
    first_seen_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL
);


--
-- Name: checkpoint_blobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoint_blobs (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    channel text NOT NULL,
    version text NOT NULL,
    type text NOT NULL,
    blob bytea
);


--
-- Name: checkpoint_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoint_migrations (
    v integer NOT NULL
);


--
-- Name: checkpoint_writes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoint_writes (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    type text,
    blob bytea NOT NULL,
    task_path text DEFAULT ''::text NOT NULL
);


--
-- Name: checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    type text,
    checkpoint jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: collector_pending_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.collector_pending_items (
    source text NOT NULL,
    channel text NOT NULL,
    item_key text NOT NULL,
    internal_id text,
    item_json jsonb NOT NULL,
    received_at_ms bigint NOT NULL,
    frame_item_index bigint NOT NULL,
    payload_hash text NOT NULL,
    due_at_ms bigint NOT NULL,
    snapshot_state text NOT NULL,
    leased_until_ms bigint,
    lease_owner text,
    attempt_count bigint DEFAULT 0 NOT NULL,
    last_error text,
    first_observed_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT ck_collector_pending_items_attempt_nonnegative CHECK ((attempt_count >= 0)),
    CONSTRAINT ck_collector_pending_items_channel_nonblank CHECK ((btrim(channel) <> ''::text)),
    CONSTRAINT ck_collector_pending_items_due_at_positive CHECK ((due_at_ms > 0)),
    CONSTRAINT ck_collector_pending_items_first_observed_positive CHECK ((first_observed_at_ms > 0)),
    CONSTRAINT ck_collector_pending_items_frame_item_index_nonnegative CHECK ((frame_item_index >= 0)),
    CONSTRAINT ck_collector_pending_items_internal_id_nonblank CHECK (((internal_id IS NULL) OR (btrim(internal_id) <> ''::text))),
    CONSTRAINT ck_collector_pending_items_json_object CHECK ((jsonb_typeof(item_json) = 'object'::text)),
    CONSTRAINT ck_collector_pending_items_key_nonblank CHECK ((btrim(item_key) <> ''::text)),
    CONSTRAINT ck_collector_pending_items_lease_pair CHECK (((leased_until_ms IS NULL) = (lease_owner IS NULL))),
    CONSTRAINT ck_collector_pending_items_lease_positive CHECK (((leased_until_ms IS NULL) OR (leased_until_ms > 0))),
    CONSTRAINT ck_collector_pending_items_payload_hash CHECK ((payload_hash ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_collector_pending_items_received_at_positive CHECK ((received_at_ms > 0)),
    CONSTRAINT ck_collector_pending_items_snapshot_state CHECK ((snapshot_state = ANY (ARRAY['partial'::text, 'complete'::text, 'immediate'::text]))),
    CONSTRAINT ck_collector_pending_items_source_nonblank CHECK ((btrim(source) <> ''::text)),
    CONSTRAINT ck_collector_pending_items_updated_positive CHECK ((updated_at_ms > 0))
);


--
-- Name: enriched_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.enriched_events (
    event_id text NOT NULL,
    intent_id text NOT NULL,
    resolution_id text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    t_event_ms bigint NOT NULL,
    tick_observed_at_ms bigint,
    tick_id text,
    tick_lag_ms bigint,
    capture_method text NOT NULL,
    capture_reason text NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT enriched_events_capture_method_check CHECK ((capture_method = ANY (ARRAY['tier1_ws'::text, 'tier2_poll'::text, 'tier3_inline'::text, 'unavailable'::text]))),
    CONSTRAINT enriched_events_check CHECK ((((capture_method = 'unavailable'::text) AND (tick_observed_at_ms IS NULL) AND (tick_id IS NULL) AND (tick_lag_ms IS NULL)) OR ((capture_method <> 'unavailable'::text) AND (tick_observed_at_ms IS NOT NULL) AND (tick_id IS NOT NULL) AND (tick_lag_ms IS NOT NULL) AND (tick_lag_ms >= 0)))),
    CONSTRAINT enriched_events_target_type_check CHECK ((target_type = ANY (ARRAY['chain_token'::text, 'cex_symbol'::text])))
);


--
-- Name: event_anchor_backfill_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_anchor_backfill_jobs (
    event_id text NOT NULL,
    intent_id text NOT NULL,
    resolution_id text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    t_event_ms bigint NOT NULL,
    status text NOT NULL,
    next_run_at_ms bigint NOT NULL,
    active_until_ms bigint NOT NULL,
    attempt_count bigint DEFAULT 0 NOT NULL,
    last_reason text,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    lease_owner text,
    leased_until_ms bigint,
    CONSTRAINT ck_event_anchor_backfill_jobs_status CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'done'::text, 'expired'::text, 'failed'::text]))),
    CONSTRAINT event_anchor_backfill_jobs_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT event_anchor_backfill_jobs_target_type_check CHECK ((target_type = ANY (ARRAY['chain_token'::text, 'cex_symbol'::text])))
);


--
-- Name: event_entities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_entities (
    entity_id text NOT NULL,
    event_id text NOT NULL,
    entity_type text NOT NULL,
    raw_value text NOT NULL,
    normalized_value text NOT NULL,
    chain text,
    token_resolution_status text NOT NULL,
    confidence double precision NOT NULL,
    source text NOT NULL,
    received_at_ms bigint NOT NULL,
    author_handle text,
    created_at_ms bigint NOT NULL,
    text_surface text,
    span_start bigint,
    span_end bigint,
    sentence_id bigint,
    local_group_key text
);


--
-- Name: events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.events (
    event_id text NOT NULL,
    logical_dedup_key text NOT NULL,
    canonical_url text,
    source_provider text NOT NULL,
    source_transport text NOT NULL,
    coverage text NOT NULL,
    channel text NOT NULL,
    action text NOT NULL,
    original_action text,
    tweet_id text,
    internal_id text,
    timestamp_ms bigint NOT NULL,
    received_at_ms bigint NOT NULL,
    author_handle text,
    author_name text,
    author_avatar text,
    author_followers bigint,
    author_tags_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    text text,
    text_raw text,
    text_clean text,
    search_text text,
    urls_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    cashtags_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    hashtags_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    mentions_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    media_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    reference_json jsonb,
    raw_json jsonb NOT NULL,
    event_json jsonb NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    search_tsv tsvector GENERATED ALWAYS AS (((setweight(to_tsvector('simple'::regconfig, COALESCE(search_text, ''::text)), 'A'::"char") || setweight(to_tsvector('english'::regconfig, COALESCE(search_text, ''::text)), 'B'::"char")) || setweight(to_tsvector('simple'::regconfig, COALESCE(author_handle, ''::text)), 'D'::"char"))) STORED
);


--
-- Name: macro_acquisition_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_acquisition_targets (
    target_key text NOT NULL,
    dataset_id text NOT NULL,
    partition_key text NOT NULL,
    clock_kind text NOT NULL,
    cursor_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    next_due_at_ms bigint NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    leased_until_ms bigint,
    lease_owner text,
    attempt_count integer DEFAULT 0 NOT NULL,
    max_attempts integer NOT NULL,
    last_receipt_id text,
    last_success_at_ms bigint,
    last_error_code text,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT macro_acquisition_targets_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT macro_acquisition_targets_check CHECK ((updated_at_ms >= created_at_ms)),
    CONSTRAINT macro_acquisition_targets_clock_kind_check CHECK ((clock_kind = ANY (ARRAY['intraday_market'::text, 'daily_settlement'::text, 'scheduled_release'::text, 'official_state'::text, 'official_document'::text, 'backfill'::text]))),
    CONSTRAINT macro_acquisition_targets_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT macro_acquisition_targets_cursor_json_check CHECK ((jsonb_typeof(cursor_json) = 'object'::text)),
    CONSTRAINT macro_acquisition_targets_dataset_id_check CHECK ((btrim(dataset_id) <> ''::text)),
    CONSTRAINT macro_acquisition_targets_lease_shape CHECK ((((status = 'claimed'::text) AND (leased_until_ms IS NOT NULL) AND (btrim(COALESCE(lease_owner, ''::text)) <> ''::text)) OR ((status <> 'claimed'::text) AND (leased_until_ms IS NULL) AND (lease_owner IS NULL)))),
    CONSTRAINT macro_acquisition_targets_max_attempts_check CHECK ((max_attempts > 0)),
    CONSTRAINT macro_acquisition_targets_next_due_at_ms_check CHECK ((next_due_at_ms >= 0)),
    CONSTRAINT macro_acquisition_targets_partition_key_check CHECK ((btrim(partition_key) <> ''::text)),
    CONSTRAINT macro_acquisition_targets_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'claimed'::text, 'current'::text, 'delayed'::text, 'stale'::text, 'invalid'::text, 'unavailable'::text, 'backfilling'::text]))),
    CONSTRAINT macro_acquisition_targets_target_key_check CHECK ((btrim(target_key) <> ''::text))
);


--
-- Name: macro_daily_judgments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_daily_judgments (
    session_date date CONSTRAINT macro_daily_judgments_session_date_not_null1 NOT NULL,
    evidence_pack_id text CONSTRAINT macro_daily_judgments_evidence_pack_id_not_null1 NOT NULL,
    judgment_cutoff_ms bigint CONSTRAINT macro_daily_judgments_judgment_cutoff_ms_not_null1 NOT NULL,
    latest_fact_at_ms bigint CONSTRAINT macro_daily_judgments_latest_fact_at_ms_not_null1 NOT NULL,
    judgment_json jsonb CONSTRAINT macro_daily_judgments_judgment_json_not_null1 NOT NULL,
    memo_text text CONSTRAINT macro_daily_judgments_memo_text_not_null1 NOT NULL,
    schema_version text CONSTRAINT macro_daily_judgments_schema_version_not_null1 NOT NULL,
    compiler_version text CONSTRAINT macro_daily_judgments_compiler_version_not_null1 NOT NULL,
    payload_hash text CONSTRAINT macro_daily_judgments_payload_hash_not_null1 NOT NULL,
    published_at_ms bigint CONSTRAINT macro_daily_judgments_published_at_ms_not_null1 NOT NULL,
    CONSTRAINT macro_daily_judgments_check1 CHECK ((published_at_ms >= judgment_cutoff_ms)),
    CONSTRAINT macro_daily_judgments_compiler_version_check1 CHECK ((btrim(compiler_version) <> ''::text)),
    CONSTRAINT macro_daily_judgments_judgment_cutoff_ms_check1 CHECK ((judgment_cutoff_ms >= 0)),
    CONSTRAINT macro_daily_judgments_judgment_json_check1 CHECK (((jsonb_typeof(judgment_json) = 'object'::text) AND ((judgment_json ->> 'schema_version'::text) = 'macro_daily_judgment_v2'::text))),
    CONSTRAINT macro_daily_judgments_latest_fact_at_ms_check1 CHECK ((latest_fact_at_ms >= 0)),
    CONSTRAINT macro_daily_judgments_memo_text_check1 CHECK ((btrim(memo_text) <> ''::text)),
    CONSTRAINT macro_daily_judgments_payload_hash_check1 CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_daily_judgments_schema_version_check1 CHECK ((schema_version = 'macro_daily_judgment_v2'::text))
);


--
-- Name: macro_daily_judgments_v1_archive; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_daily_judgments_v1_archive (
    session_date date CONSTRAINT macro_daily_judgments_session_date_not_null NOT NULL,
    evidence_pack_id text CONSTRAINT macro_daily_judgments_evidence_pack_id_not_null NOT NULL,
    judgment_cutoff_ms bigint CONSTRAINT macro_daily_judgments_judgment_cutoff_ms_not_null NOT NULL,
    latest_fact_at_ms bigint CONSTRAINT macro_daily_judgments_latest_fact_at_ms_not_null NOT NULL,
    judgment_json jsonb CONSTRAINT macro_daily_judgments_judgment_json_not_null NOT NULL,
    memo_text text CONSTRAINT macro_daily_judgments_memo_text_not_null NOT NULL,
    schema_version text CONSTRAINT macro_daily_judgments_schema_version_not_null NOT NULL,
    compiler_version text CONSTRAINT macro_daily_judgments_compiler_version_not_null NOT NULL,
    payload_hash text CONSTRAINT macro_daily_judgments_payload_hash_not_null NOT NULL,
    published_at_ms bigint CONSTRAINT macro_daily_judgments_published_at_ms_not_null NOT NULL,
    CONSTRAINT macro_daily_judgments_check CHECK ((published_at_ms >= judgment_cutoff_ms)),
    CONSTRAINT macro_daily_judgments_compiler_version_check CHECK ((btrim(compiler_version) <> ''::text)),
    CONSTRAINT macro_daily_judgments_judgment_cutoff_ms_check CHECK ((judgment_cutoff_ms >= 0)),
    CONSTRAINT macro_daily_judgments_judgment_json_check CHECK ((jsonb_typeof(judgment_json) = 'object'::text)),
    CONSTRAINT macro_daily_judgments_latest_fact_at_ms_check CHECK ((latest_fact_at_ms >= 0)),
    CONSTRAINT macro_daily_judgments_memo_text_check CHECK ((btrim(memo_text) <> ''::text)),
    CONSTRAINT macro_daily_judgments_payload_hash_check CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_daily_judgments_schema_version_check CHECK ((btrim(schema_version) <> ''::text))
);


--
-- Name: macro_document_analyses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_document_analyses (
    analysis_id text NOT NULL,
    document_id text NOT NULL,
    document_hash text NOT NULL,
    official_id text,
    policy_relevance text NOT NULL,
    stance text NOT NULL,
    confidence double precision,
    analysis_json jsonb NOT NULL,
    model_name text NOT NULL,
    prompt_version text NOT NULL,
    reviewer_disposition text NOT NULL,
    created_at_ms bigint NOT NULL,
    payload_hash text NOT NULL,
    CONSTRAINT macro_document_analyses_analysis_id_check CHECK ((btrim(analysis_id) <> ''::text)),
    CONSTRAINT macro_document_analyses_analysis_json_check CHECK ((jsonb_typeof(analysis_json) = 'object'::text)),
    CONSTRAINT macro_document_analyses_confidence_check CHECK (((confidence IS NULL) OR ((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))),
    CONSTRAINT macro_document_analyses_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT macro_document_analyses_document_hash_check CHECK ((btrim(document_hash) <> ''::text)),
    CONSTRAINT macro_document_analyses_model_name_check CHECK ((btrim(model_name) <> ''::text)),
    CONSTRAINT macro_document_analyses_payload_hash_check CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_document_analyses_policy_relevance_check CHECK ((policy_relevance = ANY (ARRAY['policy_signal'::text, 'not_policy_signal'::text, 'uncertain'::text]))),
    CONSTRAINT macro_document_analyses_prompt_version_check CHECK ((btrim(prompt_version) <> ''::text)),
    CONSTRAINT macro_document_analyses_reviewer_disposition_check CHECK ((reviewer_disposition = ANY (ARRAY['pass'::text, 'revise'::text, 'block'::text]))),
    CONSTRAINT macro_document_analyses_stance_check CHECK ((stance = ANY (ARRAY['hawkish'::text, 'neutral'::text, 'dovish'::text, 'mixed'::text, 'no_call'::text])))
);


--
-- Name: macro_document_analysis_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_document_analysis_jobs (
    analysis_job_id text NOT NULL,
    document_id text NOT NULL,
    document_hash text NOT NULL,
    model_name text NOT NULL,
    prompt_version text NOT NULL,
    status text NOT NULL,
    next_due_at_ms bigint NOT NULL,
    leased_until_ms bigint,
    lease_owner text,
    attempt_count integer DEFAULT 0 NOT NULL,
    max_attempts integer NOT NULL,
    last_error_code text,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT macro_document_analysis_jobs_analysis_job_id_check CHECK ((btrim(analysis_job_id) <> ''::text)),
    CONSTRAINT macro_document_analysis_jobs_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT macro_document_analysis_jobs_check CHECK ((((status = 'claimed'::text) AND (leased_until_ms IS NOT NULL) AND (btrim(lease_owner) <> ''::text)) OR ((status <> 'claimed'::text) AND (leased_until_ms IS NULL) AND (lease_owner IS NULL)))),
    CONSTRAINT macro_document_analysis_jobs_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT macro_document_analysis_jobs_document_hash_check CHECK ((btrim(document_hash) <> ''::text)),
    CONSTRAINT macro_document_analysis_jobs_max_attempts_check CHECK ((max_attempts > 0)),
    CONSTRAINT macro_document_analysis_jobs_model_name_check CHECK ((btrim(model_name) <> ''::text)),
    CONSTRAINT macro_document_analysis_jobs_next_due_at_ms_check CHECK ((next_due_at_ms >= 0)),
    CONSTRAINT macro_document_analysis_jobs_prompt_version_check CHECK ((btrim(prompt_version) <> ''::text)),
    CONSTRAINT macro_document_analysis_jobs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'claimed'::text, 'retryable'::text, 'failed'::text, 'completed'::text]))),
    CONSTRAINT macro_document_analysis_jobs_updated_at_ms_check CHECK ((updated_at_ms >= 0))
);


--
-- Name: macro_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_documents (
    document_id text NOT NULL,
    dataset_id text NOT NULL,
    document_type text NOT NULL,
    title text NOT NULL,
    effective_date date NOT NULL,
    published_at_ms bigint NOT NULL,
    received_at_ms bigint NOT NULL,
    source_url text NOT NULL,
    content_text text NOT NULL,
    fact_hash text NOT NULL,
    metadata_json jsonb NOT NULL,
    CONSTRAINT macro_documents_check CHECK ((received_at_ms >= published_at_ms)),
    CONSTRAINT macro_documents_content_text_check CHECK ((btrim(content_text) <> ''::text)),
    CONSTRAINT macro_documents_dataset_id_check CHECK ((btrim(dataset_id) <> ''::text)),
    CONSTRAINT macro_documents_document_id_check CHECK ((btrim(document_id) <> ''::text)),
    CONSTRAINT macro_documents_document_type_check CHECK ((document_type = ANY (ARRAY['statement'::text, 'implementation'::text, 'minutes'::text, 'sep'::text, 'speech'::text, 'auction'::text, 'survey'::text, 'calendar'::text]))),
    CONSTRAINT macro_documents_fact_hash_check CHECK ((btrim(fact_hash) <> ''::text)),
    CONSTRAINT macro_documents_metadata_json_check CHECK ((jsonb_typeof(metadata_json) = 'object'::text)),
    CONSTRAINT macro_documents_published_at_ms_check CHECK ((published_at_ms >= 0)),
    CONSTRAINT macro_documents_source_url_check CHECK ((btrim(source_url) <> ''::text)),
    CONSTRAINT macro_documents_title_check CHECK ((btrim(title) <> ''::text))
);


--
-- Name: macro_event_updates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_event_updates (
    event_update_id text CONSTRAINT macro_event_updates_event_update_id_not_null1 NOT NULL,
    session_date date CONSTRAINT macro_event_updates_session_date_not_null1 NOT NULL,
    evidence_pack_id text CONSTRAINT macro_event_updates_evidence_pack_id_not_null1 NOT NULL,
    trigger_release_fact_id text CONSTRAINT macro_event_updates_trigger_release_fact_id_not_null1 NOT NULL,
    update_json jsonb CONSTRAINT macro_event_updates_update_json_not_null1 NOT NULL,
    payload_hash text CONSTRAINT macro_event_updates_payload_hash_not_null1 NOT NULL,
    published_at_ms bigint CONSTRAINT macro_event_updates_published_at_ms_not_null1 NOT NULL,
    CONSTRAINT macro_event_updates_event_update_id_check1 CHECK ((btrim(event_update_id) <> ''::text)),
    CONSTRAINT macro_event_updates_payload_hash_check1 CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_event_updates_published_at_ms_check1 CHECK ((published_at_ms >= 0)),
    CONSTRAINT macro_event_updates_update_json_check1 CHECK ((jsonb_typeof(update_json) = 'object'::text))
);


--
-- Name: macro_event_updates_v1_archive; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_event_updates_v1_archive (
    event_update_id text CONSTRAINT macro_event_updates_event_update_id_not_null NOT NULL,
    session_date date CONSTRAINT macro_event_updates_session_date_not_null NOT NULL,
    evidence_pack_id text CONSTRAINT macro_event_updates_evidence_pack_id_not_null NOT NULL,
    trigger_release_fact_id text CONSTRAINT macro_event_updates_trigger_release_fact_id_not_null NOT NULL,
    update_json jsonb CONSTRAINT macro_event_updates_update_json_not_null NOT NULL,
    payload_hash text CONSTRAINT macro_event_updates_payload_hash_not_null NOT NULL,
    published_at_ms bigint CONSTRAINT macro_event_updates_published_at_ms_not_null NOT NULL,
    CONSTRAINT macro_event_updates_event_update_id_check CHECK ((btrim(event_update_id) <> ''::text)),
    CONSTRAINT macro_event_updates_payload_hash_check CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_event_updates_published_at_ms_check CHECK ((published_at_ms >= 0)),
    CONSTRAINT macro_event_updates_update_json_check CHECK ((jsonb_typeof(update_json) = 'object'::text))
);


--
-- Name: macro_evidence_packs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_evidence_packs (
    evidence_pack_id text CONSTRAINT macro_evidence_packs_evidence_pack_id_not_null1 NOT NULL,
    session_date date CONSTRAINT macro_evidence_packs_session_date_not_null1 NOT NULL,
    judgment_cutoff_ms bigint CONSTRAINT macro_evidence_packs_judgment_cutoff_ms_not_null1 NOT NULL,
    latest_fact_at_ms bigint CONSTRAINT macro_evidence_packs_latest_fact_at_ms_not_null1 NOT NULL,
    schema_version text CONSTRAINT macro_evidence_packs_schema_version_not_null1 NOT NULL,
    compiler_version text CONSTRAINT macro_evidence_packs_compiler_version_not_null1 NOT NULL,
    payload_json jsonb CONSTRAINT macro_evidence_packs_payload_json_not_null1 NOT NULL,
    payload_hash text CONSTRAINT macro_evidence_packs_payload_hash_not_null1 NOT NULL,
    created_at_ms bigint CONSTRAINT macro_evidence_packs_created_at_ms_not_null1 NOT NULL,
    CONSTRAINT macro_evidence_packs_check1 CHECK ((created_at_ms >= judgment_cutoff_ms)),
    CONSTRAINT macro_evidence_packs_compiler_version_check1 CHECK ((btrim(compiler_version) <> ''::text)),
    CONSTRAINT macro_evidence_packs_evidence_pack_id_check1 CHECK ((btrim(evidence_pack_id) <> ''::text)),
    CONSTRAINT macro_evidence_packs_judgment_cutoff_ms_check1 CHECK ((judgment_cutoff_ms >= 0)),
    CONSTRAINT macro_evidence_packs_latest_fact_at_ms_check1 CHECK ((latest_fact_at_ms >= 0)),
    CONSTRAINT macro_evidence_packs_payload_hash_check1 CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_evidence_packs_payload_json_check1 CHECK (((jsonb_typeof(payload_json) = 'object'::text) AND ((payload_json ->> 'schema_version'::text) = 'macro_evidence_pack_v2'::text))),
    CONSTRAINT macro_evidence_packs_schema_version_check1 CHECK ((schema_version = 'macro_evidence_pack_v2'::text))
);


--
-- Name: macro_evidence_packs_v1_archive; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_evidence_packs_v1_archive (
    evidence_pack_id text CONSTRAINT macro_evidence_packs_evidence_pack_id_not_null NOT NULL,
    session_date date CONSTRAINT macro_evidence_packs_session_date_not_null NOT NULL,
    judgment_cutoff_ms bigint CONSTRAINT macro_evidence_packs_judgment_cutoff_ms_not_null NOT NULL,
    latest_fact_at_ms bigint CONSTRAINT macro_evidence_packs_latest_fact_at_ms_not_null NOT NULL,
    schema_version text CONSTRAINT macro_evidence_packs_schema_version_not_null NOT NULL,
    compiler_version text CONSTRAINT macro_evidence_packs_compiler_version_not_null NOT NULL,
    payload_json jsonb CONSTRAINT macro_evidence_packs_payload_json_not_null NOT NULL,
    payload_hash text CONSTRAINT macro_evidence_packs_payload_hash_not_null NOT NULL,
    created_at_ms bigint CONSTRAINT macro_evidence_packs_created_at_ms_not_null NOT NULL,
    CONSTRAINT macro_evidence_packs_check CHECK ((created_at_ms >= judgment_cutoff_ms)),
    CONSTRAINT macro_evidence_packs_compiler_version_check CHECK ((btrim(compiler_version) <> ''::text)),
    CONSTRAINT macro_evidence_packs_evidence_pack_id_check CHECK ((btrim(evidence_pack_id) <> ''::text)),
    CONSTRAINT macro_evidence_packs_judgment_cutoff_ms_check CHECK ((judgment_cutoff_ms >= 0)),
    CONSTRAINT macro_evidence_packs_latest_fact_at_ms_check CHECK ((latest_fact_at_ms >= 0)),
    CONSTRAINT macro_evidence_packs_payload_hash_check CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_evidence_packs_payload_json_check CHECK ((jsonb_typeof(payload_json) = 'object'::text)),
    CONSTRAINT macro_evidence_packs_schema_version_check CHECK ((btrim(schema_version) <> ''::text))
);


--
-- Name: macro_feature_series; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_feature_series (
    feature_id text NOT NULL,
    as_of_date date NOT NULL,
    formula_version text NOT NULL,
    value_numeric double precision NOT NULL,
    unit text NOT NULL,
    inputs_json jsonb NOT NULL,
    payload_hash text NOT NULL,
    computed_at_ms bigint NOT NULL,
    CONSTRAINT macro_feature_series_computed_at_ms_check CHECK ((computed_at_ms >= 0)),
    CONSTRAINT macro_feature_series_feature_id_check CHECK ((btrim(feature_id) <> ''::text)),
    CONSTRAINT macro_feature_series_formula_version_check CHECK ((btrim(formula_version) <> ''::text)),
    CONSTRAINT macro_feature_series_inputs_json_check CHECK ((jsonb_typeof(inputs_json) = 'array'::text)),
    CONSTRAINT macro_feature_series_payload_hash_check CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_feature_series_unit_check CHECK ((btrim(unit) <> ''::text)),
    CONSTRAINT macro_feature_series_value_numeric_check CHECK (((value_numeric <> 'NaN'::double precision) AND (value_numeric <> 'Infinity'::double precision) AND (value_numeric <> '-Infinity'::double precision)))
);


--
-- Name: macro_fed_official_role_facts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_fed_official_role_facts (
    role_fact_id text NOT NULL,
    dataset_id text NOT NULL,
    official_id text NOT NULL,
    official_name text NOT NULL,
    role_title text NOT NULL,
    organization text NOT NULL,
    effective_start date NOT NULL,
    effective_end date,
    fomc_participant boolean NOT NULL,
    fomc_voter boolean NOT NULL,
    source_url text NOT NULL,
    received_at_ms bigint NOT NULL,
    fact_hash text NOT NULL,
    raw_data_json jsonb NOT NULL,
    CONSTRAINT macro_fed_official_role_facts_check CHECK (((effective_end IS NULL) OR (effective_end >= effective_start))),
    CONSTRAINT macro_fed_official_role_facts_dataset_id_check CHECK ((dataset_id = 'federal_reserve.fomc.roster'::text)),
    CONSTRAINT macro_fed_official_role_facts_fact_hash_check CHECK ((btrim(fact_hash) <> ''::text)),
    CONSTRAINT macro_fed_official_role_facts_official_id_check CHECK ((btrim(official_id) <> ''::text)),
    CONSTRAINT macro_fed_official_role_facts_official_name_check CHECK ((btrim(official_name) <> ''::text)),
    CONSTRAINT macro_fed_official_role_facts_organization_check CHECK ((btrim(organization) <> ''::text)),
    CONSTRAINT macro_fed_official_role_facts_raw_data_json_check CHECK ((jsonb_typeof(raw_data_json) = 'object'::text)),
    CONSTRAINT macro_fed_official_role_facts_received_at_ms_check CHECK ((received_at_ms >= 0)),
    CONSTRAINT macro_fed_official_role_facts_role_fact_id_check CHECK ((btrim(role_fact_id) <> ''::text)),
    CONSTRAINT macro_fed_official_role_facts_role_title_check CHECK ((btrim(role_title) <> ''::text)),
    CONSTRAINT macro_fed_official_role_facts_source_url_check CHECK ((btrim(source_url) <> ''::text))
);


--
-- Name: macro_judgment_status; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_judgment_status (
    session_date date NOT NULL,
    judgment_cutoff_ms bigint NOT NULL,
    state text NOT NULL,
    reason_code text NOT NULL,
    details_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    payload_hash text NOT NULL,
    attempted_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT macro_judgment_status_check CHECK ((attempted_at_ms >= judgment_cutoff_ms)),
    CONSTRAINT macro_judgment_status_check1 CHECK ((updated_at_ms >= attempted_at_ms)),
    CONSTRAINT macro_judgment_status_details_json_check CHECK ((jsonb_typeof(details_json) = 'object'::text)),
    CONSTRAINT macro_judgment_status_judgment_cutoff_ms_check CHECK ((judgment_cutoff_ms >= 0)),
    CONSTRAINT macro_judgment_status_payload_hash_check CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_judgment_status_reason_code_check CHECK ((btrim(reason_code) <> ''::text)),
    CONSTRAINT macro_judgment_status_state_check CHECK ((state = ANY (ARRAY['blocked'::text, 'current'::text])))
);


--
-- Name: macro_module_current; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_module_current (
    module_id text NOT NULL,
    fact_cutoff_ms bigint NOT NULL,
    payload_json jsonb NOT NULL,
    payload_hash text NOT NULL,
    updated_at_ms bigint NOT NULL,
    data_health_state text NOT NULL,
    CONSTRAINT macro_module_current_data_health_state_check CHECK ((data_health_state = ANY (ARRAY['current'::text, 'delayed'::text, 'stale'::text, 'invalid'::text, 'backfilling'::text, 'unavailable'::text]))),
    CONSTRAINT macro_module_current_fact_cutoff_ms_check CHECK ((fact_cutoff_ms >= 0)),
    CONSTRAINT macro_module_current_module_id_check CHECK ((module_id = ANY (ARRAY['rates_fed'::text, 'economy_inflation'::text, 'liquidity_funding'::text, 'credit'::text, 'volatility'::text, 'cross_asset'::text]))),
    CONSTRAINT macro_module_current_payload_hash_check CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_module_current_payload_json_check CHECK ((jsonb_typeof(payload_json) = 'object'::text)),
    CONSTRAINT macro_module_current_typed_schema_check CHECK (((payload_json ->> 'schema_version'::text) =
CASE module_id
    WHEN 'rates_fed'::text THEN 'macro_rates_fed_v2'::text
    WHEN 'economy_inflation'::text THEN 'macro_economy_inflation_v2'::text
    WHEN 'liquidity_funding'::text THEN 'macro_liquidity_funding_v2'::text
    WHEN 'credit'::text THEN 'macro_credit_v3'::text
    WHEN 'volatility'::text THEN 'macro_volatility_v2'::text
    WHEN 'cross_asset'::text THEN 'macro_cross_asset_v3'::text
    ELSE NULL::text
END)),
    CONSTRAINT macro_module_current_updated_at_ms_check CHECK ((updated_at_ms >= 0))
);


--
-- Name: macro_release_facts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_release_facts (
    release_fact_id text NOT NULL,
    dataset_id text NOT NULL,
    release_id text NOT NULL,
    series_id text NOT NULL,
    reference_period text NOT NULL,
    scheduled_at_ms bigint,
    published_at_ms bigint NOT NULL,
    received_at_ms bigint NOT NULL,
    actual_value double precision,
    prior_value double precision,
    revised_prior_value double precision,
    estimate_value double precision,
    unit text NOT NULL,
    importance_tier smallint NOT NULL,
    source_url text NOT NULL,
    fact_hash text NOT NULL,
    raw_data_json jsonb NOT NULL,
    CONSTRAINT macro_release_facts_actual_value_check CHECK (((actual_value IS NULL) OR ((actual_value <> 'NaN'::double precision) AND (actual_value <> 'Infinity'::double precision) AND (actual_value <> '-Infinity'::double precision)))),
    CONSTRAINT macro_release_facts_check CHECK ((received_at_ms >= published_at_ms)),
    CONSTRAINT macro_release_facts_dataset_id_check CHECK ((btrim(dataset_id) <> ''::text)),
    CONSTRAINT macro_release_facts_estimate_value_check CHECK (((estimate_value IS NULL) OR ((estimate_value <> 'NaN'::double precision) AND (estimate_value <> 'Infinity'::double precision) AND (estimate_value <> '-Infinity'::double precision)))),
    CONSTRAINT macro_release_facts_fact_hash_check CHECK ((btrim(fact_hash) <> ''::text)),
    CONSTRAINT macro_release_facts_importance_tier_check CHECK (((importance_tier >= 1) AND (importance_tier <= 3))),
    CONSTRAINT macro_release_facts_prior_value_check CHECK (((prior_value IS NULL) OR ((prior_value <> 'NaN'::double precision) AND (prior_value <> 'Infinity'::double precision) AND (prior_value <> '-Infinity'::double precision)))),
    CONSTRAINT macro_release_facts_published_at_ms_check CHECK ((published_at_ms >= 0)),
    CONSTRAINT macro_release_facts_raw_data_json_check CHECK ((jsonb_typeof(raw_data_json) = 'object'::text)),
    CONSTRAINT macro_release_facts_reference_period_check CHECK ((btrim(reference_period) <> ''::text)),
    CONSTRAINT macro_release_facts_release_fact_id_check CHECK ((btrim(release_fact_id) <> ''::text)),
    CONSTRAINT macro_release_facts_release_id_check CHECK ((btrim(release_id) <> ''::text)),
    CONSTRAINT macro_release_facts_revised_prior_value_check CHECK (((revised_prior_value IS NULL) OR ((revised_prior_value <> 'NaN'::double precision) AND (revised_prior_value <> 'Infinity'::double precision) AND (revised_prior_value <> '-Infinity'::double precision)))),
    CONSTRAINT macro_release_facts_scheduled_at_ms_check CHECK (((scheduled_at_ms IS NULL) OR (scheduled_at_ms >= 0))),
    CONSTRAINT macro_release_facts_series_id_check CHECK ((btrim(series_id) <> ''::text)),
    CONSTRAINT macro_release_facts_source_url_check CHECK ((btrim(source_url) <> ''::text)),
    CONSTRAINT macro_release_facts_unit_check CHECK ((btrim(unit) <> ''::text))
);


--
-- Name: macro_research_publications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_research_publications (
    session_date date CONSTRAINT macro_research_publications_session_date_not_null1 NOT NULL,
    market_cutoff_ms bigint CONSTRAINT macro_research_publications_market_cutoff_ms_not_null1 NOT NULL,
    evidence_pack_id text CONSTRAINT macro_research_publications_evidence_pack_id_not_null1 NOT NULL,
    artifact_json jsonb CONSTRAINT macro_research_publications_artifact_json_not_null1 NOT NULL,
    report_markdown text CONSTRAINT macro_research_publications_report_markdown_not_null1 NOT NULL,
    audit_json jsonb CONSTRAINT macro_research_publications_audit_json_not_null1 NOT NULL,
    reviewer_disposition text CONSTRAINT macro_research_publications_reviewer_disposition_not_null1 NOT NULL,
    model_name text CONSTRAINT macro_research_publications_model_name_not_null1 NOT NULL,
    prompt_version text CONSTRAINT macro_research_publications_prompt_version_not_null1 NOT NULL,
    workflow_version text CONSTRAINT macro_research_publications_workflow_version_not_null1 NOT NULL,
    artifact_hash text CONSTRAINT macro_research_publications_artifact_hash_not_null1 NOT NULL,
    published_at_ms bigint CONSTRAINT macro_research_publications_published_at_ms_not_null1 NOT NULL,
    CONSTRAINT macro_research_publications_artifact_hash_check1 CHECK ((btrim(artifact_hash) <> ''::text)),
    CONSTRAINT macro_research_publications_artifact_json_check1 CHECK ((jsonb_typeof(artifact_json) = 'object'::text)),
    CONSTRAINT macro_research_publications_audit_json_check1 CHECK ((jsonb_typeof(audit_json) = 'object'::text)),
    CONSTRAINT macro_research_publications_check1 CHECK ((published_at_ms >= market_cutoff_ms)),
    CONSTRAINT macro_research_publications_market_cutoff_ms_check1 CHECK ((market_cutoff_ms >= 0)),
    CONSTRAINT macro_research_publications_model_name_check1 CHECK ((btrim(model_name) <> ''::text)),
    CONSTRAINT macro_research_publications_prompt_version_check1 CHECK ((btrim(prompt_version) <> ''::text)),
    CONSTRAINT macro_research_publications_report_markdown_check1 CHECK ((btrim(report_markdown) <> ''::text)),
    CONSTRAINT macro_research_publications_reviewer_disposition_check1 CHECK ((reviewer_disposition = ANY (ARRAY['pass'::text, 'revise'::text, 'block'::text]))),
    CONSTRAINT macro_research_publications_workflow_version_check1 CHECK ((btrim(workflow_version) <> ''::text))
);


--
-- Name: macro_research_publications_v1_archive; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_research_publications_v1_archive (
    session_date date CONSTRAINT macro_research_publications_session_date_not_null NOT NULL,
    market_cutoff_ms bigint CONSTRAINT macro_research_publications_market_cutoff_ms_not_null NOT NULL,
    evidence_pack_id text CONSTRAINT macro_research_publications_evidence_pack_id_not_null NOT NULL,
    artifact_json jsonb CONSTRAINT macro_research_publications_artifact_json_not_null NOT NULL,
    report_markdown text CONSTRAINT macro_research_publications_report_markdown_not_null NOT NULL,
    audit_json jsonb CONSTRAINT macro_research_publications_audit_json_not_null NOT NULL,
    reviewer_disposition text CONSTRAINT macro_research_publications_reviewer_disposition_not_null NOT NULL,
    model_name text CONSTRAINT macro_research_publications_model_name_not_null NOT NULL,
    prompt_version text CONSTRAINT macro_research_publications_prompt_version_not_null NOT NULL,
    workflow_version text CONSTRAINT macro_research_publications_workflow_version_not_null NOT NULL,
    artifact_hash text CONSTRAINT macro_research_publications_artifact_hash_not_null NOT NULL,
    published_at_ms bigint CONSTRAINT macro_research_publications_published_at_ms_not_null NOT NULL,
    CONSTRAINT macro_research_publications_artifact_hash_check CHECK ((btrim(artifact_hash) <> ''::text)),
    CONSTRAINT macro_research_publications_artifact_json_check CHECK ((jsonb_typeof(artifact_json) = 'object'::text)),
    CONSTRAINT macro_research_publications_audit_json_check CHECK ((jsonb_typeof(audit_json) = 'object'::text)),
    CONSTRAINT macro_research_publications_check CHECK ((published_at_ms >= market_cutoff_ms)),
    CONSTRAINT macro_research_publications_market_cutoff_ms_check CHECK ((market_cutoff_ms >= 0)),
    CONSTRAINT macro_research_publications_model_name_check CHECK ((btrim(model_name) <> ''::text)),
    CONSTRAINT macro_research_publications_prompt_version_check CHECK ((btrim(prompt_version) <> ''::text)),
    CONSTRAINT macro_research_publications_report_markdown_check CHECK ((btrim(report_markdown) <> ''::text)),
    CONSTRAINT macro_research_publications_reviewer_disposition_check CHECK ((reviewer_disposition = ANY (ARRAY['pass'::text, 'revise'::text, 'block'::text]))),
    CONSTRAINT macro_research_publications_workflow_version_check CHECK ((btrim(workflow_version) <> ''::text))
);


--
-- Name: macro_research_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_research_runs (
    session_date date CONSTRAINT macro_research_runs_session_date_not_null1 NOT NULL,
    market_cutoff_ms bigint CONSTRAINT macro_research_runs_market_cutoff_ms_not_null1 NOT NULL,
    evidence_pack_id text CONSTRAINT macro_research_runs_evidence_pack_id_not_null1 NOT NULL,
    status text DEFAULT 'pending'::text CONSTRAINT macro_research_runs_status_not_null1 NOT NULL,
    sealed_at_ms bigint CONSTRAINT macro_research_runs_sealed_at_ms_not_null1 NOT NULL,
    attempt_count integer DEFAULT 0 CONSTRAINT macro_research_runs_attempt_count_not_null1 NOT NULL,
    max_attempts integer CONSTRAINT macro_research_runs_max_attempts_not_null1 NOT NULL,
    due_at_ms bigint CONSTRAINT macro_research_runs_due_at_ms_not_null1 NOT NULL,
    leased_until_ms bigint,
    lease_owner text,
    reviewer_disposition text,
    last_error_code text,
    last_error_message text,
    created_at_ms bigint CONSTRAINT macro_research_runs_created_at_ms_not_null1 NOT NULL,
    updated_at_ms bigint CONSTRAINT macro_research_runs_updated_at_ms_not_null1 NOT NULL,
    CONSTRAINT macro_research_runs_attempt_count_check1 CHECK ((attempt_count >= 0)),
    CONSTRAINT macro_research_runs_check2 CHECK ((sealed_at_ms >= market_cutoff_ms)),
    CONSTRAINT macro_research_runs_check3 CHECK ((updated_at_ms >= created_at_ms)),
    CONSTRAINT macro_research_runs_created_at_ms_check1 CHECK ((created_at_ms >= 0)),
    CONSTRAINT macro_research_runs_due_at_ms_check1 CHECK ((due_at_ms >= 0)),
    CONSTRAINT macro_research_runs_market_cutoff_ms_check1 CHECK ((market_cutoff_ms >= 0)),
    CONSTRAINT macro_research_runs_max_attempts_check1 CHECK ((max_attempts > 0)),
    CONSTRAINT macro_research_runs_reviewer_disposition_check1 CHECK (((reviewer_disposition IS NULL) OR (reviewer_disposition = ANY (ARRAY['pass'::text, 'revise'::text, 'block'::text])))),
    CONSTRAINT macro_research_runs_status_check1 CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'retryable'::text, 'failed'::text, 'published'::text]))),
    CONSTRAINT macro_research_runs_v2_lease_shape_check CHECK ((((status = 'running'::text) AND (leased_until_ms IS NOT NULL) AND (btrim(COALESCE(lease_owner, ''::text)) <> ''::text)) OR ((status <> 'running'::text) AND (leased_until_ms IS NULL) AND (lease_owner IS NULL))))
);


--
-- Name: macro_research_runs_v1_archive; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_research_runs_v1_archive (
    session_date date CONSTRAINT macro_research_runs_session_date_not_null NOT NULL,
    market_cutoff_ms bigint CONSTRAINT macro_research_runs_market_cutoff_ms_not_null NOT NULL,
    evidence_pack_id text CONSTRAINT macro_research_runs_evidence_pack_id_not_null NOT NULL,
    status text DEFAULT 'pending'::text CONSTRAINT macro_research_runs_status_not_null NOT NULL,
    sealed_at_ms bigint CONSTRAINT macro_research_runs_sealed_at_ms_not_null NOT NULL,
    attempt_count integer DEFAULT 0 CONSTRAINT macro_research_runs_attempt_count_not_null NOT NULL,
    max_attempts integer CONSTRAINT macro_research_runs_max_attempts_not_null NOT NULL,
    due_at_ms bigint CONSTRAINT macro_research_runs_due_at_ms_not_null NOT NULL,
    leased_until_ms bigint,
    lease_owner text,
    reviewer_disposition text,
    last_error_code text,
    last_error_message text,
    created_at_ms bigint CONSTRAINT macro_research_runs_created_at_ms_not_null NOT NULL,
    updated_at_ms bigint CONSTRAINT macro_research_runs_updated_at_ms_not_null NOT NULL,
    CONSTRAINT macro_research_runs_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT macro_research_runs_check CHECK ((sealed_at_ms >= market_cutoff_ms)),
    CONSTRAINT macro_research_runs_check1 CHECK ((updated_at_ms >= created_at_ms)),
    CONSTRAINT macro_research_runs_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT macro_research_runs_due_at_ms_check CHECK ((due_at_ms >= 0)),
    CONSTRAINT macro_research_runs_lease_shape_check CHECK ((((status = 'running'::text) AND (leased_until_ms IS NOT NULL) AND (btrim(COALESCE(lease_owner, ''::text)) <> ''::text)) OR ((status <> 'running'::text) AND (leased_until_ms IS NULL) AND (lease_owner IS NULL)))),
    CONSTRAINT macro_research_runs_market_cutoff_ms_check CHECK ((market_cutoff_ms >= 0)),
    CONSTRAINT macro_research_runs_max_attempts_check CHECK ((max_attempts > 0)),
    CONSTRAINT macro_research_runs_reviewer_disposition_check CHECK (((reviewer_disposition IS NULL) OR (reviewer_disposition = ANY (ARRAY['pass'::text, 'revise'::text, 'block'::text])))),
    CONSTRAINT macro_research_runs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'retryable'::text, 'failed'::text, 'published'::text])))
);


--
-- Name: macro_series_facts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_series_facts (
    fact_id text NOT NULL,
    dataset_id text NOT NULL,
    series_id text NOT NULL,
    reference_date date NOT NULL,
    vintage_date date NOT NULL,
    value_numeric double precision,
    value_text text,
    unit text NOT NULL,
    published_at_ms bigint,
    received_at_ms bigint NOT NULL,
    source_url text NOT NULL,
    fact_hash text NOT NULL,
    raw_data_json jsonb NOT NULL,
    CONSTRAINT macro_series_facts_check CHECK (((received_at_ms >= 0) AND ((published_at_ms IS NULL) OR (received_at_ms >= published_at_ms)))),
    CONSTRAINT macro_series_facts_dataset_id_check CHECK ((btrim(dataset_id) <> ''::text)),
    CONSTRAINT macro_series_facts_fact_hash_check CHECK ((btrim(fact_hash) <> ''::text)),
    CONSTRAINT macro_series_facts_fact_id_check CHECK ((btrim(fact_id) <> ''::text)),
    CONSTRAINT macro_series_facts_published_at_ms_check CHECK (((published_at_ms IS NULL) OR (published_at_ms >= 0))),
    CONSTRAINT macro_series_facts_raw_data_json_check CHECK ((jsonb_typeof(raw_data_json) = 'object'::text)),
    CONSTRAINT macro_series_facts_series_id_check CHECK ((btrim(series_id) <> ''::text)),
    CONSTRAINT macro_series_facts_source_url_check CHECK ((btrim(source_url) <> ''::text)),
    CONSTRAINT macro_series_facts_unit_check CHECK ((btrim(unit) <> ''::text)),
    CONSTRAINT macro_series_facts_value_numeric_check CHECK (((value_numeric IS NULL) OR ((value_numeric <> 'NaN'::double precision) AND (value_numeric <> 'Infinity'::double precision) AND (value_numeric <> '-Infinity'::double precision)))),
    CONSTRAINT macro_series_facts_value_shape CHECK ((((value_numeric IS NOT NULL) AND (value_text IS NULL)) OR ((value_numeric IS NULL) AND (btrim(COALESCE(value_text, ''::text)) <> ''::text))))
);


--
-- Name: macro_source_receipts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_source_receipts (
    receipt_id text NOT NULL,
    target_key text NOT NULL,
    dataset_id text NOT NULL,
    partition_key text NOT NULL,
    started_at_ms bigint NOT NULL,
    completed_at_ms bigint NOT NULL,
    status text NOT NULL,
    http_status integer,
    rows_seen integer NOT NULL,
    rows_inserted integer NOT NULL,
    response_hash text,
    error_code text,
    error_message text,
    diagnostics_json jsonb NOT NULL,
    CONSTRAINT macro_source_receipts_check CHECK ((completed_at_ms >= started_at_ms)),
    CONSTRAINT macro_source_receipts_dataset_id_check CHECK ((btrim(dataset_id) <> ''::text)),
    CONSTRAINT macro_source_receipts_diagnostics_json_check CHECK ((jsonb_typeof(diagnostics_json) = 'object'::text)),
    CONSTRAINT macro_source_receipts_partition_key_check CHECK ((btrim(partition_key) <> ''::text)),
    CONSTRAINT macro_source_receipts_receipt_id_check CHECK ((btrim(receipt_id) <> ''::text)),
    CONSTRAINT macro_source_receipts_rows_inserted_check CHECK ((rows_inserted >= 0)),
    CONSTRAINT macro_source_receipts_rows_seen_check CHECK ((rows_seen >= 0)),
    CONSTRAINT macro_source_receipts_started_at_ms_check CHECK ((started_at_ms >= 0)),
    CONSTRAINT macro_source_receipts_status_check CHECK ((status = ANY (ARRAY['ok'::text, 'not_modified'::text, 'empty'::text, 'failed'::text, 'invalid'::text]))),
    CONSTRAINT macro_source_receipts_target_key_check CHECK ((btrim(target_key) <> ''::text))
);


--
-- Name: market_instruments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.market_instruments (
    instrument_id text NOT NULL,
    symbol text NOT NULL,
    name text NOT NULL,
    asset_class text NOT NULL,
    instrument_type text NOT NULL,
    venue text NOT NULL,
    currency text NOT NULL,
    price_unit text NOT NULL,
    source_metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT market_instruments_asset_class_check CHECK ((asset_class = ANY (ARRAY['equity'::text, 'rates'::text, 'credit'::text, 'fx'::text, 'commodity'::text, 'crypto'::text, 'volatility'::text]))),
    CONSTRAINT market_instruments_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT market_instruments_currency_check CHECK ((btrim(currency) <> ''::text)),
    CONSTRAINT market_instruments_instrument_id_check CHECK ((btrim(instrument_id) <> ''::text)),
    CONSTRAINT market_instruments_instrument_type_check CHECK ((instrument_type = ANY (ARRAY['index'::text, 'etf'::text, 'spot'::text, 'future'::text, 'rate'::text, 'spread'::text]))),
    CONSTRAINT market_instruments_name_check CHECK ((btrim(name) <> ''::text)),
    CONSTRAINT market_instruments_price_unit_check CHECK ((btrim(price_unit) <> ''::text)),
    CONSTRAINT market_instruments_source_metadata_json_check CHECK ((jsonb_typeof(source_metadata_json) = 'object'::text)),
    CONSTRAINT market_instruments_symbol_check CHECK ((btrim(symbol) <> ''::text)),
    CONSTRAINT market_instruments_venue_check CHECK ((btrim(venue) <> ''::text))
);


--
-- Name: market_observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.market_observations (
    observation_id text NOT NULL,
    instrument_id text NOT NULL,
    dataset_id text NOT NULL,
    source_id text NOT NULL,
    field_name text NOT NULL,
    value_numeric double precision NOT NULL,
    unit text NOT NULL,
    observed_at_ms bigint NOT NULL,
    published_at_ms bigint,
    received_at_ms bigint NOT NULL,
    trust_tier text NOT NULL,
    source_url text NOT NULL,
    fact_hash text NOT NULL,
    raw_data_json jsonb NOT NULL,
    CONSTRAINT market_observations_clock_order CHECK (((published_at_ms IS NULL) OR (published_at_ms <= received_at_ms))),
    CONSTRAINT market_observations_dataset_id_check CHECK ((btrim(dataset_id) <> ''::text)),
    CONSTRAINT market_observations_fact_hash_check CHECK ((btrim(fact_hash) <> ''::text)),
    CONSTRAINT market_observations_field_name_check CHECK ((btrim(field_name) <> ''::text)),
    CONSTRAINT market_observations_observation_id_check CHECK ((btrim(observation_id) <> ''::text)),
    CONSTRAINT market_observations_observed_at_ms_check CHECK ((observed_at_ms >= 0)),
    CONSTRAINT market_observations_published_at_ms_check CHECK (((published_at_ms IS NULL) OR (published_at_ms >= 0))),
    CONSTRAINT market_observations_raw_data_json_check CHECK ((jsonb_typeof(raw_data_json) = 'object'::text)),
    CONSTRAINT market_observations_received_at_ms_check CHECK ((received_at_ms >= 0)),
    CONSTRAINT market_observations_source_id_check CHECK ((btrim(source_id) <> ''::text)),
    CONSTRAINT market_observations_source_url_check CHECK ((btrim(source_url) <> ''::text)),
    CONSTRAINT market_observations_trust_tier_check CHECK ((trust_tier = ANY (ARRAY['official'::text, 'exchange'::text, 'untrusted_proxy'::text]))),
    CONSTRAINT market_observations_unit_check CHECK ((btrim(unit) <> ''::text)),
    CONSTRAINT market_observations_value_numeric_check CHECK (((value_numeric <> 'NaN'::double precision) AND (value_numeric <> 'Infinity'::double precision) AND (value_numeric <> '-Infinity'::double precision)))
);


--
-- Name: market_position_facts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.market_position_facts (
    position_fact_id text NOT NULL,
    dataset_id text NOT NULL,
    contract_code text NOT NULL,
    contract_name text NOT NULL,
    report_date date NOT NULL,
    open_interest double precision NOT NULL,
    leveraged_long double precision NOT NULL,
    leveraged_short double precision NOT NULL,
    leveraged_net_pct_oi double precision NOT NULL,
    asset_manager_net_pct_oi double precision NOT NULL,
    dealer_net_pct_oi double precision NOT NULL,
    published_at_ms bigint,
    received_at_ms bigint NOT NULL,
    source_url text NOT NULL,
    fact_hash text NOT NULL,
    raw_data_json jsonb NOT NULL,
    CONSTRAINT market_position_facts_asset_manager_net_pct_oi_check CHECK (((asset_manager_net_pct_oi <> 'NaN'::double precision) AND (asset_manager_net_pct_oi <> 'Infinity'::double precision) AND (asset_manager_net_pct_oi <> '-Infinity'::double precision))),
    CONSTRAINT market_position_facts_check CHECK (((received_at_ms >= 0) AND ((published_at_ms IS NULL) OR (received_at_ms >= published_at_ms)))),
    CONSTRAINT market_position_facts_contract_code_check CHECK ((btrim(contract_code) <> ''::text)),
    CONSTRAINT market_position_facts_contract_name_check CHECK ((btrim(contract_name) <> ''::text)),
    CONSTRAINT market_position_facts_dataset_id_check CHECK ((btrim(dataset_id) <> ''::text)),
    CONSTRAINT market_position_facts_dealer_net_pct_oi_check CHECK (((dealer_net_pct_oi <> 'NaN'::double precision) AND (dealer_net_pct_oi <> 'Infinity'::double precision) AND (dealer_net_pct_oi <> '-Infinity'::double precision))),
    CONSTRAINT market_position_facts_fact_hash_check CHECK ((btrim(fact_hash) <> ''::text)),
    CONSTRAINT market_position_facts_leveraged_long_check CHECK (((leveraged_long >= (0)::double precision) AND (leveraged_long <> 'NaN'::double precision) AND (leveraged_long <> 'Infinity'::double precision))),
    CONSTRAINT market_position_facts_leveraged_net_pct_oi_check CHECK (((leveraged_net_pct_oi <> 'NaN'::double precision) AND (leveraged_net_pct_oi <> 'Infinity'::double precision) AND (leveraged_net_pct_oi <> '-Infinity'::double precision))),
    CONSTRAINT market_position_facts_leveraged_short_check CHECK (((leveraged_short >= (0)::double precision) AND (leveraged_short <> 'NaN'::double precision) AND (leveraged_short <> 'Infinity'::double precision))),
    CONSTRAINT market_position_facts_open_interest_check CHECK (((open_interest >= (0)::double precision) AND (open_interest <> 'NaN'::double precision) AND (open_interest <> 'Infinity'::double precision))),
    CONSTRAINT market_position_facts_position_fact_id_check CHECK ((btrim(position_fact_id) <> ''::text)),
    CONSTRAINT market_position_facts_published_at_ms_check CHECK (((published_at_ms IS NULL) OR (published_at_ms >= 0))),
    CONSTRAINT market_position_facts_raw_data_json_check CHECK ((jsonb_typeof(raw_data_json) = 'object'::text)),
    CONSTRAINT market_position_facts_source_url_check CHECK ((btrim(source_url) <> ''::text))
);


--
-- Name: market_settlements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.market_settlements (
    settlement_id text NOT NULL,
    instrument_id text NOT NULL,
    dataset_id text NOT NULL,
    source_id text NOT NULL,
    trade_date date NOT NULL,
    contract_code text NOT NULL,
    settlement_price double precision NOT NULL,
    open_interest double precision,
    volume double precision,
    unit text NOT NULL,
    published_at_ms bigint,
    received_at_ms bigint NOT NULL,
    source_url text NOT NULL,
    fact_hash text NOT NULL,
    raw_data_json jsonb NOT NULL,
    CONSTRAINT market_settlements_check CHECK (((received_at_ms >= 0) AND ((published_at_ms IS NULL) OR (received_at_ms >= published_at_ms)))),
    CONSTRAINT market_settlements_contract_code_check CHECK ((btrim(contract_code) <> ''::text)),
    CONSTRAINT market_settlements_dataset_id_check CHECK ((btrim(dataset_id) <> ''::text)),
    CONSTRAINT market_settlements_fact_hash_check CHECK ((btrim(fact_hash) <> ''::text)),
    CONSTRAINT market_settlements_open_interest_check CHECK (((open_interest IS NULL) OR ((open_interest <> 'NaN'::double precision) AND (open_interest <> 'Infinity'::double precision) AND (open_interest <> '-Infinity'::double precision) AND (open_interest >= (0)::double precision)))),
    CONSTRAINT market_settlements_published_at_ms_check CHECK (((published_at_ms IS NULL) OR (published_at_ms >= 0))),
    CONSTRAINT market_settlements_raw_data_json_check CHECK ((jsonb_typeof(raw_data_json) = 'object'::text)),
    CONSTRAINT market_settlements_settlement_id_check CHECK ((btrim(settlement_id) <> ''::text)),
    CONSTRAINT market_settlements_settlement_price_check CHECK (((settlement_price <> 'NaN'::double precision) AND (settlement_price <> 'Infinity'::double precision) AND (settlement_price <> '-Infinity'::double precision))),
    CONSTRAINT market_settlements_source_id_check CHECK ((btrim(source_id) <> ''::text)),
    CONSTRAINT market_settlements_source_url_check CHECK ((btrim(source_url) <> ''::text)),
    CONSTRAINT market_settlements_unit_check CHECK ((btrim(unit) <> ''::text)),
    CONSTRAINT market_settlements_volume_check CHECK (((volume IS NULL) OR ((volume <> 'NaN'::double precision) AND (volume <> 'Infinity'::double precision) AND (volume <> '-Infinity'::double precision) AND (volume >= (0)::double precision))))
);


--
-- Name: market_tick_current; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.market_tick_current (
    target_type text NOT NULL,
    target_id text NOT NULL,
    tick_observed_at_ms bigint NOT NULL,
    tick_id text NOT NULL,
    source_tier text NOT NULL,
    source_provider text NOT NULL,
    chain text,
    token_address text,
    exchange text,
    instrument text,
    pricefeed_id text,
    price_usd numeric NOT NULL,
    liquidity_usd numeric,
    volume_24h_usd numeric,
    open_interest_usd numeric,
    market_cap_usd numeric,
    holders bigint,
    updated_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT market_tick_current_price_usd_check CHECK ((price_usd > (0)::numeric)),
    CONSTRAINT market_tick_current_target_type_check CHECK ((target_type = ANY (ARRAY['chain_token'::text, 'cex_symbol'::text])))
)
WITH (fillfactor='70', autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


--
-- Name: market_ticks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.market_ticks (
    observed_at_ms bigint NOT NULL,
    tick_id text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    chain text,
    token_address text,
    exchange text,
    instrument text,
    pricefeed_id text,
    source_tier text NOT NULL,
    source_provider text NOT NULL,
    received_at_ms bigint NOT NULL,
    price_usd numeric NOT NULL,
    liquidity_usd numeric,
    volume_24h_usd numeric,
    open_interest_usd numeric,
    market_cap_usd numeric,
    holders bigint,
    raw_payload_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    payload_hash text DEFAULT ''::text NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT market_ticks_check CHECK ((((target_type = 'chain_token'::text) AND (chain IS NOT NULL) AND (token_address IS NOT NULL) AND (exchange IS NULL) AND (instrument IS NULL)) OR ((target_type = 'cex_symbol'::text) AND (exchange IS NOT NULL) AND (instrument IS NOT NULL) AND (chain IS NULL) AND (token_address IS NULL)))),
    CONSTRAINT market_ticks_price_usd_check CHECK ((price_usd > (0)::numeric)),
    CONSTRAINT market_ticks_source_provider_check CHECK ((source_provider = ANY (ARRAY['okx_dex_ws'::text, 'okx_dex_rest'::text, 'binance_cex_rest'::text, 'gmgn_dex_quote'::text]))),
    CONSTRAINT market_ticks_source_tier_check CHECK ((source_tier = ANY (ARRAY['tier1_ws'::text, 'tier2_poll'::text, 'tier3_inline'::text]))),
    CONSTRAINT market_ticks_target_type_check CHECK ((target_type = ANY (ARRAY['chain_token'::text, 'cex_symbol'::text])))
)
PARTITION BY RANGE (observed_at_ms);


--
-- Name: market_ticks_default; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.market_ticks_default (
    observed_at_ms bigint CONSTRAINT market_ticks_observed_at_ms_not_null NOT NULL,
    tick_id text CONSTRAINT market_ticks_tick_id_not_null NOT NULL,
    target_type text CONSTRAINT market_ticks_target_type_not_null NOT NULL,
    target_id text CONSTRAINT market_ticks_target_id_not_null NOT NULL,
    chain text,
    token_address text,
    exchange text,
    instrument text,
    pricefeed_id text,
    source_tier text CONSTRAINT market_ticks_source_tier_not_null NOT NULL,
    source_provider text CONSTRAINT market_ticks_source_provider_not_null NOT NULL,
    received_at_ms bigint CONSTRAINT market_ticks_received_at_ms_not_null NOT NULL,
    price_usd numeric CONSTRAINT market_ticks_price_usd_not_null NOT NULL,
    liquidity_usd numeric,
    volume_24h_usd numeric,
    open_interest_usd numeric,
    market_cap_usd numeric,
    holders bigint,
    raw_payload_json jsonb DEFAULT '{}'::jsonb CONSTRAINT market_ticks_raw_payload_json_not_null NOT NULL,
    payload_hash text DEFAULT ''::text CONSTRAINT market_ticks_payload_hash_not_null NOT NULL,
    created_at_ms bigint CONSTRAINT market_ticks_created_at_ms_not_null NOT NULL,
    CONSTRAINT market_ticks_check CHECK ((((target_type = 'chain_token'::text) AND (chain IS NOT NULL) AND (token_address IS NOT NULL) AND (exchange IS NULL) AND (instrument IS NULL)) OR ((target_type = 'cex_symbol'::text) AND (exchange IS NOT NULL) AND (instrument IS NOT NULL) AND (chain IS NULL) AND (token_address IS NULL)))),
    CONSTRAINT market_ticks_price_usd_check CHECK ((price_usd > (0)::numeric)),
    CONSTRAINT market_ticks_source_provider_check CHECK ((source_provider = ANY (ARRAY['okx_dex_ws'::text, 'okx_dex_rest'::text, 'binance_cex_rest'::text, 'gmgn_dex_quote'::text]))),
    CONSTRAINT market_ticks_source_tier_check CHECK ((source_tier = ANY (ARRAY['tier1_ws'::text, 'tier2_poll'::text, 'tier3_inline'::text]))),
    CONSTRAINT market_ticks_target_type_check CHECK ((target_type = ANY (ARRAY['chain_token'::text, 'cex_symbol'::text])))
);


--
-- Name: news_brief_current; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_brief_current (
    singleton_key boolean DEFAULT true NOT NULL,
    publication_id text,
    target_fingerprint text,
    latest_run_id text,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_brief_current_singleton_key_check CHECK (singleton_key),
    CONSTRAINT news_brief_current_updated_at_ms_check CHECK ((updated_at_ms >= 0))
);


--
-- Name: news_brief_publications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_brief_publications (
    publication_id text NOT NULL,
    fingerprint text NOT NULL,
    evidence_cutoff_at_ms bigint NOT NULL,
    published_at_ms bigint NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    prompt_version text NOT NULL,
    workflow_version text NOT NULL,
    schema_version text NOT NULL,
    locale text NOT NULL,
    selected_story_ids jsonb NOT NULL,
    lead text NOT NULL,
    lines jsonb NOT NULL,
    sources jsonb NOT NULL,
    validation jsonb NOT NULL,
    raw_response text NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_brief_publications_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT news_brief_publications_evidence_cutoff_at_ms_check CHECK ((evidence_cutoff_at_ms >= 0)),
    CONSTRAINT news_brief_publications_fingerprint_check CHECK ((btrim(fingerprint) <> ''::text)),
    CONSTRAINT news_brief_publications_lead_check CHECK ((btrim(lead) <> ''::text)),
    CONSTRAINT news_brief_publications_lines_check CHECK ((jsonb_typeof(lines) = 'array'::text)),
    CONSTRAINT news_brief_publications_locale_check CHECK ((btrim(locale) <> ''::text)),
    CONSTRAINT news_brief_publications_model_check CHECK ((btrim(model) <> ''::text)),
    CONSTRAINT news_brief_publications_prompt_version_check CHECK ((btrim(prompt_version) <> ''::text)),
    CONSTRAINT news_brief_publications_provider_check CHECK ((btrim(provider) <> ''::text)),
    CONSTRAINT news_brief_publications_published_at_ms_check CHECK ((published_at_ms >= 0)),
    CONSTRAINT news_brief_publications_schema_version_check CHECK ((btrim(schema_version) <> ''::text)),
    CONSTRAINT news_brief_publications_selected_story_ids_check CHECK ((jsonb_typeof(selected_story_ids) = 'array'::text)),
    CONSTRAINT news_brief_publications_sources_check CHECK ((jsonb_typeof(sources) = 'array'::text)),
    CONSTRAINT news_brief_publications_validation_check CHECK ((jsonb_typeof(validation) = 'object'::text)),
    CONSTRAINT news_brief_publications_workflow_version_check CHECK ((btrim(workflow_version) <> ''::text))
);


--
-- Name: news_brief_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_brief_runs (
    run_id text NOT NULL,
    fingerprint text NOT NULL,
    status text NOT NULL,
    attempt_count integer NOT NULL,
    candidate_story_count integer NOT NULL,
    candidate_source_count integer NOT NULL,
    lease_owner text,
    lease_expires_at_ms bigint,
    heartbeat_at_ms bigint,
    last_error text,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    completed_at_ms bigint,
    CONSTRAINT news_brief_runs_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT news_brief_runs_candidate_source_count_check CHECK ((candidate_source_count >= 0)),
    CONSTRAINT news_brief_runs_candidate_story_count_check CHECK ((candidate_story_count >= 0)),
    CONSTRAINT news_brief_runs_check CHECK ((updated_at_ms >= created_at_ms)),
    CONSTRAINT news_brief_runs_check1 CHECK ((((status = 'running'::text) AND (lease_owner IS NOT NULL) AND (lease_expires_at_ms IS NOT NULL) AND (heartbeat_at_ms IS NOT NULL)) OR ((status <> 'running'::text) AND (lease_owner IS NULL) AND (lease_expires_at_ms IS NULL)))),
    CONSTRAINT news_brief_runs_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT news_brief_runs_fingerprint_check CHECK ((btrim(fingerprint) <> ''::text)),
    CONSTRAINT news_brief_runs_status_check CHECK ((status = ANY (ARRAY['running'::text, 'ready'::text, 'insufficient_material'::text, 'failed'::text])))
);


--
-- Name: news_feed_observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_feed_observations (
    observation_id text NOT NULL,
    fetch_id text NOT NULL,
    source_id text NOT NULL,
    source_item_key text NOT NULL,
    observed_at_ms bigint NOT NULL,
    title text,
    url text,
    published_at_ms bigint,
    raw jsonb NOT NULL,
    admitted boolean NOT NULL,
    rejection_reason text,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_feed_observations_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT news_feed_observations_observed_at_ms_check CHECK ((observed_at_ms >= 0)),
    CONSTRAINT news_feed_observations_raw_check CHECK ((jsonb_typeof(raw) = 'object'::text)),
    CONSTRAINT news_feed_observations_source_item_key_check CHECK ((btrim(source_item_key) <> ''::text))
);


--
-- Name: news_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_items (
    item_id text NOT NULL,
    source_id text NOT NULL,
    source_item_key text NOT NULL,
    canonical_url text NOT NULL,
    reporting_origin text NOT NULL,
    title text NOT NULL,
    normalized_title text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    lang text NOT NULL,
    published_at_ms bigint NOT NULL,
    first_observed_at_ms bigint NOT NULL,
    last_observed_at_ms bigint NOT NULL,
    content_fingerprint text NOT NULL,
    level text NOT NULL,
    category text NOT NULL,
    classification_source text NOT NULL,
    classification_confidence double precision NOT NULL,
    importance_score integer DEFAULT 0 NOT NULL,
    importance_factors jsonb DEFAULT '{}'::jsonb NOT NULL,
    brief_excluded boolean DEFAULT false NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_items_canonical_url_check CHECK ((canonical_url ~ '^https?://'::text)),
    CONSTRAINT news_items_category_check CHECK ((category = ANY (ARRAY['conflict'::text, 'protest'::text, 'disaster'::text, 'diplomatic'::text, 'economic'::text, 'terrorism'::text, 'cyber'::text, 'health'::text, 'environmental'::text, 'military'::text, 'crime'::text, 'infrastructure'::text, 'tech'::text, 'general'::text]))),
    CONSTRAINT news_items_check CHECK ((last_observed_at_ms >= first_observed_at_ms)),
    CONSTRAINT news_items_classification_confidence_check CHECK (((classification_confidence >= (0)::double precision) AND (classification_confidence <= (1)::double precision))),
    CONSTRAINT news_items_classification_source_check CHECK ((classification_source = ANY (ARRAY['keyword'::text, 'keyword-historical-downgrade'::text]))),
    CONSTRAINT news_items_content_fingerprint_check CHECK ((btrim(content_fingerprint) <> ''::text)),
    CONSTRAINT news_items_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT news_items_first_observed_at_ms_check CHECK ((first_observed_at_ms >= 0)),
    CONSTRAINT news_items_importance_factors_check CHECK ((jsonb_typeof(importance_factors) = 'object'::text)),
    CONSTRAINT news_items_importance_score_check CHECK ((importance_score >= 0)),
    CONSTRAINT news_items_lang_check CHECK ((btrim(lang) <> ''::text)),
    CONSTRAINT news_items_level_check CHECK ((level = ANY (ARRAY['critical'::text, 'high'::text, 'medium'::text, 'low'::text, 'info'::text]))),
    CONSTRAINT news_items_normalized_title_check CHECK ((btrim(normalized_title) <> ''::text)),
    CONSTRAINT news_items_published_at_ms_check CHECK ((published_at_ms >= 0)),
    CONSTRAINT news_items_reporting_origin_check CHECK ((btrim(reporting_origin) <> ''::text)),
    CONSTRAINT news_items_source_item_key_check CHECK ((btrim(source_item_key) <> ''::text)),
    CONSTRAINT news_items_title_check CHECK ((btrim(title) <> ''::text)),
    CONSTRAINT news_items_updated_at_ms_check CHECK ((updated_at_ms >= 0))
);


--
-- Name: news_source_fetches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_source_fetches (
    fetch_id text NOT NULL,
    source_id text NOT NULL,
    started_at_ms bigint NOT NULL,
    finished_at_ms bigint NOT NULL,
    status text NOT NULL,
    fetch_path text,
    direct_error_code text,
    http_status integer,
    entries_seen integer DEFAULT 0 NOT NULL,
    observations_inserted integer DEFAULT 0 NOT NULL,
    items_inserted integer DEFAULT 0 NOT NULL,
    items_updated integer DEFAULT 0 NOT NULL,
    rejection_counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_code text,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_source_fetches_check CHECK ((finished_at_ms >= started_at_ms)),
    CONSTRAINT news_source_fetches_check1 CHECK (((status = 'failed'::text) OR (fetch_path IS NOT NULL))),
    CONSTRAINT news_source_fetches_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT news_source_fetches_entries_seen_check CHECK ((entries_seen >= 0)),
    CONSTRAINT news_source_fetches_fetch_path_check CHECK ((fetch_path = ANY (ARRAY['direct'::text, 'relay'::text]))),
    CONSTRAINT news_source_fetches_items_inserted_check CHECK ((items_inserted >= 0)),
    CONSTRAINT news_source_fetches_items_updated_check CHECK ((items_updated >= 0)),
    CONSTRAINT news_source_fetches_observations_inserted_check CHECK ((observations_inserted >= 0)),
    CONSTRAINT news_source_fetches_rejection_counts_check CHECK ((jsonb_typeof(rejection_counts) = 'object'::text)),
    CONSTRAINT news_source_fetches_started_at_ms_check CHECK ((started_at_ms >= 0)),
    CONSTRAINT news_source_fetches_status_check CHECK ((status = ANY (ARRAY['success'::text, 'not_modified'::text, 'failed'::text])))
);


--
-- Name: news_source_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_source_memberships (
    source_id text NOT NULL,
    membership text NOT NULL,
    CONSTRAINT news_source_memberships_membership_check CHECK ((btrim(membership) <> ''::text))
);


--
-- Name: news_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_sources (
    source_id text NOT NULL,
    name text NOT NULL,
    feed_url text NOT NULL,
    tier smallint NOT NULL,
    lang text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    refresh_interval_seconds integer NOT NULL,
    etag text,
    last_modified text,
    last_fetch_started_at_ms bigint,
    last_fetch_finished_at_ms bigint,
    last_success_at_ms bigint,
    last_http_status integer,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    last_error text,
    next_fetch_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_sources_consecutive_failures_check CHECK ((consecutive_failures >= 0)),
    CONSTRAINT news_sources_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT news_sources_feed_url_check CHECK ((feed_url ~ '^https?://'::text)),
    CONSTRAINT news_sources_lang_check CHECK ((btrim(lang) <> ''::text)),
    CONSTRAINT news_sources_name_check CHECK ((btrim(name) <> ''::text)),
    CONSTRAINT news_sources_next_fetch_at_ms_check CHECK ((next_fetch_at_ms >= 0)),
    CONSTRAINT news_sources_refresh_interval_seconds_check CHECK ((refresh_interval_seconds >= 1)),
    CONSTRAINT news_sources_tier_check CHECK (((tier >= 1) AND (tier <= 4))),
    CONSTRAINT news_sources_updated_at_ms_check CHECK ((updated_at_ms >= 0))
);


--
-- Name: news_stories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_stories (
    story_id text NOT NULL,
    canonical_key text NOT NULL,
    canonical_title text NOT NULL,
    representative_item_id text NOT NULL,
    representative_source_id text NOT NULL,
    representative_title text NOT NULL,
    representative_url text NOT NULL,
    representative_description text DEFAULT ''::text NOT NULL,
    scoring_item_id text NOT NULL,
    level text NOT NULL,
    category text NOT NULL,
    importance_score integer NOT NULL,
    importance_factors jsonb NOT NULL,
    item_count integer NOT NULL,
    source_count integer NOT NULL,
    first_published_at_ms bigint NOT NULL,
    last_published_at_ms bigint NOT NULL,
    active boolean DEFAULT true NOT NULL,
    state_fingerprint text NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_stories_canonical_key_check CHECK ((btrim(canonical_key) <> ''::text)),
    CONSTRAINT news_stories_canonical_title_check CHECK ((btrim(canonical_title) <> ''::text)),
    CONSTRAINT news_stories_category_check CHECK ((category = ANY (ARRAY['conflict'::text, 'protest'::text, 'disaster'::text, 'diplomatic'::text, 'economic'::text, 'terrorism'::text, 'cyber'::text, 'health'::text, 'environmental'::text, 'military'::text, 'crime'::text, 'infrastructure'::text, 'tech'::text, 'general'::text]))),
    CONSTRAINT news_stories_check CHECK ((last_published_at_ms >= first_published_at_ms)),
    CONSTRAINT news_stories_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT news_stories_first_published_at_ms_check CHECK ((first_published_at_ms >= 0)),
    CONSTRAINT news_stories_importance_factors_check CHECK ((jsonb_typeof(importance_factors) = 'object'::text)),
    CONSTRAINT news_stories_importance_score_check CHECK ((importance_score >= 0)),
    CONSTRAINT news_stories_item_count_check CHECK ((item_count >= 1)),
    CONSTRAINT news_stories_level_check CHECK ((level = ANY (ARRAY['critical'::text, 'high'::text, 'medium'::text, 'low'::text, 'info'::text]))),
    CONSTRAINT news_stories_representative_title_check CHECK ((btrim(representative_title) <> ''::text)),
    CONSTRAINT news_stories_representative_url_check CHECK ((representative_url ~ '^https?://'::text)),
    CONSTRAINT news_stories_source_count_check CHECK ((source_count >= 1)),
    CONSTRAINT news_stories_state_fingerprint_check CHECK ((btrim(state_fingerprint) <> ''::text)),
    CONSTRAINT news_stories_updated_at_ms_check CHECK ((updated_at_ms >= 0))
);


--
-- Name: news_story_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_story_aliases (
    alias_key text NOT NULL,
    story_id text NOT NULL,
    expires_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_story_aliases_created_at_ms_check CHECK ((created_at_ms >= 0)),
    CONSTRAINT news_story_aliases_expires_at_ms_check CHECK ((expires_at_ms >= 0))
);


--
-- Name: news_story_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_story_members (
    story_id text NOT NULL,
    item_id text NOT NULL,
    current boolean DEFAULT true NOT NULL,
    first_joined_at_ms bigint NOT NULL,
    last_confirmed_at_ms bigint NOT NULL,
    CONSTRAINT news_story_members_check CHECK ((last_confirmed_at_ms >= first_joined_at_ms)),
    CONSTRAINT news_story_members_first_joined_at_ms_check CHECK ((first_joined_at_ms >= 0))
);


--
-- Name: price_feeds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.price_feeds (
    pricefeed_id text NOT NULL,
    feed_type text NOT NULL,
    provider text NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    chain_id text,
    address text,
    native_market_id text,
    base_asset_id text,
    base_cex_token_id text,
    base_symbol text,
    quote_symbol text,
    multiplier numeric,
    status text NOT NULL,
    evidence_level text NOT NULL,
    first_seen_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT ck_price_feeds_evm_address_canonical CHECK (((address IS NULL) OR (chain_id !~~ 'eip155:%'::text) OR (address = lower(address))))
);


--
-- Name: raw_frames; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.raw_frames (
    frame_id text NOT NULL,
    source text NOT NULL,
    channel text NOT NULL,
    received_at_ms bigint NOT NULL,
    payload_hash text NOT NULL,
    raw_payload_json text NOT NULL,
    created_at_ms bigint NOT NULL
);


--
-- Name: registry_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_assets (
    asset_id text NOT NULL,
    chain_id text NOT NULL,
    token_standard text NOT NULL,
    address text NOT NULL,
    status text NOT NULL,
    first_seen_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT ck_registry_assets_evm_address_canonical CHECK (((chain_id !~~ 'eip155:%'::text) OR (address = lower(address))))
);


--
-- Name: token_discovery_dirty_lookup_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_discovery_dirty_lookup_keys (
    provider text NOT NULL,
    lookup_key text NOT NULL,
    lookup_type text NOT NULL,
    dirty_reason text NOT NULL,
    payload_hash text NOT NULL,
    due_at_ms bigint NOT NULL,
    latest_seen_ms bigint DEFAULT 0 NOT NULL,
    intent_count bigint DEFAULT 0 NOT NULL,
    refresh_priority integer DEFAULT 9 NOT NULL,
    leased_until_ms bigint,
    lease_owner text,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_error text,
    first_dirty_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT token_discovery_dirty_lookup_keys_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT token_discovery_dirty_lookup_keys_lookup_type_check CHECK ((lookup_type = ANY (ARRAY['dex_symbol_lookup'::text, 'address_lookup'::text])))
)
WITH (fillfactor='70', autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


--
-- Name: token_discovery_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_discovery_results (
    provider text NOT NULL,
    lookup_key text NOT NULL,
    lookup_type text NOT NULL,
    status text NOT NULL,
    candidate_count integer DEFAULT 0 NOT NULL,
    candidate_ids_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    result_hash text,
    last_lookup_at_ms bigint,
    next_refresh_at_ms bigint DEFAULT 0 NOT NULL,
    last_error text,
    error_count integer DEFAULT 0 NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT ck_token_discovery_results_status CHECK ((status = ANY (ARRAY['running'::text, 'found'::text, 'not_found'::text, 'error'::text])))
);


--
-- Name: token_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_evidence (
    evidence_id text NOT NULL,
    event_id text NOT NULL,
    source_kind text NOT NULL,
    source_id text NOT NULL,
    evidence_type text NOT NULL,
    raw_value text NOT NULL,
    normalized_symbol text,
    chain_hint text,
    address_hint text,
    provider text,
    provider_ref text,
    text_surface text NOT NULL,
    span_start bigint NOT NULL,
    span_end bigint NOT NULL,
    sentence_id bigint NOT NULL,
    local_group_key text NOT NULL,
    strength text NOT NULL,
    confidence double precision NOT NULL,
    created_at_ms bigint NOT NULL
);


--
-- Name: token_image_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_image_assets (
    image_id text NOT NULL,
    source_url text NOT NULL,
    source_url_hash text NOT NULL,
    source_provider text NOT NULL,
    source_kind text NOT NULL,
    status text NOT NULL,
    media_type text,
    file_extension text,
    content_sha256 text,
    byte_size bigint,
    storage_path text,
    public_url text,
    raw_ref_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    failure_count bigint DEFAULT 0 NOT NULL,
    last_error text,
    observed_at_ms bigint,
    next_refresh_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT token_image_assets_check CHECK (((status <> 'ready'::text) OR ((media_type = ANY (ARRAY['image/gif'::text, 'image/jpeg'::text, 'image/png'::text, 'image/webp'::text])) AND (file_extension = ANY (ARRAY['.gif'::text, '.jpg'::text, '.png'::text, '.webp'::text])) AND (content_sha256 IS NOT NULL) AND (byte_size IS NOT NULL) AND (byte_size > 0) AND (storage_path IS NOT NULL) AND (public_url IS NOT NULL) AND (public_url ~~ '/api/token-images/%'::text)))),
    CONSTRAINT token_image_assets_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'ready'::text, 'error'::text, 'unsupported'::text])))
);


--
-- Name: token_image_source_dirty_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_image_source_dirty_targets (
    source_url_hash text NOT NULL,
    source_url text NOT NULL,
    source_provider text NOT NULL,
    source_kind text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    raw_ref_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    dirty_reason text NOT NULL,
    payload_hash text NOT NULL,
    source_watermark_ms bigint DEFAULT 0 NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    due_at_ms bigint NOT NULL,
    leased_until_ms bigint,
    lease_owner text,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_error text,
    first_dirty_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT token_image_source_dirty_targets_attempt_count_check CHECK ((attempt_count >= 0))
)
WITH (fillfactor='70', autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


--
-- Name: token_intent_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_intent_evidence (
    intent_id text NOT NULL,
    evidence_id text NOT NULL,
    role text NOT NULL
);


--
-- Name: token_intent_lookup_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_intent_lookup_keys (
    lookup_key text NOT NULL,
    intent_id text NOT NULL,
    event_id text NOT NULL,
    source_evidence_id text,
    created_at_ms bigint NOT NULL
);


--
-- Name: token_intent_resolutions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_intent_resolutions (
    resolution_id text NOT NULL,
    intent_id text NOT NULL,
    event_id text NOT NULL,
    resolution_status text NOT NULL,
    identity_status text,
    confidence double precision,
    resolver_policy_version text,
    reasons_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    risks_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    decision_time_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    target_type text,
    target_id text,
    pricefeed_id text,
    reason_codes_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    candidate_ids_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    lookup_keys_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    registry_version text,
    record_status text DEFAULT 'current'::text NOT NULL,
    is_current boolean DEFAULT true NOT NULL,
    superseded_at_ms bigint
);


--
-- Name: token_intents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_intents (
    intent_id text NOT NULL,
    event_id text NOT NULL,
    intent_key text NOT NULL,
    construction_policy text NOT NULL,
    primary_evidence_id text,
    display_symbol text,
    display_name text,
    chain_hint text,
    address_hint text,
    intent_status text NOT NULL,
    intent_confidence double precision NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL
);


--
-- Name: token_profile_current; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_profile_current (
    target_type text NOT NULL,
    target_id text NOT NULL,
    status text NOT NULL,
    profile_provider text,
    source_kind text NOT NULL,
    source_ref text,
    symbol text,
    name text,
    logo_url text,
    banner_url text,
    website_url text,
    twitter_username text,
    twitter_url text,
    telegram_url text,
    gmgn_url text,
    geckoterminal_url text,
    description text,
    quality_flags_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    source_payload_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    observed_at_ms bigint,
    computed_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    logo_image_id text,
    logo_source_provider text,
    logo_source_url_hash text,
    payload_hash text NOT NULL,
    CONSTRAINT token_profile_current_local_logo_url_check CHECK (((logo_url IS NULL) OR (logo_url ~~ '/api/token-images/%'::text))),
    CONSTRAINT token_profile_current_status_check CHECK ((status = ANY (ARRAY['ready'::text, 'missing'::text, 'unsupported'::text, 'error'::text])))
);


--
-- Name: token_profile_current_dirty_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_profile_current_dirty_targets (
    target_type text NOT NULL,
    target_id text NOT NULL,
    dirty_reason text NOT NULL,
    payload_hash text NOT NULL,
    source_watermark_ms bigint CONSTRAINT token_profile_current_dirty_target_source_watermark_ms_not_null NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    due_at_ms bigint NOT NULL,
    leased_until_ms bigint,
    lease_owner text,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_error text,
    first_dirty_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT ck_token_profile_current_dirty_source_watermark_positive CHECK ((source_watermark_ms > 0)),
    CONSTRAINT token_profile_current_dirty_targets_attempt_count_check CHECK ((attempt_count >= 0))
)
WITH (fillfactor='70', autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


--
-- Name: token_radar_current_rows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_radar_current_rows (
    row_id text NOT NULL,
    projection_version text NOT NULL,
    "window" text NOT NULL,
    lane text NOT NULL,
    target_type_key text NOT NULL,
    identity_id text NOT NULL,
    computed_at_ms bigint NOT NULL,
    source_max_received_at_ms bigint NOT NULL,
    rank bigint NOT NULL,
    rank_score double precision NOT NULL,
    intent_id text,
    event_id text,
    target_type text,
    target_id text,
    pricefeed_id text,
    intent_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolution_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    factor_snapshot_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    factor_version text NOT NULL,
    decision text NOT NULL,
    data_health_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_event_ids_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    payload_hash text NOT NULL,
    listed_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    generation_id text NOT NULL,
    published_at_ms bigint NOT NULL,
    source_frontier_ms bigint NOT NULL,
    quality_status text NOT NULL,
    degraded_reasons_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    venue text DEFAULT 'all'::text NOT NULL,
    CONSTRAINT ck_token_radar_current_rows_quality_status CHECK ((quality_status = ANY (ARRAY['ready'::text, 'degraded'::text, 'insufficient'::text, 'failed'::text])))
)
WITH (fillfactor='70', autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


--
-- Name: token_radar_dirty_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_radar_dirty_targets (
    target_type_key text NOT NULL,
    identity_id text NOT NULL,
    dirty_reason text NOT NULL,
    payload_hash text NOT NULL,
    due_at_ms bigint NOT NULL,
    leased_until_ms bigint,
    lease_owner text,
    attempt_count bigint DEFAULT 0 NOT NULL,
    last_error text,
    first_dirty_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    market_dirty boolean DEFAULT false NOT NULL,
    repair_dirty boolean DEFAULT false NOT NULL,
    CONSTRAINT token_radar_dirty_targets_attempt_count_check CHECK ((attempt_count >= 0))
)
WITH (fillfactor='70', autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


--
-- Name: token_radar_publication_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_radar_publication_state (
    projection_version text NOT NULL,
    "window" text NOT NULL,
    venue text DEFAULT 'all'::text NOT NULL,
    current_generation_id text,
    current_published_at_ms bigint,
    current_source_frontier_ms bigint,
    current_row_count bigint DEFAULT 0 NOT NULL,
    current_source_rows bigint DEFAULT 0 NOT NULL,
    latest_attempt_generation_id text,
    latest_attempt_status text NOT NULL,
    latest_attempt_started_at_ms bigint,
    latest_attempt_finished_at_ms bigint,
    latest_attempt_error text,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT token_radar_publication_state_check CHECK (((latest_attempt_status = 'failed'::text) OR (current_generation_id = latest_attempt_generation_id))),
    CONSTRAINT token_radar_publication_state_latest_attempt_status_check CHECK ((latest_attempt_status = ANY (ARRAY['ready'::text, 'failed'::text])))
);


--
-- Name: token_radar_rank_source_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_radar_rank_source_events (
    projection_version text NOT NULL,
    target_type_key text NOT NULL,
    identity_id text NOT NULL,
    source_kind text NOT NULL,
    source_id text NOT NULL,
    event_received_at_ms bigint NOT NULL,
    projected_at_ms bigint NOT NULL,
    source_payload_hash text NOT NULL,
    intent_id text,
    event_id text,
    resolution_id text,
    target_type text,
    target_id text,
    pricefeed_id text,
    resolution_status text
);


--
-- Name: token_radar_target_features; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_radar_target_features (
    projection_version text NOT NULL,
    "window" text NOT NULL,
    lane text NOT NULL,
    target_type_key text NOT NULL,
    identity_id text NOT NULL,
    target_type text,
    target_id text,
    pricefeed_id text,
    latest_event_received_at_ms bigint CONSTRAINT token_radar_target_features_latest_event_received_at_m_not_null NOT NULL,
    latest_market_observed_at_ms bigint,
    attention_score double precision DEFAULT 0 NOT NULL,
    market_score double precision DEFAULT 0 NOT NULL,
    credibility_score double precision DEFAULT 0 NOT NULL,
    rank_score double precision DEFAULT 0 NOT NULL,
    factor_snapshot_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_event_ids_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    source_intent_ids_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    source_resolution_ids_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    payload_hash text NOT NULL,
    last_scored_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    social_heat_raw_score double precision,
    social_heat_weight double precision DEFAULT 0 NOT NULL,
    social_propagation_raw_score double precision,
    social_propagation_weight double precision DEFAULT 0 NOT NULL,
    timing_risk_raw_score double precision,
    timing_risk_weight double precision DEFAULT 0 NOT NULL,
    cohort_high_confidence_mentions integer DEFAULT 0 CONSTRAINT token_radar_target_features_cohort_high_confidence_men_not_null NOT NULL,
    cohort_kol_mentions integer DEFAULT 0 NOT NULL,
    cohort_followup_authors integer DEFAULT 0 CONSTRAINT token_radar_target_features_cohort_public_followup_aut_not_null NOT NULL,
    cohort_first_seen_global_24h boolean DEFAULT false CONSTRAINT token_radar_target_features_cohort_first_seen_global_2_not_null NOT NULL,
    cohort_symbol text DEFAULT ''::text NOT NULL,
    social_heat_mentions_1h integer DEFAULT 0 NOT NULL,
    social_propagation_mentions integer DEFAULT 0 CONSTRAINT token_radar_target_features_social_propagation_mention_not_null NOT NULL,
    social_heat_latest_seen_ms bigint,
    raw_composite_score double precision,
    recommended_decision text DEFAULT 'discard'::text NOT NULL,
    gates_max_decision text DEFAULT 'discard'::text NOT NULL,
    intent_json jsonb NOT NULL,
    resolution_json jsonb NOT NULL
);


--
-- Name: token_radar_target_first_seen; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_radar_target_first_seen (
    projection_version text NOT NULL,
    "window" text NOT NULL,
    target_type_key text NOT NULL,
    identity_id text NOT NULL,
    first_seen_ms bigint NOT NULL,
    last_seen_ms bigint NOT NULL,
    first_row_id text,
    latest_row_id text,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    venue text DEFAULT 'all'::text NOT NULL
);


--
-- Name: us_equity_symbols; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.us_equity_symbols (
    symbol text NOT NULL,
    market_instrument_id text NOT NULL,
    exchange text,
    security_name text,
    instrument_type text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    source text NOT NULL,
    source_updated_at_ms bigint NOT NULL,
    raw_payload_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL
);


--
-- Name: worker_queue_terminal_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.worker_queue_terminal_events (
    terminal_id text NOT NULL,
    worker_name text NOT NULL,
    source_table text NOT NULL,
    target_key text NOT NULL,
    source_row_json jsonb NOT NULL,
    source_row_hash text NOT NULL,
    final_status text NOT NULL,
    final_reason text NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    payload_hash text DEFAULT ''::text NOT NULL,
    first_seen_at_ms bigint,
    last_attempted_at_ms bigint,
    terminalized_at_ms bigint NOT NULL,
    terminal_generation integer DEFAULT 1 NOT NULL,
    operator_action text,
    operator_reason text,
    operator_action_at_ms bigint,
    final_reason_bucket text DEFAULT 'other'::text NOT NULL
);


--
-- Name: market_ticks_default; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_ticks ATTACH PARTITION public.market_ticks_default DEFAULT;


--
-- Name: asset_identity_current asset_identity_current_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_identity_current
    ADD CONSTRAINT asset_identity_current_pkey PRIMARY KEY (asset_id);


--
-- Name: asset_identity_evidence asset_identity_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_identity_evidence
    ADD CONSTRAINT asset_identity_evidence_pkey PRIMARY KEY (evidence_id);


--
-- Name: asset_profile_refresh_targets asset_profile_refresh_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_profile_refresh_targets
    ADD CONSTRAINT asset_profile_refresh_targets_pkey PRIMARY KEY (provider, target_type, target_id);


--
-- Name: asset_profiles asset_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_profiles
    ADD CONSTRAINT asset_profiles_pkey PRIMARY KEY (asset_id, provider);


--
-- Name: cex_token_profiles cex_token_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cex_token_profiles
    ADD CONSTRAINT cex_token_profiles_pkey PRIMARY KEY (cex_token_id, provider);


--
-- Name: cex_tokens cex_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cex_tokens
    ADD CONSTRAINT cex_tokens_pkey PRIMARY KEY (cex_token_id);


--
-- Name: checkpoint_blobs checkpoint_blobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint_blobs
    ADD CONSTRAINT checkpoint_blobs_pkey PRIMARY KEY (thread_id, checkpoint_ns, channel, version);


--
-- Name: checkpoint_migrations checkpoint_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint_migrations
    ADD CONSTRAINT checkpoint_migrations_pkey PRIMARY KEY (v);


--
-- Name: checkpoint_writes checkpoint_writes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint_writes
    ADD CONSTRAINT checkpoint_writes_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx);


--
-- Name: checkpoints checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id);


--
-- Name: collector_pending_items collector_pending_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collector_pending_items
    ADD CONSTRAINT collector_pending_items_pkey PRIMARY KEY (source, channel, item_key);


--
-- Name: enriched_events enriched_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enriched_events
    ADD CONSTRAINT enriched_events_pkey PRIMARY KEY (event_id, intent_id);


--
-- Name: event_anchor_backfill_jobs event_anchor_backfill_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_anchor_backfill_jobs
    ADD CONSTRAINT event_anchor_backfill_jobs_pkey PRIMARY KEY (event_id, intent_id);


--
-- Name: event_entities event_entities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_entities
    ADD CONSTRAINT event_entities_pkey PRIMARY KEY (entity_id);


--
-- Name: events events_logical_dedup_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_logical_dedup_key_key UNIQUE (logical_dedup_key);


--
-- Name: events events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (event_id);


--
-- Name: macro_acquisition_targets macro_acquisition_targets_dataset_id_partition_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_acquisition_targets
    ADD CONSTRAINT macro_acquisition_targets_dataset_id_partition_key_key UNIQUE (dataset_id, partition_key);


--
-- Name: macro_acquisition_targets macro_acquisition_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_acquisition_targets
    ADD CONSTRAINT macro_acquisition_targets_pkey PRIMARY KEY (target_key);


--
-- Name: macro_daily_judgments_v1_archive macro_daily_judgments_payload_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_daily_judgments_v1_archive
    ADD CONSTRAINT macro_daily_judgments_payload_hash_key UNIQUE (payload_hash);


--
-- Name: macro_daily_judgments_v1_archive macro_daily_judgments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_daily_judgments_v1_archive
    ADD CONSTRAINT macro_daily_judgments_pkey PRIMARY KEY (session_date);


--
-- Name: macro_daily_judgments macro_daily_judgments_v2_payload_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_daily_judgments
    ADD CONSTRAINT macro_daily_judgments_v2_payload_hash_key UNIQUE (payload_hash);


--
-- Name: macro_daily_judgments macro_daily_judgments_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_daily_judgments
    ADD CONSTRAINT macro_daily_judgments_v2_pkey PRIMARY KEY (session_date);


--
-- Name: macro_document_analyses macro_document_analyses_document_id_document_hash_model_nam_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_document_analyses
    ADD CONSTRAINT macro_document_analyses_document_id_document_hash_model_nam_key UNIQUE (document_id, document_hash, model_name, prompt_version);


--
-- Name: macro_document_analyses macro_document_analyses_payload_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_document_analyses
    ADD CONSTRAINT macro_document_analyses_payload_hash_key UNIQUE (payload_hash);


--
-- Name: macro_document_analyses macro_document_analyses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_document_analyses
    ADD CONSTRAINT macro_document_analyses_pkey PRIMARY KEY (analysis_id);


--
-- Name: macro_document_analysis_jobs macro_document_analysis_jobs_document_id_document_hash_mode_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_document_analysis_jobs
    ADD CONSTRAINT macro_document_analysis_jobs_document_id_document_hash_mode_key UNIQUE (document_id, document_hash, model_name, prompt_version);


--
-- Name: macro_document_analysis_jobs macro_document_analysis_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_document_analysis_jobs
    ADD CONSTRAINT macro_document_analysis_jobs_pkey PRIMARY KEY (analysis_job_id);


--
-- Name: macro_documents macro_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_documents
    ADD CONSTRAINT macro_documents_pkey PRIMARY KEY (document_id);


--
-- Name: macro_event_updates_v1_archive macro_event_updates_payload_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_event_updates_v1_archive
    ADD CONSTRAINT macro_event_updates_payload_hash_key UNIQUE (payload_hash);


--
-- Name: macro_event_updates_v1_archive macro_event_updates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_event_updates_v1_archive
    ADD CONSTRAINT macro_event_updates_pkey PRIMARY KEY (event_update_id);


--
-- Name: macro_event_updates macro_event_updates_v2_payload_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_event_updates
    ADD CONSTRAINT macro_event_updates_v2_payload_hash_key UNIQUE (payload_hash);


--
-- Name: macro_event_updates macro_event_updates_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_event_updates
    ADD CONSTRAINT macro_event_updates_v2_pkey PRIMARY KEY (event_update_id);


--
-- Name: macro_evidence_packs_v1_archive macro_evidence_packs_payload_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_evidence_packs_v1_archive
    ADD CONSTRAINT macro_evidence_packs_payload_hash_key UNIQUE (payload_hash);


--
-- Name: macro_evidence_packs_v1_archive macro_evidence_packs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_evidence_packs_v1_archive
    ADD CONSTRAINT macro_evidence_packs_pkey PRIMARY KEY (evidence_pack_id);


--
-- Name: macro_evidence_packs_v1_archive macro_evidence_packs_session_date_judgment_cutoff_ms_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_evidence_packs_v1_archive
    ADD CONSTRAINT macro_evidence_packs_session_date_judgment_cutoff_ms_key UNIQUE (session_date, judgment_cutoff_ms);


--
-- Name: macro_evidence_packs macro_evidence_packs_v2_payload_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_evidence_packs
    ADD CONSTRAINT macro_evidence_packs_v2_payload_hash_key UNIQUE (payload_hash);


--
-- Name: macro_evidence_packs macro_evidence_packs_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_evidence_packs
    ADD CONSTRAINT macro_evidence_packs_v2_pkey PRIMARY KEY (evidence_pack_id);


--
-- Name: macro_evidence_packs macro_evidence_packs_v2_session_cutoff_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_evidence_packs
    ADD CONSTRAINT macro_evidence_packs_v2_session_cutoff_key UNIQUE (session_date, judgment_cutoff_ms);


--
-- Name: macro_feature_series macro_feature_series_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_feature_series
    ADD CONSTRAINT macro_feature_series_pkey PRIMARY KEY (feature_id, as_of_date);


--
-- Name: macro_fed_official_role_facts macro_fed_official_role_facts_official_id_role_title_effect_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_fed_official_role_facts
    ADD CONSTRAINT macro_fed_official_role_facts_official_id_role_title_effect_key UNIQUE (official_id, role_title, effective_start, fact_hash);


--
-- Name: macro_fed_official_role_facts macro_fed_official_role_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_fed_official_role_facts
    ADD CONSTRAINT macro_fed_official_role_facts_pkey PRIMARY KEY (role_fact_id);


--
-- Name: macro_judgment_status macro_judgment_status_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_judgment_status
    ADD CONSTRAINT macro_judgment_status_pkey PRIMARY KEY (session_date);


--
-- Name: macro_module_current macro_module_current_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_module_current
    ADD CONSTRAINT macro_module_current_pkey PRIMARY KEY (module_id);


--
-- Name: macro_release_facts macro_release_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_release_facts
    ADD CONSTRAINT macro_release_facts_pkey PRIMARY KEY (release_fact_id);


--
-- Name: macro_research_publications_v1_archive macro_research_publications_artifact_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_publications_v1_archive
    ADD CONSTRAINT macro_research_publications_artifact_hash_key UNIQUE (artifact_hash);


--
-- Name: macro_research_publications_v1_archive macro_research_publications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_publications_v1_archive
    ADD CONSTRAINT macro_research_publications_pkey PRIMARY KEY (session_date);


--
-- Name: macro_research_publications macro_research_publications_v2_artifact_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_publications
    ADD CONSTRAINT macro_research_publications_v2_artifact_hash_key UNIQUE (artifact_hash);


--
-- Name: macro_research_publications macro_research_publications_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_publications
    ADD CONSTRAINT macro_research_publications_v2_pkey PRIMARY KEY (session_date);


--
-- Name: macro_research_runs_v1_archive macro_research_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_runs_v1_archive
    ADD CONSTRAINT macro_research_runs_pkey PRIMARY KEY (session_date);


--
-- Name: macro_research_runs_v1_archive macro_research_runs_session_date_market_cutoff_ms_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_runs_v1_archive
    ADD CONSTRAINT macro_research_runs_session_date_market_cutoff_ms_key UNIQUE (session_date, market_cutoff_ms);


--
-- Name: macro_research_runs macro_research_runs_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_runs
    ADD CONSTRAINT macro_research_runs_v2_pkey PRIMARY KEY (session_date);


--
-- Name: macro_research_runs macro_research_runs_v2_session_cutoff_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_runs
    ADD CONSTRAINT macro_research_runs_v2_session_cutoff_key UNIQUE (session_date, market_cutoff_ms);


--
-- Name: macro_series_facts macro_series_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_series_facts
    ADD CONSTRAINT macro_series_facts_pkey PRIMARY KEY (fact_id);


--
-- Name: macro_source_receipts macro_source_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_source_receipts
    ADD CONSTRAINT macro_source_receipts_pkey PRIMARY KEY (receipt_id);


--
-- Name: market_instruments market_instruments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_instruments
    ADD CONSTRAINT market_instruments_pkey PRIMARY KEY (instrument_id);


--
-- Name: market_observations market_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_observations
    ADD CONSTRAINT market_observations_pkey PRIMARY KEY (observation_id);


--
-- Name: market_position_facts market_position_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_position_facts
    ADD CONSTRAINT market_position_facts_pkey PRIMARY KEY (position_fact_id);


--
-- Name: market_settlements market_settlements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_settlements
    ADD CONSTRAINT market_settlements_pkey PRIMARY KEY (settlement_id);


--
-- Name: market_tick_current market_tick_current_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_tick_current
    ADD CONSTRAINT market_tick_current_pkey PRIMARY KEY (target_type, target_id);


--
-- Name: market_ticks market_ticks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_ticks
    ADD CONSTRAINT market_ticks_pkey PRIMARY KEY (observed_at_ms, tick_id);


--
-- Name: market_ticks_default market_ticks_default_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_ticks_default
    ADD CONSTRAINT market_ticks_default_pkey PRIMARY KEY (observed_at_ms, tick_id);


--
-- Name: news_brief_current news_brief_current_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_brief_current
    ADD CONSTRAINT news_brief_current_pkey PRIMARY KEY (singleton_key);


--
-- Name: news_brief_publications news_brief_publications_fingerprint_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_brief_publications
    ADD CONSTRAINT news_brief_publications_fingerprint_key UNIQUE (fingerprint);


--
-- Name: news_brief_publications news_brief_publications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_brief_publications
    ADD CONSTRAINT news_brief_publications_pkey PRIMARY KEY (publication_id);


--
-- Name: news_brief_runs news_brief_runs_fingerprint_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_brief_runs
    ADD CONSTRAINT news_brief_runs_fingerprint_key UNIQUE (fingerprint);


--
-- Name: news_brief_runs news_brief_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_brief_runs
    ADD CONSTRAINT news_brief_runs_pkey PRIMARY KEY (run_id);


--
-- Name: news_feed_observations news_feed_observations_fetch_id_source_item_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_feed_observations
    ADD CONSTRAINT news_feed_observations_fetch_id_source_item_key_key UNIQUE (fetch_id, source_item_key);


--
-- Name: news_feed_observations news_feed_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_feed_observations
    ADD CONSTRAINT news_feed_observations_pkey PRIMARY KEY (observation_id);


--
-- Name: news_items news_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_items
    ADD CONSTRAINT news_items_pkey PRIMARY KEY (item_id);


--
-- Name: news_items news_items_source_id_source_item_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_items
    ADD CONSTRAINT news_items_source_id_source_item_key_key UNIQUE (source_id, source_item_key);


--
-- Name: news_source_fetches news_source_fetches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_source_fetches
    ADD CONSTRAINT news_source_fetches_pkey PRIMARY KEY (fetch_id);


--
-- Name: news_source_memberships news_source_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_source_memberships
    ADD CONSTRAINT news_source_memberships_pkey PRIMARY KEY (source_id, membership);


--
-- Name: news_sources news_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_sources
    ADD CONSTRAINT news_sources_pkey PRIMARY KEY (source_id);


--
-- Name: news_stories news_stories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_stories
    ADD CONSTRAINT news_stories_pkey PRIMARY KEY (story_id);


--
-- Name: news_story_aliases news_story_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_story_aliases
    ADD CONSTRAINT news_story_aliases_pkey PRIMARY KEY (alias_key);


--
-- Name: news_story_members news_story_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_story_members
    ADD CONSTRAINT news_story_members_pkey PRIMARY KEY (story_id, item_id);


--
-- Name: price_feeds price_feeds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_feeds
    ADD CONSTRAINT price_feeds_pkey PRIMARY KEY (pricefeed_id);


--
-- Name: raw_frames raw_frames_payload_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.raw_frames
    ADD CONSTRAINT raw_frames_payload_hash_key UNIQUE (payload_hash);


--
-- Name: raw_frames raw_frames_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.raw_frames
    ADD CONSTRAINT raw_frames_pkey PRIMARY KEY (frame_id);


--
-- Name: registry_assets registry_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_assets
    ADD CONSTRAINT registry_assets_pkey PRIMARY KEY (asset_id);


--
-- Name: token_discovery_dirty_lookup_keys token_discovery_dirty_lookup_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_discovery_dirty_lookup_keys
    ADD CONSTRAINT token_discovery_dirty_lookup_keys_pkey PRIMARY KEY (provider, lookup_key);


--
-- Name: token_discovery_results token_discovery_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_discovery_results
    ADD CONSTRAINT token_discovery_results_pkey PRIMARY KEY (provider, lookup_key);


--
-- Name: token_evidence token_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_evidence
    ADD CONSTRAINT token_evidence_pkey PRIMARY KEY (evidence_id);


--
-- Name: token_image_assets token_image_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_image_assets
    ADD CONSTRAINT token_image_assets_pkey PRIMARY KEY (image_id);


--
-- Name: token_image_assets token_image_assets_source_url_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_image_assets
    ADD CONSTRAINT token_image_assets_source_url_hash_key UNIQUE (source_url_hash);


--
-- Name: token_image_source_dirty_targets token_image_source_dirty_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_image_source_dirty_targets
    ADD CONSTRAINT token_image_source_dirty_targets_pkey PRIMARY KEY (source_url_hash, target_type, target_id);


--
-- Name: token_intent_evidence token_intent_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intent_evidence
    ADD CONSTRAINT token_intent_evidence_pkey PRIMARY KEY (intent_id, evidence_id, role);


--
-- Name: token_intent_lookup_keys token_intent_lookup_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intent_lookup_keys
    ADD CONSTRAINT token_intent_lookup_keys_pkey PRIMARY KEY (lookup_key, intent_id);


--
-- Name: token_intent_resolutions token_intent_resolutions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intent_resolutions
    ADD CONSTRAINT token_intent_resolutions_pkey PRIMARY KEY (resolution_id);


--
-- Name: token_intents token_intents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intents
    ADD CONSTRAINT token_intents_pkey PRIMARY KEY (intent_id);


--
-- Name: token_profile_current_dirty_targets token_profile_current_dirty_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_profile_current_dirty_targets
    ADD CONSTRAINT token_profile_current_dirty_targets_pkey PRIMARY KEY (target_type, target_id);


--
-- Name: token_profile_current token_profile_current_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_profile_current
    ADD CONSTRAINT token_profile_current_pkey PRIMARY KEY (target_type, target_id);


--
-- Name: token_radar_current_rows token_radar_current_rows_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_radar_current_rows
    ADD CONSTRAINT token_radar_current_rows_pkey PRIMARY KEY (row_id);


--
-- Name: token_radar_dirty_targets token_radar_dirty_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_radar_dirty_targets
    ADD CONSTRAINT token_radar_dirty_targets_pkey PRIMARY KEY (target_type_key, identity_id);


--
-- Name: token_radar_publication_state token_radar_publication_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_radar_publication_state
    ADD CONSTRAINT token_radar_publication_state_pkey PRIMARY KEY (projection_version, "window", venue);


--
-- Name: token_radar_rank_source_events token_radar_rank_source_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_radar_rank_source_events
    ADD CONSTRAINT token_radar_rank_source_events_pkey PRIMARY KEY (projection_version, target_type_key, identity_id, source_kind, source_id);


--
-- Name: token_radar_target_features token_radar_target_features_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_radar_target_features
    ADD CONSTRAINT token_radar_target_features_pkey PRIMARY KEY (projection_version, "window", lane, target_type_key, identity_id);


--
-- Name: token_radar_target_first_seen token_radar_target_first_seen_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_radar_target_first_seen
    ADD CONSTRAINT token_radar_target_first_seen_pkey PRIMARY KEY (projection_version, "window", venue, target_type_key, identity_id);


--
-- Name: us_equity_symbols us_equity_symbols_market_instrument_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.us_equity_symbols
    ADD CONSTRAINT us_equity_symbols_market_instrument_id_key UNIQUE (market_instrument_id);


--
-- Name: us_equity_symbols us_equity_symbols_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.us_equity_symbols
    ADD CONSTRAINT us_equity_symbols_pkey PRIMARY KEY (symbol);


--
-- Name: worker_queue_terminal_events worker_queue_terminal_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_queue_terminal_events
    ADD CONSTRAINT worker_queue_terminal_events_pkey PRIMARY KEY (terminal_id);


--
-- Name: checkpoint_blobs_thread_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checkpoint_blobs_thread_id_idx ON public.checkpoint_blobs USING btree (thread_id);


--
-- Name: checkpoint_writes_thread_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checkpoint_writes_thread_id_idx ON public.checkpoint_writes USING btree (thread_id);


--
-- Name: checkpoints_thread_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checkpoints_thread_id_idx ON public.checkpoints USING btree (thread_id);


--
-- Name: idx_asset_identity_current_confidence; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_identity_current_confidence ON public.asset_identity_current USING btree (identity_confidence);


--
-- Name: idx_asset_identity_current_symbol; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_identity_current_symbol ON public.asset_identity_current USING btree (canonical_symbol) WHERE (canonical_symbol IS NOT NULL);


--
-- Name: idx_asset_identity_evidence_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_identity_evidence_asset ON public.asset_identity_evidence USING btree (asset_id);


--
-- Name: idx_asset_identity_evidence_kind_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_identity_evidence_kind_time ON public.asset_identity_evidence USING btree (evidence_kind, observed_at_ms DESC);


--
-- Name: idx_asset_identity_evidence_provider_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_identity_evidence_provider_lookup ON public.asset_identity_evidence USING btree (provider, lookup_mode, observed_at_ms DESC);


--
-- Name: idx_asset_profile_refresh_targets_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_profile_refresh_targets_due ON public.asset_profile_refresh_targets USING btree (provider, priority, due_at_ms, updated_at_ms, target_type, target_id);


--
-- Name: idx_asset_profile_refresh_targets_lease; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_profile_refresh_targets_lease ON public.asset_profile_refresh_targets USING btree (leased_until_ms, provider) WHERE (leased_until_ms IS NOT NULL);


--
-- Name: idx_asset_profiles_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_profiles_due ON public.asset_profiles USING btree (provider, next_refresh_at_ms, status);


--
-- Name: idx_asset_profiles_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_profiles_status ON public.asset_profiles USING btree (status, updated_at_ms DESC);


--
-- Name: idx_cex_token_profiles_ready_logo; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cex_token_profiles_ready_logo ON public.cex_token_profiles USING btree (provider, updated_at_ms DESC, cex_token_id) WHERE ((status = 'ready'::text) AND (logo_url IS NOT NULL));


--
-- Name: idx_collector_pending_items_due_lease; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_collector_pending_items_due_lease ON public.collector_pending_items USING btree (due_at_ms, leased_until_ms, first_observed_at_ms, frame_item_index, source, channel, item_key);


--
-- Name: idx_enriched_events_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_enriched_events_event ON public.enriched_events USING btree (event_id);


--
-- Name: idx_enriched_events_pending_backfill; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_enriched_events_pending_backfill ON public.enriched_events USING btree (created_at_ms, event_id, intent_id) WHERE ((capture_method = 'unavailable'::text) AND (capture_reason = 'pending_backfill'::text) AND (tick_id IS NULL));


--
-- Name: idx_enriched_events_ready_anchor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_enriched_events_ready_anchor ON public.enriched_events USING btree (event_id, intent_id) WHERE ((capture_method <> 'unavailable'::text) AND (tick_id IS NOT NULL) AND (tick_lag_ms IS NOT NULL));


--
-- Name: idx_enriched_events_target_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_enriched_events_target_time ON public.enriched_events USING btree (target_type, target_id, t_event_ms DESC, event_id, intent_id);


--
-- Name: idx_enriched_events_tick; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_enriched_events_tick ON public.enriched_events USING btree (tick_observed_at_ms, tick_id) WHERE (tick_id IS NOT NULL);


--
-- Name: idx_event_anchor_backfill_jobs_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_anchor_backfill_jobs_due ON public.event_anchor_backfill_jobs USING btree (next_run_at_ms, created_at_ms, event_id, intent_id) WHERE (status = 'pending'::text);


--
-- Name: idx_event_anchor_backfill_jobs_expired; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_anchor_backfill_jobs_expired ON public.event_anchor_backfill_jobs USING btree (active_until_ms, created_at_ms, event_id, intent_id) WHERE (status = 'pending'::text);


--
-- Name: idx_event_anchor_backfill_jobs_pending_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_anchor_backfill_jobs_pending_created ON public.event_anchor_backfill_jobs USING btree (created_at_ms, event_id, intent_id) WHERE (status = 'pending'::text);


--
-- Name: idx_event_anchor_backfill_jobs_running; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_anchor_backfill_jobs_running ON public.event_anchor_backfill_jobs USING btree (leased_until_ms, updated_at_ms, event_id, intent_id) WHERE (status = 'running'::text);


--
-- Name: idx_event_entities_span_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_entities_span_lookup ON public.event_entities USING btree (event_id, text_surface, sentence_id, span_start);


--
-- Name: idx_event_entities_token_window; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_entities_token_window ON public.event_entities USING btree (entity_type, token_resolution_status, received_at_ms);


--
-- Name: idx_event_entities_type_value; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_entities_type_value ON public.event_entities USING btree (entity_type, normalized_value);


--
-- Name: idx_events_author_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_author_received ON public.events USING btree (author_handle, received_at_ms);


--
-- Name: idx_events_author_received_event_lower_desc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_author_received_event_lower_desc ON public.events USING btree (lower(author_handle), received_at_ms DESC, event_id DESC);


--
-- Name: idx_events_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_received ON public.events USING btree (received_at_ms);


--
-- Name: idx_events_search_text_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_search_text_trgm ON public.events USING gin (search_text public.gin_trgm_ops) WHERE (search_text IS NOT NULL);


--
-- Name: idx_events_search_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_search_tsv ON public.events USING gin (search_tsv);


--
-- Name: idx_events_tweet_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_tweet_id ON public.events USING btree (tweet_id);


--
-- Name: idx_macro_acquisition_targets_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_acquisition_targets_due ON public.macro_acquisition_targets USING btree (clock_kind, priority, next_due_at_ms, target_key) WHERE (status = ANY (ARRAY['pending'::text, 'current'::text, 'delayed'::text, 'stale'::text, 'invalid'::text, 'backfilling'::text, 'claimed'::text]));


--
-- Name: idx_macro_document_analyses_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_document_analyses_document ON public.macro_document_analyses USING btree (document_id, created_at_ms DESC);


--
-- Name: idx_macro_document_analysis_jobs_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_document_analysis_jobs_due ON public.macro_document_analysis_jobs USING btree (status, next_due_at_ms, analysis_job_id);


--
-- Name: idx_macro_documents_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_documents_latest ON public.macro_documents USING btree (document_type, published_at_ms DESC);


--
-- Name: idx_macro_fed_official_role_effective; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_fed_official_role_effective ON public.macro_fed_official_role_facts USING btree (official_id, effective_start DESC, effective_end);


--
-- Name: idx_macro_judgment_status_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_judgment_status_updated ON public.macro_judgment_status USING btree (updated_at_ms DESC, session_date DESC);


--
-- Name: idx_macro_release_facts_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_release_facts_latest ON public.macro_release_facts USING btree (dataset_id, published_at_ms DESC);


--
-- Name: idx_macro_research_publications_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_research_publications_latest ON public.macro_research_publications_v1_archive USING btree (session_date DESC);


--
-- Name: idx_macro_research_publications_v2_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_research_publications_v2_latest ON public.macro_research_publications USING btree (session_date DESC);


--
-- Name: idx_macro_research_runs_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_research_runs_due ON public.macro_research_runs_v1_archive USING btree (status, due_at_ms, session_date) WHERE (status = ANY (ARRAY['pending'::text, 'retryable'::text, 'running'::text]));


--
-- Name: idx_macro_research_runs_v2_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_research_runs_v2_due ON public.macro_research_runs USING btree (status, due_at_ms, session_date) WHERE (status = ANY (ARRAY['pending'::text, 'retryable'::text, 'running'::text]));


--
-- Name: idx_macro_series_facts_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_series_facts_latest ON public.macro_series_facts USING btree (dataset_id, series_id, reference_date DESC, vintage_date DESC);


--
-- Name: idx_macro_source_receipts_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_source_receipts_target ON public.macro_source_receipts USING btree (target_key, completed_at_ms DESC);


--
-- Name: idx_market_observations_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_market_observations_latest ON public.market_observations USING btree (instrument_id, field_name, observed_at_ms DESC);


--
-- Name: idx_market_position_facts_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_market_position_facts_latest ON public.market_position_facts USING btree (dataset_id, contract_code, report_date DESC);


--
-- Name: idx_market_settlements_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_market_settlements_latest ON public.market_settlements USING btree (instrument_id, trade_date DESC, contract_code);


--
-- Name: idx_market_tick_current_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_market_tick_current_updated ON public.market_tick_current USING btree (updated_at_ms DESC, target_type, target_id);


--
-- Name: idx_market_ticks_dedupe; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_market_ticks_dedupe ON ONLY public.market_ticks USING btree (observed_at_ms, target_type, target_id, source_provider);


--
-- Name: idx_market_ticks_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_market_ticks_received ON ONLY public.market_ticks USING btree (received_at_ms DESC, tick_id DESC);


--
-- Name: idx_market_ticks_target_observed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_market_ticks_target_observed ON ONLY public.market_ticks USING btree (target_type, target_id, observed_at_ms DESC, tick_id DESC);


--
-- Name: idx_price_feeds_cex_canonical_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_price_feeds_cex_canonical_updated ON public.price_feeds USING btree (subject_id, updated_at_ms DESC, native_market_id) WHERE ((subject_type = 'CexToken'::text) AND (provider = 'binance'::text) AND (feed_type = 'cex_swap'::text) AND (quote_symbol = 'USDT'::text) AND (status = 'canonical'::text));


--
-- Name: idx_price_feeds_cex_subject_preferred; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_price_feeds_cex_subject_preferred ON public.price_feeds USING btree (subject_type, subject_id, feed_type, status, updated_at_ms DESC, native_market_id) WHERE ((subject_type = 'CexToken'::text) AND (status = ANY (ARRAY['candidate'::text, 'canonical'::text])));


--
-- Name: idx_raw_frames_channel_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_raw_frames_channel_received ON public.raw_frames USING btree (channel, received_at_ms);


--
-- Name: idx_raw_frames_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_raw_frames_received ON public.raw_frames USING btree (received_at_ms);


--
-- Name: idx_token_discovery_dirty_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_discovery_dirty_due ON public.token_discovery_dirty_lookup_keys USING btree (provider, refresh_priority, due_at_ms, latest_seen_ms DESC, updated_at_ms, lookup_key);


--
-- Name: idx_token_discovery_dirty_lease; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_discovery_dirty_lease ON public.token_discovery_dirty_lookup_keys USING btree (provider, leased_until_ms, due_at_ms);


--
-- Name: idx_token_discovery_results_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_discovery_results_due ON public.token_discovery_results USING btree (provider, status, next_refresh_at_ms, updated_at_ms);


--
-- Name: idx_token_discovery_results_lookup_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_discovery_results_lookup_type ON public.token_discovery_results USING btree (lookup_type, status, next_refresh_at_ms);


--
-- Name: idx_token_evidence_address; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_evidence_address ON public.token_evidence USING btree (lower(address_hint));


--
-- Name: idx_token_evidence_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_evidence_event ON public.token_evidence USING btree (event_id);


--
-- Name: idx_token_evidence_symbol; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_evidence_symbol ON public.token_evidence USING btree (normalized_symbol);


--
-- Name: idx_token_image_assets_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_image_assets_due ON public.token_image_assets USING btree (status, next_refresh_at_ms, updated_at_ms);


--
-- Name: idx_token_image_assets_ready_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_image_assets_ready_source ON public.token_image_assets USING btree (source_url_hash) WHERE (status = 'ready'::text);


--
-- Name: idx_token_image_source_dirty_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_image_source_dirty_due ON public.token_image_source_dirty_targets USING btree (priority, due_at_ms, updated_at_ms, source_url_hash, target_type, target_id);


--
-- Name: idx_token_image_source_dirty_lease; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_image_source_dirty_lease ON public.token_image_source_dirty_targets USING btree (leased_until_ms) WHERE (leased_until_ms IS NOT NULL);


--
-- Name: idx_token_intent_lookup_keys_intent_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_intent_lookup_keys_intent_lookup ON public.token_intent_lookup_keys USING btree (intent_id, lookup_key);


--
-- Name: idx_token_intent_lookup_keys_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_intent_lookup_keys_lookup ON public.token_intent_lookup_keys USING btree (lookup_key);


--
-- Name: idx_token_intent_resolutions_current_event_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_intent_resolutions_current_event_target ON public.token_intent_resolutions USING btree (event_id, target_type, target_id, resolver_policy_version, resolution_status, confidence DESC, decision_time_ms DESC, resolution_id DESC) WHERE ((is_current = true) AND (target_type = ANY (ARRAY['Asset'::text, 'CexToken'::text])) AND (target_id IS NOT NULL));


--
-- Name: idx_token_intent_resolutions_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_intent_resolutions_event ON public.token_intent_resolutions USING btree (event_id, decision_time_ms DESC);


--
-- Name: idx_token_intent_resolutions_public_event_current; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_intent_resolutions_public_event_current ON public.token_intent_resolutions USING btree (event_id, decision_time_ms, resolution_id) WHERE ((is_current = true) AND (target_type = ANY (ARRAY['Asset'::text, 'CexToken'::text])) AND (target_id IS NOT NULL));


--
-- Name: idx_token_intent_resolutions_target_current; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_intent_resolutions_target_current ON public.token_intent_resolutions USING btree (target_type, target_id, decision_time_ms DESC) WHERE (is_current = true);


--
-- Name: idx_token_intents_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_intents_event ON public.token_intents USING btree (event_id);


--
-- Name: idx_token_intents_event_intent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_intents_event_intent ON public.token_intents USING btree (event_id, intent_id);


--
-- Name: idx_token_profile_current_dirty_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_profile_current_dirty_due ON public.token_profile_current_dirty_targets USING btree (priority, due_at_ms, updated_at_ms, target_type, target_id);


--
-- Name: idx_token_profile_current_dirty_lease; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_profile_current_dirty_lease ON public.token_profile_current_dirty_targets USING btree (leased_until_ms) WHERE (leased_until_ms IS NOT NULL);


--
-- Name: idx_token_profile_current_logo; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_profile_current_logo ON public.token_profile_current USING btree (updated_at_ms DESC) WHERE (logo_url IS NOT NULL);


--
-- Name: idx_token_profile_current_logo_image; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_profile_current_logo_image ON public.token_profile_current USING btree (logo_image_id) WHERE (logo_image_id IS NOT NULL);


--
-- Name: idx_token_profile_current_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_profile_current_provider ON public.token_profile_current USING btree (profile_provider, updated_at_ms DESC);


--
-- Name: idx_token_profile_current_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_profile_current_status ON public.token_profile_current USING btree (status, updated_at_ms DESC);


--
-- Name: idx_token_radar_current_rows_generation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_radar_current_rows_generation ON public.token_radar_current_rows USING btree (projection_version, "window", venue, generation_id, lane, rank);


--
-- Name: idx_token_radar_current_rows_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_radar_current_rows_target ON public.token_radar_current_rows USING btree (target_type, target_id, computed_at_ms DESC) WHERE (target_id IS NOT NULL);


--
-- Name: idx_token_radar_current_rows_venue_rank; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_radar_current_rows_venue_rank ON public.token_radar_current_rows USING btree (projection_version, "window", venue, lane, rank);


--
-- Name: idx_token_radar_current_rows_venue_target; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_token_radar_current_rows_venue_target ON public.token_radar_current_rows USING btree (projection_version, "window", venue, lane, target_type_key, identity_id);


--
-- Name: idx_token_radar_dirty_targets_claim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_radar_dirty_targets_claim ON public.token_radar_dirty_targets USING btree (due_at_ms, updated_at_ms, target_type_key, identity_id);


--
-- Name: idx_token_radar_first_seen_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_radar_first_seen_updated ON public.token_radar_target_first_seen USING btree (updated_at_ms DESC);


--
-- Name: idx_token_radar_publication_state_current; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_radar_publication_state_current ON public.token_radar_publication_state USING btree (projection_version, "window", venue, latest_attempt_status, current_generation_id);


--
-- Name: idx_token_radar_rank_source_events_target_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_radar_rank_source_events_target_time ON public.token_radar_rank_source_events USING btree (projection_version, target_type_key, identity_id, event_received_at_ms DESC, source_id);


--
-- Name: idx_token_radar_target_features_freshness; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_radar_target_features_freshness ON public.token_radar_target_features USING btree (projection_version, target_type_key, identity_id, latest_market_observed_at_ms DESC);


--
-- Name: idx_token_radar_target_features_rank; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_radar_target_features_rank ON public.token_radar_target_features USING btree (projection_version, "window", lane DESC, rank_score DESC, latest_event_received_at_ms DESC, identity_id);


--
-- Name: idx_token_radar_target_features_window_freshness; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_radar_target_features_window_freshness ON public.token_radar_target_features USING btree (projection_version, "window", latest_event_received_at_ms DESC);


--
-- Name: idx_us_equity_symbols_active_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_us_equity_symbols_active_lookup ON public.us_equity_symbols USING btree (symbol) WHERE (status = 'active'::text);


--
-- Name: idx_us_equity_symbols_source_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_us_equity_symbols_source_status ON public.us_equity_symbols USING btree (source, status);


--
-- Name: idx_worker_queue_terminal_reason_bucket_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_worker_queue_terminal_reason_bucket_unresolved ON public.worker_queue_terminal_events USING btree (worker_name, source_table, final_reason_bucket, terminalized_at_ms DESC) WHERE (operator_action IS NULL);


--
-- Name: idx_worker_queue_terminal_resolved_retention; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_worker_queue_terminal_resolved_retention ON public.worker_queue_terminal_events USING btree (COALESCE(operator_action_at_ms, terminalized_at_ms), terminal_id) WHERE (operator_action IS NOT NULL);


--
-- Name: idx_worker_queue_terminal_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_worker_queue_terminal_source ON public.worker_queue_terminal_events USING btree (source_table, worker_name);


--
-- Name: idx_worker_queue_terminal_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_worker_queue_terminal_unresolved ON public.worker_queue_terminal_events USING btree (worker_name, source_table, terminalized_at_ms DESC) WHERE (operator_action IS NULL);


--
-- Name: ix_news_brief_publications_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_brief_publications_time ON public.news_brief_publications USING btree (published_at_ms DESC, publication_id DESC);


--
-- Name: ix_news_brief_runs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_brief_runs_status ON public.news_brief_runs USING btree (status, lease_expires_at_ms, updated_at_ms);


--
-- Name: ix_news_feed_observations_source_item; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_feed_observations_source_item ON public.news_feed_observations USING btree (source_id, source_item_key, observed_at_ms DESC);


--
-- Name: ix_news_items_active_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_items_active_time ON public.news_items USING btree (published_at_ms DESC, item_id) WHERE active;


--
-- Name: ix_news_items_source_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_items_source_time ON public.news_items USING btree (source_id, published_at_ms DESC, item_id) WHERE active;


--
-- Name: ix_news_source_fetches_retention; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_source_fetches_retention ON public.news_source_fetches USING btree (created_at_ms);


--
-- Name: ix_news_source_fetches_source_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_source_fetches_source_time ON public.news_source_fetches USING btree (source_id, finished_at_ms DESC, fetch_id DESC);


--
-- Name: ix_news_source_memberships_membership; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_source_memberships_membership ON public.news_source_memberships USING btree (membership, source_id);


--
-- Name: ix_news_sources_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_sources_due ON public.news_sources USING btree (next_fetch_at_ms, source_id) WHERE enabled;


--
-- Name: ix_news_stories_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_stories_category ON public.news_stories USING btree (category, importance_score DESC, last_published_at_ms DESC, story_id) WHERE active;


--
-- Name: ix_news_stories_importance_feed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_stories_importance_feed ON public.news_stories USING btree (importance_score DESC, last_published_at_ms DESC, story_id) WHERE active;


--
-- Name: ix_news_stories_latest_feed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_stories_latest_feed ON public.news_stories USING btree (last_published_at_ms DESC, importance_score DESC, story_id) WHERE active;


--
-- Name: ix_news_story_aliases_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_story_aliases_expiry ON public.news_story_aliases USING btree (expires_at_ms);


--
-- Name: ix_news_story_members_current_story; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_story_members_current_story ON public.news_story_members USING btree (story_id, item_id) WHERE current;


--
-- Name: market_ticks_default_observed_at_ms_target_type_target_id_s_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX market_ticks_default_observed_at_ms_target_type_target_id_s_idx ON public.market_ticks_default USING btree (observed_at_ms, target_type, target_id, source_provider);


--
-- Name: market_ticks_default_received_at_ms_tick_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX market_ticks_default_received_at_ms_tick_id_idx ON public.market_ticks_default USING btree (received_at_ms DESC, tick_id DESC);


--
-- Name: market_ticks_default_target_type_target_id_observed_at_ms_t_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX market_ticks_default_target_type_target_id_observed_at_ms_t_idx ON public.market_ticks_default USING btree (target_type, target_id, observed_at_ms DESC, tick_id DESC);


--
-- Name: uq_collector_pending_items_internal_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_collector_pending_items_internal_id ON public.collector_pending_items USING btree (source, channel, internal_id) WHERE (internal_id IS NOT NULL);


--
-- Name: uq_worker_queue_terminal_one_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_worker_queue_terminal_one_unresolved ON public.worker_queue_terminal_events USING btree (worker_name, source_table, target_key) WHERE (operator_action IS NULL);


--
-- Name: uq_worker_queue_terminal_source_snapshot; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_worker_queue_terminal_source_snapshot ON public.worker_queue_terminal_events USING btree (worker_name, source_table, target_key, source_row_hash, terminal_generation);


--
-- Name: ux_cex_tokens_identity; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_cex_tokens_identity ON public.cex_tokens USING btree (base_symbol);


--
-- Name: ux_event_entities_event_type_value_chain; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_event_entities_event_type_value_chain ON public.event_entities USING btree (event_id, entity_type, normalized_value, COALESCE(chain, ''::text));


--
-- Name: ux_macro_release_facts_natural_fact; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_macro_release_facts_natural_fact ON public.macro_release_facts USING btree (dataset_id, release_id, reference_period, fact_hash);


--
-- Name: ux_macro_series_facts_natural_fact; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_macro_series_facts_natural_fact ON public.macro_series_facts USING btree (dataset_id, series_id, reference_date, fact_hash);


--
-- Name: ux_market_observations_natural_fact; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_market_observations_natural_fact ON public.market_observations USING btree (dataset_id, instrument_id, field_name, observed_at_ms, fact_hash);


--
-- Name: ux_market_position_facts_natural_fact; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_market_position_facts_natural_fact ON public.market_position_facts USING btree (dataset_id, contract_code, report_date, fact_hash);


--
-- Name: ux_market_settlements_natural_fact; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_market_settlements_natural_fact ON public.market_settlements USING btree (dataset_id, instrument_id, trade_date, contract_code, fact_hash);


--
-- Name: ux_news_story_members_current_item; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_news_story_members_current_item ON public.news_story_members USING btree (item_id) WHERE current;


--
-- Name: ux_price_feeds_native_identity; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_price_feeds_native_identity ON public.price_feeds USING btree (provider, feed_type, native_market_id) WHERE (native_market_id IS NOT NULL);


--
-- Name: ux_price_feeds_token_identity; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_price_feeds_token_identity ON public.price_feeds USING btree (provider, feed_type, chain_id, address) WHERE (address IS NOT NULL);


--
-- Name: ux_registry_assets_identity; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_registry_assets_identity ON public.registry_assets USING btree (chain_id, address);


--
-- Name: ux_token_evidence_event_source_span; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_token_evidence_event_source_span ON public.token_evidence USING btree (event_id, source_kind, source_id, evidence_type, raw_value, span_start, span_end);


--
-- Name: ux_token_intent_current_resolution; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_token_intent_current_resolution ON public.token_intent_resolutions USING btree (intent_id) WHERE (is_current = true);


--
-- Name: ux_token_intents_event_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_token_intents_event_key ON public.token_intents USING btree (event_id, intent_key);


--
-- Name: market_ticks_default_observed_at_ms_target_type_target_id_s_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_market_ticks_dedupe ATTACH PARTITION public.market_ticks_default_observed_at_ms_target_type_target_id_s_idx;


--
-- Name: market_ticks_default_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.market_ticks_pkey ATTACH PARTITION public.market_ticks_default_pkey;


--
-- Name: market_ticks_default_received_at_ms_tick_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_market_ticks_received ATTACH PARTITION public.market_ticks_default_received_at_ms_tick_id_idx;


--
-- Name: market_ticks_default_target_type_target_id_observed_at_ms_t_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.idx_market_ticks_target_observed ATTACH PARTITION public.market_ticks_default_target_type_target_id_observed_at_ms_t_idx;


--
-- Name: enriched_events forbid_enriched_events_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER forbid_enriched_events_update BEFORE UPDATE ON public.enriched_events FOR EACH ROW EXECUTE FUNCTION public.forbid_market_fact_update();


--
-- Name: market_ticks forbid_market_ticks_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER forbid_market_ticks_update BEFORE UPDATE ON public.market_ticks FOR EACH ROW EXECUTE FUNCTION public.forbid_market_fact_update();


--
-- Name: macro_daily_judgments macro_daily_judgments_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_daily_judgments_append_only BEFORE DELETE OR UPDATE ON public.macro_daily_judgments FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_daily_judgments_v1_archive macro_daily_judgments_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_daily_judgments_append_only BEFORE DELETE OR UPDATE ON public.macro_daily_judgments_v1_archive FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_document_analyses macro_document_analyses_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_document_analyses_append_only BEFORE DELETE OR UPDATE ON public.macro_document_analyses FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_documents macro_documents_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_documents_append_only BEFORE DELETE OR UPDATE ON public.macro_documents FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_event_updates macro_event_updates_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_event_updates_append_only BEFORE DELETE OR UPDATE ON public.macro_event_updates FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_event_updates_v1_archive macro_event_updates_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_event_updates_append_only BEFORE DELETE OR UPDATE ON public.macro_event_updates_v1_archive FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_evidence_packs macro_evidence_packs_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_evidence_packs_append_only BEFORE DELETE OR UPDATE ON public.macro_evidence_packs FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_evidence_packs_v1_archive macro_evidence_packs_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_evidence_packs_append_only BEFORE DELETE OR UPDATE ON public.macro_evidence_packs_v1_archive FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_fed_official_role_facts macro_fed_official_role_facts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_fed_official_role_facts_append_only BEFORE DELETE OR UPDATE ON public.macro_fed_official_role_facts FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_release_facts macro_release_facts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_release_facts_append_only BEFORE DELETE OR UPDATE ON public.macro_release_facts FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_research_publications macro_research_publications_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_research_publications_append_only BEFORE DELETE OR UPDATE ON public.macro_research_publications FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_research_publications_v1_archive macro_research_publications_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_research_publications_append_only BEFORE DELETE OR UPDATE ON public.macro_research_publications_v1_archive FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_research_runs macro_research_runs_lifecycle; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_research_runs_lifecycle BEFORE INSERT OR DELETE OR UPDATE ON public.macro_research_runs FOR EACH ROW EXECUTE FUNCTION public.enforce_macro_research_run_lifecycle();


--
-- Name: macro_research_runs_v1_archive macro_research_runs_lifecycle; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_research_runs_lifecycle BEFORE INSERT OR DELETE OR UPDATE ON public.macro_research_runs_v1_archive FOR EACH ROW EXECUTE FUNCTION public.enforce_macro_research_run_lifecycle();


--
-- Name: macro_series_facts macro_series_facts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_series_facts_append_only BEFORE DELETE OR UPDATE ON public.macro_series_facts FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: macro_source_receipts macro_source_receipts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_source_receipts_append_only BEFORE DELETE OR UPDATE ON public.macro_source_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: market_observations market_observations_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER market_observations_append_only BEFORE DELETE OR UPDATE ON public.market_observations FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: market_position_facts market_position_facts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER market_position_facts_append_only BEFORE DELETE OR UPDATE ON public.market_position_facts FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: market_settlements market_settlements_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER market_settlements_append_only BEFORE DELETE OR UPDATE ON public.market_settlements FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();


--
-- Name: asset_identity_current asset_identity_current_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_identity_current
    ADD CONSTRAINT asset_identity_current_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.registry_assets(asset_id) ON DELETE CASCADE;


--
-- Name: asset_identity_current asset_identity_current_selected_evidence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_identity_current
    ADD CONSTRAINT asset_identity_current_selected_evidence_id_fkey FOREIGN KEY (selected_evidence_id) REFERENCES public.asset_identity_evidence(evidence_id) ON DELETE SET NULL;


--
-- Name: asset_identity_evidence asset_identity_evidence_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_identity_evidence
    ADD CONSTRAINT asset_identity_evidence_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.registry_assets(asset_id) ON DELETE CASCADE;


--
-- Name: asset_identity_evidence asset_identity_evidence_source_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_identity_evidence
    ADD CONSTRAINT asset_identity_evidence_source_event_id_fkey FOREIGN KEY (source_event_id) REFERENCES public.events(event_id) ON DELETE SET NULL;


--
-- Name: asset_identity_evidence asset_identity_evidence_source_intent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_identity_evidence
    ADD CONSTRAINT asset_identity_evidence_source_intent_id_fkey FOREIGN KEY (source_intent_id) REFERENCES public.token_intents(intent_id) ON DELETE SET NULL;


--
-- Name: asset_identity_evidence asset_identity_evidence_source_resolution_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_identity_evidence
    ADD CONSTRAINT asset_identity_evidence_source_resolution_id_fkey FOREIGN KEY (source_resolution_id) REFERENCES public.token_intent_resolutions(resolution_id) ON DELETE SET NULL;


--
-- Name: asset_profiles asset_profiles_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_profiles
    ADD CONSTRAINT asset_profiles_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.registry_assets(asset_id) ON DELETE CASCADE;


--
-- Name: cex_token_profiles cex_token_profiles_cex_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cex_token_profiles
    ADD CONSTRAINT cex_token_profiles_cex_token_id_fkey FOREIGN KEY (cex_token_id) REFERENCES public.cex_tokens(cex_token_id) ON DELETE CASCADE;


--
-- Name: enriched_events enriched_events_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enriched_events
    ADD CONSTRAINT enriched_events_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(event_id) ON DELETE CASCADE;


--
-- Name: enriched_events enriched_events_intent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enriched_events
    ADD CONSTRAINT enriched_events_intent_id_fkey FOREIGN KEY (intent_id) REFERENCES public.token_intents(intent_id) ON DELETE CASCADE;


--
-- Name: enriched_events enriched_events_resolution_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enriched_events
    ADD CONSTRAINT enriched_events_resolution_id_fkey FOREIGN KEY (resolution_id) REFERENCES public.token_intent_resolutions(resolution_id) ON DELETE CASCADE;


--
-- Name: enriched_events enriched_events_tick_observed_at_ms_tick_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enriched_events
    ADD CONSTRAINT enriched_events_tick_observed_at_ms_tick_id_fkey FOREIGN KEY (tick_observed_at_ms, tick_id) REFERENCES public.market_ticks(observed_at_ms, tick_id) ON DELETE RESTRICT;


--
-- Name: event_anchor_backfill_jobs event_anchor_backfill_jobs_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_anchor_backfill_jobs
    ADD CONSTRAINT event_anchor_backfill_jobs_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(event_id) ON DELETE CASCADE;


--
-- Name: event_anchor_backfill_jobs event_anchor_backfill_jobs_intent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_anchor_backfill_jobs
    ADD CONSTRAINT event_anchor_backfill_jobs_intent_id_fkey FOREIGN KEY (intent_id) REFERENCES public.token_intents(intent_id) ON DELETE CASCADE;


--
-- Name: event_anchor_backfill_jobs event_anchor_backfill_jobs_resolution_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_anchor_backfill_jobs
    ADD CONSTRAINT event_anchor_backfill_jobs_resolution_id_fkey FOREIGN KEY (resolution_id) REFERENCES public.token_intent_resolutions(resolution_id) ON DELETE CASCADE;


--
-- Name: event_entities event_entities_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_entities
    ADD CONSTRAINT event_entities_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(event_id) ON DELETE CASCADE;


--
-- Name: macro_acquisition_targets macro_acquisition_targets_last_receipt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_acquisition_targets
    ADD CONSTRAINT macro_acquisition_targets_last_receipt_id_fkey FOREIGN KEY (last_receipt_id) REFERENCES public.macro_source_receipts(receipt_id) ON DELETE SET NULL;


--
-- Name: macro_daily_judgments_v1_archive macro_daily_judgments_evidence_pack_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_daily_judgments_v1_archive
    ADD CONSTRAINT macro_daily_judgments_evidence_pack_id_fkey FOREIGN KEY (evidence_pack_id) REFERENCES public.macro_evidence_packs_v1_archive(evidence_pack_id) ON DELETE RESTRICT;


--
-- Name: macro_daily_judgments macro_daily_judgments_evidence_pack_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_daily_judgments
    ADD CONSTRAINT macro_daily_judgments_evidence_pack_id_fkey1 FOREIGN KEY (evidence_pack_id) REFERENCES public.macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT;


--
-- Name: macro_document_analyses macro_document_analyses_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_document_analyses
    ADD CONSTRAINT macro_document_analyses_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.macro_documents(document_id) ON DELETE RESTRICT;


--
-- Name: macro_document_analysis_jobs macro_document_analysis_jobs_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_document_analysis_jobs
    ADD CONSTRAINT macro_document_analysis_jobs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.macro_documents(document_id) ON DELETE RESTRICT;


--
-- Name: macro_event_updates_v1_archive macro_event_updates_evidence_pack_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_event_updates_v1_archive
    ADD CONSTRAINT macro_event_updates_evidence_pack_id_fkey FOREIGN KEY (evidence_pack_id) REFERENCES public.macro_evidence_packs_v1_archive(evidence_pack_id) ON DELETE RESTRICT;


--
-- Name: macro_event_updates macro_event_updates_evidence_pack_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_event_updates
    ADD CONSTRAINT macro_event_updates_evidence_pack_id_fkey1 FOREIGN KEY (evidence_pack_id) REFERENCES public.macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT;


--
-- Name: macro_event_updates_v1_archive macro_event_updates_trigger_release_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_event_updates_v1_archive
    ADD CONSTRAINT macro_event_updates_trigger_release_fact_id_fkey FOREIGN KEY (trigger_release_fact_id) REFERENCES public.macro_release_facts(release_fact_id) ON DELETE RESTRICT;


--
-- Name: macro_event_updates macro_event_updates_trigger_release_fact_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_event_updates
    ADD CONSTRAINT macro_event_updates_trigger_release_fact_id_fkey1 FOREIGN KEY (trigger_release_fact_id) REFERENCES public.macro_release_facts(release_fact_id) ON DELETE RESTRICT;


--
-- Name: macro_research_publications_v1_archive macro_research_publications_evidence_pack_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_publications_v1_archive
    ADD CONSTRAINT macro_research_publications_evidence_pack_id_fkey FOREIGN KEY (evidence_pack_id) REFERENCES public.macro_evidence_packs_v1_archive(evidence_pack_id) ON DELETE RESTRICT;


--
-- Name: macro_research_publications macro_research_publications_evidence_pack_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_publications
    ADD CONSTRAINT macro_research_publications_evidence_pack_id_fkey1 FOREIGN KEY (evidence_pack_id) REFERENCES public.macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT;


--
-- Name: macro_research_publications_v1_archive macro_research_publications_session_date_market_cutoff_ms_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_publications_v1_archive
    ADD CONSTRAINT macro_research_publications_session_date_market_cutoff_ms_fkey FOREIGN KEY (session_date, market_cutoff_ms) REFERENCES public.macro_research_runs_v1_archive(session_date, market_cutoff_ms) ON DELETE RESTRICT;


--
-- Name: macro_research_publications macro_research_publications_v2_run_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_publications
    ADD CONSTRAINT macro_research_publications_v2_run_fkey FOREIGN KEY (session_date, market_cutoff_ms) REFERENCES public.macro_research_runs(session_date, market_cutoff_ms) ON DELETE RESTRICT;


--
-- Name: macro_research_runs_v1_archive macro_research_runs_evidence_pack_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_runs_v1_archive
    ADD CONSTRAINT macro_research_runs_evidence_pack_id_fkey FOREIGN KEY (evidence_pack_id) REFERENCES public.macro_evidence_packs_v1_archive(evidence_pack_id) ON DELETE RESTRICT;


--
-- Name: macro_research_runs macro_research_runs_evidence_pack_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_research_runs
    ADD CONSTRAINT macro_research_runs_evidence_pack_id_fkey1 FOREIGN KEY (evidence_pack_id) REFERENCES public.macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT;


--
-- Name: market_observations market_observations_instrument_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_observations
    ADD CONSTRAINT market_observations_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.market_instruments(instrument_id) ON DELETE RESTRICT;


--
-- Name: market_settlements market_settlements_instrument_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_settlements
    ADD CONSTRAINT market_settlements_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.market_instruments(instrument_id) ON DELETE RESTRICT;


--
-- Name: market_tick_current market_tick_current_tick_observed_at_ms_tick_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_tick_current
    ADD CONSTRAINT market_tick_current_tick_observed_at_ms_tick_id_fkey FOREIGN KEY (tick_observed_at_ms, tick_id) REFERENCES public.market_ticks(observed_at_ms, tick_id) ON DELETE RESTRICT;


--
-- Name: market_ticks market_ticks_pricefeed_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.market_ticks
    ADD CONSTRAINT market_ticks_pricefeed_id_fkey FOREIGN KEY (pricefeed_id) REFERENCES public.price_feeds(pricefeed_id) ON DELETE SET NULL;


--
-- Name: news_brief_current news_brief_current_latest_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_brief_current
    ADD CONSTRAINT news_brief_current_latest_run_id_fkey FOREIGN KEY (latest_run_id) REFERENCES public.news_brief_runs(run_id) ON DELETE SET NULL;


--
-- Name: news_brief_current news_brief_current_publication_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_brief_current
    ADD CONSTRAINT news_brief_current_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES public.news_brief_publications(publication_id) ON DELETE RESTRICT;


--
-- Name: news_feed_observations news_feed_observations_fetch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_feed_observations
    ADD CONSTRAINT news_feed_observations_fetch_id_fkey FOREIGN KEY (fetch_id) REFERENCES public.news_source_fetches(fetch_id) ON DELETE CASCADE;


--
-- Name: news_feed_observations news_feed_observations_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_feed_observations
    ADD CONSTRAINT news_feed_observations_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.news_sources(source_id) ON DELETE RESTRICT;


--
-- Name: news_items news_items_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_items
    ADD CONSTRAINT news_items_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.news_sources(source_id) ON DELETE RESTRICT;


--
-- Name: news_source_fetches news_source_fetches_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_source_fetches
    ADD CONSTRAINT news_source_fetches_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.news_sources(source_id) ON DELETE RESTRICT;


--
-- Name: news_source_memberships news_source_memberships_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_source_memberships
    ADD CONSTRAINT news_source_memberships_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.news_sources(source_id) ON DELETE RESTRICT;


--
-- Name: news_stories news_stories_representative_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_stories
    ADD CONSTRAINT news_stories_representative_item_id_fkey FOREIGN KEY (representative_item_id) REFERENCES public.news_items(item_id) ON DELETE RESTRICT;


--
-- Name: news_stories news_stories_representative_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_stories
    ADD CONSTRAINT news_stories_representative_source_id_fkey FOREIGN KEY (representative_source_id) REFERENCES public.news_sources(source_id) ON DELETE RESTRICT;


--
-- Name: news_stories news_stories_scoring_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_stories
    ADD CONSTRAINT news_stories_scoring_item_id_fkey FOREIGN KEY (scoring_item_id) REFERENCES public.news_items(item_id) ON DELETE RESTRICT;


--
-- Name: news_story_aliases news_story_aliases_story_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_story_aliases
    ADD CONSTRAINT news_story_aliases_story_id_fkey FOREIGN KEY (story_id) REFERENCES public.news_stories(story_id) ON DELETE RESTRICT;


--
-- Name: news_story_members news_story_members_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_story_members
    ADD CONSTRAINT news_story_members_item_id_fkey FOREIGN KEY (item_id) REFERENCES public.news_items(item_id) ON DELETE RESTRICT;


--
-- Name: news_story_members news_story_members_story_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_story_members
    ADD CONSTRAINT news_story_members_story_id_fkey FOREIGN KEY (story_id) REFERENCES public.news_stories(story_id) ON DELETE RESTRICT;


--
-- Name: price_feeds price_feeds_base_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_feeds
    ADD CONSTRAINT price_feeds_base_asset_id_fkey FOREIGN KEY (base_asset_id) REFERENCES public.registry_assets(asset_id) ON DELETE SET NULL;


--
-- Name: price_feeds price_feeds_base_cex_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_feeds
    ADD CONSTRAINT price_feeds_base_cex_token_id_fkey FOREIGN KEY (base_cex_token_id) REFERENCES public.cex_tokens(cex_token_id) ON DELETE SET NULL;


--
-- Name: token_evidence token_evidence_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_evidence
    ADD CONSTRAINT token_evidence_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(event_id) ON DELETE CASCADE;


--
-- Name: token_intent_evidence token_intent_evidence_evidence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intent_evidence
    ADD CONSTRAINT token_intent_evidence_evidence_id_fkey FOREIGN KEY (evidence_id) REFERENCES public.token_evidence(evidence_id) ON DELETE CASCADE;


--
-- Name: token_intent_evidence token_intent_evidence_intent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intent_evidence
    ADD CONSTRAINT token_intent_evidence_intent_id_fkey FOREIGN KEY (intent_id) REFERENCES public.token_intents(intent_id) ON DELETE CASCADE;


--
-- Name: token_intent_lookup_keys token_intent_lookup_keys_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intent_lookup_keys
    ADD CONSTRAINT token_intent_lookup_keys_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(event_id) ON DELETE CASCADE;


--
-- Name: token_intent_lookup_keys token_intent_lookup_keys_intent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intent_lookup_keys
    ADD CONSTRAINT token_intent_lookup_keys_intent_id_fkey FOREIGN KEY (intent_id) REFERENCES public.token_intents(intent_id) ON DELETE CASCADE;


--
-- Name: token_intent_lookup_keys token_intent_lookup_keys_source_evidence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intent_lookup_keys
    ADD CONSTRAINT token_intent_lookup_keys_source_evidence_id_fkey FOREIGN KEY (source_evidence_id) REFERENCES public.token_evidence(evidence_id) ON DELETE SET NULL;


--
-- Name: token_intent_resolutions token_intent_resolutions_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intent_resolutions
    ADD CONSTRAINT token_intent_resolutions_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(event_id) ON DELETE CASCADE;


--
-- Name: token_intent_resolutions token_intent_resolutions_intent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intent_resolutions
    ADD CONSTRAINT token_intent_resolutions_intent_id_fkey FOREIGN KEY (intent_id) REFERENCES public.token_intents(intent_id) ON DELETE CASCADE;


--
-- Name: token_intents token_intents_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intents
    ADD CONSTRAINT token_intents_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(event_id) ON DELETE CASCADE;


--
-- Name: token_intents token_intents_primary_evidence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_intents
    ADD CONSTRAINT token_intents_primary_evidence_id_fkey FOREIGN KEY (primary_evidence_id) REFERENCES public.token_evidence(evidence_id) ON DELETE SET NULL;

--
-- Current-schema structural seeds
--

INSERT INTO public.checkpoint_migrations(v)
SELECT generate_series(0, 9);

INSERT INTO public.news_brief_current(singleton_key, updated_at_ms)
VALUES (true, 0);


--
-- PostgreSQL database dump complete
--
