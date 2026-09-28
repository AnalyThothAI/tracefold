"""Remove retired News judgments, learning releases, and Trading research tape.

Forward only. Stop News and Trading writers before the schema switch. This revision
intentionally discards the retired rows; restore a verified pre-cut backup if the
old contracts must be inspected. Raw Items, current EventUpdates, notification
decisions, market facts, Trading Cases, and execution evidence remain authoritative.

Revision ID: 20260928_0411
Revises: 20260928_0410
"""

from alembic import op

revision = "20260928_0411"
down_revision = "20260928_0410"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '1800s'")

    # Convert the old wallet evidence once so every runtime reader can enforce
    # the current strict snapshot contract without projecting retired fields.
    op.execute(
        """CREATE FUNCTION pg_temp.current_wallet_snapshot(value jsonb) RETURNS jsonb
           LANGUAGE sql STRICT AS $$
           SELECT jsonb_set(value, '{window,members}',
                  COALESCE((SELECT jsonb_agg(member.value - 'rank_quality' - 'source_closed_trades'
                                             - 'source_profit_factor' ORDER BY member.ordinality)
                     FROM jsonb_array_elements(value #> '{window,members}')
                          WITH ORDINALITY AS member(value, ordinality)), '[]'::jsonb), false)
           $$"""
    )
    op.execute(
        """UPDATE public.news_market_wallet_events
              SET initial_snapshot = pg_temp.current_wallet_snapshot(initial_snapshot),
                  latest_snapshot = pg_temp.current_wallet_snapshot(latest_snapshot),
                  send_snapshot = pg_temp.current_wallet_snapshot(send_snapshot)
            WHERE initial_snapshot::text LIKE '%rank_quality%'
               OR latest_snapshot::text LIKE '%rank_quality%'
               OR send_snapshot::text LIKE '%rank_quality%'
               OR initial_snapshot::text LIKE '%source_closed_trades%'
               OR latest_snapshot::text LIKE '%source_closed_trades%'
               OR send_snapshot::text LIKE '%source_closed_trades%'
               OR initial_snapshot::text LIKE '%source_profit_factor%'
               OR latest_snapshot::text LIKE '%source_profit_factor%'
               OR send_snapshot::text LIKE '%source_profit_factor%'"""
    )
    op.execute("DROP FUNCTION pg_temp.current_wallet_snapshot(jsonb)")

    # These Event kinds were replaced by typed market observations. Their Items
    # and market facts are independent rows and survive the Event cascade.
    op.execute(
        "DELETE FROM public.news_events WHERE event_kind NOT IN ('news', 'listing') "
        "OR admission NOT IN ('candidate', 'listing_deterministic', 'recovery') "
        "OR EXISTS (SELECT 1 FROM public.news_event_updates old_update "
        "WHERE old_update.event_id = news_events.event_id "
        "AND old_update.document ->> 'schema_version' IS DISTINCT FROM 'news_event_update_v2')"
    )
    op.execute("DELETE FROM public.news_delivery_queue WHERE kind <> 'update'")
    op.execute("DELETE FROM public.news_deliveries WHERE kind <> 'update'")

    op.execute(
        "DROP VIEW public.news_review_task_source_v1, public.news_review_pairwise_tasks_v1, "
        "public.news_review_records_v1, public.news_review_external_source_v1, "
        "public.news_review_active_agent_v1"
    )
    op.execute(
        "DROP TABLE public.news_verdicts, public.news_reviews, public.news_learning_cases, "
        "public.news_model_recordings, public.news_learning_epochs, public.news_learning_artifacts, "
        "public.news_canary_activations, public.news_agent_assignments, "
        "public.news_agent_runtime_manifests, public.news_learning_retention_state, "
        "public.trading_root_market_tapes, public.trading_case_evaluations"
    )

    op.execute("ALTER TABLE public.news_events DROP CONSTRAINT news_events_event_kind_check")
    op.execute(
        "ALTER TABLE public.news_events ADD CONSTRAINT news_events_event_kind_check "
        "CHECK (event_kind IN ('news', 'listing'))"
    )
    op.execute("ALTER TABLE public.news_events DROP COLUMN followup_of")
    op.execute("ALTER TABLE public.news_event_updates DROP CONSTRAINT news_event_updates_document_check")
    op.execute(
        """ALTER TABLE public.news_event_updates ADD CONSTRAINT news_event_updates_document_check CHECK (
          jsonb_typeof(document)='object'
          AND document->>'schema_version' = 'news_event_update_v2'
          AND document->>'event_id'=event_id
          AND document->>'content_revision'=content_revision
          AND document->'input_revision'=to_jsonb(input_revision)
          AND document->>'previous_content_revision' IS NOT DISTINCT FROM previous_content_revision)"""
    )
    op.execute(
        "ALTER TABLE public.news_events ADD CONSTRAINT news_events_admission_check "
        "CHECK (admission IN ('candidate', 'listing_deterministic', 'recovery'))"
    )
    op.execute("DROP INDEX public.ix_news_events_unpublished")
    op.execute(
        "CREATE INDEX ix_news_events_unpublished ON public.news_events (opened_at_ms) "
        "WHERE published_at_ms IS NULL AND admission IN ('candidate', 'listing_deterministic')"
    )

    op.execute("ALTER TABLE public.news_deliveries DROP CONSTRAINT news_deliveries_kind_check")
    op.execute("ALTER TABLE public.news_deliveries DROP CONSTRAINT news_deliveries_intent_check")
    op.execute("ALTER TABLE public.news_deliveries DROP CONSTRAINT news_deliveries_state_check")
    op.execute("ALTER TABLE public.news_deliveries ADD CONSTRAINT news_deliveries_kind_check CHECK (kind = 'update')")
    op.execute(
        """ALTER TABLE public.news_deliveries ADD CONSTRAINT news_deliveries_intent_check CHECK (
          intent_id ~ '^intent:[0-9a-f]{64}$' AND content_revision ~ '^[0-9a-f]{64}$'
          AND jsonb_typeof(claim_refs) = 'array' AND jsonb_array_length(claim_refs) > 0
          AND body IS NOT NULL AND plan_key IS NOT NULL AND payload_sha256 = news_text_digest(body)
        )"""
    )
    op.execute(
        "ALTER TABLE public.news_deliveries ADD CONSTRAINT news_deliveries_state_check "
        "CHECK (state IN ('sending', 'sent', 'terminal', 'ambiguous'))"
    )
    op.execute("ALTER TABLE public.news_delivery_queue DROP CONSTRAINT news_delivery_queue_kind_check")
    op.execute("ALTER TABLE public.news_delivery_queue DROP CONSTRAINT news_delivery_queue_intent_check")
    op.execute(
        "ALTER TABLE public.news_delivery_queue ADD CONSTRAINT news_delivery_queue_kind_check CHECK (kind = 'update')"
    )
    op.execute(
        """ALTER TABLE public.news_delivery_queue ADD CONSTRAINT news_delivery_queue_intent_check CHECK (
          intent_id ~ '^intent:[0-9a-f]{64}$' AND content_revision ~ '^[0-9a-f]{64}$'
          AND jsonb_typeof(claim_refs) = 'array' AND jsonb_array_length(claim_refs) > 0
          AND plan_key IS NOT NULL
          AND (frozen_card IS NULL OR (jsonb_typeof(frozen_card) = 'object'
               AND frozen_card ->> 'intent_id' = intent_id))
        )"""
    )

    # These validators and guards served only the dropped verdict/review/learning
    # relations. Shared evidence, identity and JSON helpers are still used by the
    # current EventUpdate and typed market tables.
    op.execute(
        """DROP FUNCTION
          public.news_current_decision_valid(jsonb),
          public.news_current_event_review_payload_valid(jsonb,text,jsonb,jsonb,text,jsonb,text,text),
          public.news_current_liquidation_fact_valid(jsonb),
          public.news_current_liquidation_metadata_valid(jsonb,boolean),
          public.news_current_model_editorial_valid(jsonb),
          public.news_current_oi_metadata_valid(jsonb,boolean),
          public.news_current_oi_signal_valid(jsonb),
          public.news_current_pairwise_review_payload_valid(jsonb,jsonb,text),
          public.news_current_review_acceptance_target_guard(),
          public.news_current_review_dimensions_valid(jsonb),
          public.news_current_review_evidence_refs_valid(jsonb),
          public.news_current_review_expected_valid(jsonb),
          public.news_current_review_novelty_valid(jsonb),
          public.news_current_review_selection_valid(jsonb,text),
          public.news_current_review_source_guard(),
          public.news_current_review_taxonomy_provenance_valid(jsonb),
          public.news_current_review_taxonomy_valid(jsonb),
          public.news_current_review_valid(text,text,text,text,text,integer,text,text,text,jsonb,jsonb,text,jsonb,text,text,jsonb,jsonb,text),
          public.news_current_told_trace_valid(jsonb),
          public.news_current_triage_verdict_valid(jsonb),
          public.news_current_verdict_evidence_guard(),
          public.news_current_model_taxonomy_valid(jsonb),
          public.news_current_typed_assets_valid(jsonb),
          public.news_current_taxonomy_axes_valid(jsonb),
          public.news_current_review_v7_taxonomy_provenance_valid(jsonb),
          public.news_current_review_v7_dimensions_valid(jsonb),
          public.news_current_review_explanation_valid(jsonb),
          public.news_current_event_review_payload_v7_valid(jsonb,text,jsonb,jsonb,text,jsonb,text,text),
          public.news_current_review_source_exists(text,text,text,text,integer,text,text),
          public.news_current_partial_review_taxonomy_valid(jsonb),
          public.news_current_verdict_contract_shape_valid(text,text,jsonb),
          public.news_current_editorial_taxonomy_slot_valid(jsonb),
          public.news_current_review_v8_dimensions_valid(jsonb),
          public.news_current_review_v8_expected_valid(jsonb),
          public.news_current_event_review_payload_v8_valid(jsonb,text,jsonb,jsonb,text,jsonb,text,text),
          public.purge_news_learning_retention(integer),
          public.reject_news_learning_mutation()"""
    )


def downgrade() -> None:
    raise RuntimeError("retired_contracts_forward_only_restore_verified_backup")
