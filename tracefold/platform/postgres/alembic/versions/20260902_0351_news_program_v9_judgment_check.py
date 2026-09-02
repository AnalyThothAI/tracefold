"""Open the News judgment CHECK to `news_semantic_program_v9` and the review provenance to blind drafts (#501).

Migration evidence:

- category: constraint and validator rewrite, additive
- why_database_must_change: `news_verdicts_current_judgment_check` pins the model and degraded branches to
  the literal `news_semantic_program_v8`. #501 splits taxonomy into a third Predictor and the Program
  version moves to v9, so without this revision the deployed workers cannot persist a single judgment.
  `news_current_review_taxonomy_provenance_valid` enumerates the exact `taxonomy_review` keys, so the
  blind dual drafts (`drafts`) an accepted review now carries would violate
  `news_reviews_current_contract_check`.
- current_source_revision: 20260902_0350
- minimum_supported_source_revision: 20260902_0350
- lock_level_and_order: maintenance stop; function replacement, then ACCESS EXCLUSIVE constraint drop and
  add, in one transaction
- statement_timeout: 120s set locally by the revision (the ADD CONSTRAINT scans every verdict row)
- lock_timeout: 5s set locally by the revision
- estimated_rows: `news_verdicts` under the 30-day retention and the 08-30 `0336` genesis, low tens of
  thousands; `news_reviews` low hundreds
- estimated_bytes: catalog entries only; no heap rewrite, no index build
- rewrite_or_index_build: none; ADD CONSTRAINT validates existing rows in place
- preflight_and_maintenance_boundary: News workers stopped and the News queues drained
- archive_current_compatibility: compatible. Every row written under v8 keeps validating: the two
  branches now accept either of the two program versions the `news_judgment_v2` contract has been written
  under. v8 judgments are audit truth of the previous epoch and are neither deleted nor rewritten; the
  worker never writes v8 again because `PROGRAM_VERSION` is the only value it emits. `NOT VALID` was
  rejected because `news_verdicts.published_at_ms` is updated in place, and an update would re-check a v8
  row against a v9-only predicate. Every existing review keeps validating too: the provenance validator
  accepts the key set with or without `drafts`, and `drafts` may only be an object of exactly two
  four-axis model taxonomies on a `model_draft` label.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the v8-only predicate stays
- roll_forward_or_verified_backup_restore: correct with a new forward revision or restore the verified
  pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260902_0351
Revises: 20260902_0350
Create Date: 2026-09-02 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260902_0351"
down_revision = "20260902_0350"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    # The four model-owned axes alone (`ModelTaxonomyV1`): a blind draft carries no version, codebook or
    # code-owned source authority. Same label sets as `news_current_review_taxonomy_valid`.
    op.execute(
        """
        CREATE FUNCTION public.news_current_model_taxonomy_valid(value jsonb) RETURNS boolean
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
            AS $_$
          SELECT news_jsonb_exact_keys(value, ARRAY[
                   'subject_codes','event_family','change_state','assertion_status'
                 ])
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
        $_$
        """
    )

    # `drafts` is the one new key (#501 D8): absent or null on every review written before this revision
    # and on every human label; on a model-drafted label it is exactly two blind four-axis labels keyed by
    # drafter model. Everything else in the validator is byte-identical to `20260831_0340`.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.news_current_review_taxonomy_provenance_valid(value jsonb)
            RETURNS boolean
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
            AS $_$
          SELECT (news_jsonb_exact_keys(value, ARRAY[
                   'label_source','draft_author','review_role','adjudicates_review_id','draft_taxonomy'
                 ])
                 OR news_jsonb_exact_keys(value, ARRAY[
                   'label_source','draft_author','review_role','adjudicates_review_id','draft_taxonomy','drafts'
                 ]))
             AND value ->> 'label_source' IN ('human','model_draft')
             AND jsonb_typeof(value -> 'draft_author') = 'string'
             AND length(value ->> 'draft_author') <= 128
             AND value ->> 'review_role' IN ('primary','adjudication')
             AND jsonb_typeof(value -> 'adjudicates_review_id') = 'string'
             AND length(value ->> 'adjudicates_review_id') <= 64
             AND (jsonb_typeof(value -> 'draft_taxonomy') = 'null'
                  OR news_current_review_taxonomy_valid(value -> 'draft_taxonomy'))
             AND (value -> 'drafts' IS NULL
                  OR jsonb_typeof(value -> 'drafts') = 'null'
                  OR (jsonb_typeof(value -> 'drafts') = 'object'
                      AND (SELECT count(*) FROM jsonb_object_keys(value -> 'drafts')) = 2
                      AND (SELECT bool_and(news_current_model_taxonomy_valid(draft))
                             FROM jsonb_each(value -> 'drafts') AS drafts(model, draft))))
             AND CASE WHEN value ->> 'label_source' = 'model_draft'
                      THEN btrim(value ->> 'draft_author') <> ''
                      ELSE value ->> 'draft_author' = ''
                           AND jsonb_typeof(value -> 'draft_taxonomy') = 'null'
                           AND (value -> 'drafts' IS NULL OR jsonb_typeof(value -> 'drafts') = 'null') END
             AND CASE WHEN value ->> 'review_role' = 'adjudication'
                      THEN value ->> 'adjudicates_review_id' <> ''
                      ELSE value ->> 'adjudicates_review_id' = '' END
        $_$
        """
    )

    # One CHECK holds all four judgment origins, so the whole predicate is restated from `20260901_0344`.
    # The OI and liquidation branches are byte-identical to it; the model and degraded branches change
    # only their program-version literal, from `= 'news_semantic_program_v8'` to
    # `= ANY (ARRAY['news_semantic_program_v8', 'news_semantic_program_v9'])`.
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
            'news_semantic_program_v9'::text])) AND (policy_version = 'news_triage_policy_v11'::text) AND
            public.news_current_model_editorial_valid(editorial) AND ((trace ->> 'editorial_sha256'::text) =
            (editorial ->> 'editorial_sha256'::text)) AND (scored_judgment_sha256 =
            encode(sha256(convert_to(public.news_canonical_jsonb(jsonb_build_object('judgment_contract_version',
            judgment_contract_version, 'verdict', verdict, 'editorial', editorial, 'verdict_sha256', (trace ->>
            'verdict_sha256'::text))), 'UTF8'::name)), 'hex'::text))) OR ((judgment_origin = 'oi'::text) AND
            (editorial IS NULL) AND (model IS NULL) AND (NOT degraded) AND (program_version =
            'news_oi_signal_v3'::text) AND (policy_version = 'news_triage_policy_v11'::text) AND
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
            'news_semantic_program_v9'::text])) AND (policy_version = 'news_triage_policy_v11'::text) AND (NOT
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
    raise RuntimeError("news_program_v9_judgment_check_forward_only")
