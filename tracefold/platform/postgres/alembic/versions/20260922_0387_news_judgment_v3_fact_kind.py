"""News judgment v3, editorial v4, review v8 and triage policy v16: `fact_kind` replaces relevance (#675 §1).

Migration evidence:

- category: eight function replacements, three new functions, one view replacement, one constraint
  rewrite; additive in every predicate, since each one keeps stating the shape the rows already in the
  ledger carry
- why_database_must_change: five things, all of them the same cut.
  `news_current_triage_verdict_valid` enumerates the verdict's keys exactly and requires `magnitude`,
  `confidence` and `audience`. #675 §1 deletes `TradeRelevanceV1`, `magnitude` and `audience` from
  `EventSemantics` -- seven days of 8950 judgments collapsed all eight fields into one bit, and the seed
  had to teach a threshold to make them answerable -- and adds `fact_kind` (a closed ten-value
  observation of the text) and `evidence_ref`. Without this revision the deployed workers cannot persist
  a single verdict.
  `news_current_model_editorial_valid` requires a `relevance` object on every model editorial. The
  envelope keeps the two code facts it exists for (`source_authority`, the taxonomy Predictor's answer)
  and loses the seven model-owned codes, so `EDITORIAL_CONTRACT_VERSION` moves to `news_editorial_v4`.
  `news_verdicts_current_judgment_check` pins `judgment_contract_version = 'news_judgment_v2'`, the
  policy list at `news_triage_policy_v15` and the program list at `news_semantic_program_v12`.
  `decide()` becomes the whole decision table (v16), the EventSemantics Signature changes shape
  (`news_semantic_program_v13`), and the verdict/editorial pair is a new contract (`news_judgment_v3`).
  `news_current_told_trace_valid` requires a `magnitude` on every told entry. The told ledger projects
  the verdict, and the verdict has no magnitude to project.
  The `news_review_v7` rubric asks a reviewer to label `magnitude` and six `trade_*` dimensions and to
  correct them in `expected`. Those are labels for fields the Program no longer produces, so the rubric
  is `news_review_v8`: `fact_kind` in their place, and an `expected` block of three fields.
  `news_review_task_source_v1` picks the newest *model* verdict of an Event and pins
  `judgment_contract_version = 'news_judgment_v2'`, so under v3 it answers no row for any Event the
  worker has judged and `ReviewDesk.open` reports `insufficient_evidence` for all of them.
  `news_current_review_selection_valid` pins `selection_version = 'news_review_sampler_v3'`, and
  `_selection` writes `news_review_sampler_v4` from here on because six of its strata are deleted with
  the relevance codes they read; without this revision no event review can be stored.
  `news_current_review_source_guard` fires only for the rubric versions named in it, so a `news_review_v8`
  row would otherwise be written without its task source ever being checked -- the exact weakening `0380`
  named when it added v7 to the same list.
- current_source_revision: 20260922_0386
- minimum_supported_source_revision: 20260922_0386
- lock_level_and_order: maintenance stop; function creation and replacement, then the view replacement
  (ACCESS EXCLUSIVE on one catalog entry, no row touched), then one ACCESS EXCLUSIVE constraint drop
  and add on `news_verdicts`, in one transaction, with no other table touched
- statement_timeout: 1800s set locally by the revision. ADD CONSTRAINT re-validates every `news_verdicts`
  row through the canonical-JSON sha256 predicate; the 2026-09-15 production run of the same predicate in
  `0379` over 20,453 rows exceeded the 120s the pre-0378 CHECK restatements used.
- lock_timeout: 5s set locally by the revision
- estimated_rows: `news_verdicts` under the 30-day retention, low tens of thousands; `news_reviews` is
  not re-validated, because no constraint on it is dropped and added
- estimated_bytes: catalog entries only; no heap rewrite, no index build
- rewrite_or_index_build: none; ADD CONSTRAINT validates existing rows in place
- preflight_and_maintenance_boundary: News workers stopped and the News queues drained by the canonical
  migration gate. The revision additionally refuses to run against a constraint it does not recognize: a
  definition without `news_triage_policy_v15`, or one that already names `news_judgment_v3`, raises
  rather than rewriting a predicate this revision did not read.
- archive_current_compatibility: compatible, in five halves, and nothing is rewritten.
  Every verdict written under `news_judgment_v2` keeps validating: the verdict predicate states both key
  sets completely and `news_current_verdict_contract_shape_valid` binds each one to the contract version
  that wrote it, so a v2 row is still required to carry `magnitude`/`audience` and a v3 row is required
  to carry `fact_kind`/`evidence_ref`. Those rows are audit truth addressed by `scored_judgment_sha256`;
  a migration of the column would invalidate every judgment identity in the ledger.
  Every editorial written under v2 or v3 keeps validating: the predicate branches on the contract version
  the document itself declares and states all three shapes. `storage.decisions.editorial_read_shape` is
  the read boundary that converts them, and it drops `relevance` from a v3 row rather than publishing a
  judgment the Program no longer makes.
  Every trace written before this revision keeps validating: the told predicate admits an entry with the
  16-key shape carrying `magnitude` *and* the 15-key shape without it, exactly as `0379` admits a
  reviewer's taxonomy with or without the authority.
  Every verdict written under policy v11-v15 and program v8-v12 keeps validating: the three branches keep
  every version they already admit.
  Every accepted review keeps validating: `news_review_v7` rows keep the v7 predicate, byte for byte, and
  `news_review_v8` is a second predicate beside it rather than a loosened first. A v8 row carries
  `reader_contract_v3`, the same contract v7 was written against, because the reader's duplicate-evidence
  promise did not change in this cut -- only the questions the reviewer is asked did.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the v2-only judgment contract, the
  relevance-requiring editorial predicate, the magnitude-requiring told predicate, the v15 policy list,
  the v12 program list, the v2-only review-task view, the v3-only sampler predicate and the v7-only
  rubric stay
- roll_forward_or_verified_backup_restore: correct with a new forward revision or restore the verified
  pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260922_0387
Revises: 20260922_0386
Create Date: 2026-09-22 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260922_0387"
down_revision = "20260922_0386"
branch_labels = None
depends_on = None

_FACT_KINDS = (
    "'state_change','new_quantity','level_crossed','period_record','quantified_flow',"
    "'official_measure','statement','recap','schedule','promotion'"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '1800s'")

    # The verdict document, both shapes. The branch is the key set itself rather than a version argument,
    # because three callers validate a verdict they hold alone -- the CHECK, the review-payload guard and
    # the evidence trigger -- and only the CHECK knows which contract wrote the row. What binds the shape
    # to its contract is `news_current_verdict_contract_shape_valid`, below, which the CHECK calls beside
    # this one.
    op.execute(f"""
CREATE OR REPLACE FUNCTION public.news_current_triage_verdict_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT (
                   news_jsonb_exact_keys(value, ARRAY[
                     'novelty','restates','assets','direction','scope','fact_kind',
                     'evidence_ref','confidence','headline_zh','why_zh'
                   ])
                   OR news_jsonb_exact_keys(value, ARRAY[
                     'novelty','restates','assets','direction','scope','magnitude',
                     'confidence','audience','headline_zh','why_zh'
                   ])
                 )
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
             AND CASE WHEN value ? 'fact_kind' THEN
                   jsonb_typeof(value -> 'fact_kind') IN ('string','null')
                   AND (jsonb_typeof(value -> 'fact_kind') = 'null'
                        OR value ->> 'fact_kind' IN ({_FACT_KINDS}))
                   AND jsonb_typeof(value -> 'evidence_ref') = 'string'
                   AND length(value ->> 'evidence_ref') <= 64
                 ELSE
                   jsonb_typeof(value -> 'magnitude') = 'number'
                   AND (value ->> 'magnitude') ~ '^[0-3]$'
                   AND value ->> 'audience' IN ('crypto','us_equity','macro','none')
                 END
             AND jsonb_typeof(value -> 'confidence') = 'number'
             AND (value ->> 'confidence')::numeric BETWEEN 0 AND 1
             AND jsonb_typeof(value -> 'headline_zh') = 'string'
             AND length(value ->> 'headline_zh') BETWEEN 1 AND 60
             AND jsonb_typeof(value -> 'why_zh') = 'string'
             AND length(value ->> 'why_zh') <= 140
        $_$;
    """)  # noqa: S608 - the only interpolation is this module's own closed fact-kind vocabulary

    # Which verdict shape each judgment contract may carry, and what a *model* judgment owes on top of it.
    # Without this the two key sets would be interchangeable and a v3 worker could quietly write a v2
    # verdict under the new contract version. The model branch is the stricter one on purpose: the
    # degraded lane makes no observation of the text -- there is no model answer to record -- so its
    # `fact_kind` is JSON null and its `evidence_ref` is empty, and saying so here is what stops a null
    # from meaning "the model declined to answer" on a row where the model did answer.
    #
    # Deliberately not STRICT. A STRICT predicate returns NULL for a NULL argument, `... AND NULL` is NULL,
    # and a CHECK admits a row whose predicate is NULL -- so a row with no `judgment_origin` or no
    # `verdict` would have walked straight through the one clause written to refuse it. Every argument is
    # tested for NULL and a missing one is a refusal, not an abstention (#679 review 4).
    op.execute(f"""
CREATE FUNCTION public.news_current_verdict_contract_shape_valid(
    contract_version text, origin text, value jsonb
) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $_$
          SELECT contract_version IS NOT NULL AND origin IS NOT NULL AND value IS NOT NULL
             AND CASE contract_version
            WHEN 'news_judgment_v3' THEN
              value ? 'fact_kind' AND value ? 'evidence_ref'
              AND NOT (value ? 'magnitude') AND NOT (value ? 'audience')
              AND CASE WHEN origin = 'model' THEN
                    value ->> 'fact_kind' IN ({_FACT_KINDS})
                    AND btrim(value ->> 'evidence_ref') <> ''
                  ELSE
                    jsonb_typeof(value -> 'fact_kind') = 'null'
                    AND value ->> 'evidence_ref' = ''
                  END
            WHEN 'news_judgment_v2' THEN
              value ? 'magnitude' AND value ? 'audience'
              AND NOT (value ? 'fact_kind') AND NOT (value ? 'evidence_ref')
            ELSE false
          END
        $_$;
    """)

    # The `taxonomy_status` / `taxonomy` / `taxonomy_error_code` triple, which v3 and v4 state identically.
    # Extracted because it is the third copy of it: `0379` wrote it once for v3, and restating it a second
    # time for v4 is how a clause goes missing from one of two branches that are meant to be the same.
    op.execute("""
CREATE FUNCTION public.news_current_editorial_taxonomy_slot_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT CASE value ->> 'taxonomy_status'
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
        $_$;
    """)

    # The editorial document, restated from `0379` around one more version branch. `news_editorial_v4` is
    # what the worker writes: the code-owned authority, the taxonomy Predictor's answer, and nothing the
    # model said about the reader. v3 and v2 are stated completely because every judgment written before
    # this revision carries one of them, is audit truth, and is never rewritten.
    op.execute("""
CREATE OR REPLACE FUNCTION public.news_current_model_editorial_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT value ->> 'editorial_origin' = 'model'
             AND value ->> 'editorial_sha256' ~ '^[0-9a-f]{64}$'
             AND value ->> 'editorial_sha256' = encode(sha256(
                   convert_to(news_canonical_jsonb(value - 'editorial_sha256'), 'UTF8')), 'hex')
             AND CASE WHEN value ? 'relevance' THEN
                   news_jsonb_exact_keys(value -> 'relevance', ARRAY[
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
                 ELSE true END
             AND CASE value ->> 'editorial_contract_version'
               WHEN 'news_editorial_v4' THEN
                 news_jsonb_exact_keys(value, ARRAY[
                   'editorial_contract_version','editorial_origin','source_authority',
                   'taxonomy','taxonomy_status','taxonomy_error_code','editorial_sha256'
                 ])
                 AND value ->> 'source_authority' IN (
                       'regulatory_filing','issuer_first_party','reputable_secondary','unknown'
                     )
                 AND news_current_editorial_taxonomy_slot_valid(value)
               WHEN 'news_editorial_v3' THEN
                 news_jsonb_exact_keys(value, ARRAY[
                   'editorial_contract_version','editorial_origin','relevance','source_authority',
                   'taxonomy','taxonomy_status','taxonomy_error_code','editorial_sha256'
                 ])
                 AND value ->> 'source_authority' IN (
                       'regulatory_filing','issuer_first_party','reputable_secondary','unknown'
                     )
                 AND news_current_editorial_taxonomy_slot_valid(value)
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
    """)

    # The told ledger, with `magnitude` optional. A ledger entry projects the verdict it was written
    # from, so entries written before this revision carry a magnitude and entries written after it do
    # not. Both are stated; neither is rewritten.
    #
    # Restated from the predicate as `0384` left it -- not as `0350` wrote it. `0384` rewrote the stored
    # definition in place to make `assets` and `provenance_status` optional, so a fresh copy of `0350`'s
    # text would silently drop both keys and reject every told entry the current writer produces. The
    # `entry - 'assets' - 'provenance_status'` strip and the two clauses below `NOT (` come from `0384`.
    op.execute("""
CREATE OR REPLACE FUNCTION public.news_current_told_trace_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= 16
             AND NOT EXISTS (
                   SELECT 1
                     FROM jsonb_array_elements(value) WITH ORDINALITY AS told(entry, position)
                    WHERE NOT (
                      (
                        news_jsonb_exact_keys(entry - 'assets' - 'provenance_status', ARRAY[
                          'i','event_id','at_ms','ago_min','storyline_key','comparison_title',
                          'comparison_fingerprint','symbols','direction','headline_zh',
                          'why_zh','tier','similarity','history_scope','retrieval_reason'
                        ])
                        OR (
                          news_jsonb_exact_keys(entry - 'assets' - 'provenance_status', ARRAY[
                            'i','event_id','at_ms','ago_min','storyline_key','comparison_title',
                            'comparison_fingerprint','symbols','magnitude','direction','headline_zh',
                            'why_zh','tier','similarity','history_scope','retrieval_reason'
                          ])
                          AND news_jsonb_int64_valid(entry -> 'magnitude')
                          AND (entry ->> 'magnitude')::numeric BETWEEN 0 AND 3
                        )
                      )
                      AND (NOT entry ? 'assets' OR (
                        jsonb_typeof(entry -> 'assets') = 'array'
                        AND jsonb_array_length(entry -> 'assets') <= 6
                        AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(entry -> 'assets') asset
                          WHERE jsonb_typeof(asset) <> 'object'
                             OR NOT news_jsonb_exact_keys(asset, ARRAY['symbol','market_type'])
                             OR COALESCE(asset->>'symbol', '') = ''
                             OR COALESCE(asset->>'market_type', '') NOT IN
                                  ('crypto','equity','commodity','index','fx','pre_ipo','unknown'))
                      ))
                      AND (NOT entry ? 'provenance_status' OR entry->>'provenance_status' IN
                           ('delivery_bound','legacy_receipt_only'))
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
                        'recent','exact_fingerprint','canonical_asset_overlap','title_similarity'
                      )
                    )
                 )
        $$;
    """)

    # The v8 rubric's dimension map. A third predicate rather than a loosened second: the v7 one still
    # says what a v7 row promised, and this one says what a v8 row promises. `magnitude` and the six
    # `trade_*` dimensions are gone because the Program no longer produces the fields they labelled, and
    # `fact_kind` is the observation that replaced them.
    op.execute("""
CREATE FUNCTION public.news_current_review_v8_dimensions_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT jsonb_typeof(value) = 'object'
             AND value <> '{}'::jsonb
             AND news_jsonb_required_optional_keys(value, ARRAY[]::text[], ARRAY[
                   'factual_fidelity','headline_fidelity','asset_grounding','direction','fact_kind',
                   'why_support','why_value','timeliness','taxonomy_subject_codes','taxonomy_event_family',
                   'taxonomy_change_state','taxonomy_assertion_status'
                 ])
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_each(value) AS dimension(name, result)
                    WHERE jsonb_typeof(result) <> 'string'
                       OR result #>> '{}' NOT IN ('pass','fail','uncertain','not_applicable')
                 )
        $_$;
    """)

    # The v8 `expected` block: three fields, one per semantic dimension a reviewer can fail.
    op.execute(f"""
CREATE FUNCTION public.news_current_review_v8_expected_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$
          SELECT CASE jsonb_typeof(value)
            WHEN 'null' THEN true
            WHEN 'object' THEN
              news_jsonb_exact_keys(value, ARRAY['direction','assets','fact_kind'])
              AND EXISTS (SELECT 1 FROM jsonb_each(value) field WHERE field.value <> 'null'::jsonb)
              AND (jsonb_typeof(value -> 'direction') = 'null'
                   OR value ->> 'direction' IN ('bullish','bearish','neutral','unclear'))
              AND (jsonb_typeof(value -> 'fact_kind') = 'null'
                   OR value ->> 'fact_kind' IN ({_FACT_KINDS}))
              AND (jsonb_typeof(value -> 'assets') = 'null' OR (
                    jsonb_typeof(value -> 'assets') = 'array'
                    AND jsonb_array_length(value -> 'assets') <= 16
                    AND NOT EXISTS (
                      SELECT 1 FROM jsonb_array_elements(value -> 'assets') asset
                       WHERE NOT news_jsonb_exact_keys(asset, ARRAY['symbol','market_type','role'])
                          OR jsonb_typeof(asset -> 'market_type') <> 'string'
                          OR asset ->> 'market_type' NOT IN ('crypto','equity','commodity','index','fx',
                                                             'pre_ipo','unknown')
                          OR jsonb_typeof(asset -> 'symbol') <> 'string'
                          OR length(asset ->> 'symbol') NOT BETWEEN 1 AND 32
                          OR asset ->> 'role' NOT IN ('primary','mentioned')
                    )))
            ELSE false
          END
        $_$;
    """)  # noqa: S608 - the only interpolation is this module's own closed fact-kind vocabulary

    # The v8 event payload, restated from the v7 one as `0383` left it -- not as `0380` wrote it, which
    # is a different predicate: `0383` made the dimension map optional, admitted the `reviewed_source`
    # key, bounded `equivalent_targets`, moved the taxonomy rule from all-or-nothing to per-axis and
    # exempted taxonomy dimensions from the evidence-ref requirement. Three differences and no fourth:
    # it calls the two v8 predicates, and the ten-clause "gold only with fail" table is one rule over
    # the block's own keys. That table was ten copies of one sentence -- a correction may only be stated
    # for a dimension the reviewer failed -- and a copy naming a key the block no longer has would
    # compare NULL with a string, which `IS TRUE` reads as a refusal of every submission.
    op.execute("""
CREATE FUNCTION public.news_current_event_review_payload_v8_valid(
    value jsonb, expected_should_push text, expected_dimensions jsonb, expected_novelty jsonb,
    expected_first_bad_owner text, expected_evidence_refs jsonb, expected_correction text,
    expected_note text
) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $_$
  SELECT (
    news_jsonb_required_optional_keys(value, ARRAY[
      'kind','should_push','dimensions','novelty','first_bad_owner','evidence_refs',
      'expected','explanation','taxonomy','taxonomy_review','explanation_supervision',
      'expected_correction','note'
    ], ARRAY['reviewed_source'])
    AND value ->> 'kind' = 'event_rubric'
    AND jsonb_typeof(value -> 'should_push') IN ('string','null')
    AND CASE WHEN jsonb_typeof(value -> 'should_push') = 'null'
             THEN expected_should_push IS NULL
             ELSE value ->> 'should_push' = expected_should_push
                  AND expected_should_push IN
                      ('must_push','should_push','should_hold','must_hold','uncertain') END
    AND value -> 'dimensions' = expected_dimensions
    AND (news_current_review_v8_dimensions_valid(expected_dimensions)
         OR (expected_dimensions = '{}'::jsonb AND (expected_should_push IS NOT NULL
             OR jsonb_typeof(value -> 'novelty') = 'object'
             OR jsonb_typeof(value -> 'taxonomy') = 'object')))
    AND jsonb_typeof(value -> 'novelty') IN ('object','null')
    AND CASE WHEN jsonb_typeof(value -> 'novelty') = 'null'
             THEN expected_novelty = '{}'::jsonb
             ELSE value -> 'novelty' = expected_novelty
                  AND news_current_review_novelty_valid(expected_novelty - 'equivalent_targets')
                  AND (NOT (expected_novelty ? 'equivalent_targets')
                       OR news_jsonb_bounded_text_list_valid(
                         expected_novelty -> 'equivalent_targets', 32))
                  AND (expected_novelty ->> 'judgment' = 'restatement'
                       OR COALESCE(jsonb_array_length(expected_novelty -> 'equivalent_targets'), 0) = 0)
                  AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(expected_novelty -> 'equivalent_targets') target
                     WHERE length(target) > 128
                  ) END
    AND expected_first_bad_owner IS NOT NULL
    AND jsonb_typeof(value -> 'first_bad_owner') IN ('string','null')
    AND (jsonb_typeof(value -> 'first_bad_owner') = 'null'
         OR value ->> 'first_bad_owner' = expected_first_bad_owner)
    AND value -> 'evidence_refs' = expected_evidence_refs
    AND news_current_review_evidence_refs_valid(expected_evidence_refs)
    AND value ->> 'expected_correction' = expected_correction
    AND value ->> 'note' = expected_note
    AND news_current_review_v8_expected_valid(value -> 'expected')
    AND news_current_review_explanation_valid(value -> 'explanation')
    AND (jsonb_typeof(value -> 'taxonomy') = 'null'
         OR news_current_partial_review_taxonomy_valid(value -> 'taxonomy'))
    AND news_current_review_v7_taxonomy_provenance_valid(value -> 'taxonomy_review')
    AND NOT EXISTS (
      SELECT 1 FROM jsonb_object_keys(expected_dimensions) dimension
       WHERE dimension LIKE 'taxonomy_%'
         AND NOT COALESCE((value -> 'taxonomy') ? substr(dimension, 10), false)
    )
    AND (NOT (value ? 'reviewed_source') OR (
      jsonb_typeof(value -> 'reviewed_source') = 'object'
      AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(
          COALESCE(NULLIF(value #> '{expected,assets}', 'null'::jsonb), '[]'::jsonb)) asset
         WHERE NOT (asset ? 'market_type')
      )
    ))
    AND (jsonb_typeof(value -> 'explanation') = 'null'
         OR expected_dimensions ?| ARRAY[
              'why_support','why_value','factual_fidelity','headline_fidelity'
            ])
    AND value ->> 'explanation_supervision' =
        CASE WHEN jsonb_typeof(value -> 'explanation') <> 'null' THEN 'present'
             WHEN expected_dimensions ->> 'why_support' = 'fail' THEN 'pending'
             ELSE 'not_applicable' END
    AND (expected_should_push IS NULL
         OR expected_should_push NOT IN ('must_push','should_push')
         OR expected_dimensions ? 'timeliness')
    AND (NOT EXISTS (
           SELECT 1 FROM jsonb_each_text(expected_dimensions) dimension
            WHERE dimension.value = 'fail' AND dimension.key NOT LIKE 'taxonomy_%'
         ) OR jsonb_array_length(expected_evidence_refs) > 0)
    AND (jsonb_typeof(value -> 'expected') = 'null' OR NOT EXISTS (
           SELECT 1 FROM jsonb_each(value -> 'expected') AS correction(name, answer)
            WHERE answer <> 'null'::jsonb
              AND (expected_dimensions ->> CASE correction.name
                     WHEN 'assets' THEN 'asset_grounding' ELSE correction.name END)
                  IS DISTINCT FROM 'fail'
         ))
  ) IS TRUE
$_$;
    """)

    # All three rubric contracts, dispatched by the row's own version. Restated in full from `0380`
    # because a CHECK and a security-barrier view both read it and neither may see a half-written one.
    # v8 pairs with `reader_contract_v3`: this cut changed the questions a reviewer is asked, not the
    # duplicate-evidence promise the reader contract makes.
    op.execute("""
CREATE OR REPLACE FUNCTION public.news_current_review_valid(
    review_kind_value text, subject_kind_value text, rubric_version_value text,
    reader_contract_version_value text, event_id_value text, evidence_version_value integer,
    external_snapshot_id_value text, pairwise_case_id_value text, should_push_value text,
    dimensions_value jsonb, novelty_value jsonb, first_bad_owner_value text, evidence_refs_value jsonb,
    expected_correction_value text, note_value text, selection_value jsonb, payload_value jsonb,
    accepts_review_id_value text
) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
          SELECT (
            (rubric_version_value, reader_contract_version_value) IN (
              ('news_review_v6','reader_contract_v2'), ('news_review_v7','reader_contract_v3'),
              ('news_review_v8','reader_contract_v3')
            )
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
                  WHEN 'pairwise' THEN
                    should_push_value IS NULL
                    AND dimensions_value = '{}'::jsonb
                    AND novelty_value = '{}'::jsonb
                    AND first_bad_owner_value IS NULL
                    AND expected_correction_value = ''
                    AND news_current_pairwise_review_payload_valid(
                      payload_value, evidence_refs_value, note_value)
                  WHEN 'event' THEN CASE rubric_version_value
                    WHEN 'news_review_v6' THEN news_current_event_review_payload_valid(
                      payload_value, should_push_value, dimensions_value, novelty_value,
                      first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                    WHEN 'news_review_v7' THEN news_current_event_review_payload_v7_valid(
                      payload_value, should_push_value, dimensions_value, novelty_value,
                      first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                    ELSE news_current_event_review_payload_v8_valid(
                      payload_value, should_push_value, dimensions_value, novelty_value,
                      first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                  END
                  WHEN 'external_miss' THEN CASE rubric_version_value
                    WHEN 'news_review_v6' THEN news_current_event_review_payload_valid(
                      payload_value, should_push_value, dimensions_value, novelty_value,
                      first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                    WHEN 'news_review_v7' THEN news_current_event_review_payload_v7_valid(
                      payload_value, should_push_value, dimensions_value, novelty_value,
                      first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                    ELSE news_current_event_review_payload_v8_valid(
                      payload_value, should_push_value, dimensions_value, novelty_value,
                      first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                  END
                  ELSE false
                END
              ELSE false
            END
          ) IS TRUE
        $$;
    """)

    # The sampler's own version, restated from `20260831_0340` with the two values this cut produces
    # admitted. `_selection` is `news_review_sampler_v4` from here on because six of its strata are
    # deleted with the relevance codes they read; v3 stays because every selection already stored names
    # it, and the six retired strata stay in the list for the same reason. A reviewer cannot store a
    # review at all while this predicate and `desk._selection` disagree.
    op.execute("""
CREATE OR REPLACE FUNCTION public.news_current_review_selection_valid(
    value jsonb, subject_kind_value text
) RETURNS boolean
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
              AND value ->> 'selection_version' IN ('news_review_sampler_v3','news_review_sampler_v4')
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
    """)

    # The review-task projection, restated from `0378` with one predicate widened: its lateral picks the
    # newest model verdict and pinned `news_judgment_v2`, so under v3 it would answer no row for every
    # Event the worker has judged and the whole review plane would report `insufficient_evidence`.
    # Nothing else in the definition changes.
    op.execute("""
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
                      AND x.judgment_contract_version IN ('news_judgment_v2','news_judgment_v3')
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
    """)

    # The task-source guard, restated from `0380` with `news_review_v8` in the list of rubric versions it
    # fires for. `0380` wrote the rule this obeys: restricting the guard to the versions that existed
    # when it was written lets every row under a newer one in without its source ever being checked,
    # which is a weaker contract wearing the same name.
    op.execute("""
CREATE OR REPLACE FUNCTION public.news_current_review_source_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF NEW.rubric_version IN ('news_review_v6','news_review_v7','news_review_v8')
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
                   NEW.subject_kind, NEW.rubric_version, NEW.task_id, NEW.event_id, NEW.evidence_version,
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
    """)

    # The judgment CHECK, restated in place from whatever the database actually holds, the way `0386` and
    # `0385` restate it: the predicate is one 200-line expression shared by four judgment origins, and
    # retyping it to change four list literals is how a branch silently loses a clause.
    op.execute("""
        DO $migration$
        DECLARE definition text;
        BEGIN
          SELECT pg_get_constraintdef(oid) INTO STRICT definition FROM pg_constraint
            WHERE conrelid='news_verdicts'::regclass AND conname='news_verdicts_current_judgment_check';
          IF position('news_triage_policy_v15' in definition) = 0
             OR position('news_semantic_program_v12' in definition) = 0
             OR position('news_judgment_v3' in definition) > 0 THEN
            RAISE EXCEPTION 'unexpected_news_judgment_constraint';
          END IF;
          -- The contract gate, and the shape each contract binds its verdict to.
          definition := replace(definition,
            '(judgment_contract_version = ''news_judgment_v2''::text)',
            '(judgment_contract_version = ANY (ARRAY[''news_judgment_v2''::text, ''news_judgment_v3''::text]))'
            ' AND public.news_current_verdict_contract_shape_valid('
            'judgment_contract_version, judgment_origin, verdict)');
          -- The policy list, in the model, OI and degraded branches; `replace` is global.
          definition := replace(definition,
            '''news_triage_policy_v15''::text]))',
            '''news_triage_policy_v15''::text, ''news_triage_policy_v16''::text]))');
          -- The program list in the model and degraded branches, then the typed-asset guard beside it.
          -- Order matters: after the first call the array entry no longer ends in a bare paren, so the
          -- second call reaches only the guard.
          definition := replace(definition,
            '''news_semantic_program_v12''::text]))',
            '''news_semantic_program_v12''::text, ''news_semantic_program_v13''::text]))');
          definition := replace(definition,
            '''news_semantic_program_v12''::text)',
            '''news_semantic_program_v12''::text AND program_version <> ''news_semantic_program_v13''::text)');
          IF position('news_triage_policy_v16' in definition) = 0
             OR position('news_semantic_program_v13' in definition) = 0
             OR position('news_current_verdict_contract_shape_valid' in definition) = 0 THEN
            RAISE EXCEPTION 'news_judgment_v3_not_admitted';
          END IF;
          ALTER TABLE news_verdicts DROP CONSTRAINT news_verdicts_current_judgment_check;
          EXECUTE 'ALTER TABLE news_verdicts ADD CONSTRAINT news_verdicts_current_judgment_check ' || definition;
        END $migration$;
    """)


def downgrade() -> None:
    raise RuntimeError("news_judgment_v3_fact_kind_forward_only: restore a verified backup")
