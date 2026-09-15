"""Task-level review contract `news_review_v7`, and a review plane that no longer reads the epoch (#651 §7.2, §9).

Migration evidence:

- category: five function creations, two function replacements, one function signature change with its
  trigger function, additive to the data
- why_database_must_change: four things, all of them places where the v6 rubric is a database fact
  rather than a Python promise.
  `news_current_review_valid` pins `rubric_version_value = 'news_review_v6'`, and it is both the
  `news_reviews_current_contract_check` CHECK and the `WHERE` of `news_review_records_v1`. Without this
  revision the first v7 submission is refused outright, and if it somehow landed it would be invisible
  to every reader that goes through the records view.
  `news_current_event_review_payload_valid` enumerates the payload keys exactly and requires
  `factual_fidelity`, all five `taxonomy_*` dimensions, a complete taxonomy, a novelty judgment and a
  push verdict. v7 requires only a non-empty `dimensions` map and adds an optional `explanation` block,
  so the predicate has to be a second one rather than a loosened first: a v6 row still means "every
  dimension below was answered", and rewriting the v6 predicate to accept absences would retroactively
  turn 1,020 accepted rows into rows that merely might have answered.
  `news_current_review_source_exists` recomputes the review task id from the literal `'news_review_v6'`.
  The rubric version is inside the task identity hash, so every v7 task id differs from every v6 one and
  the append guard would reject every v7 submission as a missing source. It takes the row's rubric
  version now, which also means the guard can run for both contracts instead of silently skipping v7.
  The same function's pairwise branch requires `dataset.payload ->> 'learning_epoch'` to equal the
  active bundle's epoch label. `news_learning_dataset_v4` seals no epoch, so without this every blind
  pairwise judgment on a corpus frozen after the cut would be refused.
- current_source_revision: 20260915_0378
- minimum_supported_source_revision: 20260915_0378
- lock_level_and_order: no table lock; `CREATE FUNCTION` / `CREATE OR REPLACE FUNCTION` take catalog
  locks only, and the one `DROP FUNCTION` removes a function referenced solely from a plpgsql trigger
  body, which resolves its callee at execution time. One transaction, functions before the replacement
  that calls them.
- statement_timeout: 120s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: none scanned; no CHECK is added or revalidated
- estimated_bytes: catalog entries only
- rewrite_or_index_build: none
- preflight_and_maintenance_boundary: none required; `news review submit` is the only writer and it is a
  short-lived CLI connection
- archive_current_compatibility: compatible.
  Every review accepted under v6 keeps validating byte for byte: the v6 branch of
  `news_current_review_valid` calls the unchanged `news_current_event_review_payload_valid`, so
  `news_review_records_v1` keeps showing those rows and the existing CHECK keeps passing without a
  scan. They are audit history and are neither rewritten nor deleted. What they lose is dataset
  eligibility, which is a Python-side list (`REVIEW_RUBRIC_VERSIONS`) and not a database fact: a freeze
  counts them as `rubric_ineligible` rather than pretending a v6 row answered a v7 question.
  The pairwise epoch clause is dropped rather than made optional. A dataset sealed before this cut
  carries a `learning_epoch` nobody compares any more, and its `agent_cohort.bundle_sha` — which the
  branch still requires to equal the active stable — already says the one thing the clause was for.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the v6-only contract stays, which refuses
  every v7 submission with `news_reviews_current_contract_check`
- roll_forward_or_verified_backup_restore: correct with a new forward revision or restore the verified
  pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260915_0380
Revises: 20260915_0378
Create Date: 2026-09-15 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260915_0380"
down_revision = "20260915_0379"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    # v7 provenance, whose `draft_taxonomy` is the four model axes rather than the persisted taxonomy.
    # `news_current_model_taxonomy_valid` is the predicate `20260902_0351` already wrote for exactly this
    # shape -- the blind drafts inside `drafts` are checked with it -- so the reviewer's own taxonomy and
    # the drafts behind it are now measured by one function instead of two that could drift.
    op.execute(
        """
        CREATE FUNCTION public.news_current_review_v7_taxonomy_provenance_valid(value jsonb) RETURNS boolean
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
                  OR news_current_model_taxonomy_valid(value -> 'draft_taxonomy'))
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

    # v7 dimensions: nothing is required, the map may not be empty, and there is no
    # `taxonomy_source_authority`. "Nothing required" is the contract, not a relaxation of one — a
    # dimension a reviewer did not answer is absent, and no reader may substitute a default for it.
    op.execute(
        """
        CREATE FUNCTION public.news_current_review_v7_dimensions_valid(value jsonb) RETURNS boolean
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
            AS $_$
          SELECT jsonb_typeof(value) = 'object'
             AND value <> '{}'::jsonb
             AND news_jsonb_required_optional_keys(value, ARRAY[]::text[], ARRAY[
                   'factual_fidelity','headline_fidelity','asset_grounding','direction','magnitude',
                   'why_support','why_value','timeliness','trade_impact_breadth','trade_tradability',
                   'trade_surprise','trade_development_delta','trade_channels','trade_affected_markets',
                   'reader_value','taxonomy_subject_codes','taxonomy_event_family',
                   'taxonomy_change_state','taxonomy_assertion_status'
                 ])
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_each(value) AS dimension(name, result)
                    WHERE jsonb_typeof(result) <> 'string'
                       OR result #>> '{}' NOT IN ('pass','fail','uncertain','not_applicable')
                 )
        $_$
        """
    )

    # One bounded list of non-empty, trimmed, ≤500-character strings. Three of the explanation block's
    # four lists have exactly this shape, and writing the predicate once is what keeps them the same.
    op.execute(
        """
        CREATE FUNCTION public.news_jsonb_bounded_text_list_valid(value jsonb, maximum integer) RETURNS boolean
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
            AS $_$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= maximum
             AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(value) entry
                    WHERE jsonb_typeof(entry) <> 'string'
                       OR length(entry #>> '{}') NOT BETWEEN 1 AND 500
                       OR btrim(entry #>> '{}') <> entry #>> '{}'
                 )
        $_$
        """
    )

    # The explanation supervision block. Bounded lists of short strings; `source_spans` are checked
    # against the task's frozen evidence by `ReviewDesk.submit`, which is the only place that has it.
    op.execute(
        """
        CREATE FUNCTION public.news_current_review_explanation_valid(value jsonb) RETURNS boolean
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
            AS $_$
          SELECT CASE jsonb_typeof(value)
            WHEN 'null' THEN true
            WHEN 'object' THEN
              news_jsonb_exact_keys(value, ARRAY[
                'source_spans','key_facts','forbidden_claims','error_types','reference_why_zh'
              ])
              AND news_jsonb_bounded_text_list_valid(value -> 'source_spans', 8)
              AND news_jsonb_bounded_text_list_valid(value -> 'key_facts', 6)
              AND news_jsonb_bounded_text_list_valid(value -> 'forbidden_claims', 6)
              AND jsonb_typeof(value -> 'error_types') = 'array'
              AND jsonb_array_length(value -> 'error_types') <= 7
              AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(value -> 'error_types') entry
                     WHERE entry NOT IN (
                       'entity','number_unit','condition','status_plan_vs_executed','attribution',
                       'unsupported_cause','other'
                     )
                  )
              AND (SELECT count(DISTINCT entry) FROM jsonb_array_elements_text(value -> 'error_types') entry)
                  = jsonb_array_length(value -> 'error_types')
              AND jsonb_typeof(value -> 'reference_why_zh') = 'string'
              AND length(value ->> 'reference_why_zh') <= 500
              AND (
                jsonb_array_length(value -> 'source_spans') > 0
                OR jsonb_array_length(value -> 'key_facts') > 0
                OR jsonb_array_length(value -> 'forbidden_claims') > 0
                OR jsonb_array_length(value -> 'error_types') > 0
              )
            ELSE false
          END
        $_$
        """
    )

    # The v7 event payload. A second predicate rather than a loosened first: the v6 one still says what a
    # v6 row promised, and this one says what a v7 row promises, which is strictly less.
    op.execute(
        """
        CREATE FUNCTION public.news_current_event_review_payload_v7_valid(
            value jsonb, expected_should_push text, expected_dimensions jsonb, expected_novelty jsonb,
            expected_first_bad_owner text, expected_evidence_refs jsonb, expected_correction text,
            expected_note text
        ) RETURNS boolean
            LANGUAGE sql IMMUTABLE PARALLEL SAFE
            AS $_$
          SELECT (
            news_jsonb_exact_keys(value, ARRAY[
              'kind','should_push','dimensions','novelty','first_bad_owner','evidence_refs',
              'expected','explanation','taxonomy','taxonomy_review','explanation_supervision',
              'expected_correction','note'
            ])
            AND value ->> 'kind' = 'event_rubric'
            AND jsonb_typeof(value -> 'should_push') IN ('string','null')
            AND CASE WHEN jsonb_typeof(value -> 'should_push') = 'null'
                     THEN expected_should_push IS NULL
                     ELSE value ->> 'should_push' = expected_should_push
                          AND expected_should_push IN
                              ('must_push','should_push','should_hold','must_hold','uncertain') END
            AND value -> 'dimensions' = expected_dimensions
            AND news_current_review_v7_dimensions_valid(expected_dimensions)
            AND jsonb_typeof(value -> 'novelty') IN ('object','null')
            AND CASE WHEN jsonb_typeof(value -> 'novelty') = 'null'
                     THEN expected_novelty = '{}'::jsonb
                     ELSE value -> 'novelty' = expected_novelty
                          AND news_current_review_novelty_valid(expected_novelty) END
            AND expected_first_bad_owner IS NOT NULL
            AND jsonb_typeof(value -> 'first_bad_owner') IN ('string','null')
            AND (jsonb_typeof(value -> 'first_bad_owner') = 'null'
                 OR value ->> 'first_bad_owner' = expected_first_bad_owner)
            AND value -> 'evidence_refs' = expected_evidence_refs
            AND news_current_review_evidence_refs_valid(expected_evidence_refs)
            AND value ->> 'expected_correction' = expected_correction
            AND value ->> 'note' = expected_note
            AND news_current_review_expected_valid(value -> 'expected')
            AND news_current_review_explanation_valid(value -> 'explanation')
            AND (jsonb_typeof(value -> 'taxonomy') = 'null'
                 OR news_current_model_taxonomy_valid(value -> 'taxonomy'))
            AND news_current_review_v7_taxonomy_provenance_valid(value -> 'taxonomy_review')
            -- Taxonomy is all or nothing in both directions: a label nobody compared, or a comparison
            -- against a label the submission never states, are both answers the corpus cannot read.
            AND CASE WHEN jsonb_typeof(value -> 'taxonomy') = 'null'
                     THEN NOT (expected_dimensions ?| ARRAY[
                       'taxonomy_subject_codes','taxonomy_event_family',
                       'taxonomy_change_state','taxonomy_assertion_status'
                     ])
                     ELSE expected_dimensions ?& ARRAY[
                       'taxonomy_subject_codes','taxonomy_event_family',
                       'taxonomy_change_state','taxonomy_assertion_status'
                     ] END
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
        $_$
        """
    )

    # Both contracts, dispatched by the row's own rubric version. Restated in full from the baseline
    # because a CHECK and a security-barrier view both read it and neither may see a half-written one.
    op.execute(
        """
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
            rubric_version_value IN ('news_review_v6','news_review_v7')
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
                  WHEN 'event' THEN CASE rubric_version_value
                    WHEN 'news_review_v6' THEN news_current_event_review_payload_valid(
                      payload_value, should_push_value, dimensions_value, novelty_value,
                      first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                    ELSE news_current_event_review_payload_v7_valid(
                      payload_value, should_push_value, dimensions_value, novelty_value,
                      first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                  END
                  WHEN 'external_miss' THEN CASE rubric_version_value
                    WHEN 'news_review_v6' THEN news_current_event_review_payload_valid(
                      payload_value, should_push_value, dimensions_value, novelty_value,
                      first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                    ELSE news_current_event_review_payload_v7_valid(
                      payload_value, should_push_value, dimensions_value, novelty_value,
                      first_bad_owner_value, evidence_refs_value, expected_correction_value, note_value)
                  END
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
        """
    )

    # The task-source guard, restated so it derives the task id under the row's own rubric version and
    # so its pairwise branch stops asking for an epoch label no current dataset seals.
    op.execute("DROP FUNCTION public.news_current_review_source_exists(text, text, text, integer, text, text)")
    op.execute(
        """
        CREATE FUNCTION public.news_current_review_source_exists(
            subject_kind_value text, rubric_version_value text, task_id_value text, event_id_value text,
            evidence_version_value integer, external_snapshot_id_value text, pairwise_case_id_value text
        ) RETURNS boolean
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
                         'rubric', rubric_version_value,
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
        $_$
        """
    )

    # Both contracts are guarded now. Restricting the guard to v6 would have let every v7 row in without
    # its source ever being checked, which is a weaker contract wearing the same name.
    op.execute(
        """
CREATE OR REPLACE FUNCTION public.news_current_review_source_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF NEW.rubric_version IN ('news_review_v6','news_review_v7')
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
        """
    )


def downgrade() -> None:
    raise RuntimeError("news_review_v7_task_level_forward_only")
