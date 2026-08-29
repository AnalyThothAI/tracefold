"""Hard-cut News persistence to the single current judgment contract (#369).

Historical rows remain byte-for-byte audit evidence.  ``NOT VALID`` constraints
deliberately leave those rows unscanned while rejecting every non-current write
after this revision.  Ordinary Review reads expose only exact current judgment
and evidence identities; no compatibility view is created.

Revision ID: 20260830_0330
Revises: 20260829_0329
"""

from __future__ import annotations

from alembic import op

revision = "20260830_0330"
down_revision = "20260829_0329"
branch_labels = None
depends_on = None

TRIP_REASON = "news_current_contract_hard_cut"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute("DROP VIEW news_review_task_source_v1")
    op.execute("ALTER TABLE news_events RENAME COLUMN family TO dedupe_family")
    op.execute("ALTER TABLE news_event_bands RENAME COLUMN family TO dedupe_family")
    op.execute("ALTER TABLE news_events ALTER COLUMN focus_fact_method DROP DEFAULT")

    op.execute("ALTER TABLE news_verdicts DROP COLUMN model_decision")
    op.execute("ALTER TABLE news_verdicts DROP COLUMN novelty_defaulted")
    op.execute("ALTER TABLE news_verdicts ADD COLUMN judgment_contract_version text")
    op.execute("ALTER TABLE news_verdicts ADD COLUMN judgment_origin text")
    op.execute("ALTER TABLE news_verdicts DROP CONSTRAINT news_verdicts_scored_judgment_triplet_check")
    op.execute("ALTER TABLE news_verdicts DROP CONSTRAINT news_verdicts_v10_scored_judgment_required")
    op.execute("ALTER TABLE news_verdicts DROP CONSTRAINT news_verdicts_final_decision_check")
    op.execute(
        """
        ALTER TABLE news_verdicts
        ADD CONSTRAINT news_verdicts_final_decision_check
        CHECK (final_decision IN ('push', 'escalate', 'drop', 'throttled')) NOT VALID
        """
    )

    # Python's canonical JSON is UTF-8, key-sorted and separator-free.  This
    # recursive helper gives PostgreSQL the same bytes for content-addressed
    # judgment/evidence checks and the dynamic migration receipt.
    op.execute(
        """
        CREATE FUNCTION news_canonical_jsonb(value jsonb) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_jsonb_exact_keys(value jsonb, expected text[]) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT jsonb_typeof(value) = 'object'
             AND ARRAY(SELECT key FROM jsonb_object_keys(value) key ORDER BY key COLLATE "C")
                 = ARRAY(SELECT key FROM unnest(expected) key ORDER BY key COLLATE "C")
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_jsonb_ordered_string_set_valid(
          value jsonb, allowed text[], maximum integer
        ) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_jsonb_required_optional_keys(
          value jsonb, required text[], optional text[]
        ) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT jsonb_typeof(value) = 'object'
             AND NOT EXISTS (SELECT 1 FROM unnest(required) key WHERE NOT value ? key)
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_object_keys(value) key
                    WHERE NOT key = ANY(required || optional)
                 )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_jsonb_int64_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT jsonb_typeof(value) = 'number'
             AND CASE WHEN value #>> '{}' ~ '^-?[0-9]+$'
                      THEN (value #>> '{}')::numeric BETWEEN -9223372036854775808 AND 9223372036854775807
                      ELSE false END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_jsonb_forbidden_keys_absent(
          value jsonb, forbidden text[]
        ) RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_told_trace_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_triage_verdict_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_model_editorial_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT news_jsonb_exact_keys(value, ARRAY[
                   'editorial_contract_version','editorial_origin','relevance','taxonomy','editorial_sha256'
                 ])
             AND value ->> 'editorial_contract_version' = 'news_editorial_v2'
             AND value ->> 'editorial_origin' = 'model'
             AND value ->> 'editorial_sha256' ~ '^[0-9a-f]{64}$'
             AND value ->> 'editorial_sha256' = encode(digest(
                   convert_to(news_canonical_jsonb(value - 'editorial_sha256'), 'UTF8'), 'sha256'), 'hex')
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_decision_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_oi_signal_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_liquidation_fact_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
             AND CASE WHEN value ->> 'notional_usd' ~ '^(0|[1-9][0-9]*)(\\.[0-9]+)?$'
                      THEN (value ->> 'notional_usd')::numeric > 0
                       AND (value ->> 'notional_usd')::numeric <= 1e24
                      ELSE false END
             AND (jsonb_typeof(value -> 'quantity') = 'null' OR (
                   jsonb_typeof(value -> 'quantity') = 'string'
                   AND CASE WHEN value ->> 'quantity' ~ '^(0|[1-9][0-9]*)(\\.[0-9]+)?$'
                            THEN (value ->> 'quantity')::numeric > 0
                             AND (value ->> 'quantity')::numeric <= 1e24
                            ELSE false END
                 ))
             AND jsonb_typeof(value -> 'price') = 'string'
             AND CASE WHEN value ->> 'price' ~ '^(0|[1-9][0-9]*)(\\.[0-9]+)?$'
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_oi_metadata_valid(value jsonb, parsed boolean) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_liquidation_metadata_valid(value jsonb, parsed boolean) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_evidence_snapshot_valid(
          value jsonb, expected_event_id text, expected_focus_fact_id text
        ) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )

    op.execute(
        """
        CREATE FUNCTION news_current_review_dimensions_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_review_novelty_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT news_jsonb_exact_keys(value, ARRAY['judgment','duplicate_of'])
             AND jsonb_typeof(value -> 'judgment') = 'string'
             AND value ->> 'judgment' IN ('new_fact','progression','restatement','uncertain')
             AND jsonb_typeof(value -> 'duplicate_of') = 'string'
             AND CASE WHEN value ->> 'judgment' = 'restatement'
                      THEN btrim(value ->> 'duplicate_of') <> ''
                      ELSE btrim(value ->> 'duplicate_of') = '' END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_review_evidence_refs_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= 32
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(value) reference
                    WHERE jsonb_typeof(reference) <> 'string'
                       OR length(reference #>> '{}') NOT BETWEEN 1 AND 500
                       OR btrim(reference #>> '{}') <> reference #>> '{}'
                 )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_review_taxonomy_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_review_expected_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_review_taxonomy_provenance_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_event_review_payload_valid(
          value jsonb, expected_should_push text, expected_dimensions jsonb,
          expected_novelty jsonb, expected_first_bad_owner text,
          expected_evidence_refs jsonb, expected_correction text, expected_note text
        ) RETURNS boolean
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_pairwise_review_payload_valid(
          value jsonb, expected_evidence_refs jsonb, expected_note text
        ) RETURNS boolean
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_review_selection_valid(
          value jsonb, subject_kind_value text
        ) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_review_valid(
          review_kind_value text, subject_kind_value text,
          rubric_version_value text, reader_contract_version_value text,
          event_id_value text, evidence_version_value integer,
          external_snapshot_id_value text, pairwise_case_id_value text,
          should_push_value text, dimensions_value jsonb, novelty_value jsonb,
          first_bad_owner_value text, evidence_refs_value jsonb,
          expected_correction_value text, note_value text, selection_value jsonb, payload_value jsonb,
          accepts_review_id_value text
        ) RETURNS boolean
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
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
        $$
        """
    )

    op.execute(
        """
        ALTER TABLE news_verdicts
        ADD CONSTRAINT news_verdicts_current_judgment_check CHECK ((
          judgment_contract_version IS NOT NULL
          AND judgment_origin IS NOT NULL
          AND judgment_contract_version = 'news_judgment_v2'
          AND judgment_origin IN ('model','oi','liquidation','degraded')
          AND stage = 'triage'
          AND news_current_triage_verdict_valid(verdict)
          AND scored_judgment_sha256 ~ '^[0-9a-f]{64}$'
          AND runtime_manifest_sha ~ '^[0-9a-f]{64}$'
          AND program_version IS NOT NULL
          AND program_sha256 ~ '^[0-9a-f]{64}$'
          AND evidence_version >= 1
          AND evidence_sha256 ~ '^[0-9a-f]{64}$'
          AND focus_fact_id IS NOT NULL AND focus_fact_id <> ''
          AND seen_scope IN ('','all')
          AND (throttled_by IS NULL OR right(throttled_by, 5) <> (chr(58) || 'seen') OR seen_scope = 'all')
          AND news_jsonb_forbidden_keys_absent(trace, ARRAY[
                'event_type','event_type_zh','title_zh','actionable','model_decision',
                'novelty_defaulted','provider_cost_usd','legacy_label','legacy_event_type',
                'project_legacy_event_type','unclear_push_event_types','display_title'
              ])
          AND news_current_told_trace_valid(trace -> 'told')
          AND news_jsonb_int64_valid(trace -> 'told_count')
          AND (trace ->> 'told_count')::numeric = jsonb_array_length(trace -> 'told')
          AND trace ->> 'judgment_contract_version' = judgment_contract_version
          AND trace ->> 'judgment_origin' = judgment_origin
          AND trace ->> 'judgment_sha256' = scored_judgment_sha256
          AND trace ->> 'verdict_sha256' = encode(digest(
                convert_to(news_canonical_jsonb(verdict), 'UTF8'), 'sha256'), 'hex')
          AND trace ->> 'evidence_version' = evidence_version::text
          AND trace ->> 'evidence_sha256' = evidence_sha256
          AND trace ->> 'focus_fact_id' = focus_fact_id
          AND trace ->> 'runtime_manifest_sha' = runtime_manifest_sha
          AND (
            (judgment_origin = 'model'
             AND NOT degraded AND error_code IS NULL AND model IS NOT NULL
             AND program_version = 'news_semantic_program_v8'
             AND policy_version = 'news_triage_policy_v11'
             AND news_current_model_editorial_valid(editorial)
             AND trace ->> 'editorial_sha256' = editorial ->> 'editorial_sha256'
             AND scored_judgment_sha256 = encode(digest(convert_to(news_canonical_jsonb(jsonb_build_object(
                   'judgment_contract_version', judgment_contract_version,
                   'verdict', verdict,
                   'editorial', editorial,
                   'verdict_sha256', trace ->> 'verdict_sha256'
                 )), 'UTF8'), 'sha256'), 'hex'))
            OR
            (judgment_origin = 'oi'
             AND editorial IS NULL AND model IS NULL AND NOT degraded
             AND program_version = 'news_oi_signal_v2'
             AND policy_version = 'news_triage_policy_v11'
             AND news_jsonb_exact_keys(trace -> 'judgment', ARRAY[
                   'judgment_contract_version','origin','verdict','signal',
                   'rank_in_window','rule','decision'
                 ])
             AND trace #>> '{judgment,judgment_contract_version}' = judgment_contract_version
             AND trace #>> '{judgment,origin}' = judgment_origin
             AND trace #> '{judgment,verdict}' = verdict
             AND (jsonb_typeof(trace #> '{judgment,signal}') = 'null'
                  OR news_current_oi_signal_valid(trace #> '{judgment,signal}') IS TRUE)
             AND news_current_oi_metadata_valid(
                   trace -> 'oi_signal', jsonb_typeof(trace #> '{judgment,signal}') = 'object'
                 )
             AND jsonb_typeof(trace #> '{judgment,rank_in_window}') = 'number'
             AND (trace #>> '{judgment,rank_in_window}') ~ '^[0-9]+$'
             AND jsonb_typeof(trace #> '{judgment,rule}') = 'string'
             AND trace #>> '{judgment,rule}' <> ''
             AND news_current_decision_valid(trace #> '{judgment,decision}')
             AND trace #>> '{judgment,decision,final}' = final_decision
             AND trace #>> '{judgment,decision,rule_baseline}' = rule_baseline_decision
             AND trace #>> '{judgment,decision,override_rule}' IS NOT DISTINCT FROM override_rule
             AND trace #>> '{judgment,decision,throttled_by}' IS NOT DISTINCT FROM throttled_by
             AND trace #>> '{judgment,rule}' = override_rule
             AND trace #>> '{judgment,decision,throttled_by}' IS NULL
             AND CASE WHEN jsonb_typeof(trace #> '{judgment,signal}') = 'null' THEN
                    (trace #>> '{judgment,rank_in_window}')::numeric = 0
                    AND trace #>> '{judgment,rule}' = 'oi_parse_failed'
                    AND final_decision = 'drop' AND rule_baseline_decision = 'drop'
                  ELSE
                    (trace #>> '{judgment,rank_in_window}')::numeric >= 1
                    AND trace #>> '{judgment,rule}' = CASE
                      WHEN (trace #>> '{judgment,signal,whale_oi_ratio_bps}')::numeric
                             <= (trace #>> '{oi_signal,policy,whale_oi_ratio_above_bps}')::numeric
                        THEN 'whale_ratio_below_threshold'
                      WHEN abs((trace #>> '{judgment,signal,oi_change_bps}')::numeric)
                             < (trace #>> '{oi_signal,policy,oi_change_at_least_bps}')::numeric
                        THEN 'oi_change_below_threshold'
                      WHEN (trace #>> '{judgment,rank_in_window}')::numeric
                             > (trace #>> '{oi_signal,policy,max_rank_in_window}')::numeric
                        THEN 'beyond_window_rank'
                      ELSE 'opening_move_with_whale_concentration'
                    END
                    AND final_decision = CASE
                      WHEN trace #>> '{judgment,rule}' = 'opening_move_with_whale_concentration'
                        THEN 'push' ELSE 'drop' END
                    AND rule_baseline_decision = final_decision
                  END
             AND scored_judgment_sha256 = encode(digest(convert_to(
                   news_canonical_jsonb(trace -> 'judgment'), 'UTF8'), 'sha256'), 'hex')
             AND error_code IS NOT DISTINCT FROM
                   CASE WHEN jsonb_typeof(trace #> '{judgment,signal}') = 'null'
                        THEN 'oi_parse_failed' ELSE NULL END)
            OR
            (judgment_origin = 'liquidation'
             AND editorial IS NULL AND model IS NULL AND NOT degraded
             AND program_version = 'news_liquidation_fact_v2'
             AND policy_version = 'news_liquidation_policy_v2'
             AND news_jsonb_exact_keys(trace -> 'judgment', ARRAY[
                   'judgment_contract_version','origin','verdict','fact','rule','decision'
                 ])
             AND trace #>> '{judgment,judgment_contract_version}' = judgment_contract_version
             AND trace #>> '{judgment,origin}' = judgment_origin
             AND trace #> '{judgment,verdict}' = verdict
             AND (jsonb_typeof(trace #> '{judgment,fact}') = 'null'
                  OR news_current_liquidation_fact_valid(trace #> '{judgment,fact}') IS TRUE)
             AND news_current_liquidation_metadata_valid(
                   trace -> 'liquidation', jsonb_typeof(trace #> '{judgment,fact}') = 'object'
                 )
             AND jsonb_typeof(trace #> '{judgment,rule}') = 'string'
             AND trace #>> '{judgment,rule}' <> ''
             AND news_current_decision_valid(trace #> '{judgment,decision}')
             AND trace #>> '{judgment,decision,final}' = final_decision
             AND trace #>> '{judgment,decision,rule_baseline}' = rule_baseline_decision
             AND trace #>> '{judgment,decision,override_rule}' IS NOT DISTINCT FROM override_rule
             AND trace #>> '{judgment,decision,throttled_by}' IS NOT DISTINCT FROM throttled_by
             AND trace #>> '{judgment,rule}' = override_rule
             AND trace #>> '{judgment,decision,throttled_by}' IS NULL
             AND CASE WHEN jsonb_typeof(trace #> '{judgment,fact}') = 'null' THEN
                    trace #>> '{judgment,rule}' = 'liquidation_parse_failed'
                    AND final_decision = 'drop' AND rule_baseline_decision = 'drop'
                  ELSE
                    trace #>> '{judgment,rule}' = 'liquidation_fact_only'
                    AND final_decision = 'push' AND rule_baseline_decision = 'push'
                  END
             AND scored_judgment_sha256 = encode(digest(convert_to(
                   news_canonical_jsonb(trace -> 'judgment'), 'UTF8'), 'sha256'), 'hex')
             AND error_code IS NOT DISTINCT FROM
                   CASE WHEN jsonb_typeof(trace #> '{judgment,fact}') = 'null'
                        THEN 'liquidation_parse_failed' ELSE NULL END)
            OR
            (judgment_origin = 'degraded'
             AND editorial IS NULL AND model IS NULL AND degraded AND error_code IS NOT NULL
             AND program_version = 'news_semantic_program_v8'
             AND policy_version = 'news_triage_policy_v11'
             AND NOT (trace ? 'editorial_sha256')
             AND news_jsonb_exact_keys(trace -> 'judgment', ARRAY[
                   'judgment_contract_version','origin','verdict','decision','error_code'
                 ])
             AND trace #>> '{judgment,judgment_contract_version}' = judgment_contract_version
             AND trace #>> '{judgment,origin}' = judgment_origin
             AND trace #> '{judgment,verdict}' = verdict
             AND news_current_decision_valid(trace #> '{judgment,decision}')
             AND trace #>> '{judgment,decision,final}' = final_decision
             AND trace #>> '{judgment,decision,rule_baseline}' = rule_baseline_decision
             AND trace #>> '{judgment,decision,override_rule}' IS NOT DISTINCT FROM override_rule
             AND trace #>> '{judgment,decision,throttled_by}' IS NOT DISTINCT FROM throttled_by
             AND trace #>> '{judgment,error_code}' = error_code
             AND scored_judgment_sha256 = encode(digest(convert_to(
                   news_canonical_jsonb(trace -> 'judgment'), 'UTF8'), 'sha256'), 'hex'))
          )
        ) IS TRUE) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE news_event_evidence_snapshots
        ADD CONSTRAINT news_event_evidence_current_contract_check CHECK ((
          provenance = 'observed' AND release_eligible
          AND news_current_evidence_snapshot_valid(snapshot, event_id, focus_fact_id)
          AND evidence_sha256 = encode(digest(
                convert_to(news_canonical_jsonb(snapshot), 'UTF8'), 'sha256'), 'hex')
        ) IS TRUE) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE news_events
        ADD CONSTRAINT news_events_current_focus_fact_check
        CHECK ((focus_fact_method <> 'legacy_reconstructed') IS TRUE) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE news_reviews
        ADD CONSTRAINT news_reviews_current_contract_check CHECK ((
          news_current_review_valid(
            review_kind, subject_kind, rubric_version, reader_contract_version,
            event_id, evidence_version, external_snapshot_id, pairwise_case_id,
            should_push, dimensions, novelty, first_bad_owner, evidence_refs,
            expected_correction, note, selection, payload, accepts_review_id
          )
        ) IS TRUE) NOT VALID
        """
    )
    op.execute(
        """
        CREATE FUNCTION news_current_review_acceptance_target_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER news_reviews_current_acceptance_target_check
        BEFORE INSERT ON news_reviews
        FOR EACH ROW EXECUTE FUNCTION news_current_review_acceptance_target_guard()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW news_review_records_v1 WITH (security_barrier = true) AS
        SELECT review_id, idempotency_key, idempotency_request_sha, review_kind,
               subject_kind, task_id, task_version, event_id, evidence_version,
               external_snapshot_id, pairwise_case_id, rubric_version,
               reader_contract_version, reviewer, should_push, dimensions,
               novelty, first_bad_owner, evidence_refs, expected_correction,
               note, selection, payload, supersedes_review_id,
               accepts_review_id, release_eligible, created_at_ms
          FROM news_reviews
         WHERE news_current_review_valid(
                 review_kind, subject_kind, rubric_version, reader_contract_version,
                 event_id, evidence_version, external_snapshot_id, pairwise_case_id,
                 should_push, dimensions, novelty, first_bad_owner, evidence_refs,
                 expected_correction, note, selection, payload, accepts_review_id
               ) IS TRUE
        """
    )

    op.execute(
        """
        CREATE VIEW news_review_task_source_v1 WITH (security_barrier = true) AS
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
          FROM news_events e
          JOIN LATERAL (
            SELECT x.* FROM news_verdicts x
             WHERE x.event_id = e.event_id AND x.stage = 'triage'
               AND x.judgment_contract_version = 'news_judgment_v2'
               AND x.judgment_origin = 'model'
             ORDER BY x.created_at_ms DESC LIMIT 1
          ) v ON true
          JOIN LATERAL (
            SELECT x.* FROM news_event_evidence_snapshots x
             WHERE x.event_id = e.event_id
             ORDER BY x.evidence_version DESC LIMIT 1
          ) s ON s.provenance = 'observed'
             AND s.release_eligible
             AND s.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
             AND s.evidence_version = v.evidence_version
             AND s.evidence_sha256 = v.evidence_sha256
             AND s.focus_fact_id = v.focus_fact_id
          LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
          LEFT JOIN LATERAL (
            SELECT max(abs(x.return_1h_bps)) AS max_abs_return_1h_bps
              FROM news_event_reactions x
             WHERE x.event_id = e.event_id
               AND x.metric_version = 'reaction_v1'
               AND x.is_primary
          ) reaction ON true
         WHERE e.event_kind = 'news'
        """
    )
    op.execute("GRANT SELECT ON news_review_task_source_v1 TO tracefold_serve, tracefold_workers")

    op.execute(
        """
        UPDATE news_canary_activations
           SET state = 'tripped', revision = revision + 1,
               trip_reason = 'news_current_contract_hard_cut',
               tripped_at_ms = floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint
         WHERE state IN ('armed', 'active')
        """
    )
    op.execute(
        """
        WITH old_events AS (
          SELECT e.event_id
            FROM news_events e
           WHERE EXISTS (
                   SELECT 1 FROM news_verdicts v
                    WHERE v.event_id = e.event_id
                      AND v.judgment_contract_version IS NULL
                 )
              OR NOT EXISTS (
                   SELECT 1 FROM news_event_evidence_snapshots s
                    WHERE s.event_id = e.event_id
                      AND s.evidence_version = (
                        SELECT max(x.evidence_version) FROM news_event_evidence_snapshots x
                         WHERE x.event_id = e.event_id
                      )
                      AND s.provenance = 'observed'
                      AND s.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
                 )
        ), counts AS (
          SELECT
            (SELECT count(*) FROM news_verdicts
              WHERE judgment_contract_version IS NULL) AS total_old_verdict_rows,
            (SELECT count(*) FROM news_verdicts
              WHERE judgment_contract_version IS NULL
                AND model IS NOT NULL AND NOT degraded
                AND news_current_model_editorial_valid(editorial) IS TRUE) AS current_taxonomy_present_rows,
            (SELECT count(*) FROM old_events) AS archive_only_event_rows,
            (SELECT count(*) FROM news_reviews r
              WHERE r.event_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM old_events e WHERE e.event_id = r.event_id)
            ) AS affected_review_rows,
            (SELECT count(*) FROM news_deliveries d
              WHERE d.state = 'sent'
                AND EXISTS (SELECT 1 FROM old_events e WHERE e.event_id = d.event_id)
            ) AS affected_history_rows,
            (SELECT count(*) FROM news_learning_cases c
              WHERE c.event_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM old_events e WHERE e.event_id = c.event_id)
            ) AS affected_learning_rows
        ), receipt AS (
          SELECT jsonb_build_object(
            'kind', 'news_current_contract_hard_cut',
            'source_issue', 'https://github.com/AnalyThothAI/tracefold/issues/369',
            'judgment_contract_version', 'news_judgment_v2',
            'evidence_contract_version', 'news_event_evidence_v3',
            'total_old_verdict_rows', total_old_verdict_rows,
            'current_taxonomy_present_rows', current_taxonomy_present_rows,
            'missing_invalid_conflicting_rows', total_old_verdict_rows - current_taxonomy_present_rows,
            'archive_only_event_rows', archive_only_event_rows,
            'affected_review_rows', affected_review_rows,
            'affected_history_rows', affected_history_rows,
            'affected_learning_rows', affected_learning_rows,
            'disposition', 'immutable_audit_only'
          ) AS payload
          FROM counts
        ), addressed AS (
          SELECT payload,
                 encode(digest(convert_to(news_canonical_jsonb(jsonb_build_object(
                   'kind', 'epoch_reset', 'payload', payload
                 )), 'UTF8'), 'sha256'), 'hex') AS artifact_sha
            FROM receipt
        )
        INSERT INTO news_learning_artifacts (
          artifact_sha, kind, parent_sha, payload, created_by, created_at_ms
        )
        SELECT artifact_sha, 'epoch_reset', NULL, payload,
               'migration_20260830_0330',
               floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint
          FROM addressed
        ON CONFLICT (artifact_sha) DO NOTHING
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260830_0330 is an irreversible current-contract hard cut")
