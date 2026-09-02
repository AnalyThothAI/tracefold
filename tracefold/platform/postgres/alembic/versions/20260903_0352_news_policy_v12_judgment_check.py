"""Open the News judgment CHECK to `news_triage_policy_v12` (#504 PR-A).

Migration evidence:

- category: constraint rewrite, additive
- why_database_must_change: `news_verdicts_current_judgment_check` pins the model, OI and degraded branches
  to the literal `news_triage_policy_v11`. Policy v12 adds the per-storyline budget and the escalate
  corroboration rule to `decide()`, `TRIAGE_POLICY_VERSION` moves to v12, and every model, OI and degraded
  verdict is written under it, so without this revision the deployed workers cannot persist a single
  judgment. The `:budget` withhold key needs no change: the constraint only ties a `:seen` suffix to
  `seen_scope = 'all'`.
- current_source_revision: 20260902_0351
- minimum_supported_source_revision: 20260902_0351
- lock_level_and_order: maintenance stop; ACCESS EXCLUSIVE constraint drop and add, in one transaction
- statement_timeout: 120s set locally by the revision (the ADD CONSTRAINT scans every verdict row)
- lock_timeout: 5s set locally by the revision
- estimated_rows: `news_verdicts` under the 30-day retention and the 08-30 `0336` genesis, low tens of
  thousands
- estimated_bytes: catalog entries only; no heap rewrite, no index build
- rewrite_or_index_build: none; ADD CONSTRAINT validates existing rows in place
- preflight_and_maintenance_boundary: News workers stopped and the News queues drained
- archive_current_compatibility: compatible. Every row written under v11 keeps validating: the three
  branches now accept either of the two policy versions the `news_judgment_v2` contract has been written
  under, exactly as `0351` did for program v8/v9. v11 judgments are audit truth of the previous epoch and
  are neither deleted nor rewritten; the worker never writes v11 again because `TRIAGE_POLICY_VERSION` is
  the only value it emits. `NOT VALID` was rejected because `news_verdicts.published_at_ms` is updated in
  place, and an update would re-check a v11 row against a v12-only predicate. Everything else in the
  predicate is byte-identical to `0351`.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the v11-only predicate stays
- roll_forward_or_verified_backup_restore: correct with a new forward revision or restore the verified
  pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260903_0352
Revises: 20260902_0351
Create Date: 2026-09-03 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260903_0352"
down_revision = "20260902_0351"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    # One CHECK holds all four judgment origins, so the whole predicate is restated from `20260902_0351`.
    # The liquidation branch is byte-identical to it; the model, OI and degraded branches change only their
    # policy-version literal, from `= 'news_triage_policy_v11'` to
    # `= ANY (ARRAY['news_triage_policy_v11', 'news_triage_policy_v12'])`.
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
            'news_semantic_program_v9'::text])) AND (policy_version = ANY (ARRAY['news_triage_policy_v11'::text,
            'news_triage_policy_v12'::text])) AND
            public.news_current_model_editorial_valid(editorial) AND ((trace ->> 'editorial_sha256'::text) =
            (editorial ->> 'editorial_sha256'::text)) AND (scored_judgment_sha256 =
            encode(sha256(convert_to(public.news_canonical_jsonb(jsonb_build_object('judgment_contract_version',
            judgment_contract_version, 'verdict', verdict, 'editorial', editorial, 'verdict_sha256', (trace ->>
            'verdict_sha256'::text))), 'UTF8'::name)), 'hex'::text))) OR ((judgment_origin = 'oi'::text) AND
            (editorial IS NULL) AND (model IS NULL) AND (NOT degraded) AND (program_version =
            'news_oi_signal_v3'::text) AND (policy_version = ANY (ARRAY['news_triage_policy_v11'::text,
            'news_triage_policy_v12'::text])) AND
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
            'news_semantic_program_v9'::text])) AND (policy_version = ANY (ARRAY['news_triage_policy_v11'::text,
            'news_triage_policy_v12'::text])) AND (NOT
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
    raise RuntimeError("news_policy_v12_judgment_check_forward_only")
