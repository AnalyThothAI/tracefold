"""Hard-cut the News open-interest lane from a notification rule to a stored fact.

Migration evidence:

- category: destructive hard-cut
- why_database_must_change: the judgment CHECK is a second implementation of the retired push
  rule -- it recomputes `whale_ratio_below_threshold` / `beyond_window_rank` /
  `opening_move_with_whale_concentration` from `trace.oi_signal.policy` and requires
  `program_version = 'news_oi_signal_v2'`. #458 removes that rule, so the constraint has to state
  the new one (`stored` or `oi_parse_failed`, always `drop`) or the lane cannot write at all.
- current_source_revision: 20260901_0343
- minimum_supported_source_revision: 20260901_0343
- lock_level_and_order: maintenance stop; DELETE, then ACCESS EXCLUSIVE column drop and
  constraint/function DDL in one transaction
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: `news_verdicts` OI rows are one lane's ~190/day under a 30-day retention and the
  08-30 `0336` genesis, so low thousands; `news_oi_signals` is the same order
- estimated_bytes: one integer column drop is metadata-only; the constraint and function are catalog
  entries. The DELETE rewrites only the OI slice of `news_verdicts`.
- rewrite_or_index_build: no heap rewrite, no index build; `DROP COLUMN` marks the attribute dropped
- preflight_and_maintenance_boundary: News workers stopped and the News queues drained, so no
  Triage handler is mid-settlement while the judgment contract changes
- archive_current_compatibility: **not compatible, by design.** Every `judgment_origin = 'oi'` verdict
  written under `news_oi_signal_v2` is deleted rather than grandfathered. Those rows carry a
  `program_sha256` over four `news.oi` thresholds that no longer exist, and no honest form of the new
  constraint accepts them -- the exact-key list, the rule vocabulary and the decision all changed. The
  provider frames (`news_oi_signals`) and the deliveries that really happened are kept: they record
  measurements and messages, not the retired program's output. Their Events therefore read as
  telemetry frames with no verdict, which is a state the feed already renders.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the lane keeps the v2 contract
- roll_forward_or_verified_backup_restore: correct with a new forward revision or restore the
  verified pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260901_0344
Revises: 20260901_0343
Create Date: 2026-09-01 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0344"
down_revision = "20260901_0343"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    # Retract the retired program's output. See `archive_current_compatibility` above for why this
    # cannot be a grandfather clause: the v2 rows attest thresholds the code no longer has.
    op.execute("DELETE FROM news_verdicts WHERE judgment_origin = 'oi'")

    # The rank a frame took in the push queue, and the push queue.
    op.execute("ALTER TABLE news_oi_signals DROP COLUMN rank_in_window")

    # `rank_semantics` and the four-threshold `policy` object leave the trace with the rule that
    # produced them. Everything the provider proves about *how* a frame was measured stays.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.news_current_oi_metadata_valid(value jsonb, parsed boolean)
        RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
        AS $_$
          SELECT CASE WHEN parsed THEN
            news_jsonb_exact_keys(value, ARRAY[
              'parsed','source_strategy_id','source_contract_version','measurement_window_ms',
              'source_contract_rule','parser_version','source_classifier_version'
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
        $_$
        """
    )

    # One CHECK holds all four judgment origins, so the whole predicate is restated. The model,
    # liquidation and degraded branches are byte-identical to `20260831_0340`; the OI branch is the
    # only edit, and it now says what the lane actually does: one of two rule names, always `drop`,
    # with the judgment sha still bound to the atom.
    op.execute("ALTER TABLE news_verdicts DROP CONSTRAINT news_verdicts_current_judgment_check")
    op.execute(
        """
        ALTER TABLE news_verdicts
        ADD CONSTRAINT news_verdicts_current_judgment_check CHECK (
            (((judgment_contract_version IS NOT NULL) AND (judgment_origin IS NOT NULL) AND (judgment_contract_version
            = 'news_judgment_v2'::text) AND (judgment_origin = ANY (ARRAY['model'::text, 'oi'::text,
            'liquidation'::text, 'degraded'::text])) AND (stage = 'triage'::text) AND
            public.news_current_triage_verdict_valid(verdict) AND (scored_judgment_sha256 ~ '^[0-9a-f]{64}$'::text)
            AND (runtime_manifest_sha ~ '^[0-9a-f]{64}$'::text) AND (program_version IS NOT NULL) AND (program_sha256
            ~ '^[0-9a-f]{64}$'::text) AND (evidence_version >= 1) AND (evidence_sha256 ~ '^[0-9a-f]{64}$'::text) AND
            (focus_fact_id IS NOT NULL) AND (focus_fact_id <> ''::text) AND (seen_scope = ANY (ARRAY[''::text,
            'all'::text])) AND ((throttled_by IS NULL) OR ("right"(throttled_by, 5) <> (chr(58) || 'seen'::text)) OR
            (seen_scope = 'all'::text)) AND (NOT (trace ? 'type'::text)) AND
            public.news_jsonb_forbidden_keys_absent(trace, ARRAY['event_type'::text, 'event_type_zh'::text,
            'title_zh'::text, 'actionable'::text, 'model_decision'::text, 'novelty_defaulted'::text,
            'provider_cost_usd'::text, 'legacy_label'::text, 'legacy_event_type'::text,
            'project_legacy_event_type'::text, 'unclear_push_event_types'::text, 'display_title'::text, 'sym'::text,
            'm'::text, 'dir'::text, 'family'::text]) AND public.news_current_told_trace_valid((trace -> 'told'::text))
            AND public.news_jsonb_int64_valid((trace -> 'told_count'::text)) AND (((trace ->>
            'told_count'::text))::numeric = (jsonb_array_length((trace -> 'told'::text)))::numeric) AND ((trace ->>
            'judgment_contract_version'::text) = judgment_contract_version) AND ((trace ->> 'judgment_origin'::text) =
            judgment_origin) AND ((trace ->> 'judgment_sha256'::text) = scored_judgment_sha256) AND ((trace ->>
            'verdict_sha256'::text) = encode(sha256(convert_to(public.news_canonical_jsonb(verdict), 'UTF8'::name)),
            'hex'::text)) AND ((trace ->> 'evidence_version'::text) = (evidence_version)::text) AND ((trace ->>
            'evidence_sha256'::text) = evidence_sha256) AND ((trace ->> 'focus_fact_id'::text) = focus_fact_id) AND
            ((trace ->> 'runtime_manifest_sha'::text) = runtime_manifest_sha) AND ((trace ->> 'program_version'::text)
            = program_version) AND ((trace ->> 'program_sha256'::text) = program_sha256) AND (((judgment_origin =
            'model'::text) AND (NOT degraded) AND (error_code IS NULL) AND (model IS NOT NULL) AND (program_version =
            'news_semantic_program_v8'::text) AND (policy_version = 'news_triage_policy_v11'::text) AND
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
            '{judgment,signal}'::text[])) = 'object'::text)) AND (jsonb_typeof((trace #> '{judgment,rule}'::text[])) =
            'string'::text) AND public.news_current_decision_valid((trace #> '{judgment,decision}'::text[])) AND
            ((trace #>> '{judgment,decision,final}'::text[]) = final_decision) AND ((trace #>>
            '{judgment,decision,rule_baseline}'::text[]) = rule_baseline_decision) AND (NOT ((trace #>>
            '{judgment,decision,override_rule}'::text[]) IS DISTINCT FROM override_rule)) AND (NOT ((trace #>>
            '{judgment,decision,throttled_by}'::text[]) IS DISTINCT FROM throttled_by)) AND ((trace #>>
            '{judgment,rule}'::text[]) = override_rule) AND ((trace #>> '{judgment,decision,throttled_by}'::text[]) IS
            NULL) AND ((trace #>> '{judgment,rule}'::text[]) = CASE WHEN (jsonb_typeof((trace #>
            '{judgment,signal}'::text[])) = 'null'::text) THEN 'oi_parse_failed'::text ELSE 'stored'::text END) AND
            (final_decision = 'drop'::text) AND (rule_baseline_decision = 'drop'::text) AND (scored_judgment_sha256 =
            encode(sha256(convert_to(public.news_canonical_jsonb((trace -> 'judgment'::text)), 'UTF8'::name)),
            'hex'::text)) AND (NOT (error_code IS DISTINCT FROM CASE WHEN (jsonb_typeof((trace #>
            '{judgment,signal}'::text[])) = 'null'::text) THEN 'oi_parse_failed'::text ELSE NULL::text END))) OR
            ((judgment_origin = 'liquidation'::text) AND (editorial IS NULL) AND (model IS NULL) AND (NOT degraded)
            AND (program_version = 'news_liquidation_fact_v2'::text) AND (policy_version =
            'news_liquidation_policy_v2'::text) AND public.news_jsonb_exact_keys((trace -> 'judgment'::text),
            ARRAY['judgment_contract_version'::text, 'origin'::text, 'verdict'::text, 'fact'::text, 'rule'::text,
            'decision'::text]) AND ((trace #>> '{judgment,judgment_contract_version}'::text[]) =
            judgment_contract_version) AND ((trace #>> '{judgment,origin}'::text[]) = judgment_origin) AND ((trace #>
            '{judgment,verdict}'::text[]) = verdict) AND ((jsonb_typeof((trace #> '{judgment,fact}'::text[])) =
            'null'::text) OR (public.news_current_liquidation_fact_valid((trace #> '{judgment,fact}'::text[])) IS
            TRUE)) AND public.news_current_liquidation_metadata_valid((trace -> 'liquidation'::text),
            (jsonb_typeof((trace #> '{judgment,fact}'::text[])) = 'object'::text)) AND (jsonb_typeof((trace #>
            '{judgment,rule}'::text[])) = 'string'::text) AND ((trace #>> '{judgment,rule}'::text[]) <> ''::text) AND
            public.news_current_decision_valid((trace #> '{judgment,decision}'::text[])) AND ((trace #>>
            '{judgment,decision,final}'::text[]) = final_decision) AND ((trace #>>
            '{judgment,decision,rule_baseline}'::text[]) = rule_baseline_decision) AND (NOT ((trace #>>
            '{judgment,decision,override_rule}'::text[]) IS DISTINCT FROM override_rule)) AND (NOT ((trace #>>
            '{judgment,decision,throttled_by}'::text[]) IS DISTINCT FROM throttled_by)) AND ((trace #>>
            '{judgment,rule}'::text[]) = override_rule) AND ((trace #>> '{judgment,decision,throttled_by}'::text[]) IS
            NULL) AND CASE WHEN (jsonb_typeof((trace #> '{judgment,fact}'::text[])) = 'null'::text) THEN (((trace #>>
            '{judgment,rule}'::text[]) = 'liquidation_parse_failed'::text) AND (final_decision = 'drop'::text) AND
            (rule_baseline_decision = 'drop'::text)) ELSE (((trace #>> '{judgment,rule}'::text[]) =
            'liquidation_fact_only'::text) AND (final_decision = 'push'::text) AND (rule_baseline_decision =
            'push'::text)) END AND (scored_judgment_sha256 =
            encode(sha256(convert_to(public.news_canonical_jsonb((trace -> 'judgment'::text)), 'UTF8'::name)),
            'hex'::text)) AND (NOT (error_code IS DISTINCT FROM CASE WHEN (jsonb_typeof((trace #>
            '{judgment,fact}'::text[])) = 'null'::text) THEN 'liquidation_parse_failed'::text ELSE NULL::text END)))
            OR ((judgment_origin = 'degraded'::text) AND (editorial IS NULL) AND (model IS NULL) AND degraded AND
            (error_code IS NOT NULL) AND (program_version = 'news_semantic_program_v8'::text) AND (policy_version =
            'news_triage_policy_v11'::text) AND (NOT (trace ? 'editorial_sha256'::text)) AND
            public.news_jsonb_exact_keys((trace -> 'judgment'::text), ARRAY['judgment_contract_version'::text,
            'origin'::text, 'verdict'::text, 'decision'::text, 'error_code'::text]) AND ((trace #>>
            '{judgment,judgment_contract_version}'::text[]) = judgment_contract_version) AND ((trace #>>
            '{judgment,origin}'::text[]) = judgment_origin) AND ((trace #> '{judgment,verdict}'::text[]) = verdict)
            AND public.news_current_decision_valid((trace #> '{judgment,decision}'::text[])) AND ((trace #>>
            '{judgment,decision,final}'::text[]) = final_decision) AND ((trace #>>
            '{judgment,decision,rule_baseline}'::text[]) = rule_baseline_decision) AND (NOT ((trace #>>
            '{judgment,decision,override_rule}'::text[]) IS DISTINCT FROM override_rule)) AND (NOT ((trace #>>
            '{judgment,decision,throttled_by}'::text[]) IS DISTINCT FROM throttled_by)) AND ((trace #>>
            '{judgment,error_code}'::text[]) = error_code) AND (scored_judgment_sha256 =
            encode(sha256(convert_to(public.news_canonical_jsonb((trace -> 'judgment'::text)), 'UTF8'::name)),
            'hex'::text))))) IS TRUE)
        )
        """
    )


def downgrade() -> None:
    raise RuntimeError("news_oi_push_cut_forward_only")
