"""Typed News asset identity: program v10, the `reaction_v2` review projection (#651 §6.2).

Migration evidence:

- category: constraint rewrite plus one function and one view replacement, additive
- why_database_must_change: three things.
  `news_verdicts_current_judgment_check` pins the model and degraded branches to the literals
  `news_semantic_program_v8` / `news_semantic_program_v9`. #651 makes `TriageAsset.market_type` a required
  vocabulary value and shows the Program the catalogue's uncollapsed candidates, so `PROGRAM_VERSION`
  moves to v10 and without this revision the deployed workers cannot persist a single judgment.
  The same CHECK is also where the new asset contract becomes a database fact rather than a Python
  promise: `news_current_typed_assets_valid` requires every asset of a v10 verdict to name a market from
  the instrument-class vocabulary. It is scoped to v10 on purpose -- v8 and v9 rows carry `null` and free
  strings (`token`, `cex`, `private`) in that position, are audit truth, and are never rewritten, so a
  vocabulary predicate that applied to them would refuse the ADD CONSTRAINT scan outright.
  `news_review_task_source_v1` joins the Reaction ledger on the literal `reaction_v1`. #651 types quote and
  Reaction resolution by `(symbol, market_type)`, so an equity Event is measured against an equity contract
  or against nothing -- a different measurement, and therefore `REACTION_METRIC_VERSION` moves to
  `reaction_v2`. The view must read the current version, or the review desk would keep showing numbers
  produced by the untyped rule this cut exists to retire.
- current_source_revision: 20260912_0377
- minimum_supported_source_revision: 20260912_0377
- lock_level_and_order: maintenance stop; function creation, then ACCESS EXCLUSIVE constraint drop and add,
  then the view replacement, in one transaction
- statement_timeout: 120s set locally by the revision (the ADD CONSTRAINT scans every verdict row)
- lock_timeout: 5s set locally by the revision
- estimated_rows: `news_verdicts` under the 30-day retention, low tens of thousands
- estimated_bytes: catalog entries only; no heap rewrite, no index build
- rewrite_or_index_build: none; ADD CONSTRAINT validates existing rows in place, CREATE OR REPLACE VIEW
  rewrites a catalog entry
- preflight_and_maintenance_boundary: News workers stopped and the News queues drained
- archive_current_compatibility: compatible, in both halves.
  Every verdict written under v8 or v9 keeps validating: the two branches now accept any of the three
  program versions the `news_judgment_v2` contract has been written under, and the typed-asset predicate
  is inert for them. v8 and v9 judgments are audit truth of the previous epochs and are neither deleted
  nor rewritten; the worker never writes v9 again because `PROGRAM_VERSION` is the only value it emits.
  `NOT VALID` was rejected because `news_verdicts.published_at_ms` is updated in place, and an update
  would re-check a v9 row against a v10-only predicate.
  Every `reaction_v1` row stays exactly where it is and keeps its version. Nothing reads the two versions
  together: the planner writes `reaction_v2` and every current projection asks for the current version, so
  an Event measured before this revision reports no 1 h move on the review desk until the typed planner has
  measured it again. That is the honest answer -- the old number was produced against a contract that
  could resolve an equity Event to a same-name coin.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the v8/v9-only predicate and the `reaction_v1`
  view stay
- roll_forward_or_verified_backup_restore: correct with a new forward revision or restore the verified
  pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260915_0378
Revises: 20260912_0377
Create Date: 2026-09-15 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260915_0378"
down_revision = "20260912_0377"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    # The asset shape a v10 verdict must carry. `news_current_triage_verdict_valid` already requires the
    # exact key set and a string-or-null `market_type`, which is what lets every historical row keep
    # validating; this is the narrower question the typed contract asks, and it is asked only of the
    # generation that promises to answer it.
    op.execute(
        """
        CREATE FUNCTION public.news_current_typed_assets_valid(value jsonb) RETURNS boolean
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
            AS $_$
          SELECT NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(COALESCE(value -> 'assets', '[]'::jsonb)) asset
                    WHERE asset ->> 'market_type' IS NULL
                       OR asset ->> 'market_type' NOT IN (
                            'crypto','equity','commodity','index','fx','pre_ipo','unknown'
                          )
                 )
        $_$
        """
    )

    # One CHECK holds all four judgment origins, so the whole predicate is restated from `20260903_0358`.
    # The OI and liquidation branches are byte-identical to it; the model and degraded branches gain
    # `news_semantic_program_v10` in their program-version list and the v10-scoped typed-asset predicate.
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
            'news_triage_policy_v12'::text, 'news_triage_policy_v13'::text])) AND
            public.news_current_model_editorial_valid(editorial) AND ((trace ->> 'editorial_sha256'::text) =
            (editorial ->> 'editorial_sha256'::text)) AND (scored_judgment_sha256 =
            encode(sha256(convert_to(public.news_canonical_jsonb(jsonb_build_object('judgment_contract_version',
            judgment_contract_version, 'verdict', verdict, 'editorial', editorial, 'verdict_sha256', (trace ->>
            'verdict_sha256'::text))), 'UTF8'::name)), 'hex'::text))) OR ((judgment_origin = 'oi'::text) AND
            (editorial IS NULL) AND (model IS NULL) AND (NOT degraded) AND (program_version =
            'news_oi_signal_v3'::text) AND (policy_version = ANY (ARRAY['news_triage_policy_v11'::text,
            'news_triage_policy_v12'::text, 'news_triage_policy_v13'::text])) AND
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
            'news_triage_policy_v12'::text, 'news_triage_policy_v13'::text])) AND (NOT
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

    # The review-task projection, restated from `20260904_0363` with its Reaction join moved to the
    # current measurement version. Nothing else in the definition changes.
    op.execute(
        """
        CREATE OR REPLACE VIEW public.news_review_task_source_v1 WITH (security_barrier = 'true') AS
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
           FROM public.news_events e
             JOIN LATERAL (
                   SELECT x.event_id,
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
                    WHERE x.event_id = e.event_id
                      AND x.stage = 'triage'
                      AND x.judgment_contract_version = 'news_judgment_v2'
                      AND x.judgment_origin = 'model'
                    ORDER BY x.created_at_ms DESC
                    LIMIT 1
             ) v ON true
             JOIN LATERAL (
                   SELECT x.event_id,
                          x.evidence_version,
                          x.focus_fact_id,
                          x.evidence_sha256,
                          x.provenance,
                          x.release_eligible,
                          x.snapshot,
                          x.created_at_ms
                     FROM public.news_event_evidence_snapshots x
                    WHERE x.event_id = e.event_id
                      AND x.evidence_version = v.evidence_version
             ) s
               ON (s.provenance = 'observed'
                   AND s.release_eligible
                   AND (s.snapshot ->> 'schema_version') = 'news_event_evidence_v3'
                   AND s.evidence_version = v.evidence_version
                   AND s.evidence_sha256 = v.evidence_sha256
                   AND s.focus_fact_id = v.focus_fact_id)
             LEFT JOIN public.news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
             LEFT JOIN LATERAL (
                   SELECT max(abs(x.return_1h_bps)) AS max_abs_return_1h_bps
                     FROM public.news_event_reactions x
                    WHERE x.event_id = e.event_id
                      AND x.metric_version = 'reaction_v2'
                      AND x.is_primary
             ) reaction ON true
          WHERE e.event_kind = 'news'
        """
    )


def downgrade() -> None:
    raise RuntimeError("news_program_v10_typed_assets_forward_only")
