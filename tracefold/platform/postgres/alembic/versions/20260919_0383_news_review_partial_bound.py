"""Partial review axes and bound reviewed output (#663).

- why_database_must_change: the review CHECK/read view must accept partial Gold and
  an immutable reviewed source while retaining strict online taxonomy. Per-case artifacts keep
  immutable corpus material within the existing 1 MiB per-artifact bound.
- current_source_revision: 20260918_0382
- minimum_supported_source_revision: 20260918_0382
- lock_level_and_order: function catalog locks, then ACCESS EXCLUSIVE on news_learning_artifacts for its kind CHECK.
- statement_timeout: 30s; lock_timeout: 5s.
- estimated_rows: zero business rows changed; CHECK scans retained artifact kinds; estimated_bytes: definitions only.
- rewrite_or_index_build: none.
- preflight_and_maintenance_boundary: coordinated application cut behind maintenance gate.
- archive_current_compatibility: old accepted full labels remain readable without rewriting;
  new submissions bind reviewed_source and require explicit market_type.
- role_and_grant_impact: unchanged single application login.
- failure_state: transaction rollback preserves the previous validators.
- roll_forward_or_verified_backup_restore: forward repair or verified backup restore.
- production_postgres_image: postgres:18-bookworm.
"""

from alembic import op
from sqlalchemy import text

revision = "20260919_0383"
down_revision = "20260918_0382"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("""
        ALTER TABLE news_learning_artifacts DROP CONSTRAINT news_learning_artifact_kind;
        ALTER TABLE news_learning_artifacts ADD CONSTRAINT news_learning_artifact_kind CHECK
          (kind = ANY (ARRAY['candidate_registration','proposal','candidate','dataset','dataset_case',
            'evaluation_report','release_evidence','active_agent','shadow_observation','canary_observation',
            'deployment_receipt','rollback_receipt','program_artifact','compile_receipt','compile_record',
            'prompt_candidate','epoch_reset']));
    """)
    op.execute("""
        CREATE FUNCTION public.news_current_partial_review_taxonomy_valid(value jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT (jsonb_typeof(value) = 'object' AND value <> '{}'::jsonb
            AND news_jsonb_required_optional_keys(value, ARRAY[]::text[],
                ARRAY['subject_codes','event_family','change_state','assertion_status'])
            AND news_current_model_taxonomy_valid(
                jsonb_build_object('subject_codes', '[]'::jsonb, 'event_family', 'other',
                                   'change_state', 'unknown', 'assertion_status', 'unknown') || value
            )) IS TRUE
        $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.news_current_event_review_payload_v7_valid(
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
            AND (news_current_review_v7_dimensions_valid(expected_dimensions)
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
            AND news_current_review_expected_valid(value -> 'expected')
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
    """)

    # Dataset material must live for as long as any retained root refers to it. Update both
    # deletion and remaining-work accounting, preserving the existing bounded batch algorithm.
    definition = (
        op.get_bind()
        .execute(text("SELECT pg_get_functiondef('public.purge_news_learning_retention(integer)'::regprocedure)"))
        .scalar_one()
    )
    anchor = "UNION SELECT DISTINCT dataset_sha FROM news_learning_cases"
    if definition.count(anchor) != 2:
        raise RuntimeError("news_learning_retention_reference_shape_changed")
    references = """UNION SELECT DISTINCT dataset_sha FROM news_learning_cases
            UNION SELECT jsonb_array_elements_text(payload -> 'episode_refs')
              FROM news_learning_artifacts WHERE kind = 'dataset'
            UNION SELECT jsonb_array_elements_text(payload -> 'stream_refs')
              FROM news_learning_artifacts WHERE kind = 'dataset'
            UNION SELECT material.value
              FROM news_learning_artifacts,
                   LATERAL jsonb_each_text(payload -> 'case_material_refs') material
             WHERE kind = 'dataset'
    """
    op.execute(definition.replace(anchor, references))


def downgrade() -> None:
    raise RuntimeError("news_review_partial_bound_forward_only")
