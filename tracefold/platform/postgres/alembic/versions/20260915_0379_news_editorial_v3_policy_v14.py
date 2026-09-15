"""News editorial v3 and triage policy v14: a taxonomy Predictor may fail alone (#651 §5.3).

Migration evidence:

- category: two function replacements, one new function, one constraint rewrite, additive
- why_database_must_change: three things.
  `news_current_model_editorial_valid` enumerates the exact editorial keys and requires
  `editorial_contract_version = 'news_editorial_v2'` with the code-owned `source_authority` nested inside
  the taxonomy object. #651 lifts that authority out of the taxonomy -- the model never emitted it, and
  the taxonomy Predictor can now fail on its own without costing the reader the card the other two
  Predictors produced -- so `news_editorial_v3` carries `source_authority`, `taxonomy_status` and
  `taxonomy_error_code` beside a `taxonomy` that may be JSON null. Without this revision the deployed
  workers cannot persist a single model judgment.
  `news_verdicts_current_judgment_check` pins the model, OI and degraded branches to policy versions up to
  `news_triage_policy_v13`. `decide()` now reads `editorial.source_authority` instead of
  `editorial.taxonomy.source_authority` for the uncorroborated-escalate rule, so `TRIAGE_POLICY_VERSION`
  moves to v14 and every verdict of every origin is written under it.
  `news_current_review_taxonomy_valid` enumerates a reviewer's accepted taxonomy keys exactly, including
  `source_authority`. `NewsTaxonomyV1` no longer has that field and `EventRubricSubmission` forbids extra
  keys, so without this revision every accepted review would be refused outright.
- current_source_revision: 20260915_0378
- minimum_supported_source_revision: 20260915_0378
- lock_level_and_order: maintenance stop; function creation and replacement, then ACCESS EXCLUSIVE
  constraint drop and add, in one transaction
- statement_timeout: 120s set locally by the revision (the ADD CONSTRAINT scans every verdict row)
- lock_timeout: 5s set locally by the revision
- estimated_rows: `news_verdicts` under the 30-day retention, low tens of thousands
- estimated_bytes: catalog entries only; no heap rewrite, no index build
- rewrite_or_index_build: none; ADD CONSTRAINT validates existing rows in place
- preflight_and_maintenance_boundary: News workers stopped and the News queues drained
- archive_current_compatibility: compatible, in all three halves.
  Every judgment written under `news_editorial_v2` keeps validating: the editorial predicate branches on
  the contract version the document itself declares and states the older shape completely, including the
  nested authority. Those rows are audit truth and are never rewritten -- `scored_judgment_sha256`
  addresses the exact bytes that were persisted, so a migration of the column would invalidate every
  judgment identity in the ledger. The worker never writes v2 again because `EDITORIAL_CONTRACT_VERSION`
  is the only value it emits, and the one place that has to read both -- the feed's source-authority
  filter and the Event detail -- converts at the storage read boundary
  (`tracefold.news.storage.decisions.editorial_read_shape` and its SQL sibling
  `EDITORIAL_SOURCE_AUTHORITY_SQL`).
  Every verdict written under v11-v13 keeps validating: the three branches accept any of the four policy
  versions the `news_judgment_v2` contract has been written under.
  Every accepted review keeps validating: the taxonomy predicate admits the seven-key shape every review
  written before this revision carries *and* the six-key shape without the authority, exactly as `0378`
  admits a reviewer's asset with or without its market.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the v2-only editorial predicate, the v13 policy
  list and the seven-key review taxonomy predicate stay
- roll_forward_or_verified_backup_restore: correct with a new forward revision or restore the verified
  pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260915_0379
Revises: 20260915_0378
Create Date: 2026-09-15 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260915_0379"
down_revision = "20260915_0378"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    # The four model-owned axes and the codebook they were labelled against, in one place. Three callers
    # ask exactly this question -- a judgment's editorial taxonomy, a reviewer's accepted taxonomy, and the
    # blind draft inside a review's taxonomy provenance -- and until now each restated the vocabulary
    # itself. They differ only in which keys surround the axes, which is what each caller still states.
    op.execute(
        """
CREATE FUNCTION public.news_current_taxonomy_axes_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT value ->> 'taxonomy_version' = 'news_taxonomy_v1'
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
        $_$;
        """
    )

    # The editorial document, restated from `20260831_0340` around a version branch. `news_editorial_v3` is
    # what the worker writes: the code-owned authority sits beside the relevance, the taxonomy object is the
    # four axes plus the codebook, and `taxonomy_status`/`taxonomy_error_code` say whether that Predictor
    # answered. `news_editorial_v2` is stated completely because every judgment written before this revision
    # carries it, is audit truth, and is never rewritten.
    op.execute(
        """
CREATE OR REPLACE FUNCTION public.news_current_model_editorial_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT value ->> 'editorial_origin' = 'model'
             AND value ->> 'editorial_sha256' ~ '^[0-9a-f]{64}$'
             AND value ->> 'editorial_sha256' = encode(sha256(
                   convert_to(news_canonical_jsonb(value - 'editorial_sha256'), 'UTF8')), 'hex')
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
             AND CASE value ->> 'editorial_contract_version'
               WHEN 'news_editorial_v3' THEN
                 news_jsonb_exact_keys(value, ARRAY[
                   'editorial_contract_version','editorial_origin','relevance','source_authority',
                   'taxonomy','taxonomy_status','taxonomy_error_code','editorial_sha256'
                 ])
                 AND value ->> 'source_authority' IN (
                       'regulatory_filing','issuer_first_party','reputable_secondary','unknown'
                     )
                 AND CASE value ->> 'taxonomy_status'
                   WHEN 'available' THEN
                     jsonb_typeof(value -> 'taxonomy') = 'object'
                     AND jsonb_typeof(value -> 'taxonomy_error_code') = 'null'
                     AND news_jsonb_exact_keys(value -> 'taxonomy', ARRAY[
                           'subject_codes','event_family','change_state','assertion_status',
                           'taxonomy_version','codebook_sha256'
                         ])
                     AND news_current_taxonomy_axes_valid(value -> 'taxonomy')
                   WHEN 'unavailable' THEN
                     jsonb_typeof(value -> 'taxonomy') = 'null'
                     AND jsonb_typeof(value -> 'taxonomy_error_code') = 'string'
                     AND left(value ->> 'taxonomy_error_code', 13) = 'news_program_'
                     AND length(value ->> 'taxonomy_error_code') BETWEEN 14 AND 200
                   ELSE false
                 END
               WHEN 'news_editorial_v2' THEN
                 news_jsonb_exact_keys(value, ARRAY[
                   'editorial_contract_version','editorial_origin','relevance','taxonomy','editorial_sha256'
                 ])
                 AND news_jsonb_exact_keys(value -> 'taxonomy', ARRAY[
                       'subject_codes','event_family','change_state','assertion_status',
                       'taxonomy_version','source_authority','codebook_sha256'
                     ])
                 AND news_current_taxonomy_axes_valid(value -> 'taxonomy')
                 AND value #>> '{taxonomy,source_authority}' IN (
                       'regulatory_filing','issuer_first_party','reputable_secondary','unknown'
                     )
               ELSE false
             END
        $_$;
        """
    )

    # A reviewer's accepted taxonomy, restated from `20260831_0340`. Both key sets are admitted for the same
    # reason `0378` admits a reviewer's asset with or without its market: every review accepted before this
    # revision carries the authority, is audit truth, and is never rewritten. `EventRubricSubmission` emits
    # the six-key shape from here on, because `NewsTaxonomyV1` no longer has a field to put it in.
    op.execute(
        """
CREATE OR REPLACE FUNCTION public.news_current_review_taxonomy_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT (
                   news_jsonb_exact_keys(value, ARRAY[
                     'subject_codes','event_family','change_state','assertion_status',
                     'taxonomy_version','codebook_sha256'
                   ])
                   OR (
                     news_jsonb_exact_keys(value, ARRAY[
                       'subject_codes','event_family','change_state','assertion_status',
                       'taxonomy_version','source_authority','codebook_sha256'
                     ])
                     AND value ->> 'source_authority' IN (
                           'regulatory_filing','issuer_first_party','reputable_secondary','unknown'
                         )
                   )
                 )
             AND news_current_taxonomy_axes_valid(value)
        $$;
        """
    )

    # One CHECK holds all four judgment origins, so the whole predicate is restated from `20260915_0378`.
    # The liquidation branch is byte-identical to it; the model, OI and degraded branches gain
    # `news_triage_policy_v14` in their policy-version list. The model branch's editorial predicate is
    # unchanged here because the editorial function above is where the v3 shape is stated.
    op.execute("ALTER TABLE news_verdicts DROP CONSTRAINT news_verdicts_current_judgment_check")
    op.execute(
        """
        ALTER TABLE news_verdicts
        ADD CONSTRAINT news_verdicts_current_judgment_check CHECK (
            (((judgment_contract_version IS NOT NULL) AND (judgment_origin IS NOT NULL) AND
            (judgment_contract_version = 'news_judgment_v2'::text) AND (judgment_origin = ANY (ARRAY['model'::text,
            'oi'::text, 'liquidation'::text, 'degraded'::text])) AND (stage = 'triage'::text) AND
            public.news_current_triage_verdict_valid(verdict) AND (scored_judgment_sha256 ~ '^[0-9a-f]{64}$'::text)
            AND (runtime_manifest_sha ~ '^[0-9a-f]{64}$'::text) AND (program_version IS NOT NULL) AND
            (program_sha256 ~ '^[0-9a-f]{64}$'::text) AND (evidence_version >= 1) AND (evidence_sha256 ~
            '^[0-9a-f]{64}$'::text) AND (focus_fact_id IS NOT NULL) AND (focus_fact_id <> ''::text) AND (seen_scope
            = ANY (ARRAY[''::text, 'all'::text])) AND ((throttled_by IS NULL) OR ("right"(throttled_by, 5) <>
            (chr(58) || 'seen'::text)) OR (seen_scope = 'all'::text)) AND (NOT (trace ? 'type'::text)) AND
            public.news_jsonb_forbidden_keys_absent(trace, ARRAY['event_type'::text, 'event_type_zh'::text,
            'title_zh'::text, 'actionable'::text, 'model_decision'::text, 'novelty_defaulted'::text,
            'provider_cost_usd'::text, 'legacy_label'::text, 'legacy_event_type'::text,
            'project_legacy_event_type'::text, 'unclear_push_event_types'::text, 'display_title'::text, 'sym'::text,
            'm'::text, 'dir'::text, 'family'::text]) AND public.news_current_told_trace_valid((trace ->
            'told'::text)) AND public.news_jsonb_int64_valid((trace -> 'told_count'::text)) AND (((trace ->>
            'told_count'::text))::numeric = (jsonb_array_length((trace -> 'told'::text)))::numeric) AND ((trace ->>
            'judgment_contract_version'::text) = judgment_contract_version) AND ((trace ->> 'judgment_origin'::text)
            = judgment_origin) AND ((trace ->> 'judgment_sha256'::text) = scored_judgment_sha256) AND ((trace ->>
            'verdict_sha256'::text) = encode(sha256(convert_to(public.news_canonical_jsonb(verdict), 'UTF8'::name)),
            'hex'::text)) AND ((trace ->> 'evidence_version'::text) = (evidence_version)::text) AND ((trace ->>
            'evidence_sha256'::text) = evidence_sha256) AND ((trace ->> 'focus_fact_id'::text) = focus_fact_id) AND
            ((trace ->> 'runtime_manifest_sha'::text) = runtime_manifest_sha) AND ((trace ->>
            'program_version'::text) = program_version) AND ((trace ->> 'program_sha256'::text) = program_sha256)
            AND (((judgment_origin = 'model'::text) AND (NOT degraded) AND (error_code IS NULL) AND (model IS NOT
            NULL) AND (program_version = ANY (ARRAY['news_semantic_program_v8'::text,
            'news_semantic_program_v9'::text, 'news_semantic_program_v10'::text])) AND
            ((program_version <> 'news_semantic_program_v10'::text) OR
            public.news_current_typed_assets_valid(verdict)) AND (policy_version = ANY
            (ARRAY['news_triage_policy_v11'::text,
            'news_triage_policy_v12'::text, 'news_triage_policy_v13'::text,
            'news_triage_policy_v14'::text])) AND
            public.news_current_model_editorial_valid(editorial) AND ((trace ->> 'editorial_sha256'::text) =
            (editorial ->> 'editorial_sha256'::text)) AND (scored_judgment_sha256 =
            encode(sha256(convert_to(public.news_canonical_jsonb(jsonb_build_object('judgment_contract_version',
            judgment_contract_version, 'verdict', verdict, 'editorial', editorial, 'verdict_sha256', (trace ->>
            'verdict_sha256'::text))), 'UTF8'::name)), 'hex'::text))) OR ((judgment_origin = 'oi'::text) AND
            (editorial IS NULL) AND (model IS NULL) AND (NOT degraded) AND (program_version =
            'news_oi_signal_v3'::text) AND (policy_version = ANY (ARRAY['news_triage_policy_v11'::text,
            'news_triage_policy_v12'::text, 'news_triage_policy_v13'::text,
            'news_triage_policy_v14'::text])) AND
            public.news_jsonb_exact_keys((trace -> 'judgment'::text), ARRAY['judgment_contract_version'::text,
            'origin'::text, 'verdict'::text, 'signal'::text, 'rule'::text, 'decision'::text]) AND ((trace #>>
            '{judgment,judgment_contract_version}'::text[]) = judgment_contract_version) AND ((trace #>>
            '{judgment,origin}'::text[]) = judgment_origin) AND ((trace #> '{judgment,verdict}'::text[]) = verdict)
            AND ((jsonb_typeof((trace #> '{judgment,signal}'::text[])) = 'null'::text) OR
            (public.news_current_oi_signal_valid((trace #> '{judgment,signal}'::text[])) IS TRUE)) AND
            public.news_current_oi_metadata_valid((trace -> 'oi_signal'::text), (jsonb_typeof((trace #>
            '{judgment,signal}'::text[])) = 'object'::text)) AND (jsonb_typeof((trace #> '{judgment,rule}'::text[]))
            = 'string'::text) AND public.news_current_decision_valid((trace #> '{judgment,decision}'::text[])) AND
            ((trace #>> '{judgment,decision,final}'::text[]) = final_decision) AND ((trace #>>
            '{judgment,decision,rule_baseline}'::text[]) = rule_baseline_decision) AND (NOT ((trace #>>
            '{judgment,decision,override_rule}'::text[]) IS DISTINCT FROM override_rule)) AND (NOT ((trace #>>
            '{judgment,decision,throttled_by}'::text[]) IS DISTINCT FROM throttled_by)) AND ((trace #>>
            '{judgment,rule}'::text[]) = override_rule) AND ((trace #>> '{judgment,decision,throttled_by}'::text[])
            IS NULL) AND ((trace #>> '{judgment,rule}'::text[]) = CASE WHEN (jsonb_typeof((trace #>
            '{judgment,signal}'::text[])) = 'null'::text) THEN 'oi_parse_failed'::text ELSE 'stored'::text END) AND
            (final_decision = 'drop'::text) AND (rule_baseline_decision = 'drop'::text) AND (scored_judgment_sha256
            = encode(sha256(convert_to(public.news_canonical_jsonb((trace -> 'judgment'::text)), 'UTF8'::name)),
            'hex'::text)) AND (NOT (error_code IS DISTINCT FROM CASE WHEN (jsonb_typeof((trace #>
            '{judgment,signal}'::text[])) = 'null'::text) THEN 'oi_parse_failed'::text ELSE NULL::text END))) OR
            ((judgment_origin = 'liquidation'::text) AND (editorial IS NULL) AND (model IS NULL) AND (NOT degraded)
            AND (program_version = 'news_liquidation_fact_v2'::text) AND (policy_version =
            'news_liquidation_policy_v2'::text) AND public.news_jsonb_exact_keys((trace -> 'judgment'::text),
            ARRAY['judgment_contract_version'::text, 'origin'::text, 'verdict'::text, 'fact'::text, 'rule'::text,
            'decision'::text]) AND ((trace #>> '{judgment,judgment_contract_version}'::text[]) =
            judgment_contract_version) AND ((trace #>> '{judgment,origin}'::text[]) = judgment_origin) AND ((trace
            #> '{judgment,verdict}'::text[]) = verdict) AND ((jsonb_typeof((trace #> '{judgment,fact}'::text[])) =
            'null'::text) OR (public.news_current_liquidation_fact_valid((trace #> '{judgment,fact}'::text[])) IS
            TRUE)) AND public.news_current_liquidation_metadata_valid((trace -> 'liquidation'::text),
            (jsonb_typeof((trace #> '{judgment,fact}'::text[])) = 'object'::text)) AND (jsonb_typeof((trace #>
            '{judgment,rule}'::text[])) = 'string'::text) AND ((trace #>> '{judgment,rule}'::text[]) <> ''::text)
            AND public.news_current_decision_valid((trace #> '{judgment,decision}'::text[])) AND ((trace #>>
            '{judgment,decision,final}'::text[]) = final_decision) AND ((trace #>>
            '{judgment,decision,rule_baseline}'::text[]) = rule_baseline_decision) AND (NOT ((trace #>>
            '{judgment,decision,override_rule}'::text[]) IS DISTINCT FROM override_rule)) AND (NOT ((trace #>>
            '{judgment,decision,throttled_by}'::text[]) IS DISTINCT FROM throttled_by)) AND ((trace #>>
            '{judgment,rule}'::text[]) = override_rule) AND ((trace #>> '{judgment,decision,throttled_by}'::text[])
            IS NULL) AND CASE WHEN (jsonb_typeof((trace #> '{judgment,fact}'::text[])) = 'null'::text) THEN (((trace
            #>> '{judgment,rule}'::text[]) = 'liquidation_parse_failed'::text) AND (final_decision = 'drop'::text)
            AND (rule_baseline_decision = 'drop'::text)) ELSE (((trace #>> '{judgment,rule}'::text[]) =
            'liquidation_fact_only'::text) AND (final_decision = 'push'::text) AND (rule_baseline_decision =
            'push'::text)) END AND (scored_judgment_sha256 =
            encode(sha256(convert_to(public.news_canonical_jsonb((trace -> 'judgment'::text)), 'UTF8'::name)),
            'hex'::text)) AND (NOT (error_code IS DISTINCT FROM CASE WHEN (jsonb_typeof((trace #>
            '{judgment,fact}'::text[])) = 'null'::text) THEN 'liquidation_parse_failed'::text ELSE NULL::text END)))
            OR ((judgment_origin = 'degraded'::text) AND (editorial IS NULL) AND (model IS NULL) AND degraded AND
            (error_code IS NOT NULL) AND (program_version = ANY (ARRAY['news_semantic_program_v8'::text,
            'news_semantic_program_v9'::text, 'news_semantic_program_v10'::text])) AND
            ((program_version <> 'news_semantic_program_v10'::text) OR
            public.news_current_typed_assets_valid(verdict)) AND (policy_version = ANY
            (ARRAY['news_triage_policy_v11'::text,
            'news_triage_policy_v12'::text, 'news_triage_policy_v13'::text,
            'news_triage_policy_v14'::text])) AND (NOT
            (trace ? 'editorial_sha256'::text)) AND public.news_jsonb_exact_keys((trace -> 'judgment'::text),
            ARRAY['judgment_contract_version'::text, 'origin'::text, 'verdict'::text, 'decision'::text,
            'error_code'::text]) AND ((trace #>> '{judgment,judgment_contract_version}'::text[]) =
            judgment_contract_version) AND ((trace #>> '{judgment,origin}'::text[]) = judgment_origin) AND ((trace
            #> '{judgment,verdict}'::text[]) = verdict) AND public.news_current_decision_valid((trace #>
            '{judgment,decision}'::text[])) AND ((trace #>> '{judgment,decision,final}'::text[]) = final_decision)
            AND ((trace #>> '{judgment,decision,rule_baseline}'::text[]) = rule_baseline_decision) AND (NOT ((trace
            #>> '{judgment,decision,override_rule}'::text[]) IS DISTINCT FROM override_rule)) AND (NOT ((trace #>>
            '{judgment,decision,throttled_by}'::text[]) IS DISTINCT FROM throttled_by)) AND ((trace #>>
            '{judgment,error_code}'::text[]) = error_code) AND (scored_judgment_sha256 =
            encode(sha256(convert_to(public.news_canonical_jsonb((trace -> 'judgment'::text)), 'UTF8'::name)),
            'hex'::text))))) IS TRUE)
        )
        """
    )


def downgrade() -> None:
    raise RuntimeError("news_editorial_v3_policy_v14_forward_only")
