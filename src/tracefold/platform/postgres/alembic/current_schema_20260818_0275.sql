--
-- PostgreSQL database dump
--

-- Dumped from database version 18.6 (Debian 18.6-1.pgdg12+2)
-- Dumped by pg_dump version 18.6 (Debian 18.6-1.pgdg12+2)

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

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- *not* creating schema, since initdb creates it

--
-- Name: pg_stat_statements; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

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
-- Name: news_strategy_provenance_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_strategy_provenance_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT CASE jsonb_typeof(value)
            WHEN 'array' THEN NOT EXISTS (
              SELECT 1
                FROM jsonb_array_elements(value) AS strategy(entry)
               WHERE jsonb_typeof(strategy.entry) IS DISTINCT FROM 'object'
                  OR jsonb_typeof(strategy.entry -> 'id') IS DISTINCT FROM 'string'
                  OR btrim(strategy.entry ->> 'id') = ''
                  OR char_length(strategy.entry ->> 'id') > 128
            )
            ELSE false
          END
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
    heat_tier text DEFAULT 'cold'::text NOT NULL,
    terminal_reason text,
    CONSTRAINT asset_profile_refresh_targets_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT asset_profile_refresh_targets_heat_tier_check CHECK ((heat_tier = ANY (ARRAY['hot'::text, 'warm'::text, 'cold'::text])))
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
)
WITH (autovacuum_analyze_scale_factor='0.01', autovacuum_analyze_threshold='10000', autovacuum_vacuum_scale_factor='0.01', autovacuum_vacuum_threshold='10000', autovacuum_vacuum_insert_scale_factor='0.01', autovacuum_vacuum_insert_threshold='10000');

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
    CONSTRAINT macro_acquisition_targets_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'claimed'::text, 'current'::text, 'delayed'::text, 'stale'::text, 'unavailable'::text, 'backfilling'::text]))),
    CONSTRAINT macro_acquisition_targets_target_key_check CHECK ((btrim(target_key) <> ''::text))
);

--
-- Name: macro_dataset_projection_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_dataset_projection_states (
    dataset_id text NOT NULL,
    material_fingerprint text NOT NULL,
    acquisition_status text NOT NULL,
    source_frontier_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL
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
-- Name: macro_module_current; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_module_current (
    module_id text NOT NULL,
    fact_cutoff_ms bigint NOT NULL,
    payload_json jsonb NOT NULL,
    payload_hash text NOT NULL,
    updated_at_ms bigint NOT NULL,
    current_health_state text CONSTRAINT macro_module_current_data_health_state_not_null NOT NULL,
    history_depth_state text NOT NULL,
    CONSTRAINT macro_module_current_fact_cutoff_ms_check CHECK ((fact_cutoff_ms >= 0)),
    CONSTRAINT macro_module_current_health_check CHECK ((current_health_state = ANY (ARRAY['current'::text, 'degraded'::text, 'unavailable'::text]))),
    CONSTRAINT macro_module_current_history_depth_check CHECK ((history_depth_state = ANY (ARRAY['complete'::text, 'partial'::text, 'insufficient'::text, 'not_required'::text]))),
    CONSTRAINT macro_module_current_module_id_check CHECK ((module_id = ANY (ARRAY['rates_fed'::text, 'economy_inflation'::text, 'liquidity_funding'::text, 'credit'::text, 'volatility'::text, 'cross_asset'::text]))),
    CONSTRAINT macro_module_current_payload_hash_check CHECK ((btrim(payload_hash) <> ''::text)),
    CONSTRAINT macro_module_current_payload_json_check CHECK ((jsonb_typeof(payload_json) = 'object'::text)),
    CONSTRAINT macro_module_current_typed_schema_check CHECK (((payload_json ->> 'schema_version'::text) =
CASE module_id
    WHEN 'rates_fed'::text THEN 'macro_rates_fed_v8'::text
    WHEN 'economy_inflation'::text THEN 'macro_economy_inflation_v6'::text
    WHEN 'liquidity_funding'::text THEN 'macro_liquidity_funding_v5'::text
    WHEN 'credit'::text THEN 'macro_credit_v7'::text
    WHEN 'volatility'::text THEN 'macro_volatility_v7'::text
    WHEN 'cross_asset'::text THEN 'macro_cross_asset_v8'::text
    ELSE NULL::text
END)),
    CONSTRAINT macro_module_current_updated_at_ms_check CHECK ((updated_at_ms >= 0))
);

--
-- Name: macro_module_frontiers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.macro_module_frontiers (
    module_id text NOT NULL,
    status text NOT NULL,
    first_dirty_at_ms bigint,
    deadline_at_ms bigint,
    next_attempt_at_ms bigint,
    attempt_count integer DEFAULT 0 NOT NULL,
    transient_failure_count integer DEFAULT 0 NOT NULL,
    source_frontier_ms bigint,
    input_fingerprint text,
    projection_version text NOT NULL,
    claimed_by uuid,
    claimed_until_ms bigint,
    last_error_code text,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT macro_module_frontiers_attempt_check CHECK (((attempt_count >= 0) AND (transient_failure_count >= 0))),
    CONSTRAINT macro_module_frontiers_status_check CHECK ((status = ANY (ARRAY['clean'::text, 'dirty'::text, 'running'::text, 'retry_wait'::text, 'quarantined'::text])))
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
    published_at_ms bigint,
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
    CONSTRAINT macro_release_facts_clock_check CHECK (((received_at_ms >= 0) AND ((published_at_ms IS NULL) OR ((published_at_ms >= 0) AND (received_at_ms >= published_at_ms))))),
    CONSTRAINT macro_release_facts_dataset_id_check CHECK ((btrim(dataset_id) <> ''::text)),
    CONSTRAINT macro_release_facts_estimate_value_check CHECK (((estimate_value IS NULL) OR ((estimate_value <> 'NaN'::double precision) AND (estimate_value <> 'Infinity'::double precision) AND (estimate_value <> '-Infinity'::double precision)))),
    CONSTRAINT macro_release_facts_fact_hash_check CHECK ((btrim(fact_hash) <> ''::text)),
    CONSTRAINT macro_release_facts_importance_tier_check CHECK (((importance_tier >= 1) AND (importance_tier <= 3))),
    CONSTRAINT macro_release_facts_prior_value_check CHECK (((prior_value IS NULL) OR ((prior_value <> 'NaN'::double precision) AND (prior_value <> 'Infinity'::double precision) AND (prior_value <> '-Infinity'::double precision)))),
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
    fact_schema_version text NOT NULL,
    contract_expiration_date date,
    CONSTRAINT market_settlements_check CHECK (((received_at_ms >= 0) AND ((published_at_ms IS NULL) OR (received_at_ms >= published_at_ms)))),
    CONSTRAINT market_settlements_contract_code_check CHECK ((btrim(contract_code) <> ''::text)),
    CONSTRAINT market_settlements_dataset_id_check CHECK ((btrim(dataset_id) <> ''::text)),
    CONSTRAINT market_settlements_fact_hash_check CHECK ((btrim(fact_hash) <> ''::text)),
    CONSTRAINT market_settlements_fact_schema_check CHECK ((((fact_schema_version = 'market_settlement_v1'::text) AND (contract_expiration_date IS NULL)) OR ((fact_schema_version = 'market_settlement_v2'::text) AND (contract_expiration_date IS NOT NULL)))),
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
-- Name: news_control_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_control_state (
    singleton_key text DEFAULT 'current'::text NOT NULL,
    paused boolean DEFAULT false NOT NULL,
    mutes jsonb DEFAULT '[]'::jsonb NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_control_state_singleton_key_check CHECK ((singleton_key = 'current'::text))
);

--
-- Name: news_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_deliveries (
    event_id text NOT NULL,
    kind text NOT NULL,
    state text NOT NULL,
    card jsonb DEFAULT '{}'::jsonb NOT NULL,
    receipt jsonb,
    error_code text,
    attempted_at_ms bigint NOT NULL,
    settled_at_ms bigint,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_deliveries_kind_check CHECK ((kind = ANY (ARRAY['first'::text, 'followup'::text]))),
    CONSTRAINT news_deliveries_state_check CHECK ((state = ANY (ARRAY['sending'::text, 'sent'::text, 'terminal'::text])))
);

--
-- Name: news_event_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_event_assets (
    symbol text NOT NULL,
    event_id text NOT NULL,
    market_type text,
    opened_at_ms bigint NOT NULL
);

--
-- Name: news_event_bands; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_event_bands (
    band_index smallint NOT NULL,
    band_key text NOT NULL,
    event_id text NOT NULL,
    family text NOT NULL,
    expires_at_ms bigint NOT NULL
);

--
-- Name: news_event_labels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_event_labels (
    event_id text NOT NULL,
    label_version text NOT NULL,
    source text NOT NULL,
    label jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_event_labels_source_check CHECK ((source = ANY (ARRAY['market'::text, 'human'::text, 'dual_model'::text])))
);

--
-- Name: news_event_market_marks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_event_market_marks (
    event_id text NOT NULL,
    mark text NOT NULL,
    symbol text NOT NULL,
    market_type text,
    price double precision,
    open_interest double precision,
    price_change_pct double precision,
    oi_change_pct double precision,
    captured_at_ms bigint NOT NULL,
    CONSTRAINT news_event_market_marks_mark_check CHECK ((mark = ANY (ARRAY['t0'::text, '5m'::text, '30m'::text, '4h'::text])))
);

--
-- Name: news_event_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_event_members (
    event_id text NOT NULL,
    item_id text NOT NULL,
    joined_at_ms bigint NOT NULL,
    match_kind text NOT NULL,
    jaccard_estimate double precision,
    CONSTRAINT news_event_members_match_kind_check CHECK ((match_kind = ANY (ARRAY['leader'::text, 'exact'::text, 'near'::text])))
);

--
-- Name: news_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_events (
    event_id text NOT NULL,
    leader_item_id text NOT NULL,
    family text NOT NULL,
    comparison_fingerprint text NOT NULL,
    comparison_title text NOT NULL,
    leader_title text NOT NULL,
    opened_at_ms bigint NOT NULL,
    last_member_at_ms bigint NOT NULL,
    expires_at_ms bigint NOT NULL,
    member_count integer DEFAULT 1 NOT NULL,
    admission text NOT NULL,
    priority text DEFAULT 'normal'::text NOT NULL,
    provider_score_max double precision,
    engine_type text DEFAULT 'unknown'::text NOT NULL,
    asset_class text DEFAULT 'none'::text NOT NULL,
    grounded_assets jsonb DEFAULT '[]'::jsonb NOT NULL,
    watchlist_hits jsonb DEFAULT '[]'::jsonb NOT NULL,
    macro_lexicon boolean DEFAULT false NOT NULL,
    storyline_key text DEFAULT ''::text NOT NULL,
    context_line text DEFAULT ''::text NOT NULL,
    search_doc tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, ((COALESCE(context_line, ''::text) || ' '::text) || COALESCE(leader_title, ''::text)))) STORED,
    published_at_ms bigint,
    followup_of text,
    ingest_mode text NOT NULL,
    trace_id text DEFAULT ''::text NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_events_ingest_mode_check CHECK ((ingest_mode = ANY (ARRAY['live'::text, 'recovery'::text]))),
    CONSTRAINT news_events_priority_check CHECK ((priority = ANY (ARRAY['high'::text, 'normal'::text])))
);

--
-- Name: news_ingest_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_ingest_state (
    singleton_key text DEFAULT 'opennews'::text NOT NULL,
    connected boolean DEFAULT false NOT NULL,
    last_frame_at_ms bigint,
    last_publish_at_ms bigint,
    last_error_code text,
    configured_strategy_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    provider_enabled_strategy_ids jsonb,
    strategy_warnings jsonb DEFAULT '[]'::jsonb NOT NULL,
    broker_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_ingest_state_singleton_key_check CHECK ((singleton_key = 'opennews'::text))
);

--
-- Name: news_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_items (
    item_id text NOT NULL,
    source_id text NOT NULL,
    source_item_key text NOT NULL,
    title text NOT NULL,
    raw_first_line text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    canonical_url text,
    reporting_origin text DEFAULT ''::text NOT NULL,
    published_at_ms bigint NOT NULL,
    observed_at_ms bigint NOT NULL,
    provider_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    provenance jsonb DEFAULT '[]'::jsonb NOT NULL,
    first_ingest_mode text NOT NULL,
    trace_id text DEFAULT ''::text NOT NULL,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_items_first_ingest_mode_check CHECK ((first_ingest_mode = ANY (ARRAY['live'::text, 'recovery'::text])))
);

--
-- Name: news_opennews_incidents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_opennews_incidents (
    incident_id bigint NOT NULL,
    cause_class text NOT NULL,
    opened_at_ms bigint NOT NULL,
    closed_at_ms bigint,
    planned boolean DEFAULT false NOT NULL,
    close_code integer,
    recovery_status text DEFAULT 'pending'::text NOT NULL,
    recovery_from_at_ms bigint,
    recovery_to_at_ms bigint,
    recovered_count integer DEFAULT 0 NOT NULL,
    last_error_code text,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_opennews_incidents_cause_class_check CHECK ((cause_class = ANY (ARRAY['planned_shutdown'::text, 'network_connect'::text, 'authentication'::text, 'provider_close'::text, 'protocol_error'::text, 'idle_timeout'::text, 'broker_backpressure'::text, 'broker_unavailable'::text, 'process_outage'::text, 'triage_circuit_open'::text, 'unknown'::text]))),
    CONSTRAINT news_opennews_incidents_check CHECK (((closed_at_ms IS NULL) OR (closed_at_ms >= opened_at_ms))),
    CONSTRAINT news_opennews_incidents_opened_at_ms_check CHECK ((opened_at_ms >= 0)),
    CONSTRAINT news_opennews_incidents_recovery_status_check CHECK ((recovery_status = ANY (ARRAY['pending'::text, 'recovered'::text, 'partial'::text, 'unavailable'::text, 'not_applicable'::text])))
);

--
-- Name: news_opennews_incidents_incident_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.news_opennews_incidents_incident_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: news_opennews_incidents_incident_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.news_opennews_incidents_incident_id_seq OWNED BY public.news_opennews_incidents.incident_id;

--
-- Name: news_title_presentations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_title_presentations (
    comparison_fingerprint text NOT NULL,
    original_title text NOT NULL,
    display_title text NOT NULL,
    outcome text NOT NULL,
    provider text,
    fallback_code text,
    policy_version text NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_title_presentations_outcome_check CHECK ((outcome = ANY (ARRAY['translated'::text, 'not_needed'::text, 'fallback'::text])))
);

--
-- Name: news_verdicts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_verdicts (
    event_id text NOT NULL,
    stage text NOT NULL,
    policy_version text NOT NULL,
    model_decision text,
    rule_baseline_decision text NOT NULL,
    final_decision text NOT NULL,
    override_rule text,
    throttled_by text,
    verdict jsonb DEFAULT '{}'::jsonb NOT NULL,
    model text,
    prompt_version text,
    degraded boolean DEFAULT false NOT NULL,
    error_code text,
    trace jsonb DEFAULT '{}'::jsonb NOT NULL,
    published_at_ms bigint,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_verdicts_final_decision_check CHECK ((final_decision = ANY (ARRAY['push'::text, 'escalate'::text, 'drop'::text, 'throttled'::text, 'degraded'::text]))),
    CONSTRAINT news_verdicts_stage_check CHECK ((stage = ANY (ARRAY['triage'::text, 'deep'::text])))
);

--
-- Name: persisted_live_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.persisted_live_events (
    cursor bigint NOT NULL,
    source_key text NOT NULL,
    event_kind text NOT NULL,
    target_type text,
    target_id text,
    payload_json jsonb NOT NULL,
    committed_at_ms bigint NOT NULL,
    CONSTRAINT persisted_live_events_kind_check CHECK ((event_kind = ANY (ARRAY['event'::text, 'live_market_update'::text]))),
    CONSTRAINT persisted_live_events_target_pair_check CHECK (((target_type IS NULL) = (target_id IS NULL)))
);

--
-- Name: persisted_live_events_cursor_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.persisted_live_events ALTER COLUMN cursor ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.persisted_live_events_cursor_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
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
-- Name: provider_circuit_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provider_circuit_state (
    provider text NOT NULL,
    status text NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    opened_at_ms bigint,
    next_probe_at_ms bigint,
    last_error text,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT provider_circuit_state_failure_count_check CHECK ((consecutive_failures >= 0)),
    CONSTRAINT provider_circuit_state_open_fields_check CHECK (((status = 'closed'::text) OR ((opened_at_ms IS NOT NULL) AND (next_probe_at_ms IS NOT NULL)))),
    CONSTRAINT provider_circuit_state_status_check CHECK ((status = ANY (ARRAY['closed'::text, 'open'::text])))
);

--
-- Name: queue_terminal_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.queue_terminal_events (
    terminal_id text CONSTRAINT worker_queue_terminal_events_terminal_id_not_null NOT NULL,
    owner_key text CONSTRAINT worker_queue_terminal_events_worker_name_not_null NOT NULL,
    source_table text CONSTRAINT worker_queue_terminal_events_source_table_not_null NOT NULL,
    target_key text CONSTRAINT worker_queue_terminal_events_target_key_not_null NOT NULL,
    source_row_json jsonb CONSTRAINT worker_queue_terminal_events_source_row_json_not_null NOT NULL,
    source_row_hash text CONSTRAINT worker_queue_terminal_events_source_row_hash_not_null NOT NULL,
    final_status text CONSTRAINT worker_queue_terminal_events_final_status_not_null NOT NULL,
    final_reason text CONSTRAINT worker_queue_terminal_events_final_reason_not_null NOT NULL,
    attempt_count integer DEFAULT 0 CONSTRAINT worker_queue_terminal_events_attempt_count_not_null NOT NULL,
    payload_hash text DEFAULT ''::text CONSTRAINT worker_queue_terminal_events_payload_hash_not_null NOT NULL,
    first_seen_at_ms bigint,
    last_attempted_at_ms bigint,
    terminalized_at_ms bigint CONSTRAINT worker_queue_terminal_events_terminalized_at_ms_not_null NOT NULL,
    terminal_generation integer DEFAULT 1 CONSTRAINT worker_queue_terminal_events_terminal_generation_not_null NOT NULL,
    operator_action text,
    operator_reason text,
    operator_action_at_ms bigint,
    final_reason_bucket text DEFAULT 'other'::text CONSTRAINT worker_queue_terminal_events_final_reason_bucket_not_null NOT NULL,
    CONSTRAINT queue_terminal_events_owner_key_check CHECK ((owner_key = ANY (ARRAY['event_anchor_backfill'::text, 'resolution_refresh'::text, 'asset_profile_refresh'::text, 'token_image_mirror'::text, 'profile_projection'::text, 'macro_projection'::text, 'news_brief'::text, 'macro_document_analysis'::text])))
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
    reprocess_lookup_keys text[],
    reprocess_after_intent_id text,
    reprocess_resolved boolean DEFAULT false NOT NULL,
    reprocess_queue_due_at_ms bigint,
    CONSTRAINT token_discovery_dirty_lookup_keys_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT token_discovery_dirty_lookup_keys_lookup_type_check CHECK ((lookup_type = ANY (ARRAY['dex_symbol_lookup'::text, 'address_lookup'::text]))),
    CONSTRAINT token_discovery_reprocess_continuation_check CHECK ((((reprocess_lookup_keys IS NULL) AND (reprocess_after_intent_id IS NULL) AND (reprocess_resolved = false) AND (reprocess_queue_due_at_ms IS NULL)) OR ((cardinality(reprocess_lookup_keys) > 0) AND ((reprocess_after_intent_id IS NULL) OR (length(reprocess_after_intent_id) > 0)) AND (reprocess_queue_due_at_ms IS NOT NULL) AND (reprocess_queue_due_at_ms >= 0))))
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
-- Name: token_profile_projection_frontiers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_profile_projection_frontiers (
    target_type text NOT NULL,
    target_id text NOT NULL,
    status text NOT NULL,
    first_dirty_at_ms bigint,
    deadline_at_ms bigint,
    next_attempt_at_ms bigint,
    attempt_count integer DEFAULT 0 NOT NULL,
    transient_failure_count integer DEFAULT 0 CONSTRAINT token_profile_projection_front_transient_failure_count_not_null NOT NULL,
    input_fingerprint text,
    projection_version text NOT NULL,
    claimed_by uuid,
    claimed_until_ms bigint,
    last_error_code text,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT token_profile_projection_frontiers_attempt_check CHECK (((attempt_count >= 0) AND (transient_failure_count >= 0))),
    CONSTRAINT token_profile_projection_frontiers_status_check CHECK ((status = ANY (ARRAY['clean'::text, 'dirty'::text, 'running'::text, 'retry_wait'::text, 'quarantined'::text])))
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
-- Name: workers_runtime; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workers_runtime (
    singleton_key boolean DEFAULT true NOT NULL,
    runtime_id uuid NOT NULL,
    runtime_version text NOT NULL,
    lifecycle_state text NOT NULL,
    started_at_ms bigint NOT NULL,
    heartbeat_at_ms bigint NOT NULL,
    fatal_code text,
    CONSTRAINT workers_runtime_check CHECK ((heartbeat_at_ms >= started_at_ms)),
    CONSTRAINT workers_runtime_check1 CHECK ((((lifecycle_state = 'failed'::text) AND (fatal_code IS NOT NULL)) OR ((lifecycle_state <> 'failed'::text) AND (fatal_code IS NULL)))),
    CONSTRAINT workers_runtime_fatal_code_check CHECK (((fatal_code IS NULL) OR (fatal_code = ANY (ARRAY['startup_failed'::text, 'child_failed'::text, 'control_failed'::text, 'singleton_lost'::text, 'runtime_invariant_failed'::text, 'resource_operation_overrun'::text, 'graceful_deadline_exceeded'::text, 'cleanup_failed'::text])))),
    CONSTRAINT workers_runtime_lifecycle_state_check CHECK ((lifecycle_state = ANY (ARRAY['starting'::text, 'running'::text, 'stopping'::text, 'stopped'::text, 'failed'::text]))),
    CONSTRAINT workers_runtime_runtime_version_check CHECK ((btrim(runtime_version) <> ''::text)),
    CONSTRAINT workers_runtime_singleton_key_check CHECK (singleton_key),
    CONSTRAINT workers_runtime_started_at_ms_check CHECK ((started_at_ms >= 0))
);

--
-- Name: market_ticks_default; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_ticks ATTACH PARTITION public.market_ticks_default DEFAULT;

--
-- Name: news_opennews_incidents incident_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_opennews_incidents ALTER COLUMN incident_id SET DEFAULT nextval('public.news_opennews_incidents_incident_id_seq'::regclass);

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
-- Name: macro_dataset_projection_states macro_dataset_projection_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_dataset_projection_states
    ADD CONSTRAINT macro_dataset_projection_states_pkey PRIMARY KEY (dataset_id);

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
-- Name: macro_module_current macro_module_current_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_module_current
    ADD CONSTRAINT macro_module_current_pkey PRIMARY KEY (module_id);

--
-- Name: macro_module_frontiers macro_module_frontiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_module_frontiers
    ADD CONSTRAINT macro_module_frontiers_pkey PRIMARY KEY (module_id);

--
-- Name: macro_release_facts macro_release_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_release_facts
    ADD CONSTRAINT macro_release_facts_pkey PRIMARY KEY (release_fact_id);

--
-- Name: macro_series_facts macro_series_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.macro_series_facts
    ADD CONSTRAINT macro_series_facts_pkey PRIMARY KEY (fact_id);

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
-- Name: news_control_state news_control_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_control_state
    ADD CONSTRAINT news_control_state_pkey PRIMARY KEY (singleton_key);

--
-- Name: news_deliveries news_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_deliveries
    ADD CONSTRAINT news_deliveries_pkey PRIMARY KEY (event_id, kind);

--
-- Name: news_event_assets news_event_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_assets
    ADD CONSTRAINT news_event_assets_pkey PRIMARY KEY (symbol, event_id);

--
-- Name: news_event_bands news_event_bands_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_bands
    ADD CONSTRAINT news_event_bands_pkey PRIMARY KEY (band_index, band_key, event_id);

--
-- Name: news_event_labels news_event_labels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_labels
    ADD CONSTRAINT news_event_labels_pkey PRIMARY KEY (event_id, label_version);

--
-- Name: news_event_market_marks news_event_market_marks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_market_marks
    ADD CONSTRAINT news_event_market_marks_pkey PRIMARY KEY (event_id, mark, symbol);

--
-- Name: news_event_members news_event_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_members
    ADD CONSTRAINT news_event_members_pkey PRIMARY KEY (event_id, item_id);

--
-- Name: news_events news_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_events
    ADD CONSTRAINT news_events_pkey PRIMARY KEY (event_id);

--
-- Name: news_ingest_state news_ingest_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_ingest_state
    ADD CONSTRAINT news_ingest_state_pkey PRIMARY KEY (singleton_key);

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
-- Name: news_opennews_incidents news_opennews_incidents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_opennews_incidents
    ADD CONSTRAINT news_opennews_incidents_pkey PRIMARY KEY (incident_id);

--
-- Name: news_title_presentations news_title_presentations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_title_presentations
    ADD CONSTRAINT news_title_presentations_pkey PRIMARY KEY (comparison_fingerprint);

--
-- Name: news_verdicts news_verdicts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_verdicts
    ADD CONSTRAINT news_verdicts_pkey PRIMARY KEY (event_id, stage, policy_version);

--
-- Name: persisted_live_events persisted_live_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persisted_live_events
    ADD CONSTRAINT persisted_live_events_pkey PRIMARY KEY (cursor);

--
-- Name: persisted_live_events persisted_live_events_source_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persisted_live_events
    ADD CONSTRAINT persisted_live_events_source_key_key UNIQUE (source_key);

--
-- Name: price_feeds price_feeds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_feeds
    ADD CONSTRAINT price_feeds_pkey PRIMARY KEY (pricefeed_id);

--
-- Name: provider_circuit_state provider_circuit_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_circuit_state
    ADD CONSTRAINT provider_circuit_state_pkey PRIMARY KEY (provider);

--
-- Name: queue_terminal_events queue_terminal_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.queue_terminal_events
    ADD CONSTRAINT queue_terminal_events_pkey PRIMARY KEY (terminal_id);

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
-- Name: token_profile_current token_profile_current_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_profile_current
    ADD CONSTRAINT token_profile_current_pkey PRIMARY KEY (target_type, target_id);

--
-- Name: token_profile_projection_frontiers token_profile_projection_frontiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_profile_projection_frontiers
    ADD CONSTRAINT token_profile_projection_frontiers_pkey PRIMARY KEY (target_type, target_id);

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
-- Name: workers_runtime workers_runtime_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workers_runtime
    ADD CONSTRAINT workers_runtime_pkey PRIMARY KEY (singleton_key);

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
-- Name: idx_asset_identity_evidence_asset_provider_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_identity_evidence_asset_provider_lookup ON public.asset_identity_evidence USING btree (asset_id, provider, lookup_mode, observed_at_ms DESC, evidence_id DESC);

--
-- Name: idx_asset_identity_evidence_kind_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_identity_evidence_kind_time ON public.asset_identity_evidence USING btree (evidence_kind, observed_at_ms DESC);

--
-- Name: idx_asset_identity_evidence_profile_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_identity_evidence_profile_source ON public.asset_identity_evidence USING btree (provider, evidence_kind, asset_id, observed_at_ms DESC, evidence_id DESC);

--
-- Name: idx_asset_identity_evidence_provider_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_identity_evidence_provider_lookup ON public.asset_identity_evidence USING btree (provider, lookup_mode, observed_at_ms DESC);

--
-- Name: idx_asset_profile_refresh_targets_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_profile_refresh_targets_due ON public.asset_profile_refresh_targets USING btree (provider, priority, due_at_ms, updated_at_ms, target_type, target_id, heat_tier) WHERE (terminal_reason IS NULL);

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

CREATE INDEX idx_events_received ON public.events USING btree (received_at_ms, event_id);

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

CREATE INDEX idx_macro_acquisition_targets_due ON public.macro_acquisition_targets USING btree (clock_kind, priority, next_due_at_ms, target_key) WHERE (status = ANY (ARRAY['pending'::text, 'claimed'::text, 'current'::text, 'delayed'::text, 'backfilling'::text]));

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
-- Name: idx_macro_module_frontiers_eligible; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_module_frontiers_eligible ON public.macro_module_frontiers USING btree (COALESCE(next_attempt_at_ms, first_dirty_at_ms, deadline_at_ms), deadline_at_ms, module_id) WHERE (status = ANY (ARRAY['dirty'::text, 'retry_wait'::text]));

--
-- Name: idx_macro_release_facts_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_release_facts_latest ON public.macro_release_facts USING btree (dataset_id, published_at_ms DESC);

--
-- Name: idx_macro_series_facts_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_macro_series_facts_latest ON public.macro_series_facts USING btree (dataset_id, series_id, reference_date DESC, vintage_date DESC);

--
-- Name: idx_market_observations_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_market_observations_latest ON public.market_observations USING btree (instrument_id, field_name, observed_at_ms DESC);

--
-- Name: idx_market_observations_projection_history; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_market_observations_projection_history ON public.market_observations USING btree (dataset_id, ((observed_at_ms / 86400000)) DESC, observed_at_ms DESC, received_at_ms DESC, observation_id DESC) INCLUDE (instrument_id, source_id, field_name, value_numeric, unit, published_at_ms, trust_tier, source_url, fact_hash);

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
-- Name: idx_persisted_live_events_cursor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_persisted_live_events_cursor ON public.persisted_live_events USING btree (cursor);

--
-- Name: idx_persisted_live_events_target_cursor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_persisted_live_events_target_cursor ON public.persisted_live_events USING btree (target_type, target_id, cursor) WHERE (target_type IS NOT NULL);

--
-- Name: idx_price_feeds_cex_canonical_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_price_feeds_cex_canonical_updated ON public.price_feeds USING btree (subject_id, updated_at_ms DESC, native_market_id) WHERE ((subject_type = 'CexToken'::text) AND (provider = 'binance'::text) AND (feed_type = 'cex_swap'::text) AND (quote_symbol = 'USDT'::text) AND (status = 'canonical'::text));

--
-- Name: idx_price_feeds_cex_subject_preferred; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_price_feeds_cex_subject_preferred ON public.price_feeds USING btree (subject_type, subject_id, feed_type, status, updated_at_ms DESC, native_market_id) WHERE ((subject_type = 'CexToken'::text) AND (status = ANY (ARRAY['candidate'::text, 'canonical'::text])));

--
-- Name: idx_provider_circuit_state_probe; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_provider_circuit_state_probe ON public.provider_circuit_state USING btree (next_probe_at_ms, provider) WHERE (status = 'open'::text);

--
-- Name: idx_queue_terminal_reason_bucket_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_queue_terminal_reason_bucket_unresolved ON public.queue_terminal_events USING btree (owner_key, source_table, final_reason_bucket, terminalized_at_ms DESC) WHERE (operator_action IS NULL);

--
-- Name: idx_queue_terminal_resolved_retention; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_queue_terminal_resolved_retention ON public.queue_terminal_events USING btree (COALESCE(operator_action_at_ms, terminalized_at_ms), terminal_id) WHERE (operator_action IS NOT NULL);

--
-- Name: idx_queue_terminal_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_queue_terminal_source ON public.queue_terminal_events USING btree (source_table, owner_key);

--
-- Name: idx_queue_terminal_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_queue_terminal_unresolved ON public.queue_terminal_events USING btree (owner_key, source_table, terminalized_at_ms DESC) WHERE (operator_action IS NULL);

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

CREATE INDEX idx_token_intent_lookup_keys_intent_lookup ON public.token_intent_lookup_keys USING btree (intent_id, lookup_key) INCLUDE (event_id);

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
-- Name: idx_token_intents_event_intent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_intents_event_intent ON public.token_intents USING btree (event_id, intent_id);

--
-- Name: idx_token_intents_market_targets_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_intents_market_targets_created ON public.token_intents USING btree (created_at_ms, intent_id) INCLUDE (event_id);

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
-- Name: idx_token_profile_projection_frontiers_eligible; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_profile_projection_frontiers_eligible ON public.token_profile_projection_frontiers USING btree (COALESCE(next_attempt_at_ms, first_dirty_at_ms, deadline_at_ms), deadline_at_ms, target_type, target_id) WHERE (status = ANY (ARRAY['dirty'::text, 'retry_wait'::text]));

--
-- Name: idx_us_equity_symbols_active_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_us_equity_symbols_active_lookup ON public.us_equity_symbols USING btree (symbol) WHERE (status = 'active'::text);

--
-- Name: idx_us_equity_symbols_source_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_us_equity_symbols_source_status ON public.us_equity_symbols USING btree (source, status);

--
-- Name: ix_news_deliveries_sent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_deliveries_sent ON public.news_deliveries USING btree (settled_at_ms DESC) WHERE (state = 'sent'::text);

--
-- Name: ix_news_deliveries_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_deliveries_state ON public.news_deliveries USING btree (state, attempted_at_ms DESC);

--
-- Name: ix_news_event_assets_symbol; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_event_assets_symbol ON public.news_event_assets USING btree (symbol, opened_at_ms DESC);

--
-- Name: ix_news_event_bands_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_event_bands_expires ON public.news_event_bands USING btree (expires_at_ms);

--
-- Name: ix_news_event_bands_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_event_bands_lookup ON public.news_event_bands USING btree (band_index, band_key, family, expires_at_ms);

--
-- Name: ix_news_event_members_item; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_event_members_item ON public.news_event_members USING btree (item_id);

--
-- Name: ix_news_events_admission; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_events_admission ON public.news_events USING btree (admission, opened_at_ms DESC);

--
-- Name: ix_news_events_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_events_expires ON public.news_events USING btree (expires_at_ms);

--
-- Name: ix_news_events_fingerprint; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_events_fingerprint ON public.news_events USING btree (family, comparison_fingerprint, opened_at_ms DESC);

--
-- Name: ix_news_events_opened; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_events_opened ON public.news_events USING btree (opened_at_ms DESC);

--
-- Name: ix_news_events_search; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_events_search ON public.news_events USING gin (search_doc);

--
-- Name: ix_news_events_storyline; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_events_storyline ON public.news_events USING btree (storyline_key, opened_at_ms DESC);

--
-- Name: ix_news_events_unpublished; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_events_unpublished ON public.news_events USING btree (opened_at_ms) WHERE ((published_at_ms IS NULL) AND (admission = 'candidate'::text));

--
-- Name: ix_news_incidents_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_incidents_open ON public.news_opennews_incidents USING btree (closed_at_ms) WHERE (closed_at_ms IS NULL);

--
-- Name: ix_news_incidents_recovery; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_incidents_recovery ON public.news_opennews_incidents USING btree (recovery_status, incident_id) WHERE (recovery_status = 'pending'::text);

--
-- Name: ix_news_items_published; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_items_published ON public.news_items USING btree (published_at_ms DESC);

--
-- Name: ix_news_marks_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_marks_due ON public.news_event_market_marks USING btree (captured_at_ms);

--
-- Name: ix_news_verdicts_final; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_verdicts_final ON public.news_verdicts USING btree (final_decision, created_at_ms DESC);

--
-- Name: ix_news_verdicts_stage_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_verdicts_stage_created ON public.news_verdicts USING btree (stage, created_at_ms DESC);

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
-- Name: uq_queue_terminal_one_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_queue_terminal_one_unresolved ON public.queue_terminal_events USING btree (owner_key, source_table, target_key) WHERE (operator_action IS NULL);

--
-- Name: uq_queue_terminal_source_snapshot; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_queue_terminal_source_snapshot ON public.queue_terminal_events USING btree (owner_key, source_table, target_key, source_row_hash, terminal_generation);

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

CREATE UNIQUE INDEX ux_token_intent_current_resolution ON public.token_intent_resolutions USING btree (intent_id) INCLUDE (resolution_status, target_type, target_id) WHERE (is_current = true);

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
-- Name: macro_document_analyses macro_document_analyses_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_document_analyses_append_only BEFORE DELETE OR UPDATE ON public.macro_document_analyses FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();

--
-- Name: macro_documents macro_documents_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_documents_append_only BEFORE DELETE OR UPDATE ON public.macro_documents FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();

--
-- Name: macro_fed_official_role_facts macro_fed_official_role_facts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_fed_official_role_facts_append_only BEFORE DELETE OR UPDATE ON public.macro_fed_official_role_facts FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();

--
-- Name: macro_release_facts macro_release_facts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_release_facts_append_only BEFORE DELETE OR UPDATE ON public.macro_release_facts FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();

--
-- Name: macro_series_facts macro_series_facts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER macro_series_facts_append_only BEFORE DELETE OR UPDATE ON public.macro_series_facts FOR EACH ROW EXECUTE FUNCTION public.reject_macro_fact_mutation();

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
-- Name: news_deliveries news_deliveries_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_deliveries
    ADD CONSTRAINT news_deliveries_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;

--
-- Name: news_event_assets news_event_assets_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_assets
    ADD CONSTRAINT news_event_assets_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;

--
-- Name: news_event_bands news_event_bands_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_bands
    ADD CONSTRAINT news_event_bands_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;

--
-- Name: news_event_labels news_event_labels_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_labels
    ADD CONSTRAINT news_event_labels_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;

--
-- Name: news_event_market_marks news_event_market_marks_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_market_marks
    ADD CONSTRAINT news_event_market_marks_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;

--
-- Name: news_event_members news_event_members_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_members
    ADD CONSTRAINT news_event_members_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;

--
-- Name: news_event_members news_event_members_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_members
    ADD CONSTRAINT news_event_members_item_id_fkey FOREIGN KEY (item_id) REFERENCES public.news_items(item_id) ON DELETE CASCADE;

--
-- Name: news_events news_events_leader_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_events
    ADD CONSTRAINT news_events_leader_item_id_fkey FOREIGN KEY (leader_item_id) REFERENCES public.news_items(item_id) ON DELETE CASCADE;

--
-- Name: news_verdicts news_verdicts_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_verdicts
    ADD CONSTRAINT news_verdicts_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;

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
-- PostgreSQL database dump complete
--

--
-- Singleton control rows owned by the News package (created once with the schema).
--

INSERT INTO public.news_ingest_state (singleton_key, updated_at_ms) VALUES ('opennews', 0);
INSERT INTO public.news_control_state (singleton_key, paused, mutes, updated_at_ms)
  VALUES ('current', false, '[]'::jsonb, 0);
