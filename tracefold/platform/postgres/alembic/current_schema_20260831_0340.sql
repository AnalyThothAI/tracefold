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
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- *not* creating schema, since initdb creates it


--
-- Name: materialize_trading_blacklist_expiry(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.materialize_trading_blacklist_expiry() RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
          v_now_ms bigint := floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint;
          v_removed integer := 0;
          v_revision bigint;
        BEGIN
          PERFORM id FROM public.trading_runtime_state WHERE id = 1 FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'trading_runtime_state_missing';
          END IF;
          DELETE FROM public.trading_symbol_blacklist
           WHERE expires_at_ms IS NOT NULL AND expires_at_ms <= v_now_ms;
          GET DIAGNOSTICS v_removed = ROW_COUNT;
          IF v_removed > 0 THEN
            UPDATE public.trading_runtime_state
               SET blacklist_revision = blacklist_revision + 1,
                   updated_at_ms = v_now_ms
             WHERE id = 1
         RETURNING blacklist_revision INTO v_revision;
          ELSE
            SELECT blacklist_revision INTO v_revision
              FROM public.trading_runtime_state WHERE id = 1;
          END IF;
          RETURN v_revision;
        END;
        $$;


--
-- Name: news_canonical_jsonb(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_canonical_jsonb(value jsonb) RETURNS text
    LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
        DECLARE
          result text;
          item record;
          first_item boolean := true;
        BEGIN
          CASE jsonb_typeof(value)
            WHEN 'object' THEN
              result := '{';
              FOR item IN SELECT key, val FROM jsonb_each(value) AS e(key, val) ORDER BY key COLLATE "C" LOOP
                IF NOT first_item THEN result := result || ','; END IF;
                result := result || to_jsonb(item.key)::text || ':' || news_canonical_jsonb(item.val);
                first_item := false;
              END LOOP;
              RETURN result || '}';
            WHEN 'array' THEN
              result := '[';
              FOR item IN SELECT val FROM jsonb_array_elements(value) WITH ORDINALITY AS e(val, ord) ORDER BY ord LOOP
                IF NOT first_item THEN result := result || ','; END IF;
                result := result || news_canonical_jsonb(item.val);
                first_item := false;
              END LOOP;
              RETURN result || ']';
            ELSE
              RETURN value::text;
          END CASE;
        END;
        $$;


--
-- Name: news_current_decision_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_decision_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT news_jsonb_exact_keys(value, ARRAY[
                   'final','override_rule','throttled_by','rule_baseline','watchlist_hits',
                   'seen_similarity','seen_against','seen_scope'
                 ])
             AND value ->> 'final' IN ('push','escalate','drop','throttled')
             AND value ->> 'rule_baseline' IN ('push','escalate','drop','throttled')
             AND jsonb_typeof(value -> 'override_rule') IN ('string','null')
             AND jsonb_typeof(value -> 'throttled_by') IN ('string','null')
             AND jsonb_typeof(value -> 'watchlist_hits') = 'array'
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(value -> 'watchlist_hits') hit
                    WHERE jsonb_typeof(hit) <> 'string'
                 )
             AND jsonb_typeof(value -> 'seen_similarity') IN ('number','null')
             AND (
                   jsonb_typeof(value -> 'seen_similarity') = 'null'
                   OR (value ->> 'seen_similarity')::numeric BETWEEN 0 AND 1
                 )
             AND jsonb_typeof(value -> 'seen_against') = 'number'
             AND (value ->> 'seen_against') ~ '^-?[0-9]+$'
             AND (value ->> 'seen_against')::integer >= -1
             AND jsonb_typeof(value -> 'seen_scope') = 'string'
             AND value ->> 'seen_scope' IN ('','all')
             AND (value ->> 'throttled_by' IS NULL
                  OR right(value ->> 'throttled_by', 5) <> (chr(58) || 'seen')
                  OR (value ->> 'seen_scope' = 'all'
                      AND jsonb_typeof(value -> 'seen_similarity') = 'number'
                      AND (value ->> 'seen_against')::integer >= 0))
        $_$;


--
-- Name: news_current_event_review_payload_valid(jsonb, text, jsonb, jsonb, text, jsonb, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_event_review_payload_valid(value jsonb, expected_should_push text, expected_dimensions jsonb, expected_novelty jsonb, expected_first_bad_owner text, expected_evidence_refs jsonb, expected_correction text, expected_note text) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
          SELECT (
            news_jsonb_exact_keys(value, ARRAY[
              'kind','should_push','dimensions','novelty','first_bad_owner','evidence_refs',
              'expected','taxonomy','taxonomy_review','expected_correction','note'
            ])
            AND value ->> 'kind' = 'event_rubric'
            AND value ->> 'should_push' = expected_should_push
            AND value -> 'dimensions' = expected_dimensions
            AND news_current_review_dimensions_valid(expected_dimensions)
            AND value -> 'novelty' = expected_novelty
            AND news_current_review_novelty_valid(expected_novelty)
            AND expected_first_bad_owner IS NOT NULL
            AND jsonb_typeof(value -> 'first_bad_owner') IN ('string','null')
            AND (jsonb_typeof(value -> 'first_bad_owner') = 'null'
                 OR value ->> 'first_bad_owner' = expected_first_bad_owner)
            AND value -> 'evidence_refs' = expected_evidence_refs
            AND news_current_review_evidence_refs_valid(expected_evidence_refs)
            AND value ->> 'expected_correction' = expected_correction
            AND value ->> 'note' = expected_note
            AND news_current_review_expected_valid(value -> 'expected')
            AND news_current_review_taxonomy_valid(value -> 'taxonomy')
            AND news_current_review_taxonomy_provenance_valid(value -> 'taxonomy_review')
            AND (expected_should_push NOT IN ('must_push','should_push')
                 OR expected_dimensions ? 'timeliness')
            AND (NOT EXISTS (
                   SELECT 1 FROM jsonb_each_text(expected_dimensions) dimension
                    WHERE dimension.value = 'fail'
                 ) OR jsonb_array_length(expected_evidence_refs) > 0)
            AND (jsonb_typeof(value -> 'expected') = 'null' OR (
              (jsonb_typeof(value #> '{expected,magnitude}') = 'null'
               OR expected_dimensions ->> 'magnitude' = 'fail')
              AND (jsonb_typeof(value #> '{expected,direction}') = 'null'
                   OR expected_dimensions ->> 'direction' = 'fail')
              AND (jsonb_typeof(value #> '{expected,assets}') = 'null'
                   OR expected_dimensions ->> 'asset_grounding' = 'fail')
              AND (jsonb_typeof(value #> '{expected,trade_impact_breadth}') = 'null'
                   OR expected_dimensions ->> 'trade_impact_breadth' = 'fail')
              AND (jsonb_typeof(value #> '{expected,trade_tradability}') = 'null'
                   OR expected_dimensions ->> 'trade_tradability' = 'fail')
              AND (jsonb_typeof(value #> '{expected,trade_surprise}') = 'null'
                   OR expected_dimensions ->> 'trade_surprise' = 'fail')
              AND (jsonb_typeof(value #> '{expected,trade_development_delta}') = 'null'
                   OR expected_dimensions ->> 'trade_development_delta' = 'fail')
              AND (jsonb_typeof(value #> '{expected,trade_channels}') = 'null'
                   OR expected_dimensions ->> 'trade_channels' = 'fail')
              AND (jsonb_typeof(value #> '{expected,trade_affected_markets}') = 'null'
                   OR expected_dimensions ->> 'trade_affected_markets' = 'fail')
              AND (jsonb_typeof(value #> '{expected,reader_value}') = 'null'
                   OR expected_dimensions ->> 'reader_value' = 'fail')
            ))
          ) IS TRUE
        $$;


--
-- Name: news_current_evidence_snapshot_valid(jsonb, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_evidence_snapshot_valid(value jsonb, expected_event_id text, expected_focus_fact_id text) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT news_jsonb_exact_keys(value, ARRAY[
                   'schema_version','event_id','focus_fact','card','members','provenance'
                 ])
             AND value ->> 'schema_version' = 'news_event_evidence_v3'
             AND value ->> 'event_id' = expected_event_id
             AND value ->> 'provenance' = 'observed'
             AND news_jsonb_exact_keys(value -> 'focus_fact', ARRAY[
                   'fact_id','text','context','method','span_start','span_end'
                 ])
             AND value #>> '{focus_fact,fact_id}' = expected_focus_fact_id
             AND jsonb_typeof(value #> '{focus_fact,text}') = 'string'
             AND value #>> '{focus_fact,text}' <> ''
             AND jsonb_typeof(value #> '{focus_fact,context}') = 'string'
             AND value #>> '{focus_fact,method}' IN ('whole_item','explicit_numbered')
             AND news_jsonb_int64_valid(value #> '{focus_fact,span_start}')
             AND news_jsonb_int64_valid(value #> '{focus_fact,span_end}')
             AND (value #>> '{focus_fact,span_start}')::numeric >= 0
             AND (value #>> '{focus_fact,span_end}')::numeric >=
                   (value #>> '{focus_fact,span_start}')::numeric
             AND news_jsonb_required_optional_keys(value -> 'card', ARRAY[
                   'event_id','leader_item_id','dedupe_family','event_kind','source_contract_reason',
                   'comparison_fingerprint','comparison_title','opened_at_ms','last_member_at_ms',
                   'expires_at_ms','member_count','admission','queue_priority','provider_score_max',
                   'engine_type','asset_class','grounded_assets','watchlist_hits','macro_lexicon',
                   'storyline_key','ingest_mode','trace_id','leader_url','reporting_origin',
                   'provider_metadata','provenance','leader_published_at_ms','raw_first_line',
                   'leader_title','leader_description','focus_fact_id'
                 ], ARRAY['source_age_s'])
             AND value #>> '{card,event_id}' = expected_event_id
             AND value #>> '{card,focus_fact_id}' = expected_focus_fact_id
             AND value #>> '{card,dedupe_family}' IN ('market_telemetry','filing','disaster','general')
             AND value #>> '{card,event_kind}' IN ('news','listing','oi','liquidation','unsupported_market')
             AND jsonb_typeof(value #> '{card,leader_item_id}') = 'string'
             AND value #>> '{card,leader_item_id}' <> ''
             AND jsonb_typeof(value #> '{card,leader_title}') = 'string'
             AND value #>> '{card,leader_title}' <> ''
             AND jsonb_typeof(value #> '{card,leader_description}') = 'string'
             AND jsonb_typeof(value #> '{card,grounded_assets}') = 'array'
             AND jsonb_typeof(value #> '{card,watchlist_hits}') = 'array'
             AND jsonb_typeof(value #> '{card,provider_metadata}') = 'object'
             AND jsonb_typeof(value #> '{card,provenance}') = 'array'
             AND jsonb_typeof(value -> 'members') = 'array'
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(value -> 'members') member
                    WHERE NOT (
                      news_jsonb_exact_keys(member, ARRAY[
                        'item_id','fact_id','fact_text','joined_at_ms','match_kind','jaccard_estimate',
                        'reporting_origin','canonical_url','provider_metadata','provenance'
                      ])
                      AND jsonb_typeof(member -> 'item_id') = 'string' AND member ->> 'item_id' <> ''
                      AND jsonb_typeof(member -> 'fact_id') = 'string' AND member ->> 'fact_id' <> ''
                      AND jsonb_typeof(member -> 'fact_text') = 'string'
                      AND news_jsonb_int64_valid(member -> 'joined_at_ms')
                      AND member ->> 'match_kind' IN ('leader','exact','near')
                      AND (jsonb_typeof(member -> 'jaccard_estimate') = 'null' OR (
                            jsonb_typeof(member -> 'jaccard_estimate') = 'number'
                            AND (member ->> 'jaccard_estimate')::numeric BETWEEN 0 AND 1
                          ))
                      AND jsonb_typeof(member -> 'reporting_origin') = 'string'
                      AND jsonb_typeof(member -> 'canonical_url') IN ('string','null')
                      AND jsonb_typeof(member -> 'provider_metadata') = 'object'
                      AND jsonb_typeof(member -> 'provenance') = 'array'
                    )
                 )
        $$;


--
-- Name: news_current_liquidation_fact_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_liquidation_fact_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT news_jsonb_exact_keys(value, ARRAY[
                   'source_key','item_id','fact_id','symbol','venue','liquidated_position_side',
                   'forced_order_side','notional_usd','quantity','price','event_at_ms','received_at_ms',
                   'provider_record_identity','symbol_contract_identity','position_side_semantics',
                   'quantity_semantics','notional_semantics','price_semantics','completeness_assumption',
                   'throttle_assumption','source_contract_version','source_contract_complete','parser_version'
                 ])
             AND value ->> 'source_key' ~ '^[0-9a-f]{64}$'
             AND jsonb_typeof(value -> 'item_id') = 'string' AND value ->> 'item_id' <> ''
             AND jsonb_typeof(value -> 'fact_id') = 'string' AND value ->> 'fact_id' <> ''
             AND jsonb_typeof(value -> 'symbol') = 'string'
             AND length(value ->> 'symbol') BETWEEN 1 AND 16
             AND value ->> 'symbol' !~ '[[:space:]]'
             AND value ->> 'venue' IN ('binance','hyperliquid')
             AND value ->> 'liquidated_position_side' IN ('long','short')
             AND value ->> 'forced_order_side' IN ('buy','sell')
             AND ((value ->> 'liquidated_position_side' = 'short' AND value ->> 'forced_order_side' = 'buy')
                  OR (value ->> 'liquidated_position_side' = 'long' AND value ->> 'forced_order_side' = 'sell'))
             AND jsonb_typeof(value -> 'notional_usd') = 'string'
             AND CASE WHEN value ->> 'notional_usd' ~ '^(0|[1-9][0-9]*)(\.[0-9]+)?$'
                      THEN (value ->> 'notional_usd')::numeric > 0
                       AND (value ->> 'notional_usd')::numeric <= 1e24
                      ELSE false END
             AND (jsonb_typeof(value -> 'quantity') = 'null' OR (
                   jsonb_typeof(value -> 'quantity') = 'string'
                   AND CASE WHEN value ->> 'quantity' ~ '^(0|[1-9][0-9]*)(\.[0-9]+)?$'
                            THEN (value ->> 'quantity')::numeric > 0
                             AND (value ->> 'quantity')::numeric <= 1e24
                            ELSE false END
                 ))
             AND jsonb_typeof(value -> 'price') = 'string'
             AND CASE WHEN value ->> 'price' ~ '^(0|[1-9][0-9]*)(\.[0-9]+)?$'
                      THEN (value ->> 'price')::numeric > 0
                       AND (value ->> 'price')::numeric <= 1e24
                      ELSE false END
             AND news_jsonb_int64_valid(value -> 'event_at_ms')
             AND news_jsonb_int64_valid(value -> 'received_at_ms')
             AND (value ->> 'event_at_ms')::numeric > 0
             AND (value ->> 'received_at_ms')::numeric >= (value ->> 'event_at_ms')::numeric
             AND jsonb_typeof(value -> 'provider_record_identity') = 'string'
             AND value ->> 'provider_record_identity' <> ''
             AND jsonb_typeof(value -> 'symbol_contract_identity') = 'string'
             AND value ->> 'symbol_contract_identity' <> ''
             AND jsonb_typeof(value -> 'position_side_semantics') = 'string'
             AND value ->> 'position_side_semantics' <> ''
             AND jsonb_typeof(value -> 'quantity_semantics') = 'string'
             AND value ->> 'quantity_semantics' <> ''
             AND jsonb_typeof(value -> 'notional_semantics') = 'string'
             AND value ->> 'notional_semantics' <> ''
             AND jsonb_typeof(value -> 'price_semantics') = 'string'
             AND value ->> 'price_semantics' <> ''
             AND jsonb_typeof(value -> 'completeness_assumption') = 'string'
             AND value ->> 'completeness_assumption' <> ''
             AND jsonb_typeof(value -> 'throttle_assumption') = 'string'
             AND value ->> 'throttle_assumption' <> ''
             AND value ->> 'source_contract_version' = 'opennews_liquidation_source_v1'
             AND jsonb_typeof(value -> 'source_contract_complete') = 'boolean'
             AND NOT (value ->> 'source_contract_complete')::boolean
             AND value ->> 'parser_version' = 'liquidation_parser_v1'
        $_$;


--
-- Name: news_current_liquidation_metadata_valid(jsonb, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_liquidation_metadata_valid(value jsonb, parsed boolean) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT CASE WHEN parsed THEN
            news_jsonb_exact_keys(value, ARRAY[
              'parsed','source_latency_ms','parser_version','source_classifier_version'
            ])
            AND value ->> 'parsed' = 'true'
            AND news_jsonb_int64_valid(value -> 'source_latency_ms')
            AND (value ->> 'source_latency_ms')::numeric >= 0
            AND value ->> 'parser_version' = 'liquidation_parser_v1'
            AND value ->> 'source_classifier_version' = 'opennews_source_classifier_v1'
          ELSE
            news_jsonb_exact_keys(value, ARRAY[
              'parsed','strategy_id','provider','provider_source','title_sha256','parser_version',
              'source_classifier_version','failure_stage'
            ])
            AND value ->> 'parsed' = 'false'
            AND value ->> 'strategy_id' = '2000'
            AND value ->> 'provider' = 'opennews'
            AND jsonb_typeof(value -> 'provider_source') = 'string'
            AND value ->> 'title_sha256' ~ '^[0-9a-f]{64}$'
            AND value ->> 'parser_version' = 'liquidation_parser_v1'
            AND value ->> 'source_classifier_version' = 'opennews_source_classifier_v1'
            AND value ->> 'failure_stage' = 'source_contract_drift'
          END
        $_$;


--
-- Name: news_current_model_editorial_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_model_editorial_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT news_jsonb_exact_keys(value, ARRAY[
                   'editorial_contract_version','editorial_origin','relevance','taxonomy','editorial_sha256'
                 ])
             AND value ->> 'editorial_contract_version' = 'news_editorial_v2'
             AND value ->> 'editorial_origin' = 'model'
             AND value ->> 'editorial_sha256' ~ '^[0-9a-f]{64}$'
             AND value ->> 'editorial_sha256' = encode(sha256(
                   convert_to(news_canonical_jsonb(value - 'editorial_sha256'), 'UTF8')), 'hex')
             AND news_jsonb_exact_keys(value -> 'taxonomy', ARRAY[
                   'subject_codes','event_family','change_state','assertion_status',
                   'taxonomy_version','source_authority','codebook_sha256'
                 ])
             AND value #>> '{taxonomy,taxonomy_version}' = 'news_taxonomy_v1'
             AND value #>> '{taxonomy,codebook_sha256}' =
                   '6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac'
             AND news_jsonb_ordered_string_set_valid(
                   value #> '{taxonomy,subject_codes}', ARRAY[
                     'medtop:04000000','medtop:20000174','medtop:20000175','medtop:20000177',
                     'medtop:20000178','medtop:20000180','medtop:20000183','medtop:20000186',
                     'medtop:20000187','medtop:20000189','medtop:20000190','medtop:20000192',
                     'medtop:20000195','medtop:20000196','medtop:20000197','medtop:20000199',
                     'medtop:20000200','medtop:20000204','medtop:20000205','medtop:20000207',
                     'medtop:20000208','medtop:20000344','medtop:20000346','medtop:20000350',
                     'medtop:20000359','medtop:20000365','medtop:20000370','medtop:20000371',
                     'medtop:20000373','medtop:20000379','medtop:20000384','medtop:20000385',
                     'medtop:20001164','medtop:20001279','medtop:16000000'
                   ], 3
                 )
             AND NOT (
                   value #> '{taxonomy,subject_codes}' ? 'medtop:04000000'
                   AND EXISTS (
                     SELECT 1
                       FROM jsonb_array_elements_text(value #> '{taxonomy,subject_codes}') code
                      WHERE code LIKE 'medtop:2000%'
                   )
                 )
             AND value #>> '{taxonomy,event_family}' IN (
                   'financial_results','guidance_outlook','product_service_change','corporate_transaction',
                   'financing_capital_allocation','leadership_governance','regulatory_legal',
                   'security_operational_incident','market_access','market_flow_price','macro_policy_data',
                   'geopolitical_conflict','other'
                 )
             AND value #>> '{taxonomy,change_state}' IN (
                   'announced','scheduled','effective','reported','updated','delayed','cancelled','recalled','unknown'
                 )
             AND value #>> '{taxonomy,assertion_status}' IN ('confirmed','claimed','rumor','conflicted','unknown')
             AND value #>> '{taxonomy,source_authority}' IN (
                   'regulatory_filing','issuer_first_party','reputable_secondary','unknown'
                 )
             AND news_jsonb_exact_keys(value -> 'relevance', ARRAY[
                   'impact_breadth','tradability','surprise','development_delta',
                   'channels','affected_markets','reader_value'
                 ])
             AND value #>> '{relevance,impact_breadth}' IN (
                   'none','single_instrument','sector','regional','cross_asset','global_systemic'
                 )
             AND value #>> '{relevance,tradability}' IN ('direct','second_order','contextual','none')
             AND value #>> '{relevance,surprise}' IN (
                   'unscheduled','material_vs_expectation','in_line','unknown'
                 )
             AND value #>> '{relevance,development_delta}' IN (
                   'state_change','material_detail','color_only','scheduled'
                 )
             AND value #>> '{relevance,reader_value}' IN ('escalate','realtime','background','none')
             AND news_jsonb_ordered_string_set_valid(
                   value #> '{relevance,channels}', ARRAY[
                     'rates','liquidity','risk_premium','energy_supply','commodity_supply',
                     'commodity_demand','regulation','exchange_access','product_progress',
                     'earnings_cashflow','positioning_flow','security_incident'
                   ], 4
                 )
             AND news_jsonb_ordered_string_set_valid(
                   value #> '{relevance,affected_markets}', ARRAY[
                     'crypto_broad','us_equity_broad','rates','fx','energy','metals','single_asset'
                   ], 4
                 )
             AND (
                   (jsonb_array_length(value #> '{relevance,channels}') > 0
                    AND jsonb_array_length(value #> '{relevance,affected_markets}') > 0)
                   OR
                   (value #>> '{relevance,tradability}' IN ('contextual','none')
                    AND value #>> '{relevance,reader_value}' IN ('background','none'))
                 )
        $_$;


--
-- Name: news_current_oi_metadata_valid(jsonb, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_oi_metadata_valid(value jsonb, parsed boolean) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT CASE WHEN parsed THEN
            news_jsonb_exact_keys(value, ARRAY[
              'parsed','source_strategy_id','source_contract_version','measurement_window_ms',
              'source_contract_rule','parser_version','source_classifier_version','rank_semantics','policy'
            ])
            AND value ->> 'parsed' = 'true'
            AND ((jsonb_typeof(value -> 'source_strategy_id') = 'null'
                  AND jsonb_typeof(value -> 'source_contract_version') = 'null'
                  AND jsonb_typeof(value -> 'measurement_window_ms') = 'null'
                  AND value ->> 'source_contract_rule' = 'source_window_unproven')
                 OR
                 (jsonb_typeof(value -> 'source_strategy_id') = 'string'
                  AND value ->> 'source_strategy_id' <> ''
                  AND jsonb_typeof(value -> 'source_contract_version') = 'string'
                  AND value ->> 'source_contract_version' <> ''
                  AND news_jsonb_int64_valid(value -> 'measurement_window_ms')
                  AND (value ->> 'measurement_window_ms')::numeric > 0
                  AND value ->> 'source_contract_rule' = 'proven'))
            AND value ->> 'parser_version' = 'oi_signal_parser_v1'
            AND value ->> 'source_classifier_version' = 'opennews_source_classifier_v1'
            AND value ->> 'rank_semantics' = 'eligible_rank_v1'
            AND news_jsonb_exact_keys(value -> 'policy', ARRAY[
                  'window_ms','max_rank_in_window','whale_oi_ratio_above_bps','oi_change_at_least_bps'
                ])
            AND news_jsonb_int64_valid(value #> '{policy,window_ms}')
            AND (value #>> '{policy,window_ms}')::numeric > 0
            AND news_jsonb_int64_valid(value #> '{policy,max_rank_in_window}')
            AND (value #>> '{policy,max_rank_in_window}')::numeric >= 0
            AND news_jsonb_int64_valid(value #> '{policy,whale_oi_ratio_above_bps}')
            AND news_jsonb_int64_valid(value #> '{policy,oi_change_at_least_bps}')
          ELSE
            news_jsonb_exact_keys(value, ARRAY[
              'parsed','strategy_id','provider','provider_source','title_sha256','parser_version',
              'source_classifier_version','failure_stage'
            ])
            AND value ->> 'parsed' = 'false'
            AND value ->> 'strategy_id' = '1019'
            AND value ->> 'provider' = 'opennews'
            AND jsonb_typeof(value -> 'provider_source') = 'string'
            AND value ->> 'title_sha256' ~ '^[0-9a-f]{64}$'
            AND value ->> 'parser_version' = 'oi_signal_parser_v1'
            AND value ->> 'source_classifier_version' = 'opennews_source_classifier_v1'
            AND value ->> 'failure_stage' = 'source_contract_drift'
          END
        $_$;


--
-- Name: news_current_oi_signal_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_oi_signal_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT news_jsonb_exact_keys(value, ARRAY[
                   'symbol','direction','oi_change_bps','oi_value_usd',
                   'whale_long_profit_bps','whale_oi_ratio_bps'
                 ])
             AND jsonb_typeof(value -> 'symbol') = 'string'
             AND length(value ->> 'symbol') BETWEEN 1 AND 16
             AND value ->> 'symbol' !~ '[[:space:]]'
             AND value ->> 'direction' IN ('rise','fall')
             AND news_jsonb_int64_valid(value -> 'oi_change_bps')
             AND news_jsonb_int64_valid(value -> 'oi_value_usd')
             AND (value ->> 'oi_value_usd')::numeric >= 0
             AND news_jsonb_int64_valid(value -> 'whale_long_profit_bps')
             AND news_jsonb_int64_valid(value -> 'whale_oi_ratio_bps')
        $$;


--
-- Name: news_current_pairwise_review_payload_valid(jsonb, jsonb, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_pairwise_review_payload_valid(value jsonb, expected_evidence_refs jsonb, expected_note text) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
          SELECT (
            news_jsonb_exact_keys(value, ARRAY[
              'kind','preference','critical_errors','evidence_refs','note'
            ])
            AND value ->> 'kind' = 'blind_pairwise'
            AND value ->> 'preference' IN ('A','B','tie','both_bad','uncertain')
            AND jsonb_typeof(value -> 'critical_errors') = 'array'
            AND jsonb_array_length(value -> 'critical_errors') <= 12
            AND NOT EXISTS (
                  SELECT 1 FROM jsonb_array_elements(value -> 'critical_errors') error
                   WHERE jsonb_typeof(error) <> 'string'
                      OR error #>> '{}' NOT IN (
                        'A:unsupported_fact','A:wrong_entity','A:wrong_direction','A:missed_key_fact',
                        'A:near_duplicate','A:injection_obedience','B:unsupported_fact','B:wrong_entity',
                        'B:wrong_direction','B:missed_key_fact','B:near_duplicate','B:injection_obedience'
                      )
                )
            AND (SELECT count(*) = count(DISTINCT error #>> '{}')
                   FROM jsonb_array_elements(value -> 'critical_errors') error)
            AND value -> 'evidence_refs' = expected_evidence_refs
            AND news_current_review_evidence_refs_valid(expected_evidence_refs)
            AND (jsonb_array_length(value -> 'critical_errors') = 0
                 OR jsonb_array_length(expected_evidence_refs) > 0)
            AND value ->> 'note' = expected_note
          ) IS TRUE
        $$;


--
-- Name: news_current_review_acceptance_target_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_acceptance_target_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
          target news_reviews%ROWTYPE;
        BEGIN
          IF NEW.review_kind <> 'acceptance' THEN
            RETURN NEW;
          END IF;
          SELECT * INTO target FROM news_reviews WHERE review_id = NEW.accepts_review_id;
          IF NOT FOUND
             OR target.review_kind <> 'judgment'
             OR news_current_review_valid(
                  target.review_kind, target.subject_kind,
                  target.rubric_version, target.reader_contract_version,
                  target.event_id, target.evidence_version,
                  target.external_snapshot_id, target.pairwise_case_id,
                  target.should_push, target.dimensions, target.novelty,
                  target.first_bad_owner, target.evidence_refs,
                  target.expected_correction, target.note, target.selection,
                  target.payload, target.accepts_review_id
                ) IS NOT TRUE
             OR target.subject_kind <> NEW.subject_kind
             OR target.task_id <> NEW.task_id
             OR target.task_version <> NEW.task_version
             OR target.event_id IS DISTINCT FROM NEW.event_id
             OR target.evidence_version IS DISTINCT FROM NEW.evidence_version
             OR target.external_snapshot_id IS DISTINCT FROM NEW.external_snapshot_id
             OR target.pairwise_case_id IS DISTINCT FROM NEW.pairwise_case_id
             OR target.release_eligible IS DISTINCT FROM NEW.release_eligible THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              CONSTRAINT = 'news_reviews_current_acceptance_target_check',
              MESSAGE = 'news_review_acceptance_target_not_current';
          END IF;
          RETURN NEW;
        END;
        $$;


--
-- Name: news_current_review_dimensions_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_dimensions_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT news_jsonb_required_optional_keys(value, ARRAY[
                   'factual_fidelity','taxonomy_subject_codes','taxonomy_event_family',
                   'taxonomy_change_state','taxonomy_source_authority','taxonomy_assertion_status'
                 ], ARRAY[
                   'headline_fidelity','asset_grounding','direction','magnitude','why_support','why_value',
                   'timeliness','trade_impact_breadth','trade_tradability','trade_surprise',
                   'trade_development_delta','trade_channels','trade_affected_markets','reader_value'
                 ])
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_each(value) AS dimension(name, result)
                    WHERE jsonb_typeof(result) <> 'string'
                       OR result #>> '{}' NOT IN ('pass','fail','uncertain','not_applicable')
                 )
        $$;


--
-- Name: news_current_review_evidence_refs_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_evidence_refs_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= 32
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(value) reference
                    WHERE jsonb_typeof(reference) <> 'string'
                       OR length(reference #>> '{}') NOT BETWEEN 1 AND 500
                       OR btrim(reference #>> '{}') <> reference #>> '{}'
                 )
        $$;


--
-- Name: news_current_review_expected_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_expected_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT CASE jsonb_typeof(value)
            WHEN 'null' THEN true
            WHEN 'object' THEN
              news_jsonb_exact_keys(value, ARRAY[
                'magnitude','direction','assets','trade_impact_breadth','trade_tradability',
                'trade_surprise','trade_development_delta','trade_channels',
                'trade_affected_markets','reader_value'
              ])
              AND EXISTS (SELECT 1 FROM jsonb_each(value) field WHERE field.value <> 'null'::jsonb)
              AND (jsonb_typeof(value -> 'magnitude') = 'null' OR (
                    jsonb_typeof(value -> 'magnitude') = 'number'
                    AND value ->> 'magnitude' ~ '^[0-3]$'))
              AND (jsonb_typeof(value -> 'direction') = 'null'
                   OR value ->> 'direction' IN ('bullish','bearish','neutral','unclear'))
              AND (jsonb_typeof(value -> 'assets') = 'null' OR (
                    jsonb_typeof(value -> 'assets') = 'array'
                    AND jsonb_array_length(value -> 'assets') <= 16
                    AND NOT EXISTS (
                      SELECT 1 FROM jsonb_array_elements(value -> 'assets') asset
                       WHERE NOT news_jsonb_exact_keys(asset, ARRAY['symbol','role'])
                          OR jsonb_typeof(asset -> 'symbol') <> 'string'
                          OR length(asset ->> 'symbol') NOT BETWEEN 1 AND 32
                          OR asset ->> 'role' NOT IN ('primary','mentioned')
                    )))
              AND (jsonb_typeof(value -> 'trade_impact_breadth') = 'null'
                   OR value ->> 'trade_impact_breadth' IN (
                     'none','single_instrument','sector','regional','cross_asset','global_systemic'))
              AND (jsonb_typeof(value -> 'trade_tradability') = 'null'
                   OR value ->> 'trade_tradability' IN ('direct','second_order','contextual','none'))
              AND (jsonb_typeof(value -> 'trade_surprise') = 'null'
                   OR value ->> 'trade_surprise' IN ('unscheduled','material_vs_expectation','in_line','unknown'))
              AND (jsonb_typeof(value -> 'trade_development_delta') = 'null'
                   OR value ->> 'trade_development_delta' IN (
                     'state_change','material_detail','color_only','scheduled'))
              AND (jsonb_typeof(value -> 'trade_channels') = 'null'
                   OR news_jsonb_ordered_string_set_valid(value -> 'trade_channels', ARRAY[
                     'rates','liquidity','risk_premium','energy_supply','commodity_supply',
                     'commodity_demand','regulation','exchange_access','product_progress',
                     'earnings_cashflow','positioning_flow','security_incident'
                   ], 4))
              AND (jsonb_typeof(value -> 'trade_affected_markets') = 'null'
                   OR news_jsonb_ordered_string_set_valid(value -> 'trade_affected_markets', ARRAY[
                     'crypto_broad','us_equity_broad','rates','fx','energy','metals','single_asset'
                   ], 4))
              AND (jsonb_typeof(value -> 'reader_value') = 'null'
                   OR value ->> 'reader_value' IN ('escalate','realtime','background','none'))
            ELSE false
          END
        $_$;


--
-- Name: news_current_review_novelty_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_novelty_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT news_jsonb_exact_keys(value, ARRAY['judgment','duplicate_of'])
             AND jsonb_typeof(value -> 'judgment') = 'string'
             AND value ->> 'judgment' IN ('new_fact','progression','restatement','uncertain')
             AND jsonb_typeof(value -> 'duplicate_of') = 'string'
             AND CASE WHEN value ->> 'judgment' = 'restatement'
                      THEN btrim(value ->> 'duplicate_of') <> ''
                      ELSE btrim(value ->> 'duplicate_of') = '' END
        $$;


--
-- Name: news_current_review_selection_valid(jsonb, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_selection_valid(value jsonb, subject_kind_value text) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT CASE subject_kind_value
            WHEN 'event' THEN
              news_jsonb_exact_keys(value, ARRAY[
                'stratum','stratum_zh','reason','reason_zh','sampling_probability','selection_version'
              ])
              AND value ->> 'stratum' IN (
                'local_macro_false_interrupt','systemic_macro_must_interrupt','regional_direct_exception',
                'scheduled_or_in_line_macro','color_only_progression','macro_random_control',
                'delivery_ambiguous','delivery_failed','critical','throttled','gate_suppress',
                'model_drop','delivered','high_reaction','random_control'
              )
              AND jsonb_typeof(value -> 'stratum_zh') = 'string'
              AND value ->> 'stratum_zh' <> ''
              AND value ->> 'reason' IN (
                'trade_relevance_targeted_stratum','macro_coverage_control','delivery_truth_unknown',
                'delivery_terminal_failure','semantic_escalation','duplicate_or_historical_throttle',
                'sent_quality_sample','market_discovery_only','semantic_or_policy_hold',
                'upstream_recall_sample','coverage_control'
              )
              AND jsonb_typeof(value -> 'reason_zh') = 'string'
              AND value ->> 'reason_zh' <> ''
              AND jsonb_typeof(value -> 'sampling_probability') = 'number'
              AND (value ->> 'sampling_probability')::numeric BETWEEN 0 AND 1
              AND value ->> 'selection_version' = 'news_review_sampler_v3'
            WHEN 'external_miss' THEN
              news_jsonb_exact_keys(value, ARRAY['stratum','sampling_probability','reason'])
              AND value ->> 'stratum' = 'eventless_miss'
              AND jsonb_typeof(value -> 'sampling_probability') = 'number'
              AND (value ->> 'sampling_probability')::numeric = 1
              AND value ->> 'reason' = 'operator_created'
            WHEN 'pairwise' THEN
              news_jsonb_exact_keys(value, ARRAY[
                'stratum','stratum_zh','sampling_probability','selection_version'
              ])
              AND value ->> 'stratum' IN ('blind_pairwise','development_pairwise')
              AND jsonb_typeof(value -> 'stratum_zh') = 'string'
              AND value ->> 'stratum_zh' <> ''
              AND jsonb_typeof(value -> 'sampling_probability') = 'number'
              AND (value ->> 'sampling_probability')::numeric = 1
              AND value ->> 'selection_version' = 'news_blind_pairwise_v1'
            ELSE false
          END
        $$;


--
-- Name: news_current_review_source_exists(text, text, text, integer, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_source_exists(subject_kind_value text, task_id_value text, event_id_value text, evidence_version_value integer, external_snapshot_id_value text, pairwise_case_id_value text) RETURNS boolean
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $_$
          SELECT CASE subject_kind_value
            WHEN 'event' THEN EXISTS (
              SELECT 1 FROM news_review_task_source_v1 source
               WHERE source.event_id = event_id_value
                 AND source.evidence_version = evidence_version_value
                 AND source.trace #>> '{agent_assignment,bundle_sha}' ~ '^[0-9a-f]{64}$'
                 AND task_id_value =
                       'evt.' || source.event_id || '.' || source.evidence_version::text || '.' ||
                       left(encode(sha256(convert_to(news_canonical_jsonb(jsonb_build_object(
                         'task', 'news_review_task_v2',
                         'event_id', source.event_id,
                         'evidence_version', source.evidence_version,
                         'rubric', 'news_review_v6',
                         'reader_contract', 'reader_contract_v2',
                         'reader_contract_sha256',
                           'bb7f436d232b02446c4f0f17c7b0b4f56c421aa4daf1a3869c5baa9b89970082',
                         'agent_cohort_sha256', source.trace #>> '{agent_assignment,bundle_sha}'
                       )), 'UTF8')), 'hex'), 16)
            )
            WHEN 'external_miss' THEN
              task_id_value = 'external.' || external_snapshot_id_value
              AND EXISTS (
                    SELECT 1 FROM news_review_external_source_v1 source
                     WHERE source.snapshot_id = external_snapshot_id_value
                       AND source.provenance = 'operator_reported'
                       AND source.snapshot ->> 'schema_version' = 'news_external_miss_v1'
                  )
            WHEN 'pairwise' THEN
              EXISTS (
                SELECT 1
                  FROM news_learning_cases pair_source
                  JOIN news_learning_artifacts dataset
                    ON dataset.kind = 'dataset'
                   AND dataset.artifact_sha = pair_source.dataset_sha
                  JOIN LATERAL (
                    SELECT stable_sha FROM news_review_active_agent_v1
                     ORDER BY created_at_ms DESC LIMIT 1
                  ) active ON true
                 WHERE pairwise_case_id_value = pair_source.run_sha || ':' || pair_source.case_id
                   AND task_id_value = 'pair.' || pair_source.run_sha || '.' || pair_source.case_id
                   AND pair_source.evaluation_stage IN ('offline','holdout')
                   AND COALESCE((pair_source.comparison ->> 'review_eligible')::boolean, false)
                   AND pair_source.review_id IS NOT NULL
                   AND dataset.payload #>> '{agent_cohort,bundle_sha}' = active.stable_sha
                   AND dataset.payload ->> 'learning_epoch' = 'bundle_' || left(active.stable_sha, 8)
                   AND CASE pair_source.subject_kind
                     WHEN 'event' THEN EXISTS (
                       SELECT 1 FROM news_review_task_source_v1 source
                        WHERE source.event_id = pair_source.event_id
                          AND source.evidence_version = pair_source.evidence_version
                     )
                     WHEN 'external_miss' THEN EXISTS (
                       SELECT 1 FROM news_review_external_source_v1 source
                        WHERE source.snapshot_id = pair_source.external_snapshot_id
                          AND source.provenance = 'operator_reported'
                          AND source.snapshot ->> 'schema_version' = 'news_external_miss_v1'
                     )
                     ELSE false
                   END
              )
            ELSE false
          END
        $_$;


--
-- Name: news_current_review_source_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_source_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF NEW.rubric_version = 'news_review_v6'
             AND news_current_review_valid(
                   NEW.review_kind, NEW.subject_kind,
                   NEW.rubric_version, NEW.reader_contract_version,
                   NEW.event_id, NEW.evidence_version,
                   NEW.external_snapshot_id, NEW.pairwise_case_id,
                   NEW.should_push, NEW.dimensions, NEW.novelty,
                   NEW.first_bad_owner, NEW.evidence_refs,
                   NEW.expected_correction, NEW.note, NEW.selection,
                   NEW.payload, NEW.accepts_review_id
                 ) IS TRUE
             AND news_current_review_source_exists(
                   NEW.subject_kind, NEW.task_id, NEW.event_id, NEW.evidence_version,
                   NEW.external_snapshot_id, NEW.pairwise_case_id
                 ) IS NOT TRUE THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              CONSTRAINT = 'news_reviews_current_task_source_check',
              MESSAGE = 'news_review_current_task_source_missing';
          END IF;
          RETURN NEW;
        END;
        $$;


--
-- Name: news_current_review_taxonomy_provenance_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_taxonomy_provenance_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT news_jsonb_exact_keys(value, ARRAY[
                   'label_source','draft_author','review_role','adjudicates_review_id','draft_taxonomy'
                 ])
             AND value ->> 'label_source' IN ('human','model_draft')
             AND jsonb_typeof(value -> 'draft_author') = 'string'
             AND length(value ->> 'draft_author') <= 128
             AND value ->> 'review_role' IN ('primary','adjudication')
             AND jsonb_typeof(value -> 'adjudicates_review_id') = 'string'
             AND length(value ->> 'adjudicates_review_id') <= 64
             AND (jsonb_typeof(value -> 'draft_taxonomy') = 'null'
                  OR news_current_review_taxonomy_valid(value -> 'draft_taxonomy'))
             AND CASE WHEN value ->> 'label_source' = 'model_draft'
                      THEN btrim(value ->> 'draft_author') <> ''
                      ELSE value ->> 'draft_author' = ''
                           AND jsonb_typeof(value -> 'draft_taxonomy') = 'null' END
             AND CASE WHEN value ->> 'review_role' = 'adjudication'
                      THEN value ->> 'adjudicates_review_id' <> ''
                      ELSE value ->> 'adjudicates_review_id' = '' END
        $$;


--
-- Name: news_current_review_taxonomy_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_taxonomy_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT news_jsonb_exact_keys(value, ARRAY[
                   'subject_codes','event_family','change_state','assertion_status',
                   'taxonomy_version','source_authority','codebook_sha256'
                 ])
             AND value ->> 'taxonomy_version' = 'news_taxonomy_v1'
             AND value ->> 'codebook_sha256' =
                   '6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac'
             AND news_jsonb_ordered_string_set_valid(value -> 'subject_codes', ARRAY[
                   'medtop:04000000','medtop:20000174','medtop:20000175','medtop:20000177',
                   'medtop:20000178','medtop:20000180','medtop:20000183','medtop:20000186',
                   'medtop:20000187','medtop:20000189','medtop:20000190','medtop:20000192',
                   'medtop:20000195','medtop:20000196','medtop:20000197','medtop:20000199',
                   'medtop:20000200','medtop:20000204','medtop:20000205','medtop:20000207',
                   'medtop:20000208','medtop:20000344','medtop:20000346','medtop:20000350',
                   'medtop:20000359','medtop:20000365','medtop:20000370','medtop:20000371',
                   'medtop:20000373','medtop:20000379','medtop:20000384','medtop:20000385',
                   'medtop:20001164','medtop:20001279','medtop:16000000'
                 ], 3)
             AND NOT (
                   value -> 'subject_codes' ? 'medtop:04000000'
                   AND EXISTS (
                     SELECT 1 FROM jsonb_array_elements_text(value -> 'subject_codes') code
                      WHERE code LIKE 'medtop:2000%'
                   )
                 )
             AND value ->> 'event_family' IN (
                   'financial_results','guidance_outlook','product_service_change','corporate_transaction',
                   'financing_capital_allocation','leadership_governance','regulatory_legal',
                   'security_operational_incident','market_access','market_flow_price','macro_policy_data',
                   'geopolitical_conflict','other'
                 )
             AND value ->> 'change_state' IN (
                   'announced','scheduled','effective','reported','updated','delayed','cancelled','recalled','unknown'
                 )
             AND value ->> 'assertion_status' IN ('confirmed','claimed','rumor','conflicted','unknown')
             AND value ->> 'source_authority' IN (
                   'regulatory_filing','issuer_first_party','reputable_secondary','unknown'
                 )
        $$;


--
-- Name: news_current_review_valid(text, text, text, text, text, integer, text, text, text, jsonb, jsonb, text, jsonb, text, text, jsonb, jsonb, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_review_valid(review_kind_value text, subject_kind_value text, rubric_version_value text, reader_contract_version_value text, event_id_value text, evidence_version_value integer, external_snapshot_id_value text, pairwise_case_id_value text, should_push_value text, dimensions_value jsonb, novelty_value jsonb, first_bad_owner_value text, evidence_refs_value jsonb, expected_correction_value text, note_value text, selection_value jsonb, payload_value jsonb, accepts_review_id_value text) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
          SELECT (
            rubric_version_value = 'news_review_v6'
            AND reader_contract_version_value = 'reader_contract_v2'
            AND subject_kind_value IN ('event','external_miss','pairwise')
            AND CASE subject_kind_value
              WHEN 'event' THEN event_id_value IS NOT NULL AND evidence_version_value >= 1
                                AND external_snapshot_id_value IS NULL AND pairwise_case_id_value IS NULL
              WHEN 'external_miss' THEN event_id_value IS NULL AND evidence_version_value IS NULL
                                        AND external_snapshot_id_value IS NOT NULL
                                        AND pairwise_case_id_value IS NULL
              WHEN 'pairwise' THEN event_id_value IS NULL AND evidence_version_value IS NULL
                                   AND external_snapshot_id_value IS NULL
                                   AND pairwise_case_id_value IS NOT NULL
              ELSE false
            END
            AND CASE review_kind_value
              WHEN 'acceptance' THEN
                should_push_value IS NULL
                AND dimensions_value = '{}'::jsonb
                AND novelty_value = '{}'::jsonb
                AND first_bad_owner_value IS NULL
                AND evidence_refs_value = '[]'::jsonb
                AND expected_correction_value = ''
                AND note_value = ''
                AND selection_value = '{}'::jsonb
                AND payload_value = '{}'::jsonb
                AND accepts_review_id_value IS NOT NULL
              WHEN 'judgment' THEN
                accepts_review_id_value IS NULL
                AND news_current_review_selection_valid(selection_value, subject_kind_value)
                AND CASE subject_kind_value
                  WHEN 'event' THEN news_current_event_review_payload_valid(
                    payload_value, should_push_value, dimensions_value, novelty_value,
                    first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                  WHEN 'external_miss' THEN news_current_event_review_payload_valid(
                    payload_value, should_push_value, dimensions_value, novelty_value,
                    first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                  WHEN 'pairwise' THEN
                    should_push_value IS NULL
                    AND dimensions_value = '{}'::jsonb
                    AND novelty_value = '{}'::jsonb
                    AND first_bad_owner_value IS NULL
                    AND expected_correction_value = ''
                    AND news_current_pairwise_review_payload_valid(
                      payload_value, evidence_refs_value, note_value)
                  ELSE false
                END
              ELSE false
            END
          ) IS TRUE
        $$;


--
-- Name: news_current_told_trace_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_told_trace_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= 16
             AND NOT EXISTS (
                   SELECT 1
                     FROM jsonb_array_elements(value) WITH ORDINALITY AS told(entry, position)
                    WHERE NOT (
                      news_jsonb_exact_keys(entry, ARRAY[
                        'i','event_id','at_ms','ago_min','storyline_key','comparison_title',
                        'comparison_fingerprint','symbols','magnitude','direction','headline_zh',
                        'why_zh','tier','similarity','history_scope','retrieval_reason'
                      ])
                      AND news_jsonb_int64_valid(entry -> 'i')
                      AND (entry ->> 'i')::numeric = position - 1
                      AND jsonb_typeof(entry -> 'event_id') = 'string' AND entry ->> 'event_id' <> ''
                      AND news_jsonb_int64_valid(entry -> 'at_ms') AND (entry ->> 'at_ms')::numeric >= 0
                      AND news_jsonb_int64_valid(entry -> 'ago_min') AND (entry ->> 'ago_min')::numeric >= 0
                      AND jsonb_typeof(entry -> 'storyline_key') = 'string'
                      AND jsonb_typeof(entry -> 'comparison_title') = 'string'
                      AND jsonb_typeof(entry -> 'comparison_fingerprint') = 'string'
                      AND jsonb_typeof(entry -> 'symbols') = 'array'
                      AND jsonb_array_length(entry -> 'symbols') <= 6
                      AND NOT EXISTS (
                        SELECT 1 FROM jsonb_array_elements(entry -> 'symbols') symbol
                         WHERE jsonb_typeof(symbol) <> 'string' OR symbol #>> '{}' = ''
                      )
                      AND news_jsonb_int64_valid(entry -> 'magnitude')
                      AND (entry ->> 'magnitude')::numeric BETWEEN 0 AND 3
                      AND entry ->> 'direction' IN ('bullish','bearish','neutral','unclear')
                      AND jsonb_typeof(entry -> 'headline_zh') = 'string'
                      AND length(entry ->> 'headline_zh') <= 60
                      AND jsonb_typeof(entry -> 'why_zh') = 'string'
                      AND length(entry ->> 'why_zh') <= 140
                      AND entry ->> 'tier' IN (
                        'exact_fact','storyline','asset_overlap','fact_similarity','recency'
                      )
                      AND jsonb_typeof(entry -> 'similarity') = 'number'
                      AND (entry ->> 'similarity')::numeric BETWEEN 0 AND 1
                      AND entry ->> 'history_scope' IN ('recent','targeted')
                      AND entry ->> 'retrieval_reason' IN (
                        'recent','exact_fingerprint','canonical_asset_overlap'
                      )
                    )
                 )
        $$;


--
-- Name: news_current_triage_verdict_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_triage_verdict_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT news_jsonb_exact_keys(value, ARRAY[
                   'novelty','restates','assets','direction','scope','magnitude',
                   'confidence','audience','headline_zh','why_zh'
                 ])
             AND value ->> 'novelty' IN ('new_fact','progression','restatement')
             AND jsonb_typeof(value -> 'restates') = 'number'
             AND (value ->> 'restates') ~ '^-?[0-9]+$'
             AND (value ->> 'restates')::integer >= -1
             AND jsonb_typeof(value -> 'assets') = 'array'
             AND jsonb_array_length(value -> 'assets') <= 8
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(value -> 'assets') asset
                    WHERE NOT news_jsonb_exact_keys(asset, ARRAY['symbol','market_type','role'])
                       OR asset ->> 'symbol' IS NULL OR length(asset ->> 'symbol') NOT BETWEEN 1 AND 16
                       OR jsonb_typeof(asset -> 'market_type') NOT IN ('string','null')
                       OR asset ->> 'role' NOT IN ('primary','mentioned')
                 )
             AND value ->> 'direction' IN ('bullish','bearish','neutral','unclear')
             AND value ->> 'scope' IN ('macro','sector','single_name')
             AND jsonb_typeof(value -> 'magnitude') = 'number'
             AND (value ->> 'magnitude') ~ '^[0-3]$'
             AND jsonb_typeof(value -> 'confidence') = 'number'
             AND (value ->> 'confidence')::numeric BETWEEN 0 AND 1
             AND value ->> 'audience' IN ('crypto','us_equity','macro','none')
             AND jsonb_typeof(value -> 'headline_zh') = 'string'
             AND length(value ->> 'headline_zh') BETWEEN 1 AND 60
             AND jsonb_typeof(value -> 'why_zh') = 'string'
             AND length(value ->> 'why_zh') <= 140
        $_$;


--
-- Name: news_current_verdict_evidence_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_current_verdict_evidence_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF NEW.judgment_contract_version = 'news_judgment_v2'
             AND NOT EXISTS (
               SELECT 1 FROM news_event_evidence_snapshots evidence
                WHERE evidence.event_id = NEW.event_id
                  AND evidence.evidence_version = NEW.evidence_version
                  AND evidence.evidence_sha256 = NEW.evidence_sha256
                  AND evidence.focus_fact_id = NEW.focus_fact_id
             ) THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              CONSTRAINT = 'news_verdicts_current_evidence_check',
              MESSAGE = 'news_current_verdict_evidence_not_exact';
          END IF;
          RETURN NEW;
        END;
        $$;


--
-- Name: news_jsonb_exact_keys(jsonb, text[]); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_jsonb_exact_keys(value jsonb, expected text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT jsonb_typeof(value) = 'object'
             AND ARRAY(SELECT key FROM jsonb_object_keys(value) key ORDER BY key COLLATE "C")
                 = ARRAY(SELECT key FROM unnest(expected) key ORDER BY key COLLATE "C")
        $$;


--
-- Name: news_jsonb_forbidden_keys_absent(jsonb, text[]); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_jsonb_forbidden_keys_absent(value jsonb, forbidden text[]) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
        DECLARE
          item record;
        BEGIN
          IF jsonb_typeof(value) = 'object' THEN
            FOR item IN SELECT key, val FROM jsonb_each(value) entry(key, val) LOOP
              IF item.key = ANY(forbidden)
                 OR NOT news_jsonb_forbidden_keys_absent(item.val, forbidden) THEN
                RETURN false;
              END IF;
            END LOOP;
          ELSIF jsonb_typeof(value) = 'array' THEN
            FOR item IN SELECT val FROM jsonb_array_elements(value) entry(val) LOOP
              IF NOT news_jsonb_forbidden_keys_absent(item.val, forbidden) THEN
                RETURN false;
              END IF;
            END LOOP;
          END IF;
          RETURN true;
        END;
        $$;


--
-- Name: news_jsonb_int64_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_jsonb_int64_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT jsonb_typeof(value) = 'number'
             AND CASE WHEN value #>> '{}' ~ '^-?[0-9]+$'
                      THEN (value #>> '{}')::numeric BETWEEN -9223372036854775808 AND 9223372036854775807
                      ELSE false END
        $_$;


--
-- Name: news_jsonb_ordered_string_set_valid(jsonb, text[], integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_jsonb_ordered_string_set_valid(value jsonb, allowed text[], maximum integer) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= maximum
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(value) AS element(item)
                    WHERE jsonb_typeof(item) <> 'string'
                 )
             AND ARRAY(
                   SELECT item #>> '{}'
                     FROM jsonb_array_elements(value) WITH ORDINALITY AS element(item, position)
                    ORDER BY position
                 ) = ARRAY(
                   SELECT code
                     FROM unnest(allowed) WITH ORDINALITY AS candidate(code, position)
                    WHERE value ? code
                    ORDER BY position
                 )
        $$;


--
-- Name: news_jsonb_required_optional_keys(jsonb, text[], text[]); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.news_jsonb_required_optional_keys(value jsonb, required text[], optional text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT jsonb_typeof(value) = 'object'
             AND NOT EXISTS (SELECT 1 FROM unnest(required) key WHERE NOT value ? key)
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_object_keys(value) key
                    WHERE NOT key = ANY(required || optional)
                 )
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
-- Name: purge_news_learning_retention(integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.purge_news_learning_retention(p_batch integer DEFAULT 500) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
          v_now_ms bigint := floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint;
          v_unreferenced_cutoff bigint;
          v_referenced_cutoff bigint;
          v_recordings integer := 0;
          v_cases integer := 0;
          v_artifacts integer := 0;
          v_eligible_recordings integer := 0;
          v_eligible_cases integer := 0;
          v_eligible_artifacts integer := 0;
          v_oldest_recording bigint;
          v_oldest_case bigint;
          v_oldest_artifact bigint;
        BEGIN
          IF p_batch < 1 OR p_batch > 1000 THEN
            RAISE EXCEPTION 'news_learning_retention_batch_invalid';
          END IF;
          v_unreferenced_cutoff := v_now_ms - 90::bigint * 86400000;
          v_referenced_cutoff := v_now_ms - 365::bigint * 86400000;
          PERFORM set_config('tracefold.learning_retention_purge', 'on', true);

          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha
              FROM news_canary_activations a
             WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (
                 SELECT stable_bundle_sha FROM pinned_runtime
               )
          ), pinned_runs AS (
            SELECT DISTINCT r.payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts r
              JOIN pinned_candidates p ON p.candidate_sha = r.payload ->> 'candidate_sha'
             WHERE r.kind = 'release_evidence' AND r.payload ? 'run_sha'
          ), referenced_runs AS (
            SELECT DISTINCT payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts
             WHERE kind IN ('evaluation_report', 'release_evidence') AND payload ? 'run_sha'
          ), doomed AS (
            SELECT r.recording_sha
              FROM news_model_recordings r
             WHERE NOT EXISTS (SELECT 1 FROM pinned_runs p WHERE p.run_sha = r.run_sha)
               AND (
                 (r.created_at_ms < v_unreferenced_cutoff
                  AND NOT EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = r.run_sha))
                 OR
                 (r.created_at_ms < v_referenced_cutoff
                  AND EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = r.run_sha))
               )
             ORDER BY r.created_at_ms, r.recording_sha
             LIMIT p_batch
          )
          DELETE FROM news_model_recordings r USING doomed d
           WHERE r.recording_sha = d.recording_sha;
          GET DIAGNOSTICS v_recordings = ROW_COUNT;

          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha
              FROM news_canary_activations a
             WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (
                 SELECT stable_bundle_sha FROM pinned_runtime
               )
          ), pinned_runs AS (
            SELECT DISTINCT r.payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts r
              JOIN pinned_candidates p ON p.candidate_sha = r.payload ->> 'candidate_sha'
             WHERE r.kind = 'release_evidence' AND r.payload ? 'run_sha'
          ), referenced_runs AS (
            SELECT DISTINCT payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts
             WHERE kind IN ('evaluation_report', 'release_evidence') AND payload ? 'run_sha'
          ), doomed AS (
            SELECT c.run_sha, c.case_id
              FROM news_learning_cases c
             WHERE NOT EXISTS (SELECT 1 FROM pinned_runs p WHERE p.run_sha = c.run_sha)
               AND (
                 (c.created_at_ms < v_unreferenced_cutoff
                  AND NOT EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = c.run_sha))
                 OR
                 (c.created_at_ms < v_referenced_cutoff
                  AND EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = c.run_sha))
               )
             ORDER BY c.created_at_ms, c.run_sha, c.case_id
             LIMIT p_batch
          )
          DELETE FROM news_learning_cases c USING doomed d
           WHERE c.run_sha = d.run_sha AND c.case_id = d.case_id;
          GET DIAGNOSTICS v_cases = ROW_COUNT;

          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha
              FROM news_canary_activations a
             WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (
                 SELECT stable_bundle_sha FROM pinned_runtime
               )
          ), pinned_release AS (
            SELECT artifact_sha, payload
              FROM news_learning_artifacts r
             WHERE r.kind = 'release_evidence'
               AND r.payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
          ), protected AS (
            SELECT artifact_sha
              FROM news_learning_artifacts
             WHERE kind IN ('active_agent', 'deployment_receipt', 'rollback_receipt')
            UNION SELECT artifact_sha FROM pinned_release
            UNION SELECT payload ->> 'report_sha' FROM pinned_release
            UNION SELECT payload #>> '{evidence,development_dataset_sha}'
              FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION SELECT payload #>> '{evidence,validation_dataset_sha}'
              FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION SELECT payload #>> '{evidence,observation_manifest_sha}'
              FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION
            SELECT artifact_sha FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION
            SELECT payload ->> 'proposal_sha' FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION
            SELECT payload #>> '{manifest,development_dataset_sha}' FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION
            SELECT payload #>> '{manifest,proposal_receipt,registration_receipt_sha}'
              FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION
            SELECT artifact_sha FROM news_learning_artifacts
             WHERE kind = 'dataset'
               AND payload ->> 'observation_ref' IN (SELECT candidate_sha FROM pinned_candidates)
          ), semantic_references AS (
            SELECT payload ->> 'proposal_sha' AS artifact_sha
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{manifest,development_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{manifest,proposal_receipt,registration_receipt_sha}'
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{evidence,development_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload #>> '{evidence,validation_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload #>> '{evidence,observation_manifest_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload ->> 'report_sha'
              FROM news_learning_artifacts WHERE kind = 'release_evidence'
            UNION SELECT DISTINCT dataset_sha FROM news_learning_cases
          ), doomed AS (
            SELECT a.artifact_sha
              FROM news_learning_artifacts a
             WHERE a.created_at_ms < v_referenced_cutoff
               AND a.kind NOT IN ('active_agent', 'deployment_receipt', 'rollback_receipt')
               AND NOT EXISTS (SELECT 1 FROM protected p WHERE p.artifact_sha = a.artifact_sha)
               AND NOT EXISTS (SELECT 1 FROM semantic_references r WHERE r.artifact_sha = a.artifact_sha)
               AND NOT (
                 a.kind = 'candidate' AND EXISTS (
                   SELECT 1 FROM news_learning_artifacts ref
                    WHERE ref.kind IN (
                      'evaluation_report', 'release_evidence', 'shadow_observation', 'canary_observation'
                    )
                      AND ref.payload ->> 'candidate_sha' = a.payload ->> 'candidate_sha'
                 )
               )
               AND NOT EXISTS (
                 SELECT 1 FROM news_learning_artifacts child WHERE child.parent_sha = a.artifact_sha
               )
             ORDER BY a.created_at_ms, a.artifact_sha
             LIMIT p_batch
          )
          DELETE FROM news_learning_artifacts a USING doomed d
           WHERE a.artifact_sha = d.artifact_sha;
          GET DIAGNOSTICS v_artifacts = ROW_COUNT;

          -- Remaining eligible counts are deliberately capped at batch + 1.
          -- Zero means drained; batch + 1 means "more work remains" without an
          -- unbounded count over a cold operational table.
          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha FROM news_canary_activations a WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (SELECT stable_bundle_sha FROM pinned_runtime)
          ), pinned_runs AS (
            SELECT DISTINCT r.payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts r
              JOIN pinned_candidates p ON p.candidate_sha = r.payload ->> 'candidate_sha'
             WHERE r.kind = 'release_evidence' AND r.payload ? 'run_sha'
          ), referenced_runs AS (
            SELECT DISTINCT payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts
             WHERE kind IN ('evaluation_report', 'release_evidence') AND payload ? 'run_sha'
          )
          SELECT count(*) INTO v_eligible_recordings
            FROM (
              SELECT 1 FROM news_model_recordings r
               WHERE NOT EXISTS (SELECT 1 FROM pinned_runs p WHERE p.run_sha = r.run_sha)
                 AND (
                   (r.created_at_ms < v_unreferenced_cutoff
                    AND NOT EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = r.run_sha))
                   OR
                   (r.created_at_ms < v_referenced_cutoff
                    AND EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = r.run_sha))
                 )
               LIMIT p_batch + 1
            ) remaining;

          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha FROM news_canary_activations a WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (SELECT stable_bundle_sha FROM pinned_runtime)
          ), pinned_runs AS (
            SELECT DISTINCT r.payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts r
              JOIN pinned_candidates p ON p.candidate_sha = r.payload ->> 'candidate_sha'
             WHERE r.kind = 'release_evidence' AND r.payload ? 'run_sha'
          ), referenced_runs AS (
            SELECT DISTINCT payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts
             WHERE kind IN ('evaluation_report', 'release_evidence') AND payload ? 'run_sha'
          )
          SELECT count(*) INTO v_eligible_cases
            FROM (
              SELECT 1 FROM news_learning_cases c
               WHERE NOT EXISTS (SELECT 1 FROM pinned_runs p WHERE p.run_sha = c.run_sha)
                 AND (
                   (c.created_at_ms < v_unreferenced_cutoff
                    AND NOT EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = c.run_sha))
                   OR
                   (c.created_at_ms < v_referenced_cutoff
                    AND EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = c.run_sha))
                 )
               LIMIT p_batch + 1
            ) remaining;

          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha FROM news_canary_activations a WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (SELECT stable_bundle_sha FROM pinned_runtime)
          ), pinned_release AS (
            SELECT artifact_sha, payload FROM news_learning_artifacts r
             WHERE r.kind = 'release_evidence'
               AND r.payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
          ), protected AS (
            SELECT artifact_sha FROM news_learning_artifacts
             WHERE kind IN ('active_agent', 'deployment_receipt', 'rollback_receipt')
            UNION SELECT artifact_sha FROM pinned_release
            UNION SELECT payload ->> 'report_sha' FROM pinned_release
            UNION SELECT payload #>> '{evidence,development_dataset_sha}' FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION SELECT payload #>> '{evidence,validation_dataset_sha}' FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION SELECT payload #>> '{evidence,observation_manifest_sha}' FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION SELECT artifact_sha FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION SELECT payload ->> 'proposal_sha' FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION SELECT payload #>> '{manifest,development_dataset_sha}' FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION SELECT payload #>> '{manifest,proposal_receipt,registration_receipt_sha}'
              FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION SELECT artifact_sha FROM news_learning_artifacts
             WHERE kind = 'dataset'
               AND payload ->> 'observation_ref' IN (SELECT candidate_sha FROM pinned_candidates)
          ), semantic_references AS (
            SELECT payload ->> 'proposal_sha' AS artifact_sha
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{manifest,development_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{manifest,proposal_receipt,registration_receipt_sha}'
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{evidence,development_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload #>> '{evidence,validation_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload #>> '{evidence,observation_manifest_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload ->> 'report_sha'
              FROM news_learning_artifacts WHERE kind = 'release_evidence'
            UNION SELECT DISTINCT dataset_sha FROM news_learning_cases
          )
          SELECT count(*) INTO v_eligible_artifacts
            FROM (
              SELECT 1 FROM news_learning_artifacts a
               WHERE a.created_at_ms < v_referenced_cutoff
                 AND a.kind NOT IN ('active_agent', 'deployment_receipt', 'rollback_receipt')
                 AND NOT EXISTS (SELECT 1 FROM protected p WHERE p.artifact_sha = a.artifact_sha)
                 AND NOT EXISTS (SELECT 1 FROM semantic_references r WHERE r.artifact_sha = a.artifact_sha)
                 AND NOT (
                   a.kind = 'candidate' AND EXISTS (
                     SELECT 1 FROM news_learning_artifacts ref
                      WHERE ref.kind IN (
                        'evaluation_report', 'release_evidence', 'shadow_observation', 'canary_observation'
                      )
                        AND ref.payload ->> 'candidate_sha' = a.payload ->> 'candidate_sha'
                   )
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM news_learning_artifacts child WHERE child.parent_sha = a.artifact_sha
                 )
               LIMIT p_batch + 1
            ) remaining;

          SELECT v_now_ms - created_at_ms INTO v_oldest_recording
            FROM news_model_recordings ORDER BY created_at_ms ASC LIMIT 1;
          SELECT v_now_ms - created_at_ms INTO v_oldest_case
            FROM news_learning_cases ORDER BY created_at_ms ASC LIMIT 1;
          SELECT v_now_ms - created_at_ms INTO v_oldest_artifact
            FROM news_learning_artifacts ORDER BY created_at_ms ASC LIMIT 1;
          UPDATE news_learning_retention_state
             SET last_run_at_ms = v_now_ms,
                 eligible_recordings = v_eligible_recordings,
                 eligible_cases = v_eligible_cases,
                 eligible_artifacts = v_eligible_artifacts,
                 deleted_recordings = v_recordings,
                 deleted_cases = v_cases,
                 deleted_artifacts = v_artifacts,
                 oldest_recording_age_ms = v_oldest_recording,
                 oldest_case_age_ms = v_oldest_case,
                 oldest_artifact_age_ms = v_oldest_artifact,
                 last_error_code = NULL,
                 updated_at_ms = v_now_ms
           WHERE singleton;
          RETURN jsonb_build_object(
            'measured_at_ms', v_now_ms,
            'eligible_recordings', v_eligible_recordings,
            'eligible_cases', v_eligible_cases,
            'eligible_artifacts', v_eligible_artifacts,
            'deleted_recordings', v_recordings,
            'deleted_cases', v_cases,
            'deleted_artifacts', v_artifacts,
            'oldest_recording_age_ms', v_oldest_recording,
            'oldest_case_age_ms', v_oldest_case,
            'oldest_artifact_age_ms', v_oldest_artifact
          );
        END;
        $$;


--
-- Name: reject_new_execution_capability_v1(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_new_execution_capability_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF NEW.payload ->> 'snapshot_version' <> 'execution_capability_snapshot_v2' THEN
            RAISE EXCEPTION 'new_execution_capability_v1_forbidden';
          END IF;
          RETURN NEW;
        END
        $$;


--
-- Name: reject_new_legacy_trade_intent(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_new_legacy_trade_intent() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF NEW.intent_version <> 'trade_intent_v3' THEN
            RAISE EXCEPTION 'new_legacy_trade_intent_forbidden';
          END IF;
          RETURN NEW;
        END
        $$;


--
-- Name: reject_news_canary_append_only_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_news_canary_append_only_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          RAISE EXCEPTION 'news_canary_append_only';
        END;
        $$;


--
-- Name: reject_news_event_evidence_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_news_event_evidence_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          RAISE EXCEPTION 'news_event_evidence_append_only';
        END;
        $$;


--
-- Name: reject_news_learning_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_news_learning_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF current_setting('tracefold.learning_retention_purge', true) = 'on' THEN
            RETURN OLD;
          END IF;
          RAISE EXCEPTION 'news_learning_append_only';
        END;
        $$;


--
-- Name: reject_news_review_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_news_review_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          RAISE EXCEPTION 'news_review_append_only';
        END;
        $$;


--
-- Name: reject_retired_candidate_gate_stage(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_retired_candidate_gate_stage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF TG_OP = 'INSERT' AND NEW.stage = 'capability' THEN
            RAISE EXCEPTION 'trading_candidate_gate_stage_retired';
          ELSIF TG_OP = 'UPDATE'
                AND NEW.stage = 'capability'
                AND OLD.stage IS DISTINCT FROM 'capability' THEN
            RAISE EXCEPTION 'trading_candidate_gate_stage_retired';
          END IF;
          RETURN NEW;
        END
        $$;


--
-- Name: reject_trading_append_only_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_trading_append_only_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          RAISE EXCEPTION 'trading_append_only_mutation_forbidden';
        END
        $$;


--
-- Name: reject_trading_execution_stream_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_trading_execution_stream_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          RAISE EXCEPTION 'trading_execution_stream_append_only';
        END
        $$;


--
-- Name: reject_trading_terminal_intent_revival(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_trading_terminal_intent_revival() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF OLD.execution_state = 'TERMINAL' AND NEW.execution_state <> 'TERMINAL' THEN
            RAISE EXCEPTION 'trading_terminal_intent_revival_forbidden';
          END IF;
          RETURN NEW;
        END
        $$;


--
-- Name: stamp_trading_release_registration(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.stamp_trading_release_registration() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.registered_at_ms := trading_evidence_now_ms();
          RETURN NEW;
        END
        $$;


--
-- Name: store_trading_venue_catalog_snapshot(text, text, bigint, bigint, integer, jsonb, bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.store_trading_venue_catalog_snapshot(p_digest text, p_binding text, p_captured_at_ms bigint, p_stale_after_ms bigint, p_instrument_count integer, p_payload jsonb, p_now_ms bigint) RETURNS TABLE(identity_valid boolean, activated_binding text)
    LANGUAGE plpgsql
    AS $$
        BEGIN
          PERFORM pg_advisory_xact_lock(hashtextextended(p_digest, 0));
          INSERT INTO trading_venue_catalog_snapshots (
            snapshot_sha256, binding, captured_at_ms, stale_after_ms,
            provider_instrument_count, payload, created_at_ms
          ) VALUES (
            p_digest, p_binding, p_captured_at_ms, p_stale_after_ms,
            p_instrument_count, p_payload, p_now_ms
          )
          ON CONFLICT (snapshot_sha256) DO NOTHING;

          SELECT EXISTS (
            SELECT 1
              FROM trading_venue_catalog_snapshots existing
             WHERE existing.snapshot_sha256 = p_digest
               AND existing.binding = p_binding
               AND existing.captured_at_ms = p_captured_at_ms
               AND existing.stale_after_ms = p_stale_after_ms
               AND existing.provider_instrument_count = p_instrument_count
               AND existing.payload = p_payload
          ) INTO identity_valid;

          activated_binding := NULL;
          IF identity_valid THEN
            UPDATE trading_binding_runtime AS runtime
               SET catalog_state = 'ready',
                   catalog_snapshot_sha256 = p_digest,
                   catalog_captured_at_ms = p_captured_at_ms,
                   capability_state = CASE
                     WHEN runtime.capability_snapshot_sha256 IS NULL THEN 'missing'
                     WHEN EXISTS (
                       SELECT 1 FROM trading_execution_capability_snapshots capability
                        WHERE capability.snapshot_sha256 = runtime.capability_snapshot_sha256
                          AND capability.catalog_snapshot_sha256 = p_digest
                     ) THEN runtime.capability_state
                     ELSE 'stale'
                   END,
                   reason = CASE
                     WHEN credential_state = 'unconfigured' THEN 'credentials_unconfigured'
                     WHEN credential_state = 'invalid' THEN 'credentials_invalid'
                     WHEN runtime_state = 'stopped' THEN 'binding_adapter_unavailable'
                     WHEN runtime_state <> 'ready' THEN 'binding_unready'
                     ELSE NULL
                   END,
                   updated_at_ms = p_now_ms
             WHERE runtime.binding = p_binding
         RETURNING runtime.binding INTO activated_binding;
          END IF;
          RETURN NEXT;
        END
        $$;


--
-- Name: trading_canonical_jsonb(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trading_canonical_jsonb(value jsonb) RETURNS text
    LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
        DECLARE
          result TEXT;
          item RECORD;
          first_item BOOLEAN := true;
        BEGIN
          CASE jsonb_typeof(value)
            WHEN 'object' THEN
              result := '{';
              FOR item IN SELECT key, val FROM jsonb_each(value) AS e(key, val) ORDER BY key COLLATE "C" LOOP
                IF NOT first_item THEN result := result || ','; END IF;
                result := result || to_jsonb(item.key)::text || ':' || trading_canonical_jsonb(item.val);
                first_item := false;
              END LOOP;
              RETURN result || '}';
            WHEN 'array' THEN
              result := '[';
              FOR item IN SELECT val FROM jsonb_array_elements(value) WITH ORDINALITY AS e(val, ord) ORDER BY ord LOOP
                IF NOT first_item THEN result := result || ','; END IF;
                result := result || trading_canonical_jsonb(item.val);
                first_item := false;
              END LOOP;
              RETURN result || ']';
            ELSE
              RETURN value::text;
          END CASE;
        END;
        $$;


--
-- Name: trading_evidence_now_ms(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trading_evidence_now_ms() RETURNS bigint
    LANGUAGE sql
    AS $$
          SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::BIGINT
        $$;


--
-- Name: trading_execution_metadata_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trading_execution_metadata_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT jsonb_typeof(value) = 'object'
             AND trading_jsonb_object_size(value) <= 16
             AND octet_length(value::text) <= 2048
             AND NOT EXISTS (
               SELECT 1
                 FROM jsonb_each(value) item
                WHERE item.key !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'
                   OR CASE jsonb_typeof(item.value)
                        WHEN 'string' THEN char_length(item.value #>> '{}') > 256
                        WHEN 'number' THEN
                          (item.value #>> '{}') !~ '^-?[0-9]+$'
                          OR (item.value #>> '{}')::numeric < -9223372036854775808
                          OR (item.value #>> '{}')::numeric > 9223372036854775807
                        WHEN 'boolean' THEN false
                        ELSE true
                      END
             )
        $_$;


--
-- Name: trading_execution_string_array_valid(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trading_execution_string_array_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= 16
             AND octet_length(value::text) <= 4096
             AND NOT EXISTS (
               SELECT 1 FROM jsonb_array_elements(value) item
                WHERE jsonb_typeof(item) <> 'string' OR char_length(item #>> '{}') NOT BETWEEN 1 AND 256
             )
             AND value = COALESCE(
               (SELECT jsonb_agg(item ORDER BY item #>> '{}') FROM jsonb_array_elements(value) item),
               '[]'::jsonb
             )
             AND jsonb_array_length(value) = (
               SELECT count(DISTINCT item) FROM jsonb_array_elements(value) item
             )
        $$;


--
-- Name: trading_jsonb_object_size(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trading_jsonb_object_size(value jsonb) RETURNS integer
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT count(*)::INTEGER FROM jsonb_object_keys(value)
        $$;


--
-- Name: validate_trading_evidence_parent(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.validate_trading_evidence_parent() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
          parent_row trading_evidence_clock_receipts%ROWTYPE;
          future_batch_count INTEGER;
          future_batch_start BIGINT;
          future_batch_end BIGINT;
          future_start BIGINT;
          future_end BIGINT;
          capture_interval BIGINT;
          maximum_missingness_bps INTEGER;
          expected_batch_count INTEGER;
          expected_health_sha256 TEXT;
          source_incident BOOLEAN;
          market_incident BOOLEAN;
          expected_incidents JSONB;
        BEGIN
          NEW.recorded_at_ms := trading_evidence_now_ms();
          IF NEW.receipt_kind = 'DISCOVERY_CORPUS' THEN
            RETURN NEW;
          END IF;
          SELECT * INTO parent_row
            FROM trading_evidence_clock_receipts
           WHERE receipt_sha256 = NEW.parent_receipt_sha256
           FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'trading_evidence_parent_missing';
          END IF;
          IF NEW.recorded_at_ms <= parent_row.recorded_at_ms THEN
            RAISE EXCEPTION 'trading_evidence_parent_clock_invalid';
          END IF;
          IF NEW.receipt_kind = 'CANDIDATE_DECISION' AND (
            parent_row.receipt_kind <> 'DISCOVERY_CORPUS'
            OR parent_row.artifact_sha256 <> NEW.corpus_sha256
          ) THEN
            RAISE EXCEPTION 'trading_evidence_candidate_parent_invalid';
          END IF;
          IF NEW.receipt_kind = 'CANDIDATE_DECISION'
            AND NEW.terminal = 'CANDIDATE_LOCKED'
            AND NEW.recorded_at_ms >= (NEW.payload #>> '{evidence,statistics,future_start_ms}')::BIGINT
          THEN
            RAISE EXCEPTION 'trading_evidence_candidate_recorded_after_future_start';
          END IF;
          IF NEW.receipt_kind = 'FUTURE_DRAIN' AND (
            parent_row.receipt_kind <> 'FUTURE_CAPTURE'
            OR parent_row.terminal <> 'FUTURE_CAPTURE_SEALED'
            OR parent_row.binding <> NEW.binding
            OR parent_row.corpus_sha256 <> NEW.corpus_sha256
            OR parent_row.protocol_sha256 <> NEW.protocol_sha256
            OR NEW.payload -> 'receipt' ->> 'capture_receipt_sha256'
              IS DISTINCT FROM parent_row.receipt_sha256
            OR NEW.payload -> 'receipt' ->> 'candidate_receipt_sha256'
              IS DISTINCT FROM parent_row.parent_receipt_sha256
            OR NEW.payload -> 'receipt' ->> 'capture_sha256'
              IS DISTINCT FROM parent_row.artifact_sha256
          ) THEN
            RAISE EXCEPTION 'trading_evidence_future_drain_parent_invalid';
          END IF;
          IF NEW.receipt_kind = 'FUTURE_CAPTURE' THEN
            future_start := (parent_row.payload #>> '{evidence,statistics,future_start_ms}')::BIGINT;
            future_end := (parent_row.payload #>> '{evidence,statistics,future_end_ms}')::BIGINT;
            capture_interval := (parent_row.payload #>> '{evidence,statistics,capture_interval_ms}')::BIGINT;
            maximum_missingness_bps :=
              (parent_row.payload #>> '{evidence,statistics,maximum_missingness_bps}')::INTEGER;
            expected_batch_count :=
              ((future_end - future_start + capture_interval - 1) / capture_interval)::INTEGER;
            SELECT count(*), min(batch_start_ms), max(batch_end_ms),
                   encode(sha256(convert_to(trading_canonical_jsonb(
                     COALESCE(jsonb_agg(payload -> 'health' ORDER BY batch_start_ms), '[]'::jsonb)
                   ), 'UTF8')), 'hex'),
                   COALESCE(bool_or(
                     NOT collector_connected
                     OR payload #>> '{health,collector_last_frame_at_ms}' IS NULL
                     OR (payload #>> '{health,collector_last_frame_at_ms}')::BIGINT < batch_start_ms
                     OR payload #>> '{health,collector_error_code}' IS NOT NULL
                     OR payload #>> '{health,workers_state}' IS DISTINCT FROM 'running'
                     OR payload #>> '{health,workers_heartbeat_at_ms}' IS NULL
                     OR (payload #>> '{health,workers_heartbeat_at_ms}')::BIGINT < batch_end_ms
                     OR missing_source_bps > maximum_missingness_bps
                     OR late_source_bps > maximum_missingness_bps
                     OR catalog_missing_bps > maximum_missingness_bps
                   ), false),
                   COALESCE(bool_or(
                     (payload #>> '{health,market_instrument_count}')::INTEGER > 0
                     AND (bar_continuity_bps < 10000 OR funding_continuity_bps < 10000)
                   ), false)
              INTO future_batch_count, future_batch_start, future_batch_end,
                   expected_health_sha256, source_incident, market_incident
              FROM trading_evidence_future_capture_batches
             WHERE protocol_sha256 = NEW.protocol_sha256;
            expected_incidents := to_jsonb(array_remove(ARRAY[
              CASE WHEN market_incident THEN 'bar_or_funding_missing' END,
              CASE WHEN source_incident THEN 'source_mass_missingness' END
            ]::TEXT[], NULL));
            IF parent_row.receipt_kind <> 'CANDIDATE_DECISION'
              OR parent_row.terminal <> 'CANDIDATE_LOCKED'
              OR parent_row.binding <> NEW.binding
              OR parent_row.corpus_sha256 <> NEW.corpus_sha256
              OR parent_row.protocol_sha256 <> NEW.protocol_sha256
              OR future_batch_start <> future_start
              OR future_batch_end <> future_end
              OR future_batch_count <> expected_batch_count
              OR (NEW.payload #>> '{receipt,batch_count}')::INTEGER <> future_batch_count
              OR NEW.payload #>> '{receipt,batch_health_sha256}' IS DISTINCT FROM expected_health_sha256
              OR NEW.payload #> '{receipt,collection_incidents}' IS DISTINCT FROM expected_incidents
            THEN
              RAISE EXCEPTION 'trading_evidence_future_capture_parent_invalid';
            END IF;
          END IF;
          IF NEW.receipt_kind = 'FUTURE_RESULT' AND (
            parent_row.receipt_kind <> 'FUTURE_DRAIN'
            OR parent_row.terminal <> 'FUTURE_DRAIN_SEALED'
            OR parent_row.binding <> NEW.binding
            OR parent_row.corpus_sha256 <> NEW.corpus_sha256
            OR parent_row.protocol_sha256 <> NEW.protocol_sha256
            OR NEW.payload -> 'evidence' ->> 'candidate_receipt_sha256'
              IS DISTINCT FROM parent_row.payload -> 'receipt' ->> 'candidate_receipt_sha256'
            OR NEW.payload -> 'evidence' ->> 'future_capture_sha256'
              IS DISTINCT FROM parent_row.payload -> 'receipt' ->> 'capture_sha256'
            OR NEW.payload -> 'evidence' ->> 'future_drain_sha256'
              IS DISTINCT FROM parent_row.artifact_sha256
          ) THEN
            RAISE EXCEPTION 'trading_evidence_future_parent_invalid';
          END IF;
          RETURN NEW;
        END
        $$;


--
-- Name: validate_trading_future_capture_batch(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.validate_trading_future_capture_batch() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
          candidate trading_evidence_clock_receipts%ROWTYPE;
          future_start BIGINT;
          future_end BIGINT;
          capture_interval BIGINT;
          maximum_lag BIGINT;
          expected_start BIGINT;
          expected_end BIGINT;
        BEGIN
          NEW.recorded_at_ms := trading_evidence_now_ms();
          IF EXISTS (
            SELECT 1 FROM trading_evidence_future_capture_batches existing
             WHERE existing.protocol_sha256 = NEW.protocol_sha256
               AND existing.batch_start_ms = NEW.batch_start_ms
               AND existing.batch_sha256 = NEW.batch_sha256
               AND existing.payload = NEW.payload
          ) THEN
            RETURN NEW;
          END IF;
          SELECT * INTO candidate
            FROM trading_evidence_clock_receipts
           WHERE receipt_sha256 = NEW.candidate_receipt_sha256
           FOR UPDATE;
          IF NOT FOUND OR candidate.receipt_kind <> 'CANDIDATE_DECISION'
            OR candidate.terminal <> 'CANDIDATE_LOCKED'
            OR candidate.protocol_sha256 <> NEW.protocol_sha256
            OR candidate.binding <> NEW.binding
          THEN
            RAISE EXCEPTION 'trading_future_batch_candidate_invalid';
          END IF;
          future_start := (candidate.payload #>> '{evidence,statistics,future_start_ms}')::BIGINT;
          future_end := (candidate.payload #>> '{evidence,statistics,future_end_ms}')::BIGINT;
          capture_interval := (candidate.payload #>> '{evidence,statistics,capture_interval_ms}')::BIGINT;
          maximum_lag := (candidate.payload #>> '{evidence,statistics,maximum_capture_lag_ms}')::BIGINT;
          SELECT COALESCE(max(batch_end_ms), future_start) INTO expected_start
            FROM trading_evidence_future_capture_batches
           WHERE protocol_sha256 = NEW.protocol_sha256;
          expected_end := least(expected_start + capture_interval, future_end);
          IF NEW.batch_start_ms <> expected_start OR NEW.batch_end_ms <> expected_end
            OR NEW.recorded_at_ms < expected_end
            OR NEW.recorded_at_ms > expected_end + maximum_lag
          THEN
            RAISE EXCEPTION 'trading_future_batch_clock_invalid';
          END IF;
          RETURN NEW;
        END
        $$;


--
-- Name: validate_trading_promotion_future_evidence(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.validate_trading_promotion_future_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
          result_row trading_evidence_clock_receipts%ROWTYPE;
        BEGIN
          SELECT * INTO result_row
            FROM trading_evidence_clock_receipts
           WHERE artifact_sha256 = NEW.locked_future_report_sha256;
          IF NOT FOUND
            OR result_row.receipt_kind <> 'FUTURE_RESULT'
            OR result_row.terminal <> 'PROMOTE'
            OR result_row.binding <> NEW.binding
            OR result_row.corpus_sha256 <> NEW.sealed_corpus_sha256
          THEN
            RAISE EXCEPTION 'trading_promotion_future_evidence_invalid';
          END IF;
          RETURN NEW;
        END
        $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: news_agent_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_agent_assignments (
    event_id text NOT NULL,
    activation_id text,
    arm text NOT NULL,
    bundle_sha text NOT NULL,
    selector_version text NOT NULL,
    eligibility_reason text NOT NULL,
    assigned_at_ms bigint NOT NULL,
    CONSTRAINT news_agent_assignment_arm CHECK ((arm = ANY (ARRAY['stable'::text, 'candidate'::text]))),
    CONSTRAINT news_agent_assignment_bundle CHECK ((bundle_sha ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: news_agent_runtime_manifests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_agent_runtime_manifests (
    manifest_sha text NOT NULL,
    stable_bundle_sha text NOT NULL,
    candidate_shas jsonb NOT NULL,
    image_digest text NOT NULL,
    runtime_revision text NOT NULL,
    registered_at_ms bigint NOT NULL,
    CONSTRAINT news_agent_manifest_candidates CHECK ((jsonb_typeof(candidate_shas) = 'array'::text)),
    CONSTRAINT news_agent_manifest_sha CHECK ((manifest_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_agent_manifest_stable_sha CHECK ((stable_bundle_sha ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: news_canary_activations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_canary_activations (
    activation_id text NOT NULL,
    baseline_bundle_sha text NOT NULL,
    candidate_manifest_sha text NOT NULL,
    candidate_bundle_sha text NOT NULL,
    selector_version text NOT NULL,
    exposure_bps integer NOT NULL,
    eligibility_profile_sha text NOT NULL,
    rolling_profile_sha text NOT NULL,
    state text NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    trip_reason text,
    hold_reason text,
    rolling_last_bucket_ms bigint,
    rolling_breach_windows integer DEFAULT 0 NOT NULL,
    created_at_ms bigint NOT NULL,
    activated_at_ms bigint,
    held_at_ms bigint,
    resumed_at_ms bigint,
    tripped_at_ms bigint,
    closed_at_ms bigint,
    CONSTRAINT news_canary_activation_id CHECK ((activation_id ~ '^[0-9a-f]{32}$'::text)),
    CONSTRAINT news_canary_baseline_sha CHECK ((baseline_bundle_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_canary_candidate_manifest_sha CHECK ((candidate_manifest_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_canary_candidate_sha CHECK ((candidate_bundle_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_canary_exposure CHECK (((exposure_bps >= 1) AND (exposure_bps <= 10000))),
    CONSTRAINT news_canary_profile_sha CHECK ((eligibility_profile_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_canary_revision CHECK ((revision >= 1)),
    CONSTRAINT news_canary_rolling_breaches CHECK ((rolling_breach_windows >= 0)),
    CONSTRAINT news_canary_rolling_profile_sha CHECK ((rolling_profile_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_canary_state CHECK ((state = ANY (ARRAY['armed'::text, 'active'::text, 'tripped'::text, 'closed'::text])))
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
    edit_state text,
    pending_card jsonb,
    edit_error_code text,
    edit_attempted_at_ms bigint,
    edit_settled_at_ms bigint,
    delete_state text,
    delete_evidence jsonb,
    delete_reason text,
    delete_error_code text,
    delete_attempted_at_ms bigint,
    delete_settled_at_ms bigint,
    CONSTRAINT news_deliveries_delete_shape_check CHECK ((((delete_state IS NULL) AND (delete_evidence IS NULL) AND (delete_reason IS NULL) AND (delete_error_code IS NULL) AND (delete_attempted_at_ms IS NULL) AND (delete_settled_at_ms IS NULL)) OR ((delete_state IS NOT NULL) AND (((delete_state = 'deleting'::text) AND (delete_evidence IS NOT NULL) AND (delete_reason IS NOT NULL) AND (delete_error_code IS NULL) AND (delete_attempted_at_ms IS NOT NULL) AND (delete_settled_at_ms IS NULL)) OR ((delete_state = 'deleted'::text) AND (delete_evidence IS NOT NULL) AND (delete_reason IS NOT NULL) AND (delete_error_code IS NULL) AND (delete_attempted_at_ms IS NOT NULL) AND (delete_settled_at_ms IS NOT NULL)) OR ((delete_state = 'ambiguous'::text) AND (delete_evidence IS NOT NULL) AND (delete_reason IS NOT NULL) AND (delete_error_code IS NOT NULL) AND (delete_attempted_at_ms IS NOT NULL) AND (delete_settled_at_ms IS NOT NULL)))))),
    CONSTRAINT news_deliveries_delete_state_check CHECK (((delete_state IS NULL) OR (delete_state = ANY (ARRAY['deleting'::text, 'deleted'::text, 'ambiguous'::text])))),
    CONSTRAINT news_deliveries_edit_shape_check CHECK ((((edit_state IS NULL) AND (pending_card IS NULL) AND (edit_error_code IS NULL) AND (edit_attempted_at_ms IS NULL) AND (edit_settled_at_ms IS NULL)) OR ((edit_state IS NOT NULL) AND (((edit_state = 'editing'::text) AND (pending_card IS NOT NULL) AND (edit_error_code IS NULL) AND (edit_attempted_at_ms IS NOT NULL) AND (edit_settled_at_ms IS NULL)) OR ((edit_state = 'edited'::text) AND (pending_card IS NULL) AND (edit_error_code IS NULL) AND (edit_attempted_at_ms IS NOT NULL) AND (edit_settled_at_ms IS NOT NULL)) OR ((edit_state = 'ambiguous'::text) AND (pending_card IS NOT NULL) AND (edit_error_code IS NOT NULL) AND (edit_attempted_at_ms IS NOT NULL) AND (edit_settled_at_ms IS NOT NULL)))))),
    CONSTRAINT news_deliveries_edit_state_check CHECK (((edit_state IS NULL) OR (edit_state = ANY (ARRAY['editing'::text, 'edited'::text, 'ambiguous'::text])))),
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
    dedupe_family text CONSTRAINT news_event_bands_family_not_null NOT NULL,
    expires_at_ms bigint NOT NULL
);


--
-- Name: news_event_evidence_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_event_evidence_snapshots (
    event_id text NOT NULL,
    evidence_version integer NOT NULL,
    focus_fact_id text NOT NULL,
    evidence_sha256 text NOT NULL,
    provenance text NOT NULL,
    release_eligible boolean DEFAULT true NOT NULL,
    snapshot jsonb NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_event_evidence_current_contract_check CHECK ((((provenance = 'observed'::text) AND release_eligible AND public.news_current_evidence_snapshot_valid(snapshot, event_id, focus_fact_id) AND (evidence_sha256 = encode(sha256(convert_to(public.news_canonical_jsonb(snapshot), 'UTF8'::name)), 'hex'::text))) IS TRUE)),
    CONSTRAINT news_event_evidence_focus_nonempty CHECK ((focus_fact_id <> ''::text)),
    CONSTRAINT news_event_evidence_provenance_check CHECK ((provenance = ANY (ARRAY['observed'::text, 'legacy_reconstructed'::text]))),
    CONSTRAINT news_event_evidence_sha_check CHECK ((evidence_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_event_evidence_version_check CHECK ((evidence_version >= 0))
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
    fact_id text NOT NULL,
    fact_text text DEFAULT ''::text NOT NULL,
    CONSTRAINT news_event_members_fact_id_nonempty CHECK ((fact_id <> ''::text)),
    CONSTRAINT news_event_members_match_kind_check CHECK ((match_kind = ANY (ARRAY['leader'::text, 'exact'::text, 'near'::text])))
);


--
-- Name: news_event_reactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_event_reactions (
    event_id text NOT NULL,
    symbol text NOT NULL,
    metric_version text NOT NULL,
    venue text DEFAULT ''::text NOT NULL,
    venue_symbol text DEFAULT ''::text NOT NULL,
    instrument_class text DEFAULT 'unknown'::text NOT NULL,
    anchor_at_ms bigint NOT NULL,
    p0 numeric,
    p0_at_ms bigint,
    p1 numeric,
    p1_at_ms bigint,
    p4 numeric,
    p4_at_ms bigint,
    return_1h_bps integer,
    return_4h_bps integer,
    is_primary boolean DEFAULT false NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    unavailable_reason text,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_event_reactions_reason_check CHECK (((unavailable_reason IS NULL) OR (unavailable_reason = ANY (ARRAY['instrument_unresolved'::text, 'reference_only'::text, 'history_expired'::text, 'no_candle_within_gap'::text])))),
    CONSTRAINT news_event_reactions_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'partial'::text, 'complete'::text, 'unavailable'::text])))
);


--
-- Name: news_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_events (
    event_id text NOT NULL,
    leader_item_id text NOT NULL,
    dedupe_family text CONSTRAINT news_events_family_not_null NOT NULL,
    comparison_fingerprint text NOT NULL,
    comparison_title text NOT NULL,
    leader_title text NOT NULL,
    opened_at_ms bigint NOT NULL,
    last_member_at_ms bigint NOT NULL,
    expires_at_ms bigint NOT NULL,
    member_count integer DEFAULT 1 NOT NULL,
    admission text NOT NULL,
    queue_priority text DEFAULT 'normal'::text CONSTRAINT news_events_priority_not_null NOT NULL,
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
    focus_fact_id text NOT NULL,
    focus_fact_text text DEFAULT ''::text NOT NULL,
    focus_fact_context text DEFAULT ''::text NOT NULL,
    focus_fact_method text NOT NULL,
    focus_span_start integer DEFAULT 0 NOT NULL,
    focus_span_end integer DEFAULT 0 NOT NULL,
    event_kind text NOT NULL,
    source_contract_reason text,
    CONSTRAINT news_events_current_focus_fact_check CHECK (((focus_fact_method <> 'legacy_reconstructed'::text) IS TRUE)),
    CONSTRAINT news_events_event_kind_check CHECK ((event_kind = ANY (ARRAY['news'::text, 'listing'::text, 'oi'::text, 'liquidation'::text, 'unsupported_market'::text]))),
    CONSTRAINT news_events_focus_fact_id_nonempty CHECK ((focus_fact_id <> ''::text)),
    CONSTRAINT news_events_ingest_mode_check CHECK ((ingest_mode = ANY (ARRAY['live'::text, 'recovery'::text]))),
    CONSTRAINT news_events_queue_priority_check CHECK ((queue_priority = ANY (ARRAY['high'::text, 'normal'::text]))),
    CONSTRAINT news_events_source_contract_consistency_check CHECK (((((event_kind = ANY (ARRAY['news'::text, 'listing'::text])) AND (source_contract_reason IS NULL)) OR ((event_kind = ANY (ARRAY['oi'::text, 'liquidation'::text])) AND ((source_contract_reason IS NULL) OR (source_contract_reason = 'source_contract_drift'::text))) OR ((event_kind = 'unsupported_market'::text) AND (source_contract_reason = ANY (ARRAY['source_contract_drift'::text, 'unsupported_market_contract'::text])))) IS TRUE)),
    CONSTRAINT news_events_source_contract_reason_check CHECK ((((source_contract_reason IS NULL) OR (source_contract_reason = ANY (ARRAY['source_contract_drift'::text, 'unsupported_market_contract'::text]))) IS TRUE))
);


--
-- Name: news_external_miss_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_external_miss_snapshots (
    snapshot_id text NOT NULL,
    evidence_sha256 text NOT NULL,
    source_url text NOT NULL,
    title text NOT NULL,
    body text DEFAULT ''::text NOT NULL,
    occurred_at_ms bigint NOT NULL,
    observed_at_ms bigint NOT NULL,
    provenance text NOT NULL,
    snapshot jsonb NOT NULL,
    created_by text NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_external_miss_evidence_sha CHECK ((evidence_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_external_miss_id_sha CHECK ((snapshot_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_external_miss_snapshot_object CHECK ((jsonb_typeof(snapshot) = 'object'::text)),
    CONSTRAINT news_external_miss_source_url_nonempty CHECK ((btrim(source_url) <> ''::text)),
    CONSTRAINT news_external_miss_time_order CHECK ((observed_at_ms >= occurred_at_ms)),
    CONSTRAINT news_external_miss_title_nonempty CHECK ((btrim(title) <> ''::text))
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
    source_artifact_id text DEFAULT ''::text NOT NULL,
    CONSTRAINT news_items_first_ingest_mode_check CHECK ((first_ingest_mode = ANY (ARRAY['live'::text, 'recovery'::text])))
);


--
-- Name: news_learning_artifacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_learning_artifacts (
    artifact_sha text NOT NULL,
    kind text NOT NULL,
    parent_sha text,
    payload jsonb NOT NULL,
    created_by text NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_learning_artifact_kind CHECK ((kind = ANY (ARRAY['candidate_registration'::text, 'proposal'::text, 'candidate'::text, 'dataset'::text, 'evaluation_report'::text, 'release_evidence'::text, 'active_agent'::text, 'shadow_observation'::text, 'canary_observation'::text, 'deployment_receipt'::text, 'rollback_receipt'::text, 'program_artifact'::text, 'compile_receipt'::text, 'compile_record'::text, 'prompt_candidate'::text, 'epoch_reset'::text]))),
    CONSTRAINT news_learning_artifact_parent_sha CHECK (((parent_sha IS NULL) OR (parent_sha ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT news_learning_artifact_payload_object CHECK ((jsonb_typeof(payload) = 'object'::text)),
    CONSTRAINT news_learning_artifact_payload_size CHECK ((pg_column_size(payload) <= 1048576)),
    CONSTRAINT news_learning_artifact_sha CHECK ((artifact_sha ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: news_learning_cases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_learning_cases (
    run_sha text NOT NULL,
    case_id text NOT NULL,
    dataset_sha text NOT NULL,
    dataset_role text NOT NULL,
    evaluation_stage text NOT NULL,
    subject_kind text NOT NULL,
    event_id text,
    evidence_version integer,
    external_snapshot_id text,
    review_id text,
    opened_at_ms bigint NOT NULL,
    evidence_sha256 text NOT NULL,
    cluster_id text NOT NULL,
    stratum text NOT NULL,
    stable_observation jsonb NOT NULL,
    candidate_observation jsonb NOT NULL,
    comparison jsonb NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_learning_case_candidate_object CHECK ((jsonb_typeof(candidate_observation) = 'object'::text)),
    CONSTRAINT news_learning_case_comparison_object CHECK ((jsonb_typeof(comparison) = 'object'::text)),
    CONSTRAINT news_learning_case_dataset_sha CHECK ((dataset_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_learning_case_evidence_sha CHECK ((evidence_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_learning_case_role CHECK ((dataset_role = ANY (ARRAY['development'::text, 'validation'::text]))),
    CONSTRAINT news_learning_case_run_sha CHECK ((run_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_learning_case_stable_object CHECK ((jsonb_typeof(stable_observation) = 'object'::text)),
    CONSTRAINT news_learning_case_stage CHECK ((evaluation_stage = ANY (ARRAY['offline'::text, 'holdout'::text, 'shadow'::text, 'canary'::text]))),
    CONSTRAINT news_learning_case_subject CHECK ((subject_kind = ANY (ARRAY['event'::text, 'external_miss'::text, 'pairwise'::text])))
);


--
-- Name: news_learning_epochs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_learning_epochs (
    epoch_id text NOT NULL,
    starts_at_ms bigint NOT NULL,
    source_issue text NOT NULL,
    program_factory_id text,
    artifact_schema_version text NOT NULL,
    baseline_program_version text NOT NULL,
    baseline_program_sha256 text NOT NULL,
    prior_evidence_disposition text NOT NULL,
    reset_reason text NOT NULL,
    created_at_ms bigint NOT NULL,
    bundle_sha text,
    envelope_sha256 text,
    CONSTRAINT news_learning_epoch_baseline_sha CHECK ((baseline_program_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_learning_epoch_bundle_sha CHECK (((bundle_sha IS NULL) OR (bundle_sha ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT news_learning_epoch_disposition CHECK ((prior_evidence_disposition = 'audit_only'::text)),
    CONSTRAINT news_learning_epoch_envelope_sha CHECK (((envelope_sha256 IS NULL) OR (envelope_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT news_learning_epoch_id CHECK ((epoch_id ~ '^[a-z0-9_]+$'::text)),
    CONSTRAINT news_learning_epoch_id_derives_from_bundle CHECK (((bundle_sha IS NULL) OR (epoch_id = ('bundle_'::text || "left"(bundle_sha, 8))))),
    CONSTRAINT news_learning_epoch_runtime_identity CHECK (((bundle_sha IS NULL) = (envelope_sha256 IS NULL)))
);


--
-- Name: news_learning_retention_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_learning_retention_state (
    singleton boolean DEFAULT true NOT NULL,
    last_run_at_ms bigint,
    eligible_recordings integer DEFAULT 0 NOT NULL,
    eligible_cases integer DEFAULT 0 NOT NULL,
    eligible_artifacts integer DEFAULT 0 NOT NULL,
    deleted_recordings integer DEFAULT 0 NOT NULL,
    deleted_cases integer DEFAULT 0 NOT NULL,
    deleted_artifacts integer DEFAULT 0 NOT NULL,
    oldest_recording_age_ms bigint,
    oldest_case_age_ms bigint,
    oldest_artifact_age_ms bigint,
    last_error_code text,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_learning_retention_state_singleton_check CHECK (singleton)
);


--
-- Name: news_market_instrument_listing_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_market_instrument_listing_events (
    venue text NOT NULL,
    venue_symbol text NOT NULL,
    observed_at_ms bigint NOT NULL,
    base_symbol text NOT NULL,
    instrument_class text NOT NULL,
    quote_asset text,
    status text NOT NULL,
    CONSTRAINT news_instrument_listing_event_class_check CHECK ((instrument_class = ANY (ARRAY['crypto'::text, 'equity'::text, 'commodity'::text, 'index'::text, 'fx'::text, 'pre_ipo'::text, 'unknown'::text]))),
    CONSTRAINT news_instrument_listing_event_status_check CHECK ((status = ANY (ARRAY['trading'::text, 'delisted'::text]))),
    CONSTRAINT news_instrument_listing_event_time_check CHECK ((observed_at_ms >= 0))
);


--
-- Name: news_market_instruments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_market_instruments (
    venue text NOT NULL,
    venue_symbol text NOT NULL,
    base_symbol text NOT NULL,
    instrument_class text DEFAULT 'unknown'::text NOT NULL,
    quote_asset text,
    status text DEFAULT 'trading'::text NOT NULL,
    last_seen_ms bigint NOT NULL,
    CONSTRAINT news_market_instruments_class_check CHECK ((instrument_class = ANY (ARRAY['crypto'::text, 'equity'::text, 'commodity'::text, 'index'::text, 'fx'::text, 'pre_ipo'::text, 'unknown'::text]))),
    CONSTRAINT news_market_instruments_status_check CHECK ((status = ANY (ARRAY['trading'::text, 'delisted'::text])))
);


--
-- Name: news_market_liquidations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_market_liquidations (
    source_key text NOT NULL,
    item_id text NOT NULL,
    fact_id text NOT NULL,
    ingest_mode text NOT NULL,
    symbol text NOT NULL,
    venue text NOT NULL,
    liquidated_position_side text NOT NULL,
    forced_order_side text NOT NULL,
    notional_usd numeric NOT NULL,
    quantity numeric,
    price numeric NOT NULL,
    event_at_ms bigint NOT NULL,
    received_at_ms bigint NOT NULL,
    parser_version text NOT NULL,
    provider_record_identity text NOT NULL,
    symbol_contract_identity text NOT NULL,
    position_side_semantics text NOT NULL,
    quantity_semantics text NOT NULL,
    notional_semantics text NOT NULL,
    price_semantics text NOT NULL,
    completeness_assumption text NOT NULL,
    throttle_assumption text NOT NULL,
    source_contract_version text NOT NULL,
    source_contract_complete boolean NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_market_liquidations_forced_side_check CHECK ((forced_order_side = ANY (ARRAY['buy'::text, 'sell'::text]))),
    CONSTRAINT news_market_liquidations_ingest_mode_check CHECK ((ingest_mode = ANY (ARRAY['live'::text, 'recovery'::text]))),
    CONSTRAINT news_market_liquidations_notional_positive CHECK ((notional_usd > (0)::numeric)),
    CONSTRAINT news_market_liquidations_position_side_check CHECK ((liquidated_position_side = ANY (ARRAY['long'::text, 'short'::text]))),
    CONSTRAINT news_market_liquidations_price_positive CHECK ((price > (0)::numeric)),
    CONSTRAINT news_market_liquidations_quantity_positive CHECK (((quantity IS NULL) OR (quantity > (0)::numeric))),
    CONSTRAINT news_market_liquidations_side_semantics_check CHECK ((((liquidated_position_side = 'short'::text) AND (forced_order_side = 'buy'::text)) OR ((liquidated_position_side = 'long'::text) AND (forced_order_side = 'sell'::text)))),
    CONSTRAINT news_market_liquidations_source_contract_check CHECK (((source_contract_version <> ''::text) AND (provider_record_identity <> ''::text) AND (symbol_contract_identity <> ''::text) AND (position_side_semantics <> ''::text) AND (quantity_semantics <> ''::text) AND (notional_semantics <> ''::text) AND (price_semantics <> ''::text) AND (completeness_assumption <> ''::text) AND (throttle_assumption <> ''::text))),
    CONSTRAINT news_market_liquidations_time_order CHECK ((received_at_ms >= event_at_ms)),
    CONSTRAINT news_market_liquidations_venue_check CHECK ((venue = ANY (ARRAY['binance'::text, 'hyperliquid'::text])))
);


--
-- Name: news_model_recordings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_model_recordings (
    recording_sha text NOT NULL,
    run_sha text NOT NULL,
    case_id text NOT NULL,
    arm text NOT NULL,
    trial integer NOT NULL,
    request_sha256 text NOT NULL,
    response_sha256 text,
    request jsonb NOT NULL,
    response jsonb,
    provider text NOT NULL,
    model text NOT NULL,
    model_sha text NOT NULL,
    execution_contract_sha text NOT NULL,
    latency_ms integer,
    input_tokens integer,
    output_tokens integer,
    finish_reason text,
    error_code text,
    created_at_ms bigint NOT NULL,
    predictor_name text NOT NULL,
    call_index integer NOT NULL,
    attempt integer NOT NULL,
    route text NOT NULL,
    cached_tokens integer,
    total_tokens integer,
    provider_cost_microusd bigint,
    CONSTRAINT news_model_recording_arm CHECK ((arm = ANY (ARRAY['stable'::text, 'candidate'::text]))),
    CONSTRAINT news_model_recording_attempt CHECK (((attempt >= 1) AND (attempt <= 2))),
    CONSTRAINT news_model_recording_call_index CHECK ((call_index >= 0)),
    CONSTRAINT news_model_recording_provider_cost CHECK (((provider_cost_microusd IS NULL) OR (provider_cost_microusd >= 0))),
    CONSTRAINT news_model_recording_request_object CHECK ((jsonb_typeof(request) = 'object'::text)),
    CONSTRAINT news_model_recording_request_sha CHECK ((request_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_model_recording_request_size CHECK ((pg_column_size(request) <= 65536)),
    CONSTRAINT news_model_recording_response_object CHECK (((response IS NULL) OR (jsonb_typeof(response) = 'object'::text))),
    CONSTRAINT news_model_recording_response_sha CHECK (((response_sha256 IS NULL) OR (response_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT news_model_recording_response_size CHECK (((response IS NULL) OR (pg_column_size(response) <= 65536))),
    CONSTRAINT news_model_recording_route CHECK ((route = ANY (ARRAY['primary'::text, 'fallback'::text, 'legacy'::text]))),
    CONSTRAINT news_model_recording_run_sha CHECK ((run_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_model_recording_sha CHECK ((recording_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_model_recording_token_counts CHECK ((((cached_tokens IS NULL) OR (cached_tokens >= 0)) AND ((total_tokens IS NULL) OR (total_tokens >= 0)))),
    CONSTRAINT news_model_recording_trial CHECK (((trial >= 1) AND (trial <= 3)))
);


--
-- Name: news_oi_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_oi_signals (
    event_id text NOT NULL,
    metric_version text NOT NULL,
    symbol text NOT NULL,
    direction text NOT NULL,
    oi_change_bps bigint NOT NULL,
    oi_value_usd bigint NOT NULL,
    whale_long_profit_bps bigint NOT NULL,
    whale_oi_ratio_bps bigint NOT NULL,
    observed_at_ms bigint NOT NULL,
    rank_in_window integer NOT NULL,
    created_at_ms bigint NOT NULL,
    source_strategy_id text,
    source_contract_version text,
    measurement_window_ms bigint,
    source_item_id text NOT NULL,
    source_venue text,
    available_at_ms bigint NOT NULL,
    learning_epoch text NOT NULL,
    CONSTRAINT news_oi_signals_available_clock_check CHECK (((available_at_ms >= observed_at_ms) AND (available_at_ms >= created_at_ms))),
    CONSTRAINT news_oi_signals_direction_check CHECK ((direction = ANY (ARRAY['rise'::text, 'fall'::text]))),
    CONSTRAINT news_oi_signals_learning_epoch_nonempty CHECK ((learning_epoch <> ''::text)),
    CONSTRAINT news_oi_signals_source_contract_check CHECK ((((source_strategy_id IS NULL) AND (source_contract_version IS NULL) AND (measurement_window_ms IS NULL)) OR ((source_strategy_id IS NOT NULL) AND (source_contract_version IS NOT NULL) AND (measurement_window_ms IS NOT NULL) AND (measurement_window_ms > 0))))
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
-- Name: news_quote_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_quote_snapshots (
    source_key text NOT NULL,
    quotes jsonb DEFAULT '{}'::jsonb NOT NULL,
    target_count integer DEFAULT 0 NOT NULL,
    payload_sha256 text DEFAULT ''::text NOT NULL,
    source_at_ms bigint,
    received_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL
)
WITH (fillfactor='70', autovacuum_vacuum_scale_factor='0.0', autovacuum_vacuum_threshold='200', autovacuum_analyze_scale_factor='0.0', autovacuum_analyze_threshold='500');


--
-- Name: news_review_active_agent_v1; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.news_review_active_agent_v1 WITH (security_barrier='true') AS
 SELECT (payload ->> 'stable_sha'::text) AS stable_sha,
    created_at_ms
   FROM public.news_learning_artifacts
  WHERE (kind = 'active_agent'::text);


--
-- Name: news_review_external_source_v1; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.news_review_external_source_v1 WITH (security_barrier='true') AS
 SELECT snapshot_id,
    evidence_sha256,
    source_url,
    title,
    body,
    occurred_at_ms,
    observed_at_ms,
    provenance,
    snapshot,
    created_at_ms
   FROM public.news_external_miss_snapshots;


--
-- Name: news_review_pairwise_tasks_v1; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.news_review_pairwise_tasks_v1 WITH (security_barrier='true') AS
 SELECT run_sha,
    case_id,
    dataset_sha,
    dataset_role,
    evaluation_stage,
    subject_kind,
    event_id,
    evidence_version,
    external_snapshot_id,
    review_id,
    opened_at_ms,
    evidence_sha256,
    cluster_id,
    stratum,
        CASE
            WHEN ((comparison ->> 'pair_order'::text) = 'candidate_A'::text) THEN candidate_observation
            ELSE stable_observation
        END AS output_a,
        CASE
            WHEN ((comparison ->> 'pair_order'::text) = 'candidate_A'::text) THEN stable_observation
            ELSE candidate_observation
        END AS output_b,
    jsonb_build_object('blind_task_version', COALESCE((comparison ->> 'blind_task_version'::text), 'news_blind_pairwise_v1'::text), 'outcome_revealed', false) AS disclosure,
    created_at_ms
   FROM public.news_learning_cases
  WHERE ((evaluation_stage = ANY (ARRAY['offline'::text, 'holdout'::text])) AND COALESCE(((comparison ->> 'review_eligible'::text))::boolean, false) AND (review_id IS NOT NULL));


--
-- Name: news_reviews; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_reviews (
    review_id text NOT NULL,
    idempotency_key text,
    idempotency_request_sha text,
    review_kind text NOT NULL,
    subject_kind text NOT NULL,
    task_id text NOT NULL,
    task_version text NOT NULL,
    event_id text,
    evidence_version integer,
    external_snapshot_id text,
    pairwise_case_id text,
    rubric_version text NOT NULL,
    reader_contract_version text NOT NULL,
    reviewer text NOT NULL,
    should_push text,
    dimensions jsonb DEFAULT '{}'::jsonb NOT NULL,
    novelty jsonb DEFAULT '{}'::jsonb NOT NULL,
    first_bad_owner text,
    evidence_refs jsonb DEFAULT '[]'::jsonb NOT NULL,
    expected_correction text DEFAULT ''::text NOT NULL,
    note text DEFAULT ''::text NOT NULL,
    selection jsonb DEFAULT '{}'::jsonb NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    supersedes_review_id text,
    accepts_review_id text,
    release_eligible boolean DEFAULT true NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT news_reviews_acceptance_ref CHECK (((review_kind = 'acceptance'::text) = (accepts_review_id IS NOT NULL))),
    CONSTRAINT news_reviews_current_contract_check CHECK ((public.news_current_review_valid(review_kind, subject_kind, rubric_version, reader_contract_version, event_id, evidence_version, external_snapshot_id, pairwise_case_id, should_push, dimensions, novelty, first_bad_owner, evidence_refs, expected_correction, note, selection, payload, accepts_review_id) IS TRUE)),
    CONSTRAINT news_reviews_dimensions_object CHECK ((jsonb_typeof(dimensions) = 'object'::text)),
    CONSTRAINT news_reviews_event_subject CHECK (((subject_kind <> 'event'::text) OR ((event_id IS NOT NULL) AND (evidence_version IS NOT NULL)))),
    CONSTRAINT news_reviews_evidence_refs_array CHECK ((jsonb_typeof(evidence_refs) = 'array'::text)),
    CONSTRAINT news_reviews_external_subject CHECK (((subject_kind <> 'external_miss'::text) OR (external_snapshot_id IS NOT NULL))),
    CONSTRAINT news_reviews_id_sha CHECK ((review_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT news_reviews_idempotency_request_sha CHECK (((idempotency_request_sha IS NULL) OR (idempotency_request_sha ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT news_reviews_kind_check CHECK ((review_kind = ANY (ARRAY['judgment'::text, 'acceptance'::text, 'legacy'::text]))),
    CONSTRAINT news_reviews_novelty_object CHECK ((jsonb_typeof(novelty) = 'object'::text)),
    CONSTRAINT news_reviews_owner_check CHECK (((first_bad_owner IS NULL) OR (first_bad_owner = ANY (ARRAY['receiver'::text, 'deduper'::text, 'event_evidence'::text, 'gate'::text, 'retrieval'::text, 'storyline'::text, 'triage_prompt'::text, 'model'::text, 'policy'::text, 'delivery'::text, 'taxonomy'::text, 'unknown'::text])))),
    CONSTRAINT news_reviews_pairwise_subject CHECK (((subject_kind <> 'pairwise'::text) OR (pairwise_case_id IS NOT NULL))),
    CONSTRAINT news_reviews_payload_object CHECK ((jsonb_typeof(payload) = 'object'::text)),
    CONSTRAINT news_reviews_selection_object CHECK ((jsonb_typeof(selection) = 'object'::text)),
    CONSTRAINT news_reviews_should_push_check CHECK (((should_push IS NULL) OR (should_push = ANY (ARRAY['must_push'::text, 'should_push'::text, 'should_hold'::text, 'must_hold'::text, 'uncertain'::text])))),
    CONSTRAINT news_reviews_subject_check CHECK ((subject_kind = ANY (ARRAY['event'::text, 'external_miss'::text, 'pairwise'::text, 'legacy_label'::text])))
);


--
-- Name: news_review_records_v1; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.news_review_records_v1 WITH (security_barrier='true') AS
 SELECT review_id,
    idempotency_key,
    idempotency_request_sha,
    review_kind,
    subject_kind,
    task_id,
    task_version,
    event_id,
    evidence_version,
    external_snapshot_id,
    pairwise_case_id,
    rubric_version,
    reader_contract_version,
    reviewer,
    should_push,
    dimensions,
    novelty,
    first_bad_owner,
    evidence_refs,
    expected_correction,
    note,
    selection,
    payload,
    supersedes_review_id,
    accepts_review_id,
    release_eligible,
    created_at_ms
   FROM public.news_reviews
  WHERE (public.news_current_review_valid(review_kind, subject_kind, rubric_version, reader_contract_version, event_id, evidence_version, external_snapshot_id, pairwise_case_id, should_push, dimensions, novelty, first_bad_owner, evidence_refs, expected_correction, note, selection, payload, accepts_review_id) IS TRUE);


--
-- Name: news_verdicts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_verdicts (
    event_id text NOT NULL,
    stage text NOT NULL,
    policy_version text NOT NULL,
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
    evidence_version integer,
    evidence_sha256 text,
    focus_fact_id text,
    program_version text,
    program_sha256 text,
    editorial jsonb,
    scored_judgment_sha256 text,
    runtime_manifest_sha text,
    latency_ms double precision GENERATED ALWAYS AS (((trace ->> 'latency_ms'::text))::double precision) STORED,
    queue_lag_ms double precision GENERATED ALWAYS AS (((trace ->> 'queue_lag_ms'::text))::double precision) STORED,
    reasked_after_told_change boolean GENERATED ALWAYS AS (COALESCE(((trace ->> 'reasked_after_told_change'::text))::boolean, false)) STORED,
    seen_scope text GENERATED ALWAYS AS (COALESCE((trace ->> 'seen_scope'::text), ''::text)) STORED,
    judgment_contract_version text,
    judgment_origin text,
    CONSTRAINT news_verdicts_current_judgment_check CHECK ((((judgment_contract_version IS NOT NULL) AND (judgment_origin IS NOT NULL) AND (judgment_contract_version = 'news_judgment_v2'::text) AND (judgment_origin = ANY (ARRAY['model'::text, 'oi'::text, 'liquidation'::text, 'degraded'::text])) AND (stage = 'triage'::text) AND public.news_current_triage_verdict_valid(verdict) AND (scored_judgment_sha256 ~ '^[0-9a-f]{64}$'::text) AND (runtime_manifest_sha ~ '^[0-9a-f]{64}$'::text) AND (program_version IS NOT NULL) AND (program_sha256 ~ '^[0-9a-f]{64}$'::text) AND (evidence_version >= 1) AND (evidence_sha256 ~ '^[0-9a-f]{64}$'::text) AND (focus_fact_id IS NOT NULL) AND (focus_fact_id <> ''::text) AND (seen_scope = ANY (ARRAY[''::text, 'all'::text])) AND ((throttled_by IS NULL) OR ("right"(throttled_by, 5) <> (chr(58) || 'seen'::text)) OR (seen_scope = 'all'::text)) AND (NOT (trace ? 'type'::text)) AND public.news_jsonb_forbidden_keys_absent(trace, ARRAY['event_type'::text, 'event_type_zh'::text, 'title_zh'::text, 'actionable'::text, 'model_decision'::text, 'novelty_defaulted'::text, 'provider_cost_usd'::text, 'legacy_label'::text, 'legacy_event_type'::text, 'project_legacy_event_type'::text, 'unclear_push_event_types'::text, 'display_title'::text, 'sym'::text, 'm'::text, 'dir'::text, 'family'::text]) AND public.news_current_told_trace_valid((trace -> 'told'::text)) AND public.news_jsonb_int64_valid((trace -> 'told_count'::text)) AND (((trace ->> 'told_count'::text))::numeric = (jsonb_array_length((trace -> 'told'::text)))::numeric) AND ((trace ->> 'judgment_contract_version'::text) = judgment_contract_version) AND ((trace ->> 'judgment_origin'::text) = judgment_origin) AND ((trace ->> 'judgment_sha256'::text) = scored_judgment_sha256) AND ((trace ->> 'verdict_sha256'::text) = encode(sha256(convert_to(public.news_canonical_jsonb(verdict), 'UTF8'::name)), 'hex'::text)) AND ((trace ->> 'evidence_version'::text) = (evidence_version)::text) AND ((trace ->> 'evidence_sha256'::text) = evidence_sha256) AND ((trace ->> 'focus_fact_id'::text) = focus_fact_id) AND ((trace ->> 'runtime_manifest_sha'::text) = runtime_manifest_sha) AND ((trace ->> 'program_version'::text) = program_version) AND ((trace ->> 'program_sha256'::text) = program_sha256) AND (((judgment_origin = 'model'::text) AND (NOT degraded) AND (error_code IS NULL) AND (model IS NOT NULL) AND (program_version = 'news_semantic_program_v8'::text) AND (policy_version = 'news_triage_policy_v11'::text) AND public.news_current_model_editorial_valid(editorial) AND ((trace ->> 'editorial_sha256'::text) = (editorial ->> 'editorial_sha256'::text)) AND (scored_judgment_sha256 = encode(sha256(convert_to(public.news_canonical_jsonb(jsonb_build_object('judgment_contract_version', judgment_contract_version, 'verdict', verdict, 'editorial', editorial, 'verdict_sha256', (trace ->> 'verdict_sha256'::text))), 'UTF8'::name)), 'hex'::text))) OR ((judgment_origin = 'oi'::text) AND (editorial IS NULL) AND (model IS NULL) AND (NOT degraded) AND (program_version = 'news_oi_signal_v2'::text) AND (policy_version = 'news_triage_policy_v11'::text) AND public.news_jsonb_exact_keys((trace -> 'judgment'::text), ARRAY['judgment_contract_version'::text, 'origin'::text, 'verdict'::text, 'signal'::text, 'rank_in_window'::text, 'rule'::text, 'decision'::text]) AND ((trace #>> '{judgment,judgment_contract_version}'::text[]) = judgment_contract_version) AND ((trace #>> '{judgment,origin}'::text[]) = judgment_origin) AND ((trace #> '{judgment,verdict}'::text[]) = verdict) AND ((jsonb_typeof((trace #> '{judgment,signal}'::text[])) = 'null'::text) OR (public.news_current_oi_signal_valid((trace #> '{judgment,signal}'::text[])) IS TRUE)) AND public.news_current_oi_metadata_valid((trace -> 'oi_signal'::text), (jsonb_typeof((trace #> '{judgment,signal}'::text[])) = 'object'::text)) AND (jsonb_typeof((trace #> '{judgment,rank_in_window}'::text[])) = 'number'::text) AND ((trace #>> '{judgment,rank_in_window}'::text[]) ~ '^[0-9]+$'::text) AND (jsonb_typeof((trace #> '{judgment,rule}'::text[])) = 'string'::text) AND ((trace #>> '{judgment,rule}'::text[]) <> ''::text) AND public.news_current_decision_valid((trace #> '{judgment,decision}'::text[])) AND ((trace #>> '{judgment,decision,final}'::text[]) = final_decision) AND ((trace #>> '{judgment,decision,rule_baseline}'::text[]) = rule_baseline_decision) AND (NOT ((trace #>> '{judgment,decision,override_rule}'::text[]) IS DISTINCT FROM override_rule)) AND (NOT ((trace #>> '{judgment,decision,throttled_by}'::text[]) IS DISTINCT FROM throttled_by)) AND ((trace #>> '{judgment,rule}'::text[]) = override_rule) AND ((trace #>> '{judgment,decision,throttled_by}'::text[]) IS NULL) AND
CASE
    WHEN (jsonb_typeof((trace #> '{judgment,signal}'::text[])) = 'null'::text) THEN ((((trace #>> '{judgment,rank_in_window}'::text[]))::numeric = (0)::numeric) AND ((trace #>> '{judgment,rule}'::text[]) = 'oi_parse_failed'::text) AND (final_decision = 'drop'::text) AND (rule_baseline_decision = 'drop'::text))
    ELSE ((((trace #>> '{judgment,rank_in_window}'::text[]))::numeric >= (1)::numeric) AND ((trace #>> '{judgment,rule}'::text[]) =
    CASE
        WHEN (((trace #>> '{judgment,signal,whale_oi_ratio_bps}'::text[]))::numeric <= ((trace #>> '{oi_signal,policy,whale_oi_ratio_above_bps}'::text[]))::numeric) THEN 'whale_ratio_below_threshold'::text
        WHEN (abs(((trace #>> '{judgment,signal,oi_change_bps}'::text[]))::numeric) < ((trace #>> '{oi_signal,policy,oi_change_at_least_bps}'::text[]))::numeric) THEN 'oi_change_below_threshold'::text
        WHEN (((trace #>> '{judgment,rank_in_window}'::text[]))::numeric > ((trace #>> '{oi_signal,policy,max_rank_in_window}'::text[]))::numeric) THEN 'beyond_window_rank'::text
        ELSE 'opening_move_with_whale_concentration'::text
    END) AND (final_decision =
    CASE
        WHEN ((trace #>> '{judgment,rule}'::text[]) = 'opening_move_with_whale_concentration'::text) THEN 'push'::text
        ELSE 'drop'::text
    END) AND (rule_baseline_decision = final_decision))
END AND (scored_judgment_sha256 = encode(sha256(convert_to(public.news_canonical_jsonb((trace -> 'judgment'::text)), 'UTF8'::name)), 'hex'::text)) AND (NOT (error_code IS DISTINCT FROM
CASE
    WHEN (jsonb_typeof((trace #> '{judgment,signal}'::text[])) = 'null'::text) THEN 'oi_parse_failed'::text
    ELSE NULL::text
END))) OR ((judgment_origin = 'liquidation'::text) AND (editorial IS NULL) AND (model IS NULL) AND (NOT degraded) AND (program_version = 'news_liquidation_fact_v2'::text) AND (policy_version = 'news_liquidation_policy_v2'::text) AND public.news_jsonb_exact_keys((trace -> 'judgment'::text), ARRAY['judgment_contract_version'::text, 'origin'::text, 'verdict'::text, 'fact'::text, 'rule'::text, 'decision'::text]) AND ((trace #>> '{judgment,judgment_contract_version}'::text[]) = judgment_contract_version) AND ((trace #>> '{judgment,origin}'::text[]) = judgment_origin) AND ((trace #> '{judgment,verdict}'::text[]) = verdict) AND ((jsonb_typeof((trace #> '{judgment,fact}'::text[])) = 'null'::text) OR (public.news_current_liquidation_fact_valid((trace #> '{judgment,fact}'::text[])) IS TRUE)) AND public.news_current_liquidation_metadata_valid((trace -> 'liquidation'::text), (jsonb_typeof((trace #> '{judgment,fact}'::text[])) = 'object'::text)) AND (jsonb_typeof((trace #> '{judgment,rule}'::text[])) = 'string'::text) AND ((trace #>> '{judgment,rule}'::text[]) <> ''::text) AND public.news_current_decision_valid((trace #> '{judgment,decision}'::text[])) AND ((trace #>> '{judgment,decision,final}'::text[]) = final_decision) AND ((trace #>> '{judgment,decision,rule_baseline}'::text[]) = rule_baseline_decision) AND (NOT ((trace #>> '{judgment,decision,override_rule}'::text[]) IS DISTINCT FROM override_rule)) AND (NOT ((trace #>> '{judgment,decision,throttled_by}'::text[]) IS DISTINCT FROM throttled_by)) AND ((trace #>> '{judgment,rule}'::text[]) = override_rule) AND ((trace #>> '{judgment,decision,throttled_by}'::text[]) IS NULL) AND
CASE
    WHEN (jsonb_typeof((trace #> '{judgment,fact}'::text[])) = 'null'::text) THEN (((trace #>> '{judgment,rule}'::text[]) = 'liquidation_parse_failed'::text) AND (final_decision = 'drop'::text) AND (rule_baseline_decision = 'drop'::text))
    ELSE (((trace #>> '{judgment,rule}'::text[]) = 'liquidation_fact_only'::text) AND (final_decision = 'push'::text) AND (rule_baseline_decision = 'push'::text))
END AND (scored_judgment_sha256 = encode(sha256(convert_to(public.news_canonical_jsonb((trace -> 'judgment'::text)), 'UTF8'::name)), 'hex'::text)) AND (NOT (error_code IS DISTINCT FROM
CASE
    WHEN (jsonb_typeof((trace #> '{judgment,fact}'::text[])) = 'null'::text) THEN 'liquidation_parse_failed'::text
    ELSE NULL::text
END))) OR ((judgment_origin = 'degraded'::text) AND (editorial IS NULL) AND (model IS NULL) AND degraded AND (error_code IS NOT NULL) AND (program_version = 'news_semantic_program_v8'::text) AND (policy_version = 'news_triage_policy_v11'::text) AND (NOT (trace ? 'editorial_sha256'::text)) AND public.news_jsonb_exact_keys((trace -> 'judgment'::text), ARRAY['judgment_contract_version'::text, 'origin'::text, 'verdict'::text, 'decision'::text, 'error_code'::text]) AND ((trace #>> '{judgment,judgment_contract_version}'::text[]) = judgment_contract_version) AND ((trace #>> '{judgment,origin}'::text[]) = judgment_origin) AND ((trace #> '{judgment,verdict}'::text[]) = verdict) AND public.news_current_decision_valid((trace #> '{judgment,decision}'::text[])) AND ((trace #>> '{judgment,decision,final}'::text[]) = final_decision) AND ((trace #>> '{judgment,decision,rule_baseline}'::text[]) = rule_baseline_decision) AND (NOT ((trace #>> '{judgment,decision,override_rule}'::text[]) IS DISTINCT FROM override_rule)) AND (NOT ((trace #>> '{judgment,decision,throttled_by}'::text[]) IS DISTINCT FROM throttled_by)) AND ((trace #>> '{judgment,error_code}'::text[]) = error_code) AND (scored_judgment_sha256 = encode(sha256(convert_to(public.news_canonical_jsonb((trace -> 'judgment'::text)), 'UTF8'::name)), 'hex'::text))))) IS TRUE)),
    CONSTRAINT news_verdicts_evidence_pair_check CHECK (((evidence_version IS NULL) = (evidence_sha256 IS NULL))),
    CONSTRAINT news_verdicts_final_decision_check CHECK ((final_decision = ANY (ARRAY['push'::text, 'escalate'::text, 'drop'::text, 'throttled'::text]))),
    CONSTRAINT news_verdicts_program_pair_check CHECK (((program_version IS NULL) = (program_sha256 IS NULL))),
    CONSTRAINT news_verdicts_program_sha_check CHECK (((program_sha256 IS NULL) OR (program_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT news_verdicts_program_version_check CHECK (((program_version IS NULL) OR (btrim(program_version) <> ''::text))),
    CONSTRAINT news_verdicts_stage_check CHECK ((stage = ANY (ARRAY['triage'::text, 'deep'::text])))
);


--
-- Name: news_review_task_source_v1; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.news_review_task_source_v1 WITH (security_barrier='true') AS
 SELECT e.event_id,
    s.evidence_version,
    s.evidence_sha256,
    s.release_eligible AS evidence_release_eligible,
    s.snapshot AS evidence_snapshot,
    e.opened_at_ms,
    e.admission,
    e.queue_priority,
    e.storyline_key,
    e.ingest_mode,
    v.created_at_ms AS verdict_created_at_ms,
    v.evidence_version AS verdict_evidence_version,
    v.final_decision,
    v.degraded,
    v.error_code AS verdict_error_code,
    v.override_rule,
    v.throttled_by,
    v.verdict,
    v.trace,
    v.policy_version,
    v.model,
    d.state AS delivery_state,
    d.card AS delivery_card,
    d.settled_at_ms,
    d.error_code AS delivery_error_code,
    reaction.max_abs_return_1h_bps,
    v.program_version,
    v.program_sha256,
    v.judgment_contract_version,
    v.judgment_origin,
    v.editorial AS model_editorial,
    v.scored_judgment_sha256 AS judgment_sha256,
    v.runtime_manifest_sha,
    e.event_kind
   FROM ((((public.news_events e
     JOIN LATERAL ( SELECT x.event_id,
            x.stage,
            x.policy_version,
            x.rule_baseline_decision,
            x.final_decision,
            x.override_rule,
            x.throttled_by,
            x.verdict,
            x.model,
            x.prompt_version,
            x.degraded,
            x.error_code,
            x.trace,
            x.published_at_ms,
            x.created_at_ms,
            x.evidence_version,
            x.evidence_sha256,
            x.focus_fact_id,
            x.program_version,
            x.program_sha256,
            x.editorial,
            x.scored_judgment_sha256,
            x.runtime_manifest_sha,
            x.latency_ms,
            x.queue_lag_ms,
            x.reasked_after_told_change,
            x.seen_scope,
            x.judgment_contract_version,
            x.judgment_origin
           FROM public.news_verdicts x
          WHERE ((x.event_id = e.event_id) AND (x.stage = 'triage'::text) AND (x.judgment_contract_version = 'news_judgment_v2'::text) AND (x.judgment_origin = 'model'::text))
          ORDER BY x.created_at_ms DESC
         LIMIT 1) v ON (true))
     JOIN LATERAL ( SELECT x.event_id,
            x.evidence_version,
            x.focus_fact_id,
            x.evidence_sha256,
            x.provenance,
            x.release_eligible,
            x.snapshot,
            x.created_at_ms
           FROM public.news_event_evidence_snapshots x
          WHERE (x.event_id = e.event_id)
          ORDER BY x.evidence_version DESC
         LIMIT 1) s ON (((s.provenance = 'observed'::text) AND s.release_eligible AND ((s.snapshot ->> 'schema_version'::text) = 'news_event_evidence_v3'::text) AND (s.evidence_version = v.evidence_version) AND (s.evidence_sha256 = v.evidence_sha256) AND (s.focus_fact_id = v.focus_fact_id))))
     LEFT JOIN public.news_deliveries d ON (((d.event_id = e.event_id) AND (d.kind = 'first'::text))))
     LEFT JOIN LATERAL ( SELECT max(abs(x.return_1h_bps)) AS max_abs_return_1h_bps
           FROM public.news_event_reactions x
          WHERE ((x.event_id = e.event_id) AND (x.metric_version = 'reaction_v1'::text) AND x.is_primary)) reaction ON (true))
  WHERE (e.event_kind = 'news'::text);


--
-- Name: news_symbol_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_symbol_aliases (
    alias text NOT NULL,
    base_symbol text NOT NULL,
    source text NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT news_symbol_aliases_source_check CHECK ((source = ANY (ARRAY['venue'::text, 'opennews_prefix'::text, 'seed'::text])))
);


--
-- Name: trading_binding_runtime; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_binding_runtime (
    binding text NOT NULL,
    credential_state text NOT NULL,
    credential_fingerprint text,
    runtime_state text NOT NULL,
    account_state text NOT NULL,
    catalog_state text NOT NULL,
    catalog_snapshot_sha256 text,
    catalog_captured_at_ms bigint,
    heartbeat_at_ms bigint,
    reason text,
    updated_at_ms bigint NOT NULL,
    account_generation bigint DEFAULT 0 NOT NULL,
    capability_state text DEFAULT 'missing'::text NOT NULL,
    capability_snapshot_sha256 text,
    capability_compiled_at_ms bigint,
    capability_compile_error text,
    execution_binding_sha256 text,
    active_arm_receipt_sha256 text,
    CONSTRAINT trading_binding_account_generation_check CHECK ((account_generation >= 0)),
    CONSTRAINT trading_binding_account_state_check CHECK ((account_state = ANY (ARRAY['unknown'::text, 'reconciled_flat'::text, 'exposure_present'::text]))),
    CONSTRAINT trading_binding_capability_error_check CHECK (((capability_compile_error IS NULL) OR ((length(capability_compile_error) >= 1) AND (length(capability_compile_error) <= 128)))),
    CONSTRAINT trading_binding_capability_pair_check CHECK ((((capability_snapshot_sha256 IS NULL) AND (capability_compiled_at_ms IS NULL) AND (capability_state = ANY (ARRAY['missing'::text, 'error'::text]))) OR ((capability_snapshot_sha256 IS NOT NULL) AND (capability_compiled_at_ms IS NOT NULL) AND (capability_state = ANY (ARRAY['ready'::text, 'stale'::text, 'error'::text]))))),
    CONSTRAINT trading_binding_capability_state_check CHECK ((capability_state = ANY (ARRAY['missing'::text, 'ready'::text, 'stale'::text, 'error'::text]))),
    CONSTRAINT trading_binding_catalog_pair_check CHECK ((((catalog_snapshot_sha256 IS NULL) AND (catalog_captured_at_ms IS NULL) AND (catalog_state = ANY (ARRAY['missing'::text, 'error'::text]))) OR ((catalog_snapshot_sha256 IS NOT NULL) AND (catalog_captured_at_ms IS NOT NULL) AND (catalog_state = ANY (ARRAY['ready'::text, 'stale'::text]))))),
    CONSTRAINT trading_binding_catalog_state_check CHECK ((catalog_state = ANY (ARRAY['missing'::text, 'ready'::text, 'stale'::text, 'error'::text]))),
    CONSTRAINT trading_binding_runtime_binding_check CHECK ((binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text]))),
    CONSTRAINT trading_binding_runtime_credential_check CHECK ((credential_state = ANY (ARRAY['unconfigured'::text, 'configured'::text, 'invalid'::text]))),
    CONSTRAINT trading_binding_runtime_fingerprint_check CHECK (((credential_fingerprint IS NULL) OR (credential_fingerprint ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT trading_binding_runtime_state_check CHECK ((runtime_state = ANY (ARRAY['stopped'::text, 'starting'::text, 'ready'::text, 'stale'::text, 'faulted'::text])))
);


--
-- Name: trading_candidate_gate_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_candidate_gate_decisions (
    source_key text NOT NULL,
    gate_version text NOT NULL,
    gate_config_digest character(64) NOT NULL,
    trigger_kind text NOT NULL,
    underlying_key text,
    source_observed_at_ms bigint NOT NULL,
    status text NOT NULL,
    stage text NOT NULL,
    reason text NOT NULL,
    retryable boolean NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    case_id text,
    first_evaluated_at_ms bigint NOT NULL,
    last_evaluated_at_ms bigint NOT NULL,
    attempt_count integer DEFAULT 1 NOT NULL,
    release_revision text NOT NULL,
    CONSTRAINT trading_candidate_gate_attempts_check CHECK ((attempt_count >= 1)),
    CONSTRAINT trading_candidate_gate_case_link_check CHECK (((status = 'CASE_CREATED'::text) = (case_id IS NOT NULL))),
    CONSTRAINT trading_candidate_gate_clock_check CHECK ((last_evaluated_at_ms >= first_evaluated_at_ms)),
    CONSTRAINT trading_candidate_gate_kind_check CHECK ((trigger_kind = ANY (ARRAY['oi'::text, 'news'::text, 'liquidation'::text]))),
    CONSTRAINT trading_candidate_gate_release_nonempty CHECK ((release_revision <> ''::text)),
    CONSTRAINT trading_candidate_gate_stage_check CHECK ((stage = ANY (ARRAY['source'::text, 'venue'::text, 'eligibility'::text, 'capability'::text, 'catalog'::text, 'routing'::text, 'market_context'::text, 'freeze'::text]))),
    CONSTRAINT trading_candidate_gate_status_check CHECK ((status = ANY (ARRAY['DEFERRED'::text, 'REJECTED'::text, 'RESEARCH_ONLY'::text, 'CASE_CREATED'::text, 'EXPIRED'::text])))
);


--
-- Name: trading_capital_authorization_receipts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_capital_authorization_receipts (
    authorization_receipt_sha256 text CONSTRAINT trading_capital_authorizati_authorization_receipt_sha2_not_null NOT NULL,
    reservation_sha256 text CONSTRAINT trading_capital_authorization_recei_reservation_sha256_not_null NOT NULL,
    case_id text NOT NULL,
    binding text NOT NULL,
    created_at_ms bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_authorization_binding_check CHECK ((binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text]))),
    CONSTRAINT trading_authorization_payload_check CHECK ((((payload ->> 'authorization_version'::text) = 'capital_authorization_receipt_v1'::text) AND ((payload ->> 'reservation_sha256'::text) = reservation_sha256) AND ((payload ->> 'case_id'::text) = case_id) AND ((payload ->> 'binding'::text) = binding))),
    CONSTRAINT trading_authorization_receipt_sha_check CHECK ((authorization_receipt_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_capital_risk_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_capital_risk_events (
    event_sha256 text NOT NULL,
    reservation_sha256 text NOT NULL,
    intent_id text NOT NULL,
    event_kind text NOT NULL,
    current_planned_risk_amount numeric CONSTRAINT trading_capital_risk_events_current_planned_risk_amoun_not_null NOT NULL,
    attempt_consumed boolean NOT NULL,
    settlement_asset text,
    realized_loss_amount numeric,
    occurred_at_ms bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_risk_event_amount_check CHECK ((current_planned_risk_amount >= (0)::numeric)),
    CONSTRAINT trading_risk_event_kind_check CHECK ((event_kind = ANY (ARRAY['RESERVED'::text, 'FENCE_COMMITTED'::text, 'PLANNED_RISK_RELEASED'::text, 'EXPOSURE_OPENED'::text, 'MANUAL_REVIEW'::text, 'SETTLED'::text]))),
    CONSTRAINT trading_risk_event_payload_check CHECK ((((payload ->> 'event_version'::text) = 'capital_risk_event_v1'::text) AND ((payload ->> 'reservation_sha256'::text) = reservation_sha256) AND ((payload ->> 'intent_id'::text) = intent_id) AND ((payload ->> 'event_kind'::text) = event_kind))),
    CONSTRAINT trading_risk_event_settlement_check CHECK ((((event_kind = 'SETTLED'::text) AND (settlement_asset = ANY (ARRAY['USDT'::text, 'USDC'::text])) AND (realized_loss_amount >= (0)::numeric)) OR ((event_kind <> 'SETTLED'::text) AND (settlement_asset IS NULL) AND (realized_loss_amount IS NULL)))),
    CONSTRAINT trading_risk_event_sha_check CHECK ((event_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_capital_risk_reservation_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_capital_risk_reservation_state (
    reservation_sha256 text CONSTRAINT trading_capital_risk_reservation_st_reservation_sha256_not_null NOT NULL,
    intent_id text NOT NULL,
    status text NOT NULL,
    current_planned_risk_amount numeric CONSTRAINT trading_capital_risk_reserv_current_planned_risk_amoun_not_null NOT NULL,
    attempt_consumed boolean CONSTRAINT trading_capital_risk_reservation_stat_attempt_consumed_not_null NOT NULL,
    attempt_day_start_ms bigint,
    attempt_day_end_ms bigint,
    settlement_known boolean CONSTRAINT trading_capital_risk_reservation_stat_settlement_known_not_null NOT NULL,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT trading_risk_state_amount_check CHECK ((current_planned_risk_amount >= (0)::numeric)),
    CONSTRAINT trading_risk_state_attempt_day_check CHECK ((((NOT attempt_consumed) AND (attempt_day_start_ms IS NULL) AND (attempt_day_end_ms IS NULL)) OR (attempt_consumed AND (attempt_day_start_ms >= 0) AND (attempt_day_end_ms = (attempt_day_start_ms + 86400000))))),
    CONSTRAINT trading_risk_state_settlement_check CHECK ((((status = 'SETTLED'::text) AND settlement_known) OR (status <> 'SETTLED'::text))),
    CONSTRAINT trading_risk_state_status_check CHECK ((status = ANY (ARRAY['RESERVED'::text, 'FENCED'::text, 'OPEN'::text, 'MANUAL_REVIEW'::text, 'RELEASED'::text, 'SETTLED'::text]))),
    CONSTRAINT trading_risk_state_terminal_amount_check CHECK (((status <> ALL (ARRAY['RELEASED'::text, 'SETTLED'::text])) OR (current_planned_risk_amount = (0)::numeric)))
);


--
-- Name: trading_capital_risk_reservations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_capital_risk_reservations (
    reservation_sha256 text NOT NULL,
    case_id text NOT NULL,
    economic_lifecycle_id text CONSTRAINT trading_capital_risk_reservation_economic_lifecycle_id_not_null NOT NULL,
    binding text NOT NULL,
    settlement_asset text NOT NULL,
    risk_policy_sha256 text NOT NULL,
    grant_sha256 text NOT NULL,
    arm_receipt_sha256 text NOT NULL,
    risk_day_start_ms bigint NOT NULL,
    risk_day_end_ms bigint NOT NULL,
    target_notional numeric NOT NULL,
    planned_risk_amount numeric NOT NULL,
    created_at_ms bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_risk_reservation_amount_check CHECK (((target_notional > (0)::numeric) AND (target_notional <= (10)::numeric) AND (planned_risk_amount > (0)::numeric) AND (planned_risk_amount <= target_notional))),
    CONSTRAINT trading_risk_reservation_asset_check CHECK ((settlement_asset = ANY (ARRAY['USDT'::text, 'USDC'::text]))),
    CONSTRAINT trading_risk_reservation_binding_check CHECK ((binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text]))),
    CONSTRAINT trading_risk_reservation_day_check CHECK (((risk_day_start_ms >= 0) AND (risk_day_end_ms = (risk_day_start_ms + 86400000)))),
    CONSTRAINT trading_risk_reservation_payload_check CHECK ((((payload ->> 'reservation_version'::text) = 'capital_risk_reservation_v1'::text) AND ((payload ->> 'case_id'::text) = case_id) AND ((payload ->> 'economic_lifecycle_id'::text) = economic_lifecycle_id) AND ((payload ->> 'binding'::text) = binding) AND ((payload ->> 'settlement_asset'::text) = settlement_asset) AND ((payload ->> 'risk_policy_sha256'::text) = risk_policy_sha256) AND ((payload ->> 'grant_sha256'::text) = grant_sha256) AND ((payload ->> 'arm_receipt_sha256'::text) = arm_receipt_sha256))),
    CONSTRAINT trading_risk_reservation_sha_check CHECK ((reservation_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_cases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_cases (
    case_id text NOT NULL,
    underlying_key text NOT NULL,
    trigger_kind text CONSTRAINT trading_cases_case_kind_not_null NOT NULL,
    primary_source_key text NOT NULL,
    supplemental_source_keys jsonb DEFAULT '[]'::jsonb NOT NULL,
    manifest jsonb NOT NULL,
    manifest_sha256 text NOT NULL,
    state text NOT NULL,
    run_id text,
    lease_expires_at_ms bigint,
    attempt_count integer DEFAULT 0 NOT NULL,
    regime text,
    program_version text,
    program_sha256 text,
    program_output jsonb,
    policy_decision text NOT NULL,
    policy_reason text,
    observed_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    decided_at_ms bigint,
    updated_at_ms bigint NOT NULL,
    source_observed_at_ms bigint,
    trigger_persisted_at_ms bigint,
    strategy_id text NOT NULL,
    strategy_version text NOT NULL,
    strategy_config_digest text NOT NULL,
    policy_checks jsonb,
    capital_disposition text NOT NULL,
    capital_reason text,
    CONSTRAINT trading_cases_capital_disposition_check CHECK ((capital_disposition = ANY (ARRAY['allowed'::text, 'blocked'::text, 'not_applicable'::text]))),
    CONSTRAINT trading_cases_policy_decision_check CHECK ((policy_decision = ANY (ARRAY['long'::text, 'no_trade'::text, 'not_run'::text]))),
    CONSTRAINT trading_cases_state_check CHECK ((state = ANY (ARRAY['PENDING'::text, 'RUNNING'::text, 'NO_TRADE'::text, 'POLICY_REJECTED'::text, 'INTENT_EMITTED'::text, 'ORDER_PREPARED'::text, 'BLOCKED'::text]))),
    CONSTRAINT trading_cases_strategy_digest_check CHECK ((strategy_config_digest ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_cases_trigger_kind_check CHECK ((trigger_kind = ANY (ARRAY['oi'::text, 'liquidation'::text, 'news'::text])))
);


--
-- Name: trading_daily_risk_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_daily_risk_policies (
    risk_policy_sha256 text NOT NULL,
    approved_release text NOT NULL,
    effective_from_ms bigint NOT NULL,
    expires_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_risk_policy_clock_check CHECK (((effective_from_ms > 0) AND (expires_at_ms > effective_from_ms) AND (created_at_ms > 0))),
    CONSTRAINT trading_risk_policy_payload_check CHECK ((((payload ->> 'risk_policy_version'::text) = 'daily_risk_policy_v1'::text) AND ((payload ->> 'approved_release'::text) = approved_release) AND (((payload ->> 'effective_from_ms'::text))::bigint = effective_from_ms) AND (((payload ->> 'expires_at_ms'::text))::bigint = expires_at_ms))),
    CONSTRAINT trading_risk_policy_sha_check CHECK ((risk_policy_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_decision_runtime; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_decision_runtime (
    id smallint NOT NULL,
    state text NOT NULL,
    heartbeat_at_ms bigint,
    reason text,
    updated_at_ms bigint NOT NULL,
    CONSTRAINT trading_decision_runtime_singleton CHECK ((id = 1)),
    CONSTRAINT trading_decision_runtime_state_check CHECK ((state = ANY (ARRAY['DISABLED'::text, 'STARTING'::text, 'RUNNING'::text, 'FAULTED'::text])))
);


--
-- Name: trading_evidence_clock_receipts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_evidence_clock_receipts (
    receipt_sha256 text NOT NULL,
    receipt_kind text NOT NULL,
    terminal text NOT NULL,
    binding text,
    parent_receipt_sha256 text,
    artifact_sha256 text NOT NULL,
    corpus_sha256 text NOT NULL,
    protocol_sha256 text,
    created_at_ms bigint NOT NULL,
    recorded_at_ms bigint DEFAULT public.trading_evidence_now_ms() NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_evidence_artifact_sha_check CHECK ((artifact_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_evidence_binding_check CHECK (((binding IS NULL) OR (binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text])))),
    CONSTRAINT trading_evidence_corpus_sha_check CHECK ((corpus_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_evidence_created_at_check CHECK (((created_at_ms > 0) AND (recorded_at_ms >= created_at_ms))),
    CONSTRAINT trading_evidence_kind_shape_check CHECK ((((receipt_kind = 'DISCOVERY_CORPUS'::text) AND (terminal = 'SOURCE_FEATURE_DISCOVERY_CORPUS_V1_SEALED'::text) AND (binding IS NULL) AND (parent_receipt_sha256 IS NULL) AND (artifact_sha256 = corpus_sha256) AND (protocol_sha256 IS NULL)) OR ((receipt_kind = 'CANDIDATE_DECISION'::text) AND (terminal = ANY (ARRAY['CANDIDATE_LOCKED'::text, 'NO_CANDIDATE'::text])) AND (binding IS NOT NULL) AND (parent_receipt_sha256 IS NOT NULL) AND (((terminal = 'CANDIDATE_LOCKED'::text) AND (protocol_sha256 IS NOT NULL) AND (artifact_sha256 = protocol_sha256)) OR ((terminal = 'NO_CANDIDATE'::text) AND (protocol_sha256 IS NULL)))) OR ((receipt_kind = 'FUTURE_CAPTURE'::text) AND (terminal = 'FUTURE_CAPTURE_SEALED'::text) AND (binding IS NOT NULL) AND (parent_receipt_sha256 IS NOT NULL) AND (protocol_sha256 IS NOT NULL)) OR ((receipt_kind = 'FUTURE_DRAIN'::text) AND (terminal = 'FUTURE_DRAIN_SEALED'::text) AND (binding IS NOT NULL) AND (parent_receipt_sha256 IS NOT NULL) AND (protocol_sha256 IS NOT NULL)) OR ((receipt_kind = 'FUTURE_RESULT'::text) AND (terminal = ANY (ARRAY['PROMOTE'::text, 'HOLD'::text, 'INSUFFICIENT_EVIDENCE'::text])) AND (binding IS NOT NULL) AND (parent_receipt_sha256 IS NOT NULL) AND (protocol_sha256 IS NOT NULL)))),
    CONSTRAINT trading_evidence_payload_check CHECK ((((payload ->> 'receipt_sha256'::text) = receipt_sha256) AND ((payload ->> 'receipt_kind'::text) = receipt_kind) AND ((payload ->> 'terminal'::text) = terminal) AND ((payload ->> 'artifact_sha256'::text) = artifact_sha256) AND ((payload ->> 'corpus_sha256'::text) = corpus_sha256) AND ((binding IS NULL) OR ((payload ->> 'binding'::text) = binding)) AND ((parent_receipt_sha256 IS NULL) OR ((payload ->> 'parent_receipt_sha256'::text) = parent_receipt_sha256)) AND ((protocol_sha256 IS NULL) OR ((payload ->> 'protocol_sha256'::text) = protocol_sha256)) AND (((payload -> 'receipt'::text) ->> 'artifact_sha256'::text) = artifact_sha256) AND ((((payload -> 'receipt'::text) ->> 'created_at_ms'::text))::bigint = created_at_ms) AND ((receipt_kind <> 'FUTURE_RESULT'::text) OR (((payload -> 'receipt'::text) ->> 'report_sha256'::text) = artifact_sha256)))),
    CONSTRAINT trading_evidence_protocol_sha_check CHECK (((protocol_sha256 IS NULL) OR (protocol_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT trading_evidence_receipt_sha_check CHECK ((receipt_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_evidence_future_capture_batches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_evidence_future_capture_batches (
    protocol_sha256 text CONSTRAINT trading_evidence_future_capture_batche_protocol_sha256_not_null NOT NULL,
    batch_start_ms bigint NOT NULL,
    batch_end_ms bigint NOT NULL,
    captured_at_ms bigint NOT NULL,
    recorded_at_ms bigint DEFAULT public.trading_evidence_now_ms() NOT NULL,
    capture_lag_ms bigint NOT NULL,
    batch_sha256 text NOT NULL,
    candidate_receipt_sha256 text CONSTRAINT trading_evidence_future_captu_candidate_receipt_sha256_not_null NOT NULL,
    binding text NOT NULL,
    source_count integer NOT NULL,
    late_source_count integer CONSTRAINT trading_evidence_future_capture_batc_late_source_count_not_null NOT NULL,
    catalog_missing_count integer CONSTRAINT trading_evidence_future_capture__catalog_missing_count_not_null NOT NULL,
    collector_connected boolean CONSTRAINT trading_evidence_future_capture_ba_collector_connected_not_null NOT NULL,
    missing_source_bps integer CONSTRAINT trading_evidence_future_capture_bat_missing_source_bps_not_null NOT NULL,
    late_source_bps integer CONSTRAINT trading_evidence_future_capture_batche_late_source_bps_not_null NOT NULL,
    catalog_missing_bps integer CONSTRAINT trading_evidence_future_capture_ba_catalog_missing_bps_not_null NOT NULL,
    bar_continuity_bps integer CONSTRAINT trading_evidence_future_capture_bat_bar_continuity_bps_not_null NOT NULL,
    funding_continuity_bps integer CONSTRAINT trading_evidence_future_capture_funding_continuity_bps_not_null NOT NULL,
    artifact_integrity_sha256 text CONSTRAINT trading_evidence_future_capt_artifact_integrity_sha256_not_null NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_future_batch_binding_check CHECK ((binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text]))),
    CONSTRAINT trading_future_batch_clock_check CHECK (((batch_start_ms >= 0) AND (batch_end_ms > batch_start_ms) AND (captured_at_ms >= batch_end_ms) AND (recorded_at_ms >= captured_at_ms) AND (capture_lag_ms = (captured_at_ms - batch_end_ms)))),
    CONSTRAINT trading_future_batch_count_check CHECK (((source_count >= 0) AND (late_source_count >= 0) AND (catalog_missing_count >= 0) AND (late_source_count <= source_count) AND (catalog_missing_count <= source_count) AND ((missing_source_bps >= 0) AND (missing_source_bps <= 10000)) AND ((late_source_bps >= 0) AND (late_source_bps <= 10000)) AND ((catalog_missing_bps >= 0) AND (catalog_missing_bps <= 10000)) AND ((bar_continuity_bps >= 0) AND (bar_continuity_bps <= 10000)) AND ((funding_continuity_bps >= 0) AND (funding_continuity_bps <= 10000)))),
    CONSTRAINT trading_future_batch_integrity_sha_check CHECK ((artifact_integrity_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_future_batch_payload_check CHECK ((((payload ->> 'batch_version'::text) = 'future_capture_batch_v1'::text) AND ((payload ->> 'protocol_sha256'::text) = protocol_sha256) AND ((payload ->> 'candidate_receipt_sha256'::text) = candidate_receipt_sha256) AND ((payload ->> 'binding'::text) = binding) AND (((payload ->> 'batch_start_ms'::text))::bigint = batch_start_ms) AND (((payload ->> 'batch_end_ms'::text))::bigint = batch_end_ms) AND (((payload ->> 'captured_at_ms'::text))::bigint = captured_at_ms) AND (((payload ->> 'capture_lag_ms'::text))::bigint = capture_lag_ms) AND (((payload ->> 'source_count'::text))::integer = source_count) AND (((payload ->> 'late_source_count'::text))::integer = late_source_count) AND (((payload ->> 'catalog_missing_count'::text))::integer = catalog_missing_count) AND (((payload #>> '{health,collector_connected}'::text[]))::boolean = collector_connected) AND (((payload #>> '{health,missing_source_bps}'::text[]))::integer = missing_source_bps) AND (((payload #>> '{health,late_source_bps}'::text[]))::integer = late_source_bps) AND (((payload #>> '{health,catalog_missing_bps}'::text[]))::integer = catalog_missing_bps) AND (((payload #>> '{health,bar_continuity_bps}'::text[]))::integer = bar_continuity_bps) AND (((payload #>> '{health,funding_continuity_bps}'::text[]))::integer = funding_continuity_bps) AND ((payload #>> '{health,artifact_integrity_sha256}'::text[]) = artifact_integrity_sha256) AND (jsonb_array_length((payload -> 'sources'::text)) = source_count))),
    CONSTRAINT trading_future_batch_protocol_sha_check CHECK ((protocol_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_future_batch_sha_check CHECK ((batch_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_execution_bindings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_execution_bindings (
    binding_sha256 text NOT NULL,
    binding text NOT NULL,
    account_generation bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_execution_binding_binding_check CHECK ((binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text]))),
    CONSTRAINT trading_execution_binding_generation_check CHECK ((account_generation >= 1)),
    CONSTRAINT trading_execution_binding_payload_check CHECK ((((payload ->> 'binding_version'::text) = 'execution_binding_v1'::text) AND ((payload ->> 'binding'::text) = binding) AND (((payload ->> 'account_generation'::text))::bigint = account_generation))),
    CONSTRAINT trading_execution_binding_sha_check CHECK ((binding_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_execution_capability_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_execution_capability_snapshots (
    snapshot_sha256 text NOT NULL,
    created_at_ms bigint NOT NULL,
    execution_environment text,
    included_count integer NOT NULL,
    excluded_count integer NOT NULL,
    payload jsonb NOT NULL,
    binding text,
    venue text,
    catalog_snapshot_sha256 text,
    catalog_instrument_count integer,
    partition_sha256 text,
    CONSTRAINT trading_capability_snapshot_sha_check CHECK ((snapshot_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_capability_snapshot_shape_check CHECK (((((payload ->> 'snapshot_version'::text) = 'execution_capability_snapshot_v1'::text) AND (execution_environment = 'BINANCE_USDM_DEMO'::text) AND (binding IS NULL) AND (venue IS NULL) AND (catalog_snapshot_sha256 IS NULL) AND (catalog_instrument_count IS NULL) AND (partition_sha256 IS NULL) AND (included_count > 0) AND (excluded_count >= 0)) OR (((payload ->> 'snapshot_version'::text) = 'execution_capability_snapshot_v2'::text) AND (execution_environment IS NULL) AND (binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text])) AND (venue =
CASE binding
    WHEN 'BINANCE_USDM'::text THEN 'binance.usdm'::text
    ELSE 'hyperliquid.perp'::text
END) AND (catalog_snapshot_sha256 ~ '^[0-9a-f]{64}$'::text) AND (catalog_instrument_count >= 0) AND (included_count >= 0) AND (excluded_count >= 0) AND (catalog_instrument_count = (included_count + excluded_count)) AND (partition_sha256 ~ '^[0-9a-f]{64}$'::text) AND ((payload ->> 'binding'::text) = binding) AND ((payload ->> 'venue'::text) = venue) AND ((payload ->> 'catalog_snapshot_sha256'::text) = catalog_snapshot_sha256) AND (((payload ->> 'catalog_instrument_count'::text))::integer = catalog_instrument_count) AND (((payload ->> 'included_count'::text))::integer = included_count) AND (((payload ->> 'excluded_count'::text))::integer = excluded_count) AND ((payload ->> 'partition_sha256'::text) = partition_sha256) AND (jsonb_typeof((payload -> 'included'::text)) = 'object'::text) AND (jsonb_typeof((payload -> 'excluded'::text)) = 'object'::text) AND (public.trading_jsonb_object_size((payload -> 'included'::text)) = included_count) AND (public.trading_jsonb_object_size((payload -> 'excluded'::text)) = excluded_count))))
);


--
-- Name: trading_execution_observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_execution_observations (
    seq bigint NOT NULL,
    event_id text NOT NULL,
    runtime_profile_id text NOT NULL,
    runtime_release text NOT NULL,
    execution_strategy text NOT NULL,
    signal_id text,
    command_id text,
    normalized_kind text NOT NULL,
    occurred_at_ns bigint NOT NULL,
    observed_at_ns bigint NOT NULL,
    native_identity_references jsonb CONSTRAINT trading_execution_observati_native_identity_references_not_null NOT NULL,
    summary jsonb NOT NULL,
    payload_digest text NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_execution_observation_clock_check CHECK (((occurred_at_ns > 0) AND (observed_at_ns >= occurred_at_ns))),
    CONSTRAINT trading_execution_observation_correlation_check CHECK (((NOT ((signal_id IS NOT NULL) AND (command_id IS NOT NULL))) AND ((normalized_kind <> 'signal_disposition'::text) OR (signal_id IS NOT NULL)) AND ((normalized_kind <> 'control_disposition'::text) OR (command_id IS NOT NULL)))),
    CONSTRAINT trading_execution_observation_digest_check CHECK ((payload_digest ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_execution_observation_id_check CHECK ((event_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_execution_observation_kind_check CHECK ((normalized_kind = ANY (ARRAY['signal_disposition'::text, 'control_disposition'::text, 'risk'::text, 'order'::text, 'fill'::text, 'position'::text, 'protection'::text, 'reconciliation'::text, 'readiness'::text, 'audit_gap'::text]))),
    CONSTRAINT trading_execution_observation_native_refs_check CHECK (public.trading_execution_string_array_valid(native_identity_references)),
    CONSTRAINT trading_execution_observation_payload_check CHECK (COALESCE(((jsonb_typeof(payload) = 'object'::text) AND (public.trading_jsonb_object_size(payload) = 13) AND ((payload ->> 'observation_version'::text) = 'execution_observation_v1'::text) AND ((payload ->> 'event_id'::text) = event_id) AND ((payload ->> 'runtime_profile_id'::text) = runtime_profile_id) AND ((payload ->> 'runtime_release'::text) = runtime_release) AND ((payload ->> 'execution_strategy'::text) = execution_strategy) AND (NOT ((payload ->> 'signal_id'::text) IS DISTINCT FROM signal_id)) AND (NOT ((payload ->> 'command_id'::text) IS DISTINCT FROM command_id)) AND ((payload ->> 'normalized_kind'::text) = normalized_kind) AND (((payload ->> 'occurred_at_ns'::text))::bigint = occurred_at_ns) AND (((payload ->> 'observed_at_ns'::text))::bigint = observed_at_ns) AND ((payload -> 'native_identity_references'::text) = native_identity_references) AND ((payload -> 'summary'::text) = summary) AND ((payload ->> 'payload_digest'::text) = payload_digest)), false)),
    CONSTRAINT trading_execution_observation_profile_check CHECK ((runtime_profile_id ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'::text)),
    CONSTRAINT trading_execution_observation_release_check CHECK (((char_length(runtime_release) >= 1) AND (char_length(runtime_release) <= 128))),
    CONSTRAINT trading_execution_observation_strategy_check CHECK ((execution_strategy ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'::text)),
    CONSTRAINT trading_execution_observation_summary_check CHECK (public.trading_execution_metadata_valid(summary))
);


--
-- Name: trading_execution_observations_seq_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.trading_execution_observations ALTER COLUMN seq ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.trading_execution_observations_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: trading_execution_profile_activations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_execution_profile_activations (
    runtime_profile_id text CONSTRAINT trading_execution_profile_activatio_runtime_profile_id_not_null NOT NULL,
    account_slot text NOT NULL,
    activated_after_signal_seq bigint CONSTRAINT trading_execution_profile_a_activated_after_signal_seq_not_null NOT NULL,
    activated_after_command_seq bigint CONSTRAINT trading_execution_profile_a_activated_after_command_se_not_null NOT NULL,
    mode text NOT NULL,
    runtime_release text NOT NULL,
    config_sha256 text NOT NULL,
    created_at_ns bigint NOT NULL,
    CONSTRAINT trading_execution_activation_clock_check CHECK ((created_at_ns > 0)),
    CONSTRAINT trading_execution_activation_config_check CHECK ((config_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_execution_activation_fence_check CHECK (((activated_after_signal_seq >= 0) AND (activated_after_command_seq >= 0))),
    CONSTRAINT trading_execution_activation_mode_check CHECK ((mode = ANY (ARRAY['disabled'::text, 'paper'::text, 'live'::text]))),
    CONSTRAINT trading_execution_activation_profile_check CHECK ((runtime_profile_id ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'::text)),
    CONSTRAINT trading_execution_activation_release_check CHECK (((char_length(runtime_release) >= 1) AND (char_length(runtime_release) <= 128))),
    CONSTRAINT trading_execution_activation_slot_check CHECK ((account_slot ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'::text))
);


--
-- Name: trading_intents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_intents (
    intent_id text NOT NULL,
    intent_version text NOT NULL,
    case_id text NOT NULL,
    case_manifest_sha256 text NOT NULL,
    intent_policy_sha256 text NOT NULL,
    execution_environment text,
    instrument_id text NOT NULL,
    side text NOT NULL,
    created_at_ms bigint NOT NULL,
    valid_until_ms bigint NOT NULL,
    reference_price numeric NOT NULL,
    target_notional_usd numeric,
    stop_loss_bps integer NOT NULL,
    max_holding_ms bigint NOT NULL,
    max_entry_drift_bps integer NOT NULL,
    max_spread_bps integer NOT NULL,
    engine_identity text,
    execution_state text DEFAULT 'PENDING'::text NOT NULL,
    execution_phase text,
    terminal_outcome text,
    reason_code text,
    entry_client_order_id text,
    entry_fenced_at_ms bigint,
    stop_client_order_id text,
    stop_generation integer,
    stop_submitted_at_ms bigint,
    close_client_order_id text,
    close_submitted_at_ms bigint,
    actual_quantity numeric,
    protected_quantity numeric,
    avg_entry_price numeric,
    avg_exit_price numeric,
    position_id text,
    protection_order_id text,
    stop_price numeric,
    opened_at_ms bigint,
    protected_at_ms bigint,
    closed_at_ms bigint,
    flat_verified_at_ms bigint,
    realized_pnl_amount numeric,
    realized_pnl_currency text,
    commissions_by_currency jsonb,
    updated_at_ms bigint DEFAULT ((EXTRACT(epoch FROM now()) * (1000)::numeric))::bigint NOT NULL,
    execution_capability_snapshot_sha256 text,
    blacklist_revision_at_emission bigint,
    blacklist_snapshot_sha256_at_emission text,
    blacklist_snapshot_payload_at_emission jsonb,
    underlying_key text,
    blacklist_revision_at_fence bigint,
    blacklist_snapshot_sha256_at_fence text,
    blacklist_snapshot_payload_at_fence jsonb,
    adopted_at_ms bigint,
    entry_fence_requested_at_ms bigint,
    submission_fence_version text,
    submission_quantity numeric,
    entry_quote_q1 jsonb,
    entry_quote_q2 jsonb,
    entry_submitted_at_ms bigint,
    entry_accepted_at_ms bigint,
    source_venue text,
    source_identity text,
    canonical_asset text,
    binding text,
    account_generation bigint,
    execution_binding_sha256 text,
    venue_catalog_snapshot_sha256 text,
    capability_entry_id text,
    provider_instrument_id text,
    settlement_asset text,
    execution_policy_sha256 text,
    quote_contract_sha256 text,
    protection_contract_sha256 text,
    capital_authorization_receipt_sha256 text,
    economic_lifecycle_id text,
    entry_leg_id text,
    protection_leg_id text,
    close_leg_id text,
    leverage integer,
    target_notional numeric,
    max_risk_amount numeric,
    risk_currency text,
    funding_by_currency jsonb,
    CONSTRAINT trading_intents_commissions_object_check CHECK (((commissions_by_currency IS NULL) OR ((jsonb_typeof(commissions_by_currency) = 'object'::text) AND (octet_length((commissions_by_currency)::text) <= 2048) AND (NOT jsonb_path_exists(commissions_by_currency, '$.*?(@.type() != "string")'::jsonpath)) AND (NOT jsonb_path_exists(commissions_by_currency, '$.*?(!(@ like_regex "^-?(0|[1-9][0-9]*)(.[0-9]+)?$"))'::jsonpath))))),
    CONSTRAINT trading_intents_current_shape_check CHECK ((((intent_version = ANY (ARRAY['trade_intent_v1'::text, 'trade_intent_v2'::text])) AND (source_venue IS NULL) AND (source_identity IS NULL) AND (canonical_asset IS NULL) AND (binding IS NULL) AND (account_generation IS NULL) AND (execution_binding_sha256 IS NULL) AND (venue_catalog_snapshot_sha256 IS NULL) AND (capability_entry_id IS NULL) AND (provider_instrument_id IS NULL) AND (settlement_asset IS NULL) AND (execution_policy_sha256 IS NULL) AND (quote_contract_sha256 IS NULL) AND (protection_contract_sha256 IS NULL) AND (capital_authorization_receipt_sha256 IS NULL) AND (economic_lifecycle_id IS NULL) AND (entry_leg_id IS NULL) AND (protection_leg_id IS NULL) AND (close_leg_id IS NULL) AND (leverage IS NULL) AND (target_notional IS NULL) AND (max_risk_amount IS NULL) AND (risk_currency IS NULL) AND (execution_environment = 'BINANCE_USDM_DEMO'::text) AND (target_notional_usd IS NOT NULL) AND (((intent_version = 'trade_intent_v1'::text) AND (execution_capability_snapshot_sha256 IS NULL) AND (blacklist_revision_at_emission IS NULL) AND (blacklist_snapshot_sha256_at_emission IS NULL) AND (blacklist_snapshot_payload_at_emission IS NULL) AND (underlying_key IS NULL)) OR ((intent_version = 'trade_intent_v2'::text) AND (execution_capability_snapshot_sha256 IS NOT NULL) AND (blacklist_revision_at_emission IS NOT NULL) AND (blacklist_snapshot_sha256_at_emission ~ '^[0-9a-f]{64}$'::text) AND ((blacklist_snapshot_payload_at_emission ->> 'snapshot_version'::text) = 'blacklist_snapshot_v1'::text) AND (underlying_key ~ '^crypto:[A-Z0-9]{1,32}$'::text)))) OR ((intent_version = 'trade_intent_v3'::text) AND (execution_environment IS NULL) AND (target_notional_usd IS NULL) AND (source_venue = ANY (ARRAY['binance.usdm'::text, 'hyperliquid.perp'::text])) AND ((length(source_identity) >= 1) AND (length(source_identity) <= 256)) AND (canonical_asset ~ '^[A-Z0-9][A-Z0-9._-]{0,31}$'::text) AND (underlying_key = ('crypto:'::text || canonical_asset)) AND (binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text])) AND (source_venue =
CASE binding
    WHEN 'BINANCE_USDM'::text THEN 'binance.usdm'::text
    ELSE 'hyperliquid.perp'::text
END) AND (account_generation >= 1) AND (execution_binding_sha256 ~ '^[0-9a-f]{64}$'::text) AND (venue_catalog_snapshot_sha256 ~ '^[0-9a-f]{64}$'::text) AND (execution_capability_snapshot_sha256 ~ '^[0-9a-f]{64}$'::text) AND (capability_entry_id ~ '^[0-9a-f]{64}$'::text) AND (length(provider_instrument_id) > 0) AND (length(instrument_id) > 0) AND (settlement_asset = ANY (ARRAY['USDT'::text, 'USDC'::text])) AND (intent_policy_sha256 ~ '^[0-9a-f]{64}$'::text) AND (execution_policy_sha256 ~ '^[0-9a-f]{64}$'::text) AND (quote_contract_sha256 ~ '^[0-9a-f]{64}$'::text) AND (protection_contract_sha256 ~ '^[0-9a-f]{64}$'::text) AND (capital_authorization_receipt_sha256 ~ '^[0-9a-f]{64}$'::text) AND (blacklist_revision_at_emission >= 0) AND (blacklist_snapshot_sha256_at_emission ~ '^[0-9a-f]{64}$'::text) AND ((blacklist_snapshot_payload_at_emission ->> 'snapshot_version'::text) = 'blacklist_snapshot_v1'::text) AND (economic_lifecycle_id ~ '^[0-9a-f]{64}$'::text) AND (entry_leg_id ~ '^[0-9a-f]{64}$'::text) AND (protection_leg_id ~ '^[0-9a-f]{64}$'::text) AND (close_leg_id ~ '^[0-9a-f]{64}$'::text) AND (side = 'long'::text) AND (leverage = 1) AND (reference_price > (0)::numeric) AND (target_notional > (0)::numeric) AND (target_notional <= (10)::numeric) AND (max_risk_amount > (0)::numeric) AND (max_risk_amount <= target_notional) AND (risk_currency = settlement_asset)))),
    CONSTRAINT trading_intents_environment_check CHECK ((execution_environment = 'BINANCE_USDM_DEMO'::text)),
    CONSTRAINT trading_intents_execution_clock_order_check CHECK ((((adopted_at_ms IS NULL) OR (adopted_at_ms >= created_at_ms)) AND ((entry_fence_requested_at_ms IS NULL) OR ((adopted_at_ms IS NOT NULL) AND (entry_fence_requested_at_ms >= adopted_at_ms))) AND ((submission_fence_version IS NULL) OR ((entry_fence_requested_at_ms IS NOT NULL) AND (entry_fenced_at_ms >= entry_fence_requested_at_ms))) AND ((entry_submitted_at_ms IS NULL) OR (entry_submitted_at_ms >= entry_fenced_at_ms)) AND ((entry_accepted_at_ms IS NULL) OR (entry_accepted_at_ms >= entry_submitted_at_ms)))),
    CONSTRAINT trading_intents_execution_values_positive CHECK ((((actual_quantity IS NULL) OR (actual_quantity > (0)::numeric)) AND ((protected_quantity IS NULL) OR (protected_quantity > (0)::numeric)) AND ((avg_entry_price IS NULL) OR (avg_entry_price > (0)::numeric)) AND ((avg_exit_price IS NULL) OR (avg_exit_price > (0)::numeric)) AND ((stop_price IS NULL) OR (stop_price > (0)::numeric)))),
    CONSTRAINT trading_intents_expired_unfenced_check CHECK (((terminal_outcome <> 'EXPIRED'::text) OR ((entry_fenced_at_ms IS NULL) AND (execution_phase IS NULL) AND (reason_code = 'intent_expired'::text)))),
    CONSTRAINT trading_intents_expiry_check CHECK ((valid_until_ms = (created_at_ms + 60000))),
    CONSTRAINT trading_intents_fence_blacklist_shape CHECK ((((blacklist_revision_at_fence IS NULL) AND (blacklist_snapshot_sha256_at_fence IS NULL) AND (blacklist_snapshot_payload_at_fence IS NULL)) OR ((blacklist_revision_at_fence IS NOT NULL) AND (blacklist_snapshot_sha256_at_fence ~ '^[0-9a-f]{64}$'::text) AND ((blacklist_snapshot_payload_at_fence ->> 'snapshot_version'::text) = 'blacklist_snapshot_v1'::text)))),
    CONSTRAINT trading_intents_flat_check CHECK (((terminal_outcome <> 'CLOSED_FLAT'::text) OR ((execution_phase = 'EXIT'::text) AND (entry_fenced_at_ms IS NOT NULL) AND (actual_quantity IS NOT NULL) AND (position_id IS NOT NULL) AND (closed_at_ms IS NOT NULL) AND (flat_verified_at_ms IS NOT NULL) AND (reason_code IS NULL)))),
    CONSTRAINT trading_intents_funding_check CHECK (((funding_by_currency IS NULL) OR ((jsonb_typeof(funding_by_currency) = 'object'::text) AND (octet_length((funding_by_currency)::text) <= 2048) AND (NOT jsonb_path_exists(funding_by_currency, '$.keyvalue()?((!(@."key" like_regex "^[A-Z0-9]{1,12}$") || @."value".type() != "string") || !(@."value" like_regex "^-?(0|[1-9][0-9]*)([.][0-9]+)?$"))'::jsonpath))))),
    CONSTRAINT trading_intents_money_positive CHECK (((reference_price > (0)::numeric) AND (target_notional_usd > (0)::numeric) AND (target_notional_usd <= (10)::numeric))),
    CONSTRAINT trading_intents_phase_check CHECK (((execution_phase IS NULL) OR (execution_phase = ANY (ARRAY['ENTRY'::text, 'PROTECTION'::text, 'EXIT'::text])))),
    CONSTRAINT trading_intents_policy_bounds CHECK (((stop_loss_bps = 200) AND (max_holding_ms = 180000) AND (max_entry_drift_bps = 25) AND (max_spread_bps = 30))),
    CONSTRAINT trading_intents_q2_submission_check CHECK ((((entry_quote_q2 IS NULL) AND (entry_submitted_at_ms IS NULL) AND (entry_accepted_at_ms IS NULL)) OR ((submission_fence_version = 'submission_fence_v1'::text) AND (entry_quote_q2 IS NOT NULL) AND (((entry_quote_q2 ->> 'reason'::text) = 'accepted'::text) OR (((entry_quote_q2 ->> 'reason'::text) = reason_code) AND (execution_state = 'TERMINAL'::text) AND (terminal_outcome = 'REJECTED'::text) AND (entry_submitted_at_ms IS NULL) AND (entry_accepted_at_ms IS NULL))) AND ((entry_submitted_at_ms IS NULL) OR ((entry_quote_q2 ->> 'reason'::text) = 'accepted'::text)) AND ((entry_accepted_at_ms IS NULL) OR (entry_submitted_at_ms IS NOT NULL))))),
    CONSTRAINT trading_intents_quote_q1_audit_check CHECK (((entry_quote_q1 IS NULL) OR ((jsonb_typeof(entry_quote_q1) = 'object'::text) AND (octet_length((entry_quote_q1)::text) <= 2048) AND ((entry_quote_q1 ->> 'snapshot_version'::text) = ANY (ARRAY['execution_quote_snapshot_v1'::text, 'execution_quote_rejection_v1'::text])) AND ((entry_quote_q1 ->> 'stage'::text) = 'Q1'::text) AND ((entry_quote_q1 ->> 'reason'::text) IS NOT NULL) AND ((entry_quote_q1 ->> 'intent_id'::text) = intent_id) AND ((entry_quote_q1 ->> 'instrument_id'::text) = instrument_id) AND ((entry_quote_q1 ->> 'side'::text) =
CASE side
    WHEN 'long'::text THEN 'buy'::text
    ELSE 'sell'::text
END) AND (((entry_quote_q1 ->> 'reason'::text) <> 'accepted'::text) OR (entry_quote_q1 ?& ARRAY['side_price'::text, 'bid'::text, 'ask'::text, 'ts_event_ns'::text, 'ts_init_ns'::text, 'evaluated_at_ns'::text, 'stream_generation'::text, 'receive_age_ns'::text, 'event_age_ns'::text, 'source_latency_ns'::text, 'spread_bps'::text, 'reference_drift_bps'::text]))))),
    CONSTRAINT trading_intents_quote_q2_audit_check CHECK (((entry_quote_q2 IS NULL) OR ((jsonb_typeof(entry_quote_q2) = 'object'::text) AND (octet_length((entry_quote_q2)::text) <= 2048) AND ((entry_quote_q2 ->> 'snapshot_version'::text) = ANY (ARRAY['execution_quote_snapshot_v1'::text, 'execution_quote_rejection_v1'::text])) AND ((entry_quote_q2 ->> 'stage'::text) = 'Q2'::text) AND ((entry_quote_q2 ->> 'reason'::text) IS NOT NULL) AND ((entry_quote_q2 ->> 'intent_id'::text) = intent_id) AND ((entry_quote_q2 ->> 'instrument_id'::text) = instrument_id) AND ((entry_quote_q2 ->> 'side'::text) =
CASE side
    WHEN 'long'::text THEN 'buy'::text
    ELSE 'sell'::text
END) AND (((entry_quote_q2 ->> 'reason'::text) <> 'accepted'::text) OR (entry_quote_q2 ?& ARRAY['side_price'::text, 'bid'::text, 'ask'::text, 'ts_event_ns'::text, 'ts_init_ns'::text, 'evaluated_at_ns'::text, 'stream_generation'::text, 'receive_age_ns'::text, 'event_age_ns'::text, 'source_latency_ns'::text, 'spread_bps'::text, 'reference_drift_bps'::text]))))),
    CONSTRAINT trading_intents_reason_check CHECK (((reason_code IS NULL) OR (reason_code = ANY (ARRAY['intent_expired'::text, 'runtime_not_ready'::text, 'external_exposure'::text, 'blacklisted'::text, 'capability_mismatch'::text, 'market_unacceptable'::text, 'quantity_unexecutable'::text, 'risk_denied'::text, 'entry_outcome_unknown'::text, 'protection_unproven'::text, 'close_outcome_unknown'::text, 'settlement_unproven'::text, 'operator_intervention'::text, 'quote_missing'::text, 'quote_type_invalid'::text, 'quote_instrument_mismatch'::text, 'quote_book_invalid'::text, 'quote_side_unsupported'::text, 'quote_intent_not_active'::text, 'quote_intent_expired'::text, 'quote_clock_invalid'::text, 'quote_receive_stale'::text, 'quote_event_stale'::text, 'quote_source_latency_exceeded'::text, 'quote_future_skew'::text, 'quote_event_out_of_order'::text, 'quote_spread_exceeded'::text, 'quote_reference_drift_exceeded'::text])))),
    CONSTRAINT trading_intents_rejected_flat_check CHECK (((terminal_outcome <> 'REJECTED'::text) OR ((actual_quantity IS NULL) AND (opened_at_ms IS NULL) AND (position_id IS NULL) AND (reason_code = ANY (ARRAY['runtime_not_ready'::text, 'external_exposure'::text, 'blacklisted'::text, 'capability_mismatch'::text, 'market_unacceptable'::text, 'quantity_unexecutable'::text, 'risk_denied'::text, 'quote_missing'::text, 'quote_type_invalid'::text, 'quote_instrument_mismatch'::text, 'quote_book_invalid'::text, 'quote_side_unsupported'::text, 'quote_intent_not_active'::text, 'quote_intent_expired'::text, 'quote_clock_invalid'::text, 'quote_receive_stale'::text, 'quote_event_stale'::text, 'quote_source_latency_exceeded'::text, 'quote_future_skew'::text, 'quote_event_out_of_order'::text, 'quote_spread_exceeded'::text, 'quote_reference_drift_exceeded'::text])) AND ((entry_fenced_at_ms IS NULL) OR (flat_verified_at_ms IS NOT NULL))))),
    CONSTRAINT trading_intents_sha256_check CHECK (((intent_id ~ '^[0-9a-f]{64}$'::text) AND (case_manifest_sha256 ~ '^[0-9a-f]{64}$'::text) AND (intent_policy_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT trading_intents_side_check CHECK ((side = 'long'::text)),
    CONSTRAINT trading_intents_state_check CHECK ((execution_state = ANY (ARRAY['PENDING'::text, 'IN_FLIGHT'::text, 'OPEN_PROTECTED'::text, 'MANUAL_REVIEW'::text, 'TERMINAL'::text]))),
    CONSTRAINT trading_intents_state_shape_check CHECK ((((execution_state = 'PENDING'::text) AND (execution_phase IS NULL) AND (entry_fenced_at_ms IS NULL) AND (reason_code IS NULL)) OR ((execution_state = 'IN_FLIGHT'::text) AND (execution_phase IS NOT NULL) AND (entry_fenced_at_ms IS NOT NULL)) OR ((execution_state = 'OPEN_PROTECTED'::text) AND (execution_phase = 'PROTECTION'::text) AND (entry_fenced_at_ms IS NOT NULL) AND (actual_quantity IS NOT NULL) AND (protected_quantity = actual_quantity) AND (position_id IS NOT NULL) AND (avg_entry_price IS NOT NULL) AND (opened_at_ms IS NOT NULL) AND (stop_client_order_id IS NOT NULL) AND (stop_generation IS NOT NULL) AND (stop_submitted_at_ms IS NOT NULL) AND (protection_order_id IS NOT NULL) AND (stop_price IS NOT NULL) AND (protected_at_ms IS NOT NULL)) OR ((execution_state = 'MANUAL_REVIEW'::text) AND (entry_fenced_at_ms IS NOT NULL) AND (execution_phase IS NOT NULL) AND (reason_code = ANY (ARRAY['entry_outcome_unknown'::text, 'protection_unproven'::text, 'close_outcome_unknown'::text, 'settlement_unproven'::text, 'operator_intervention'::text]))) OR (execution_state = 'TERMINAL'::text))),
    CONSTRAINT trading_intents_stop_generation_check CHECK (((stop_generation IS NULL) OR (stop_generation >= 0))),
    CONSTRAINT trading_intents_submission_fence_v1_check CHECK ((((submission_fence_version IS NULL) AND (submission_quantity IS NULL)) OR ((submission_fence_version = 'submission_fence_v1'::text) AND (submission_quantity > (0)::numeric) AND ((submission_quantity * ((entry_quote_q1 ->> 'side_price'::text))::numeric) <= COALESCE(target_notional, target_notional_usd)) AND (entry_client_order_id IS NOT NULL) AND (entry_fenced_at_ms IS NOT NULL) AND ((entry_quote_q1 ->> 'snapshot_version'::text) = 'execution_quote_snapshot_v1'::text) AND ((entry_quote_q1 ->> 'reason'::text) = 'accepted'::text)))),
    CONSTRAINT trading_intents_submission_identity_pairs CHECK ((((entry_client_order_id IS NULL) = (entry_fenced_at_ms IS NULL)) AND ((stop_client_order_id IS NULL) = (stop_submitted_at_ms IS NULL)) AND ((stop_client_order_id IS NULL) = (stop_generation IS NULL)) AND ((close_client_order_id IS NULL) = (close_submitted_at_ms IS NULL)))),
    CONSTRAINT trading_intents_terminal_check CHECK ((((execution_state = 'TERMINAL'::text) AND (terminal_outcome = ANY (ARRAY['EXPIRED'::text, 'REJECTED'::text, 'CLOSED_FLAT'::text]))) OR ((execution_state <> 'TERMINAL'::text) AND (terminal_outcome IS NULL)))),
    CONSTRAINT trading_intents_version_check CHECK ((intent_version = ANY (ARRAY['trade_intent_v1'::text, 'trade_intent_v2'::text, 'trade_intent_v3'::text])))
);


--
-- Name: trading_nautilus_runtime_starts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_nautilus_runtime_starts (
    start_sha256 text NOT NULL,
    runtime_id uuid NOT NULL,
    runtime_revision text NOT NULL,
    image_digest text NOT NULL,
    nautilus_version text NOT NULL,
    nautilus_source_git_commit text CONSTRAINT trading_nautilus_runtime_st_nautilus_source_git_commit_not_null NOT NULL,
    nautilus_wheel_identity text CONSTRAINT trading_nautilus_runtime_start_nautilus_wheel_identity_not_null NOT NULL,
    started_at_ms bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_nautilus_source_sha_check CHECK ((nautilus_source_git_commit ~ '^[0-9a-f]{40}$'::text)),
    CONSTRAINT trading_nautilus_start_clock_check CHECK ((started_at_ms > 0)),
    CONSTRAINT trading_nautilus_start_identity_check CHECK (((length(runtime_revision) > 0) AND (length(image_digest) > 0) AND (length(nautilus_version) > 0) AND (length(nautilus_wheel_identity) > 0) AND ((payload ->> 'start_version'::text) = 'nautilus_runtime_start_v1'::text) AND (((payload ->> 'runtime_id'::text))::uuid = runtime_id) AND ((payload ->> 'runtime_revision'::text) = runtime_revision) AND ((payload ->> 'image_digest'::text) = image_digest) AND ((payload ->> 'nautilus_version'::text) = nautilus_version) AND ((payload ->> 'nautilus_source_git_commit'::text) = nautilus_source_git_commit) AND ((payload ->> 'nautilus_wheel_identity'::text) = nautilus_wheel_identity) AND (((payload ->> 'started_at_ms'::text))::bigint = started_at_ms))),
    CONSTRAINT trading_nautilus_start_sha_check CHECK ((start_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_operator_arm_receipts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_operator_arm_receipts (
    arm_receipt_sha256 text NOT NULL,
    arm_epoch bigint NOT NULL,
    binding text NOT NULL,
    grant_sha256 text NOT NULL,
    risk_policy_sha256 text NOT NULL,
    armed_at_ms bigint NOT NULL,
    expires_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_arm_binding_check CHECK ((binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text]))),
    CONSTRAINT trading_arm_clock_check CHECK (((armed_at_ms > 0) AND (expires_at_ms > armed_at_ms))),
    CONSTRAINT trading_arm_epoch_check CHECK ((arm_epoch >= 1)),
    CONSTRAINT trading_arm_payload_check CHECK ((((payload ->> 'arm_version'::text) = 'operator_arm_receipt_v1'::text) AND (((payload ->> 'arm_epoch'::text))::bigint = arm_epoch) AND ((payload ->> 'binding'::text) = binding) AND ((payload ->> 'grant_sha256'::text) = grant_sha256) AND ((payload ->> 'risk_policy_sha256'::text) = risk_policy_sha256) AND ((payload ->> 'reconciliation_state'::text) = 'reconciled_flat'::text))),
    CONSTRAINT trading_arm_receipt_sha_check CHECK ((arm_receipt_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_operator_intents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_operator_intents (
    seq bigint NOT NULL,
    command_id text NOT NULL,
    target_profile_id text NOT NULL,
    action text NOT NULL,
    scope text NOT NULL,
    reason text NOT NULL,
    operator_identity text NOT NULL,
    authentication_identity text NOT NULL,
    requested_at_ns bigint NOT NULL,
    expires_at_ns bigint NOT NULL,
    confirmation_identity text,
    market_key text,
    direction text,
    payload jsonb NOT NULL,
    CONSTRAINT trading_operator_intent_action_check CHECK ((action = ANY (ARRAY['pause_entries'::text, 'resume_entries'::text, 'emergency_halt'::text, 'flatten'::text, 'manual_entry'::text]))),
    CONSTRAINT trading_operator_intent_clock_check CHECK (((requested_at_ns > 0) AND (expires_at_ns > requested_at_ns) AND ((expires_at_ns - requested_at_ns) <= '3600000000000'::bigint))),
    CONSTRAINT trading_operator_intent_confirmation_check CHECK ((((action = ANY (ARRAY['resume_entries'::text, 'emergency_halt'::text, 'flatten'::text])) AND (confirmation_identity IS NOT NULL) AND (confirmation_identity ~ '^[0-9a-f]{64}$'::text)) OR ((action <> ALL (ARRAY['resume_entries'::text, 'emergency_halt'::text, 'flatten'::text])) AND (confirmation_identity IS NULL)))),
    CONSTRAINT trading_operator_intent_id_check CHECK ((command_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_operator_intent_manual_entry_check CHECK ((((action = 'manual_entry'::text) AND (market_key IS NOT NULL) AND (market_key ~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$'::text) AND (direction IS NOT NULL) AND (direction = ANY (ARRAY['long'::text, 'short'::text]))) OR ((action <> 'manual_entry'::text) AND (market_key IS NULL) AND (direction IS NULL)))),
    CONSTRAINT trading_operator_intent_payload_check CHECK (COALESCE(((jsonb_typeof(payload) = 'object'::text) AND (public.trading_jsonb_object_size(payload) = 13) AND ((payload ->> 'intent_version'::text) = 'operator_intent_v1'::text) AND ((payload ->> 'command_id'::text) = command_id) AND ((payload ->> 'target_profile_id'::text) = target_profile_id) AND ((payload ->> 'action'::text) = action) AND ((payload ->> 'scope'::text) = scope) AND ((payload ->> 'reason'::text) = reason) AND ((payload ->> 'operator_identity'::text) = operator_identity) AND ((payload ->> 'authentication_identity'::text) = authentication_identity) AND (((payload ->> 'requested_at_ns'::text))::bigint = requested_at_ns) AND (((payload ->> 'expires_at_ns'::text))::bigint = expires_at_ns) AND (NOT ((payload ->> 'confirmation_identity'::text) IS DISTINCT FROM confirmation_identity)) AND (NOT ((payload ->> 'market_key'::text) IS DISTINCT FROM market_key)) AND (NOT ((payload ->> 'direction'::text) IS DISTINCT FROM direction))), false)),
    CONSTRAINT trading_operator_intent_profile_check CHECK ((target_profile_id ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'::text)),
    CONSTRAINT trading_operator_intent_text_check CHECK ((char_length(scope) BETWEEN 1 AND 128) AND (char_length(reason) BETWEEN 1 AND 256) AND (char_length(operator_identity) BETWEEN 1 AND 128) AND (char_length(authentication_identity) BETWEEN 1 AND 256))
);


--
-- Name: trading_operator_intents_seq_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.trading_operator_intents ALTER COLUMN seq ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.trading_operator_intents_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: trading_order_observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_order_observations (
    order_id text NOT NULL,
    observation_kind text NOT NULL,
    content_sha256 text NOT NULL,
    content jsonb NOT NULL,
    first_seen_at_ms bigint NOT NULL,
    last_seen_at_ms bigint NOT NULL,
    seen_count integer DEFAULT 1 NOT NULL
);


--
-- Name: trading_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_orders (
    order_id text NOT NULL,
    case_id text NOT NULL,
    underlying_key text NOT NULL,
    exchange_id text NOT NULL,
    provider_symbol text NOT NULL,
    account_ref text NOT NULL,
    mode text NOT NULL,
    side text NOT NULL,
    notional_usd numeric NOT NULL,
    quantity numeric NOT NULL,
    entry_reference numeric NOT NULL,
    stop_price numeric NOT NULL,
    take_profit_price numeric,
    payload jsonb NOT NULL,
    payload_sha256 text NOT NULL,
    state text NOT NULL,
    state_reason text,
    provider_attempt_count integer DEFAULT 0 NOT NULL,
    exit_attempt_count integer DEFAULT 0 NOT NULL,
    exit_attempt_total integer DEFAULT 0 NOT NULL,
    remote_order_id text,
    filled_quantity numeric,
    average_price numeric,
    exit_price numeric,
    exit_reason text,
    realized_bps integer,
    position_opened_at_ms bigint,
    position_closed_at_ms bigint,
    must_close_at_ms bigint,
    next_reconcile_at_ms bigint,
    closed_at_ms bigint,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL,
    max_holding_ms bigint,
    taker_fee_bps integer,
    CONSTRAINT trading_orders_bounded_exit_attempts CHECK ((exit_attempt_total <= 3)),
    CONSTRAINT trading_orders_exchange_check CHECK ((exchange_id = ANY (ARRAY['binance'::text, 'hyperliquid'::text, 'paper'::text]))),
    CONSTRAINT trading_orders_max_holding_positive CHECK (((max_holding_ms IS NULL) OR (max_holding_ms > 0))),
    CONSTRAINT trading_orders_mode_check CHECK ((mode = ANY (ARRAY['paper'::text, 'live_reviewed'::text, 'live_bounded'::text]))),
    CONSTRAINT trading_orders_notional_positive CHECK ((notional_usd > (0)::numeric)),
    CONSTRAINT trading_orders_one_attempt CHECK ((provider_attempt_count <= 1)),
    CONSTRAINT trading_orders_one_exit_attempt CHECK ((exit_attempt_count <= 1)),
    CONSTRAINT trading_orders_quantity_positive CHECK ((quantity > (0)::numeric)),
    CONSTRAINT trading_orders_side_check CHECK ((side = ANY (ARRAY['buy'::text, 'sell'::text]))),
    CONSTRAINT trading_orders_state_check CHECK ((state = ANY (ARRAY['PREPARED'::text, 'AWAITING_APPROVAL'::text, 'APPROVED'::text, 'REJECTED_BY_OPERATOR'::text, 'SUBMITTING'::text, 'REJECTED'::text, 'ACKNOWLEDGED'::text, 'PARTIAL'::text, 'OPEN'::text, 'UNPROTECTED'::text, 'SAFETY_CLOSING'::text, 'AMBIGUOUS'::text, 'RECONCILING'::text, 'MANUAL_REVIEW_REQUIRED'::text, 'CLOSED'::text]))),
    CONSTRAINT trading_orders_taker_fee_non_negative CHECK (((taker_fee_bps IS NULL) OR (taker_fee_bps >= 0)))
);


--
-- Name: trading_production_promotion_grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_production_promotion_grants (
    grant_sha256 text NOT NULL,
    binding text NOT NULL,
    risk_policy_sha256 text NOT NULL,
    issued_at_ms bigint NOT NULL,
    review_at_ms bigint NOT NULL,
    expires_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    payload jsonb NOT NULL,
    sealed_corpus_sha256 text CONSTRAINT trading_production_promotion_gran_sealed_corpus_sha256_not_null NOT NULL,
    locked_future_report_sha256 text CONSTRAINT trading_production_promotio_locked_future_report_sha25_not_null NOT NULL,
    CONSTRAINT trading_promotion_grant_binding_check CHECK ((binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text]))),
    CONSTRAINT trading_promotion_grant_clock_check CHECK (((issued_at_ms > 0) AND (review_at_ms >= issued_at_ms) AND (expires_at_ms > review_at_ms))),
    CONSTRAINT trading_promotion_grant_corpus_sha_check CHECK ((sealed_corpus_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_promotion_grant_evidence_payload_check CHECK ((((payload ->> 'sealed_corpus_sha256'::text) = sealed_corpus_sha256) AND ((payload ->> 'locked_future_report_sha256'::text) = locked_future_report_sha256) AND (jsonb_typeof((payload -> 'allowed_capability_entry_ids'::text)) = 'array'::text) AND (jsonb_array_length((payload -> 'allowed_capability_entry_ids'::text)) = 1))),
    CONSTRAINT trading_promotion_grant_future_sha_check CHECK ((locked_future_report_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_promotion_grant_payload_check CHECK ((((payload ->> 'grant_version'::text) = 'production_promotion_grant_v1'::text) AND ((payload ->> 'scope'::text) = 'canary'::text) AND ((payload ->> 'binding'::text) = binding) AND ((payload ->> 'locked_future_result'::text) = 'PROMOTE'::text) AND ((payload ->> 'risk_policy_sha256'::text) = risk_policy_sha256))),
    CONSTRAINT trading_promotion_grant_sha_check CHECK ((grant_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_production_release_registrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_production_release_registrations (
    release_sha256 text CONSTRAINT trading_production_release_registration_release_sha256_not_null NOT NULL,
    window_sha256 text NOT NULL,
    release_tag text NOT NULL,
    git_commit_sha text CONSTRAINT trading_production_release_registration_git_commit_sha_not_null NOT NULL,
    oci_image_digest text CONSTRAINT trading_production_release_registrati_oci_image_digest_not_null NOT NULL,
    window_start_ms bigint CONSTRAINT trading_production_release_registratio_window_start_ms_not_null NOT NULL,
    window_end_ms bigint NOT NULL,
    workers_runtime_id uuid CONSTRAINT trading_production_release_registra_workers_runtime_id_not_null NOT NULL,
    workers_runtime_revision text CONSTRAINT trading_production_release_re_workers_runtime_revision_not_null NOT NULL,
    workers_image_digest text CONSTRAINT trading_production_release_regist_workers_image_digest_not_null NOT NULL,
    workers_started_at_ms bigint CONSTRAINT trading_production_release_regis_workers_started_at_ms_not_null NOT NULL,
    serve_runtime_id uuid CONSTRAINT trading_production_release_registrati_serve_runtime_id_not_null NOT NULL,
    serve_runtime_revision text CONSTRAINT trading_production_release_regi_serve_runtime_revision_not_null NOT NULL,
    serve_image_digest text CONSTRAINT trading_production_release_registra_serve_image_digest_not_null NOT NULL,
    serve_started_at_ms bigint CONSTRAINT trading_production_release_registr_serve_started_at_ms_not_null NOT NULL,
    serve_measured_at_ms bigint CONSTRAINT trading_production_release_regist_serve_measured_at_ms_not_null NOT NULL,
    registered_at_ms bigint DEFAULT public.trading_evidence_now_ms() CONSTRAINT trading_production_release_registrati_registered_at_ms_not_null NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_release_registration_clock_check CHECK (((window_end_ms > window_start_ms) AND (workers_started_at_ms <= registered_at_ms) AND (serve_started_at_ms <= serve_measured_at_ms) AND (serve_measured_at_ms <= registered_at_ms) AND (registered_at_ms < window_start_ms))),
    CONSTRAINT trading_release_registration_git_sha_check CHECK ((git_commit_sha ~ '^[0-9a-f]{40}$'::text)),
    CONSTRAINT trading_release_registration_payload_check CHECK ((((payload ->> 'registration_version'::text) = 'production_release_registration_v1'::text) AND ((payload ->> 'release_sha256'::text) = release_sha256) AND ((payload ->> 'window_sha256'::text) = window_sha256) AND ((payload #>> '{release,release_tag}'::text[]) = release_tag) AND ((payload #>> '{release,git_commit_sha}'::text[]) = git_commit_sha) AND ((payload #>> '{release,oci_image_digest}'::text[]) = oci_image_digest) AND (((payload #>> '{release,acceptance_window,start_ms}'::text[]))::bigint = window_start_ms) AND (((payload #>> '{release,acceptance_window,end_ms}'::text[]))::bigint = window_end_ms) AND (((payload #>> '{serve_runtime,runtime_id}'::text[]))::uuid = serve_runtime_id) AND ((payload #>> '{serve_runtime,runtime_revision}'::text[]) = serve_runtime_revision) AND ((payload #>> '{serve_runtime,image_digest}'::text[]) = serve_image_digest) AND (((payload #>> '{serve_runtime,started_at_ms}'::text[]))::bigint = serve_started_at_ms) AND (((payload #>> '{serve_runtime,measured_at_ms}'::text[]))::bigint = serve_measured_at_ms))),
    CONSTRAINT trading_release_registration_release_sha_check CHECK ((release_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_release_registration_runtime_identity_check CHECK (((workers_runtime_revision = git_commit_sha) AND (workers_image_digest = oci_image_digest) AND (serve_runtime_revision = git_commit_sha) AND (serve_image_digest = oci_image_digest))),
    CONSTRAINT trading_release_registration_window_sha_check CHECK ((window_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_promotion_grant_revocations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_promotion_grant_revocations (
    revocation_sha256 text NOT NULL,
    grant_sha256 text NOT NULL,
    revoked_at_ms bigint NOT NULL,
    created_at_ms bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_grant_revocation_clock_check CHECK ((revoked_at_ms > 0)),
    CONSTRAINT trading_grant_revocation_payload_check CHECK ((((payload ->> 'revocation_version'::text) = 'production_promotion_grant_revocation_v1'::text) AND ((payload ->> 'grant_sha256'::text) = grant_sha256) AND (((payload ->> 'revoked_at_ms'::text))::bigint = revoked_at_ms))),
    CONSTRAINT trading_grant_revocation_sha_check CHECK ((revocation_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: trading_replay_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_replay_runs (
    run_id text NOT NULL,
    spec_sha256 text NOT NULL,
    created_at_ms bigint NOT NULL,
    terminal_status text NOT NULL,
    artifact_path text NOT NULL,
    artifact_sha256 text NOT NULL,
    source_count integer NOT NULL,
    directional_count integer NOT NULL,
    terminal_outcome_count integer NOT NULL,
    CONSTRAINT trading_replay_runs_counts_check CHECK (((source_count >= 0) AND (directional_count >= 0) AND (terminal_outcome_count >= 0))),
    CONSTRAINT trading_replay_runs_sha_check CHECK (((run_id ~ '^[0-9a-f]{64}$'::text) AND (spec_sha256 ~ '^[0-9a-f]{64}$'::text) AND (artifact_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT trading_replay_runs_spec_identity_check CHECK ((run_id = spec_sha256)),
    CONSTRAINT trading_replay_runs_status_check CHECK ((terminal_status = 'SUCCEEDED'::text))
);


--
-- Name: trading_runtime_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_runtime_state (
    id smallint NOT NULL,
    control text NOT NULL,
    orders_today integer DEFAULT 0 NOT NULL,
    updated_at_ms bigint NOT NULL,
    active_capability_snapshot_sha256 text,
    active_capability_included_count integer DEFAULT 0 NOT NULL,
    nautilus_bootstrap_account_zero_at_ms bigint,
    blacklist_revision bigint DEFAULT 0 NOT NULL,
    arm_epoch bigint DEFAULT 1 NOT NULL,
    CONSTRAINT trading_runtime_arm_epoch_check CHECK ((arm_epoch >= 1)),
    CONSTRAINT trading_runtime_bootstrap_zero_at_check CHECK (((nautilus_bootstrap_account_zero_at_ms IS NULL) OR (nautilus_bootstrap_account_zero_at_ms >= 0))),
    CONSTRAINT trading_runtime_capability_count_check CHECK ((active_capability_included_count >= 0)),
    CONSTRAINT trading_runtime_state_control_check CHECK ((control = ANY (ARRAY['RUNNING'::text, 'CLOSE_ONLY'::text, 'PAUSED'::text]))),
    CONSTRAINT trading_runtime_state_singleton CHECK ((id = 1))
);


--
-- Name: trading_symbol_blacklist; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_symbol_blacklist (
    base_symbol text NOT NULL,
    reason text NOT NULL,
    expires_at_ms bigint,
    created_at_ms bigint NOT NULL,
    updated_at_ms bigint NOT NULL
);


--
-- Name: trading_trade_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_trade_signals (
    seq bigint NOT NULL,
    signal_id text NOT NULL,
    case_id text NOT NULL,
    alpha_contract_sha256 text NOT NULL,
    market_key text NOT NULL,
    direction text NOT NULL,
    observed_at_ns bigint NOT NULL,
    expires_at_ns bigint NOT NULL,
    evidence_sha256 text NOT NULL,
    alpha_metadata jsonb NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trading_trade_signal_alpha_sha_check CHECK ((alpha_contract_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_trade_signal_case_check CHECK (((char_length(case_id) >= 1) AND (char_length(case_id) <= 128))),
    CONSTRAINT trading_trade_signal_clock_check CHECK (((observed_at_ns > 0) AND (expires_at_ns > observed_at_ns))),
    CONSTRAINT trading_trade_signal_direction_check CHECK ((direction = ANY (ARRAY['long'::text, 'short'::text]))),
    CONSTRAINT trading_trade_signal_evidence_sha_check CHECK ((evidence_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_trade_signal_id_check CHECK ((signal_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_trade_signal_market_check CHECK ((market_key ~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$'::text)),
    CONSTRAINT trading_trade_signal_metadata_check CHECK (public.trading_execution_metadata_valid(alpha_metadata)),
    CONSTRAINT trading_trade_signal_payload_check CHECK (COALESCE(((jsonb_typeof(payload) = 'object'::text) AND (public.trading_jsonb_object_size(payload) = 10) AND ((payload ->> 'signal_version'::text) = 'trade_signal_v1'::text) AND ((payload ->> 'signal_id'::text) = signal_id) AND ((payload ->> 'case_id'::text) = case_id) AND ((payload ->> 'alpha_contract_sha256'::text) = alpha_contract_sha256) AND ((payload ->> 'market_key'::text) = market_key) AND ((payload ->> 'direction'::text) = direction) AND (((payload ->> 'observed_at_ns'::text))::bigint = observed_at_ns) AND (((payload ->> 'expires_at_ns'::text))::bigint = expires_at_ns) AND ((payload ->> 'evidence_sha256'::text) = evidence_sha256) AND ((payload -> 'alpha_metadata'::text) = alpha_metadata)), false))
);


--
-- Name: trading_trade_signals_seq_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.trading_trade_signals ALTER COLUMN seq ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.trading_trade_signals_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: trading_venue_catalog_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trading_venue_catalog_snapshots (
    snapshot_sha256 text NOT NULL,
    binding text NOT NULL,
    captured_at_ms bigint NOT NULL,
    stale_after_ms bigint NOT NULL,
    provider_instrument_count integer CONSTRAINT trading_venue_catalog_snapsh_provider_instrument_count_not_null NOT NULL,
    payload jsonb NOT NULL,
    created_at_ms bigint NOT NULL,
    CONSTRAINT trading_venue_catalog_binding_check CHECK ((binding = ANY (ARRAY['BINANCE_USDM'::text, 'HYPERLIQUID_PERP'::text]))),
    CONSTRAINT trading_venue_catalog_count_check CHECK ((provider_instrument_count >= 0)),
    CONSTRAINT trading_venue_catalog_payload_check CHECK ((((payload ->> 'snapshot_version'::text) = 'venue_instrument_catalog_snapshot_v1'::text) AND ((payload ->> 'binding'::text) = binding) AND (((payload ->> 'captured_at_ms'::text))::bigint = captured_at_ms) AND (((payload ->> 'provider_instrument_count'::text))::integer = provider_instrument_count) AND (jsonb_array_length((payload -> 'instruments'::text)) = provider_instrument_count))),
    CONSTRAINT trading_venue_catalog_sha_check CHECK ((snapshot_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trading_venue_catalog_stale_check CHECK ((stale_after_ms > 0))
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
    runtime_revision text NOT NULL,
    image_digest text NOT NULL,
    CONSTRAINT workers_runtime_check CHECK ((heartbeat_at_ms >= started_at_ms)),
    CONSTRAINT workers_runtime_check1 CHECK ((((lifecycle_state = 'failed'::text) AND (fatal_code IS NOT NULL)) OR ((lifecycle_state <> 'failed'::text) AND (fatal_code IS NULL)))),
    CONSTRAINT workers_runtime_fatal_code_check CHECK (((fatal_code IS NULL) OR (fatal_code = ANY (ARRAY['startup_failed'::text, 'child_failed'::text, 'control_failed'::text, 'singleton_lost'::text, 'runtime_invariant_failed'::text, 'resource_operation_overrun'::text, 'graceful_deadline_exceeded'::text, 'cleanup_failed'::text])))),
    CONSTRAINT workers_runtime_lifecycle_state_check CHECK ((lifecycle_state = ANY (ARRAY['starting'::text, 'running'::text, 'stopping'::text, 'stopped'::text, 'failed'::text]))),
    CONSTRAINT workers_runtime_release_identity_nonempty CHECK (((runtime_revision <> ''::text) AND (image_digest <> ''::text))),
    CONSTRAINT workers_runtime_runtime_version_check CHECK ((btrim(runtime_version) <> ''::text)),
    CONSTRAINT workers_runtime_singleton_key_check CHECK (singleton_key),
    CONSTRAINT workers_runtime_started_at_ms_check CHECK ((started_at_ms >= 0))
);


--
-- Name: news_opennews_incidents incident_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_opennews_incidents ALTER COLUMN incident_id SET DEFAULT nextval('public.news_opennews_incidents_incident_id_seq'::regclass);


--
-- Name: news_agent_assignments news_agent_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_agent_assignments
    ADD CONSTRAINT news_agent_assignments_pkey PRIMARY KEY (event_id);


--
-- Name: news_agent_runtime_manifests news_agent_runtime_manifests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_agent_runtime_manifests
    ADD CONSTRAINT news_agent_runtime_manifests_pkey PRIMARY KEY (manifest_sha);


--
-- Name: news_canary_activations news_canary_activations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_canary_activations
    ADD CONSTRAINT news_canary_activations_pkey PRIMARY KEY (activation_id);


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
-- Name: news_event_evidence_snapshots news_event_evidence_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_evidence_snapshots
    ADD CONSTRAINT news_event_evidence_snapshots_pkey PRIMARY KEY (event_id, evidence_version);


--
-- Name: news_event_members news_event_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_members
    ADD CONSTRAINT news_event_members_pkey PRIMARY KEY (event_id, item_id, fact_id);


--
-- Name: news_event_reactions news_event_reactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_reactions
    ADD CONSTRAINT news_event_reactions_pkey PRIMARY KEY (event_id, symbol, metric_version);


--
-- Name: news_events news_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_events
    ADD CONSTRAINT news_events_pkey PRIMARY KEY (event_id);


--
-- Name: news_external_miss_snapshots news_external_miss_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_external_miss_snapshots
    ADD CONSTRAINT news_external_miss_snapshots_pkey PRIMARY KEY (snapshot_id);


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
-- Name: news_learning_artifacts news_learning_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_learning_artifacts
    ADD CONSTRAINT news_learning_artifacts_pkey PRIMARY KEY (artifact_sha);


--
-- Name: news_learning_cases news_learning_cases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_learning_cases
    ADD CONSTRAINT news_learning_cases_pkey PRIMARY KEY (run_sha, case_id);


--
-- Name: news_learning_epochs news_learning_epochs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_learning_epochs
    ADD CONSTRAINT news_learning_epochs_pkey PRIMARY KEY (epoch_id);


--
-- Name: news_learning_retention_state news_learning_retention_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_learning_retention_state
    ADD CONSTRAINT news_learning_retention_state_pkey PRIMARY KEY (singleton);


--
-- Name: news_market_instrument_listing_events news_market_instrument_listing_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_market_instrument_listing_events
    ADD CONSTRAINT news_market_instrument_listing_events_pkey PRIMARY KEY (venue, venue_symbol, observed_at_ms);


--
-- Name: news_market_instruments news_market_instruments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_market_instruments
    ADD CONSTRAINT news_market_instruments_pkey PRIMARY KEY (venue, venue_symbol);


--
-- Name: news_market_liquidations news_market_liquidations_fact_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_market_liquidations
    ADD CONSTRAINT news_market_liquidations_fact_unique UNIQUE (item_id, fact_id, parser_version);


--
-- Name: news_market_liquidations news_market_liquidations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_market_liquidations
    ADD CONSTRAINT news_market_liquidations_pkey PRIMARY KEY (source_key);


--
-- Name: news_model_recordings news_model_recordings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_model_recordings
    ADD CONSTRAINT news_model_recordings_pkey PRIMARY KEY (recording_sha);


--
-- Name: news_oi_signals news_oi_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_oi_signals
    ADD CONSTRAINT news_oi_signals_pkey PRIMARY KEY (event_id, metric_version);


--
-- Name: news_opennews_incidents news_opennews_incidents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_opennews_incidents
    ADD CONSTRAINT news_opennews_incidents_pkey PRIMARY KEY (incident_id);


--
-- Name: news_quote_snapshots news_quote_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_quote_snapshots
    ADD CONSTRAINT news_quote_snapshots_pkey PRIMARY KEY (source_key);


--
-- Name: news_reviews news_reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_reviews
    ADD CONSTRAINT news_reviews_pkey PRIMARY KEY (review_id);


--
-- Name: news_symbol_aliases news_symbol_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_symbol_aliases
    ADD CONSTRAINT news_symbol_aliases_pkey PRIMARY KEY (alias);


--
-- Name: news_verdicts news_verdicts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_verdicts
    ADD CONSTRAINT news_verdicts_pkey PRIMARY KEY (event_id, stage, policy_version);


--
-- Name: trading_binding_runtime trading_binding_runtime_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_binding_runtime
    ADD CONSTRAINT trading_binding_runtime_pkey PRIMARY KEY (binding);


--
-- Name: trading_candidate_gate_decisions trading_candidate_gate_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_candidate_gate_decisions
    ADD CONSTRAINT trading_candidate_gate_decisions_pkey PRIMARY KEY (source_key, gate_version, gate_config_digest);


--
-- Name: trading_capital_authorization_receipts trading_capital_authorization_receipts_case_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_authorization_receipts
    ADD CONSTRAINT trading_capital_authorization_receipts_case_id_key UNIQUE (case_id);


--
-- Name: trading_capital_authorization_receipts trading_capital_authorization_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_authorization_receipts
    ADD CONSTRAINT trading_capital_authorization_receipts_pkey PRIMARY KEY (authorization_receipt_sha256);


--
-- Name: trading_capital_authorization_receipts trading_capital_authorization_receipts_reservation_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_authorization_receipts
    ADD CONSTRAINT trading_capital_authorization_receipts_reservation_sha256_key UNIQUE (reservation_sha256);


--
-- Name: trading_capital_risk_events trading_capital_risk_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_events
    ADD CONSTRAINT trading_capital_risk_events_pkey PRIMARY KEY (event_sha256);


--
-- Name: trading_capital_risk_reservation_state trading_capital_risk_reservation_state_intent_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservation_state
    ADD CONSTRAINT trading_capital_risk_reservation_state_intent_id_key UNIQUE (intent_id);


--
-- Name: trading_capital_risk_reservation_state trading_capital_risk_reservation_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservation_state
    ADD CONSTRAINT trading_capital_risk_reservation_state_pkey PRIMARY KEY (reservation_sha256);


--
-- Name: trading_capital_risk_reservations trading_capital_risk_reservations_case_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservations
    ADD CONSTRAINT trading_capital_risk_reservations_case_id_key UNIQUE (case_id);


--
-- Name: trading_capital_risk_reservations trading_capital_risk_reservations_economic_lifecycle_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservations
    ADD CONSTRAINT trading_capital_risk_reservations_economic_lifecycle_id_key UNIQUE (economic_lifecycle_id);


--
-- Name: trading_capital_risk_reservations trading_capital_risk_reservations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservations
    ADD CONSTRAINT trading_capital_risk_reservations_pkey PRIMARY KEY (reservation_sha256);


--
-- Name: trading_cases trading_cases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_cases
    ADD CONSTRAINT trading_cases_pkey PRIMARY KEY (case_id);


--
-- Name: trading_cases trading_cases_primary_source_key_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_cases
    ADD CONSTRAINT trading_cases_primary_source_key_unique UNIQUE (primary_source_key);


--
-- Name: trading_daily_risk_policies trading_daily_risk_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_daily_risk_policies
    ADD CONSTRAINT trading_daily_risk_policies_pkey PRIMARY KEY (risk_policy_sha256);


--
-- Name: trading_decision_runtime trading_decision_runtime_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_decision_runtime
    ADD CONSTRAINT trading_decision_runtime_pkey PRIMARY KEY (id);


--
-- Name: trading_evidence_clock_receipts trading_evidence_clock_receipts_artifact_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_evidence_clock_receipts
    ADD CONSTRAINT trading_evidence_clock_receipts_artifact_sha256_key UNIQUE (artifact_sha256);


--
-- Name: trading_evidence_clock_receipts trading_evidence_clock_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_evidence_clock_receipts
    ADD CONSTRAINT trading_evidence_clock_receipts_pkey PRIMARY KEY (receipt_sha256);


--
-- Name: trading_evidence_future_capture_batches trading_evidence_future_capture_batches_batch_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_evidence_future_capture_batches
    ADD CONSTRAINT trading_evidence_future_capture_batches_batch_sha256_key UNIQUE (batch_sha256);


--
-- Name: trading_evidence_future_capture_batches trading_evidence_future_capture_batches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_evidence_future_capture_batches
    ADD CONSTRAINT trading_evidence_future_capture_batches_pkey PRIMARY KEY (protocol_sha256, batch_start_ms);


--
-- Name: trading_execution_bindings trading_execution_bindings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_execution_bindings
    ADD CONSTRAINT trading_execution_bindings_pkey PRIMARY KEY (binding_sha256);


--
-- Name: trading_execution_capability_snapshots trading_execution_capability_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_execution_capability_snapshots
    ADD CONSTRAINT trading_execution_capability_snapshots_pkey PRIMARY KEY (snapshot_sha256);


--
-- Name: trading_execution_observations trading_execution_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_execution_observations
    ADD CONSTRAINT trading_execution_observations_pkey PRIMARY KEY (event_id);


--
-- Name: trading_execution_observations trading_execution_observations_seq_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_execution_observations
    ADD CONSTRAINT trading_execution_observations_seq_key UNIQUE (seq);


--
-- Name: trading_execution_profile_activations trading_execution_profile_activations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_execution_profile_activations
    ADD CONSTRAINT trading_execution_profile_activations_pkey PRIMARY KEY (runtime_profile_id);


--
-- Name: trading_intents trading_intents_case_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_intents
    ADD CONSTRAINT trading_intents_case_id_key UNIQUE (case_id);


--
-- Name: trading_intents trading_intents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_intents
    ADD CONSTRAINT trading_intents_pkey PRIMARY KEY (intent_id);


--
-- Name: trading_nautilus_runtime_starts trading_nautilus_runtime_starts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_nautilus_runtime_starts
    ADD CONSTRAINT trading_nautilus_runtime_starts_pkey PRIMARY KEY (start_sha256);


--
-- Name: trading_nautilus_runtime_starts trading_nautilus_runtime_starts_runtime_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_nautilus_runtime_starts
    ADD CONSTRAINT trading_nautilus_runtime_starts_runtime_id_key UNIQUE (runtime_id);


--
-- Name: trading_operator_arm_receipts trading_operator_arm_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_operator_arm_receipts
    ADD CONSTRAINT trading_operator_arm_receipts_pkey PRIMARY KEY (arm_receipt_sha256);


--
-- Name: trading_operator_intents trading_operator_intent_profile_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_operator_intents
    ADD CONSTRAINT trading_operator_intent_profile_unique UNIQUE (command_id, target_profile_id);


--
-- Name: trading_operator_intents trading_operator_intents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_operator_intents
    ADD CONSTRAINT trading_operator_intents_pkey PRIMARY KEY (command_id);


--
-- Name: trading_order_observations trading_order_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_order_observations
    ADD CONSTRAINT trading_order_observations_pkey PRIMARY KEY (order_id, observation_kind, content_sha256);


--
-- Name: trading_orders trading_orders_case_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_orders
    ADD CONSTRAINT trading_orders_case_unique UNIQUE (case_id);


--
-- Name: trading_orders trading_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_orders
    ADD CONSTRAINT trading_orders_pkey PRIMARY KEY (order_id);


--
-- Name: trading_production_promotion_grants trading_production_promotion_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_production_promotion_grants
    ADD CONSTRAINT trading_production_promotion_grants_pkey PRIMARY KEY (grant_sha256);


--
-- Name: trading_production_release_registrations trading_production_release_registrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_production_release_registrations
    ADD CONSTRAINT trading_production_release_registrations_pkey PRIMARY KEY (release_sha256);


--
-- Name: trading_production_release_registrations trading_production_release_registrations_window_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_production_release_registrations
    ADD CONSTRAINT trading_production_release_registrations_window_sha256_key UNIQUE (window_sha256);


--
-- Name: trading_promotion_grant_revocations trading_promotion_grant_revocations_grant_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_promotion_grant_revocations
    ADD CONSTRAINT trading_promotion_grant_revocations_grant_sha256_key UNIQUE (grant_sha256);


--
-- Name: trading_promotion_grant_revocations trading_promotion_grant_revocations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_promotion_grant_revocations
    ADD CONSTRAINT trading_promotion_grant_revocations_pkey PRIMARY KEY (revocation_sha256);


--
-- Name: trading_replay_runs trading_replay_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_replay_runs
    ADD CONSTRAINT trading_replay_runs_pkey PRIMARY KEY (run_id);


--
-- Name: trading_runtime_state trading_runtime_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_runtime_state
    ADD CONSTRAINT trading_runtime_state_pkey PRIMARY KEY (id);


--
-- Name: trading_symbol_blacklist trading_symbol_blacklist_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_symbol_blacklist
    ADD CONSTRAINT trading_symbol_blacklist_pkey PRIMARY KEY (base_symbol);


--
-- Name: trading_trade_signals trading_trade_signals_case_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_trade_signals
    ADD CONSTRAINT trading_trade_signals_case_id_key UNIQUE (case_id);


--
-- Name: trading_trade_signals trading_trade_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_trade_signals
    ADD CONSTRAINT trading_trade_signals_pkey PRIMARY KEY (signal_id);


--
-- Name: trading_venue_catalog_snapshots trading_venue_catalog_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_venue_catalog_snapshots
    ADD CONSTRAINT trading_venue_catalog_snapshots_pkey PRIMARY KEY (snapshot_sha256);


--
-- Name: trading_cases uq_trading_cases_manifest_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_cases
    ADD CONSTRAINT uq_trading_cases_manifest_identity UNIQUE (case_id, manifest_sha256);


--
-- Name: workers_runtime workers_runtime_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workers_runtime
    ADD CONSTRAINT workers_runtime_pkey PRIMARY KEY (singleton_key);


--
-- Name: ix_news_agent_assignments_activation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_agent_assignments_activation ON public.news_agent_assignments USING btree (activation_id, arm, assigned_at_ms DESC);


--
-- Name: ix_news_aliases_base; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_aliases_base ON public.news_symbol_aliases USING btree (base_symbol);


--
-- Name: ix_news_canary_activations_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_canary_activations_created ON public.news_canary_activations USING btree (created_at_ms DESC);


--
-- Name: ix_news_deliveries_deleting; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_deliveries_deleting ON public.news_deliveries USING btree (delete_attempted_at_ms, event_id) WHERE (delete_state = 'deleting'::text);


--
-- Name: ix_news_deliveries_editing; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_deliveries_editing ON public.news_deliveries USING btree (edit_attempted_at_ms, event_id) WHERE (edit_state = 'editing'::text);


--
-- Name: ix_news_deliveries_sent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_deliveries_sent ON public.news_deliveries USING btree (settled_at_ms DESC) WHERE (state = 'sent'::text);


--
-- Name: ix_news_deliveries_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_deliveries_state ON public.news_deliveries USING btree (state, attempted_at_ms DESC);


--
-- Name: ix_news_event_assets_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_event_assets_event ON public.news_event_assets USING btree (event_id, symbol);


--
-- Name: ix_news_event_assets_opened; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_event_assets_opened ON public.news_event_assets USING btree (opened_at_ms);


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

CREATE INDEX ix_news_event_bands_lookup ON public.news_event_bands USING btree (band_index, band_key, dedupe_family, expires_at_ms);


--
-- Name: ix_news_event_evidence_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_event_evidence_created ON public.news_event_evidence_snapshots USING btree (created_at_ms DESC, event_id);


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

CREATE INDEX ix_news_events_fingerprint ON public.news_events USING btree (dedupe_family, comparison_fingerprint, opened_at_ms DESC);


--
-- Name: ix_news_events_kind_opened; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_events_kind_opened ON public.news_events USING btree (event_kind, opened_at_ms DESC, event_id DESC);


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

CREATE INDEX ix_news_events_unpublished ON public.news_events USING btree (opened_at_ms) WHERE ((published_at_ms IS NULL) AND (admission = ANY (ARRAY['candidate'::text, 'listing_deterministic'::text, 'telemetry_deterministic'::text, 'liquidation_deterministic'::text])));


--
-- Name: ix_news_external_miss_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_external_miss_created ON public.news_external_miss_snapshots USING btree (created_at_ms DESC);


--
-- Name: ix_news_incidents_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_incidents_open ON public.news_opennews_incidents USING btree (closed_at_ms) WHERE (closed_at_ms IS NULL);


--
-- Name: ix_news_incidents_recovery; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_incidents_recovery ON public.news_opennews_incidents USING btree (recovery_status, incident_id) WHERE (recovery_status = 'pending'::text);


--
-- Name: ix_news_instrument_listing_events_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_instrument_listing_events_lookup ON public.news_market_instrument_listing_events USING btree (venue, base_symbol, observed_at_ms DESC, venue_symbol);


--
-- Name: ix_news_instruments_base; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_instruments_base ON public.news_market_instruments USING btree (base_symbol, status);


--
-- Name: ix_news_items_observed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_items_observed ON public.news_items USING btree (observed_at_ms);


--
-- Name: ix_news_items_published; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_items_published ON public.news_items USING btree (published_at_ms DESC);


--
-- Name: ix_news_items_source_artifact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_items_source_artifact ON public.news_items USING btree (source_artifact_id) WHERE (source_artifact_id <> ''::text);


--
-- Name: ix_news_learning_artifacts_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_learning_artifacts_created ON public.news_learning_artifacts USING btree (created_at_ms, artifact_sha);


--
-- Name: ix_news_learning_artifacts_kind_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_learning_artifacts_kind_created ON public.news_learning_artifacts USING btree (kind, created_at_ms DESC);


--
-- Name: ix_news_learning_cases_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_learning_cases_created ON public.news_learning_cases USING btree (created_at_ms, run_sha, case_id);


--
-- Name: ix_news_learning_cases_dataset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_learning_cases_dataset ON public.news_learning_cases USING btree (dataset_sha, cluster_id);


--
-- Name: ix_news_market_liquidations_symbol_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_market_liquidations_symbol_event ON public.news_market_liquidations USING btree (symbol, venue, event_at_ms DESC);


--
-- Name: ix_news_model_recording_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_model_recording_created ON public.news_model_recordings USING btree (created_at_ms DESC);


--
-- Name: ix_news_oi_signals_symbol_observed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_oi_signals_symbol_observed ON public.news_oi_signals USING btree (metric_version, symbol, observed_at_ms DESC);


--
-- Name: ix_news_reactions_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_reactions_due ON public.news_event_reactions USING btree (anchor_at_ms) WHERE (state = ANY (ARRAY['pending'::text, 'partial'::text]));


--
-- Name: ix_news_reactions_review; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_reactions_review ON public.news_event_reactions USING btree (metric_version, anchor_at_ms DESC) WHERE is_primary;


--
-- Name: ix_news_reactions_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_reactions_state ON public.news_event_reactions USING btree (metric_version, anchor_at_ms DESC, state);


--
-- Name: ix_news_reviews_accepted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_reviews_accepted ON public.news_reviews USING btree (accepts_review_id, created_at_ms DESC) WHERE (review_kind = 'acceptance'::text);


--
-- Name: ix_news_reviews_event_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_reviews_event_created ON public.news_reviews USING btree (event_id, created_at_ms DESC);


--
-- Name: ix_news_reviews_external_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_reviews_external_created ON public.news_reviews USING btree (external_snapshot_id, created_at_ms DESC);


--
-- Name: ix_news_reviews_task_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_reviews_task_created ON public.news_reviews USING btree (task_id, created_at_ms DESC);


--
-- Name: ix_news_verdicts_final; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_verdicts_final ON public.news_verdicts USING btree (final_decision, created_at_ms DESC);


--
-- Name: ix_news_verdicts_stage_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_verdicts_stage_created ON public.news_verdicts USING btree (stage, created_at_ms DESC);


--
-- Name: ix_news_verdicts_unpublished_delivery; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_news_verdicts_unpublished_delivery ON public.news_verdicts USING btree (created_at_ms, event_id, policy_version) WHERE ((stage = 'triage'::text) AND (published_at_ms IS NULL) AND (final_decision = ANY (ARRAY['push'::text, 'escalate'::text])));


--
-- Name: ix_trading_candidate_gate_observed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trading_candidate_gate_observed ON public.trading_candidate_gate_decisions USING btree (source_observed_at_ms DESC);


--
-- Name: ix_trading_candidate_gate_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trading_candidate_gate_open ON public.trading_candidate_gate_decisions USING btree (source_observed_at_ms) WHERE (status = 'DEFERRED'::text);


--
-- Name: ix_trading_cases_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trading_cases_created ON public.trading_cases USING btree (created_at_ms DESC);


--
-- Name: ix_trading_cases_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trading_cases_state ON public.trading_cases USING btree (state, created_at_ms);


--
-- Name: ix_trading_cases_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trading_cases_strategy ON public.trading_cases USING btree (strategy_id, created_at_ms DESC);


--
-- Name: ix_trading_cases_underlying; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trading_cases_underlying ON public.trading_cases USING btree (underlying_key, created_at_ms DESC);


--
-- Name: ix_trading_execution_observations_runtime; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trading_execution_observations_runtime ON public.trading_execution_observations USING btree (runtime_profile_id, seq);


--
-- Name: ix_trading_operator_intents_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_trading_operator_intents_unresolved ON public.trading_operator_intents USING btree (target_profile_id, seq) INCLUDE (command_id, expires_at_ns);


--
-- Name: ix_trading_orders_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trading_orders_due ON public.trading_orders USING btree (state, next_reconcile_at_ms);


--
-- Name: ix_trading_trade_signals_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_trading_trade_signals_unresolved ON public.trading_trade_signals USING btree (seq) INCLUDE (signal_id, alpha_contract_sha256, expires_at_ns, payload);


--
-- Name: ix_trading_venue_catalog_binding_captured; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trading_venue_catalog_binding_captured ON public.trading_venue_catalog_snapshots USING btree (binding, captured_at_ms DESC, snapshot_sha256);


--
-- Name: news_learning_epochs_bundle_sha_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX news_learning_epochs_bundle_sha_key ON public.news_learning_epochs USING btree (bundle_sha) WHERE (bundle_sha IS NOT NULL);


--
-- Name: uq_trading_evidence_candidate_per_corpus_binding; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_trading_evidence_candidate_per_corpus_binding ON public.trading_evidence_clock_receipts USING btree (corpus_sha256, binding) WHERE (receipt_kind = 'CANDIDATE_DECISION'::text);


--
-- Name: uq_trading_evidence_future_capture_protocol_once; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_trading_evidence_future_capture_protocol_once ON public.trading_evidence_clock_receipts USING btree (protocol_sha256) WHERE (receipt_kind = 'FUTURE_CAPTURE'::text);


--
-- Name: uq_trading_evidence_future_drain_protocol_once; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_trading_evidence_future_drain_protocol_once ON public.trading_evidence_clock_receipts USING btree (protocol_sha256) WHERE (receipt_kind = 'FUTURE_DRAIN'::text);


--
-- Name: uq_trading_evidence_future_protocol_once; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_trading_evidence_future_protocol_once ON public.trading_evidence_clock_receipts USING btree (protocol_sha256) WHERE (receipt_kind = 'FUTURE_RESULT'::text);


--
-- Name: ux_news_canary_one_open; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_news_canary_one_open ON public.news_canary_activations USING btree ((1)) WHERE (state = ANY (ARRAY['armed'::text, 'active'::text]));


--
-- Name: ux_news_event_evidence_content; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_news_event_evidence_content ON public.news_event_evidence_snapshots USING btree (event_id, evidence_sha256);


--
-- Name: ux_news_event_evidence_current_identity; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_news_event_evidence_current_identity ON public.news_event_evidence_snapshots USING btree (event_id, evidence_version, evidence_sha256, focus_fact_id);


--
-- Name: ux_news_model_recording_call; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_news_model_recording_call ON public.news_model_recordings USING btree (run_sha, case_id, arm, trial, predictor_name, call_index, attempt);


--
-- Name: ux_news_opennews_incidents_open_cause; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_news_opennews_incidents_open_cause ON public.news_opennews_incidents USING btree (cause_class) WHERE (closed_at_ms IS NULL);


--
-- Name: ux_news_reviews_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_news_reviews_idempotency ON public.news_reviews USING btree (reviewer, idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: ux_trading_active_underlying; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_trading_active_underlying ON public.trading_orders USING btree (underlying_key) WHERE (state = ANY (ARRAY['PREPARED'::text, 'AWAITING_APPROVAL'::text, 'APPROVED'::text, 'SUBMITTING'::text, 'AMBIGUOUS'::text, 'RECONCILING'::text, 'MANUAL_REVIEW_REQUIRED'::text, 'ACKNOWLEDGED'::text, 'PARTIAL'::text, 'OPEN'::text, 'UNPROTECTED'::text, 'SAFETY_CLOSING'::text]));


--
-- Name: ux_trading_case_in_flight_underlying; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_trading_case_in_flight_underlying ON public.trading_cases USING btree (underlying_key) WHERE (state = ANY (ARRAY['PENDING'::text, 'RUNNING'::text]));


--
-- Name: ux_trading_execution_control_disposition; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_trading_execution_control_disposition ON public.trading_execution_observations USING btree (runtime_profile_id, execution_strategy, command_id) WHERE (normalized_kind = 'control_disposition'::text);


--
-- Name: ux_trading_execution_signal_disposition; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_trading_execution_signal_disposition ON public.trading_execution_observations USING btree (runtime_profile_id, execution_strategy, signal_id) WHERE (normalized_kind = 'signal_disposition'::text);


--
-- Name: ux_trading_intents_one_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_trading_intents_one_active ON public.trading_intents USING btree ((true)) WHERE (execution_state = ANY (ARRAY['PENDING'::text, 'IN_FLIGHT'::text, 'OPEN_PROTECTED'::text, 'MANUAL_REVIEW'::text]));


--
-- Name: news_reviews news_reviews_current_acceptance_target_check; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER news_reviews_current_acceptance_target_check BEFORE INSERT OR UPDATE ON public.news_reviews FOR EACH ROW EXECUTE FUNCTION public.news_current_review_acceptance_target_guard();


--
-- Name: news_reviews news_reviews_current_task_source_check; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER news_reviews_current_task_source_check BEFORE INSERT OR UPDATE ON public.news_reviews FOR EACH ROW EXECUTE FUNCTION public.news_current_review_source_guard();


--
-- Name: news_verdicts news_verdicts_current_evidence_check; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER news_verdicts_current_evidence_check BEFORE INSERT OR UPDATE ON public.news_verdicts FOR EACH ROW EXECUTE FUNCTION public.news_current_verdict_evidence_guard();


--
-- Name: news_agent_assignments trg_news_agent_assignments_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_news_agent_assignments_append_only BEFORE DELETE OR UPDATE ON public.news_agent_assignments FOR EACH ROW EXECUTE FUNCTION public.reject_news_canary_append_only_mutation();


--
-- Name: news_agent_runtime_manifests trg_news_agent_runtime_manifests_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_news_agent_runtime_manifests_append_only BEFORE DELETE OR UPDATE ON public.news_agent_runtime_manifests FOR EACH ROW EXECUTE FUNCTION public.reject_news_canary_append_only_mutation();


--
-- Name: news_event_evidence_snapshots trg_news_event_evidence_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_news_event_evidence_append_only BEFORE DELETE OR UPDATE ON public.news_event_evidence_snapshots FOR EACH ROW EXECUTE FUNCTION public.reject_news_event_evidence_mutation();


--
-- Name: news_external_miss_snapshots trg_news_external_miss_snapshots_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_news_external_miss_snapshots_append_only BEFORE DELETE OR UPDATE ON public.news_external_miss_snapshots FOR EACH ROW EXECUTE FUNCTION public.reject_news_review_mutation();


--
-- Name: news_learning_artifacts trg_news_learning_artifacts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_news_learning_artifacts_append_only BEFORE DELETE OR UPDATE ON public.news_learning_artifacts FOR EACH ROW EXECUTE FUNCTION public.reject_news_learning_mutation();


--
-- Name: news_learning_cases trg_news_learning_cases_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_news_learning_cases_append_only BEFORE DELETE OR UPDATE ON public.news_learning_cases FOR EACH ROW EXECUTE FUNCTION public.reject_news_learning_mutation();


--
-- Name: news_learning_epochs trg_news_learning_epochs_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_news_learning_epochs_append_only BEFORE DELETE OR UPDATE ON public.news_learning_epochs FOR EACH ROW EXECUTE FUNCTION public.reject_news_learning_mutation();


--
-- Name: news_model_recordings trg_news_model_recordings_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_news_model_recordings_append_only BEFORE DELETE OR UPDATE ON public.news_model_recordings FOR EACH ROW EXECUTE FUNCTION public.reject_news_learning_mutation();


--
-- Name: news_reviews trg_news_reviews_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_news_reviews_append_only BEFORE DELETE OR UPDATE ON public.news_reviews FOR EACH ROW EXECUTE FUNCTION public.reject_news_review_mutation();


--
-- Name: trading_capital_authorization_receipts trg_trading_authorization_receipts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_authorization_receipts_append_only BEFORE DELETE OR UPDATE ON public.trading_capital_authorization_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_candidate_gate_decisions trg_trading_candidate_gate_stage_hard_cut; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_candidate_gate_stage_hard_cut BEFORE INSERT OR UPDATE ON public.trading_candidate_gate_decisions FOR EACH ROW EXECUTE FUNCTION public.reject_retired_candidate_gate_stage();


--
-- Name: trading_execution_capability_snapshots trg_trading_capability_v2_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_capability_v2_only BEFORE INSERT ON public.trading_execution_capability_snapshots FOR EACH ROW EXECUTE FUNCTION public.reject_new_execution_capability_v1();


--
-- Name: trading_capital_risk_events trg_trading_capital_risk_events_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_capital_risk_events_append_only BEFORE DELETE OR UPDATE ON public.trading_capital_risk_events FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_daily_risk_policies trg_trading_daily_risk_policies_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_daily_risk_policies_append_only BEFORE DELETE OR UPDATE ON public.trading_daily_risk_policies FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_evidence_clock_receipts trg_trading_evidence_clock_receipts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_evidence_clock_receipts_append_only BEFORE DELETE OR UPDATE ON public.trading_evidence_clock_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_evidence_clock_receipts trg_trading_evidence_parent; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_evidence_parent BEFORE INSERT ON public.trading_evidence_clock_receipts FOR EACH ROW EXECUTE FUNCTION public.validate_trading_evidence_parent();


--
-- Name: trading_execution_bindings trg_trading_execution_bindings_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_execution_bindings_append_only BEFORE DELETE OR UPDATE ON public.trading_execution_bindings FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_execution_capability_snapshots trg_trading_execution_capability_snapshots_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_execution_capability_snapshots_append_only BEFORE DELETE OR UPDATE ON public.trading_execution_capability_snapshots FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_execution_observations trg_trading_execution_observations_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_execution_observations_append_only BEFORE DELETE OR UPDATE ON public.trading_execution_observations FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation();


--
-- Name: trading_execution_profile_activations trg_trading_execution_profile_activations_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_execution_profile_activations_append_only BEFORE DELETE OR UPDATE ON public.trading_execution_profile_activations FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation();


--
-- Name: trading_evidence_future_capture_batches trg_trading_future_capture_batch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_future_capture_batch BEFORE INSERT ON public.trading_evidence_future_capture_batches FOR EACH ROW EXECUTE FUNCTION public.validate_trading_future_capture_batch();


--
-- Name: trading_evidence_future_capture_batches trg_trading_future_capture_batches_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_future_capture_batches_append_only BEFORE DELETE OR UPDATE ON public.trading_evidence_future_capture_batches FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_promotion_grant_revocations trg_trading_grant_revocations_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_grant_revocations_append_only BEFORE DELETE OR UPDATE ON public.trading_promotion_grant_revocations FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_intents trg_trading_intents_v3_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_intents_v3_only BEFORE INSERT ON public.trading_intents FOR EACH ROW EXECUTE FUNCTION public.reject_new_legacy_trade_intent();


--
-- Name: trading_nautilus_runtime_starts trg_trading_nautilus_runtime_starts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_nautilus_runtime_starts_append_only BEFORE DELETE OR UPDATE ON public.trading_nautilus_runtime_starts FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_operator_arm_receipts trg_trading_operator_arm_receipts_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_operator_arm_receipts_append_only BEFORE DELETE OR UPDATE ON public.trading_operator_arm_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_operator_intents trg_trading_operator_intents_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_operator_intents_append_only BEFORE DELETE OR UPDATE ON public.trading_operator_intents FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation();


--
-- Name: trading_production_release_registrations trg_trading_production_release_registrations_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_production_release_registrations_append_only BEFORE DELETE OR UPDATE ON public.trading_production_release_registrations FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_production_promotion_grants trg_trading_promotion_future_evidence; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_promotion_future_evidence BEFORE INSERT ON public.trading_production_promotion_grants FOR EACH ROW EXECUTE FUNCTION public.validate_trading_promotion_future_evidence();


--
-- Name: trading_production_promotion_grants trg_trading_promotion_grants_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_promotion_grants_append_only BEFORE DELETE OR UPDATE ON public.trading_production_promotion_grants FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_production_release_registrations trg_trading_release_registration_clock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_release_registration_clock BEFORE INSERT ON public.trading_production_release_registrations FOR EACH ROW EXECUTE FUNCTION public.stamp_trading_release_registration();


--
-- Name: trading_replay_runs trg_trading_replay_runs_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_replay_runs_append_only BEFORE DELETE OR UPDATE ON public.trading_replay_runs FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_capital_risk_reservations trg_trading_risk_reservations_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_risk_reservations_append_only BEFORE DELETE OR UPDATE ON public.trading_capital_risk_reservations FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: trading_intents trg_trading_terminal_intent_revival; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_terminal_intent_revival BEFORE UPDATE ON public.trading_intents FOR EACH ROW EXECUTE FUNCTION public.reject_trading_terminal_intent_revival();


--
-- Name: trading_trade_signals trg_trading_trade_signals_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_trade_signals_append_only BEFORE DELETE OR UPDATE ON public.trading_trade_signals FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation();


--
-- Name: trading_venue_catalog_snapshots trg_trading_venue_catalog_snapshots_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trading_venue_catalog_snapshots_append_only BEFORE DELETE OR UPDATE ON public.trading_venue_catalog_snapshots FOR EACH ROW EXECUTE FUNCTION public.reject_trading_append_only_mutation();


--
-- Name: news_agent_assignments news_agent_assignments_activation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_agent_assignments
    ADD CONSTRAINT news_agent_assignments_activation_id_fkey FOREIGN KEY (activation_id) REFERENCES public.news_canary_activations(activation_id);


--
-- Name: news_agent_assignments news_agent_assignments_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_agent_assignments
    ADD CONSTRAINT news_agent_assignments_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;


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
-- Name: news_event_reactions news_event_reactions_event_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_event_reactions
    ADD CONSTRAINT news_event_reactions_event_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;


--
-- Name: news_events news_events_leader_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_events
    ADD CONSTRAINT news_events_leader_item_id_fkey FOREIGN KEY (leader_item_id) REFERENCES public.news_items(item_id) ON DELETE CASCADE;


--
-- Name: news_oi_signals news_oi_signals_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_oi_signals
    ADD CONSTRAINT news_oi_signals_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;


--
-- Name: news_oi_signals news_oi_signals_source_item_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_oi_signals
    ADD CONSTRAINT news_oi_signals_source_item_fk FOREIGN KEY (source_item_id) REFERENCES public.news_items(item_id) ON DELETE CASCADE;


--
-- Name: news_verdicts news_verdicts_current_evidence_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_verdicts
    ADD CONSTRAINT news_verdicts_current_evidence_fk FOREIGN KEY (event_id, evidence_version, evidence_sha256, focus_fact_id) REFERENCES public.news_event_evidence_snapshots(event_id, evidence_version, evidence_sha256, focus_fact_id);


--
-- Name: news_verdicts news_verdicts_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_verdicts
    ADD CONSTRAINT news_verdicts_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE;


--
-- Name: trading_binding_runtime trading_binding_active_arm_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_binding_runtime
    ADD CONSTRAINT trading_binding_active_arm_fk FOREIGN KEY (active_arm_receipt_sha256) REFERENCES public.trading_operator_arm_receipts(arm_receipt_sha256) ON DELETE RESTRICT;


--
-- Name: trading_binding_runtime trading_binding_capability_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_binding_runtime
    ADD CONSTRAINT trading_binding_capability_fk FOREIGN KEY (capability_snapshot_sha256) REFERENCES public.trading_execution_capability_snapshots(snapshot_sha256) ON DELETE RESTRICT;


--
-- Name: trading_binding_runtime trading_binding_catalog_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_binding_runtime
    ADD CONSTRAINT trading_binding_catalog_fk FOREIGN KEY (catalog_snapshot_sha256) REFERENCES public.trading_venue_catalog_snapshots(snapshot_sha256) ON DELETE RESTRICT;


--
-- Name: trading_binding_runtime trading_binding_execution_binding_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_binding_runtime
    ADD CONSTRAINT trading_binding_execution_binding_fk FOREIGN KEY (execution_binding_sha256) REFERENCES public.trading_execution_bindings(binding_sha256) ON DELETE RESTRICT;


--
-- Name: trading_candidate_gate_decisions trading_candidate_gate_decisions_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_candidate_gate_decisions
    ADD CONSTRAINT trading_candidate_gate_decisions_case_id_fkey FOREIGN KEY (case_id) REFERENCES public.trading_cases(case_id);


--
-- Name: trading_execution_capability_snapshots trading_capability_catalog_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_execution_capability_snapshots
    ADD CONSTRAINT trading_capability_catalog_fk FOREIGN KEY (catalog_snapshot_sha256) REFERENCES public.trading_venue_catalog_snapshots(snapshot_sha256) ON DELETE RESTRICT;


--
-- Name: trading_capital_authorization_receipts trading_capital_authorization_receipts_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_authorization_receipts
    ADD CONSTRAINT trading_capital_authorization_receipts_case_id_fkey FOREIGN KEY (case_id) REFERENCES public.trading_cases(case_id) ON DELETE RESTRICT;


--
-- Name: trading_capital_authorization_receipts trading_capital_authorization_receipts_reservation_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_authorization_receipts
    ADD CONSTRAINT trading_capital_authorization_receipts_reservation_sha256_fkey FOREIGN KEY (reservation_sha256) REFERENCES public.trading_capital_risk_reservations(reservation_sha256) ON DELETE RESTRICT;


--
-- Name: trading_capital_risk_events trading_capital_risk_events_intent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_events
    ADD CONSTRAINT trading_capital_risk_events_intent_id_fkey FOREIGN KEY (intent_id) REFERENCES public.trading_intents(intent_id) ON DELETE RESTRICT;


--
-- Name: trading_capital_risk_events trading_capital_risk_events_reservation_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_events
    ADD CONSTRAINT trading_capital_risk_events_reservation_sha256_fkey FOREIGN KEY (reservation_sha256) REFERENCES public.trading_capital_risk_reservations(reservation_sha256) ON DELETE RESTRICT;


--
-- Name: trading_capital_risk_reservation_state trading_capital_risk_reservation_state_intent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservation_state
    ADD CONSTRAINT trading_capital_risk_reservation_state_intent_id_fkey FOREIGN KEY (intent_id) REFERENCES public.trading_intents(intent_id) ON DELETE RESTRICT;


--
-- Name: trading_capital_risk_reservation_state trading_capital_risk_reservation_state_reservation_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservation_state
    ADD CONSTRAINT trading_capital_risk_reservation_state_reservation_sha256_fkey FOREIGN KEY (reservation_sha256) REFERENCES public.trading_capital_risk_reservations(reservation_sha256) ON DELETE RESTRICT;


--
-- Name: trading_capital_risk_reservations trading_capital_risk_reservations_arm_receipt_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservations
    ADD CONSTRAINT trading_capital_risk_reservations_arm_receipt_sha256_fkey FOREIGN KEY (arm_receipt_sha256) REFERENCES public.trading_operator_arm_receipts(arm_receipt_sha256) ON DELETE RESTRICT;


--
-- Name: trading_capital_risk_reservations trading_capital_risk_reservations_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservations
    ADD CONSTRAINT trading_capital_risk_reservations_case_id_fkey FOREIGN KEY (case_id) REFERENCES public.trading_cases(case_id) ON DELETE RESTRICT;


--
-- Name: trading_capital_risk_reservations trading_capital_risk_reservations_grant_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservations
    ADD CONSTRAINT trading_capital_risk_reservations_grant_sha256_fkey FOREIGN KEY (grant_sha256) REFERENCES public.trading_production_promotion_grants(grant_sha256) ON DELETE RESTRICT;


--
-- Name: trading_capital_risk_reservations trading_capital_risk_reservations_risk_policy_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_capital_risk_reservations
    ADD CONSTRAINT trading_capital_risk_reservations_risk_policy_sha256_fkey FOREIGN KEY (risk_policy_sha256) REFERENCES public.trading_daily_risk_policies(risk_policy_sha256) ON DELETE RESTRICT;


--
-- Name: trading_evidence_clock_receipts trading_evidence_clock_receipts_parent_receipt_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_evidence_clock_receipts
    ADD CONSTRAINT trading_evidence_clock_receipts_parent_receipt_sha256_fkey FOREIGN KEY (parent_receipt_sha256) REFERENCES public.trading_evidence_clock_receipts(receipt_sha256) ON DELETE RESTRICT;


--
-- Name: trading_evidence_future_capture_batches trading_evidence_future_capture_b_candidate_receipt_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_evidence_future_capture_batches
    ADD CONSTRAINT trading_evidence_future_capture_b_candidate_receipt_sha256_fkey FOREIGN KEY (candidate_receipt_sha256) REFERENCES public.trading_evidence_clock_receipts(receipt_sha256) ON DELETE RESTRICT;


--
-- Name: trading_execution_observations trading_execution_observation_command_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_execution_observations
    ADD CONSTRAINT trading_execution_observation_command_fk FOREIGN KEY (command_id, runtime_profile_id) REFERENCES public.trading_operator_intents(command_id, target_profile_id) ON DELETE RESTRICT;


--
-- Name: trading_execution_observations trading_execution_observations_signal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_execution_observations
    ADD CONSTRAINT trading_execution_observations_signal_id_fkey FOREIGN KEY (signal_id) REFERENCES public.trading_trade_signals(signal_id) ON DELETE RESTRICT;


--
-- Name: trading_intents trading_intents_capability_snapshot_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_intents
    ADD CONSTRAINT trading_intents_capability_snapshot_fk FOREIGN KEY (execution_capability_snapshot_sha256) REFERENCES public.trading_execution_capability_snapshots(snapshot_sha256) ON DELETE RESTRICT;


--
-- Name: trading_intents trading_intents_capital_authorization_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_intents
    ADD CONSTRAINT trading_intents_capital_authorization_fk FOREIGN KEY (capital_authorization_receipt_sha256) REFERENCES public.trading_capital_authorization_receipts(authorization_receipt_sha256) ON DELETE RESTRICT;


--
-- Name: trading_intents trading_intents_case_manifest_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_intents
    ADD CONSTRAINT trading_intents_case_manifest_fk FOREIGN KEY (case_id, case_manifest_sha256) REFERENCES public.trading_cases(case_id, manifest_sha256) ON DELETE RESTRICT;


--
-- Name: trading_intents trading_intents_execution_binding_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_intents
    ADD CONSTRAINT trading_intents_execution_binding_fk FOREIGN KEY (execution_binding_sha256) REFERENCES public.trading_execution_bindings(binding_sha256) ON DELETE RESTRICT;


--
-- Name: trading_intents trading_intents_venue_catalog_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_intents
    ADD CONSTRAINT trading_intents_venue_catalog_fk FOREIGN KEY (venue_catalog_snapshot_sha256) REFERENCES public.trading_venue_catalog_snapshots(snapshot_sha256) ON DELETE RESTRICT;


--
-- Name: trading_operator_arm_receipts trading_operator_arm_receipts_grant_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_operator_arm_receipts
    ADD CONSTRAINT trading_operator_arm_receipts_grant_sha256_fkey FOREIGN KEY (grant_sha256) REFERENCES public.trading_production_promotion_grants(grant_sha256) ON DELETE RESTRICT;


--
-- Name: trading_operator_arm_receipts trading_operator_arm_receipts_risk_policy_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_operator_arm_receipts
    ADD CONSTRAINT trading_operator_arm_receipts_risk_policy_sha256_fkey FOREIGN KEY (risk_policy_sha256) REFERENCES public.trading_daily_risk_policies(risk_policy_sha256) ON DELETE RESTRICT;


--
-- Name: trading_order_observations trading_order_observations_order_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_order_observations
    ADD CONSTRAINT trading_order_observations_order_fk FOREIGN KEY (order_id) REFERENCES public.trading_orders(order_id) ON DELETE CASCADE;


--
-- Name: trading_orders trading_orders_case_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_orders
    ADD CONSTRAINT trading_orders_case_fk FOREIGN KEY (case_id) REFERENCES public.trading_cases(case_id) ON DELETE CASCADE;


--
-- Name: trading_production_promotion_grants trading_production_promotion_grants_risk_policy_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_production_promotion_grants
    ADD CONSTRAINT trading_production_promotion_grants_risk_policy_sha256_fkey FOREIGN KEY (risk_policy_sha256) REFERENCES public.trading_daily_risk_policies(risk_policy_sha256) ON DELETE RESTRICT;


--
-- Name: trading_production_promotion_grants trading_promotion_grant_future_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_production_promotion_grants
    ADD CONSTRAINT trading_promotion_grant_future_fk FOREIGN KEY (locked_future_report_sha256) REFERENCES public.trading_evidence_clock_receipts(artifact_sha256) ON DELETE RESTRICT;


--
-- Name: trading_promotion_grant_revocations trading_promotion_grant_revocations_grant_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_promotion_grant_revocations
    ADD CONSTRAINT trading_promotion_grant_revocations_grant_sha256_fkey FOREIGN KEY (grant_sha256) REFERENCES public.trading_production_promotion_grants(grant_sha256) ON DELETE RESTRICT;


--
-- Name: trading_runtime_state trading_runtime_capability_snapshot_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trading_runtime_state
    ADD CONSTRAINT trading_runtime_capability_snapshot_fk FOREIGN KEY (active_capability_snapshot_sha256) REFERENCES public.trading_execution_capability_snapshots(snapshot_sha256) ON DELETE RESTRICT;


--
-- PostgreSQL database dump complete
--

--
-- Current structural seeds. Migration receipts are history, not blank-cluster state.
--

INSERT INTO public.news_ingest_state (singleton_key, updated_at_ms)
VALUES ('opennews', 0);

INSERT INTO public.news_learning_retention_state (singleton, updated_at_ms)
VALUES (true, 0);

INSERT INTO public.trading_runtime_state (id, control, orders_today, updated_at_ms)
VALUES (1, 'PAUSED', 0, 0);

INSERT INTO public.trading_symbol_blacklist (
    base_symbol, reason, expires_at_ms, created_at_ms, updated_at_ms
) VALUES
    ('BTC', 'benchmark_large_cap', NULL, 0, 0),
    ('ETH', 'benchmark_large_cap', NULL, 0, 0),
    ('CL', 'commodity_not_target', NULL, 0, 0);

INSERT INTO public.trading_decision_runtime (id, state, heartbeat_at_ms, reason, updated_at_ms)
VALUES (1, 'DISABLED', NULL, 'trading_disabled', 0);

INSERT INTO public.trading_binding_runtime (
    binding, credential_state, credential_fingerprint, runtime_state, account_state,
    catalog_state, catalog_snapshot_sha256, catalog_captured_at_ms, heartbeat_at_ms,
    reason, updated_at_ms
) VALUES
    (
        'BINANCE_USDM', 'unconfigured', NULL, 'stopped', 'unknown',
        'missing', NULL, NULL, NULL, 'credentials_unconfigured', 0
    ),
    (
        'HYPERLIQUID_PERP', 'unconfigured', NULL, 'stopped', 'unknown',
        'missing', NULL, NULL, NULL, 'credentials_unconfigured', 0
    );

RESET search_path;
