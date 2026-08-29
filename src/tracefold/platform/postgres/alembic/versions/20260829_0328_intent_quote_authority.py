"""Intent-level execution Quote authority and SubmissionFenceV1 (#303).

Revision ID: 20260829_0328
Revises: 20260829_0327
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0328"
down_revision = "20260829_0327"
branch_labels = None
depends_on = None

_QUOTE_REASONS = """
  'quote_missing', 'quote_type_invalid', 'quote_instrument_mismatch',
  'quote_book_invalid', 'quote_side_unsupported', 'quote_intent_not_active',
  'quote_intent_expired', 'quote_clock_invalid', 'quote_receive_stale',
  'quote_event_stale', 'quote_source_latency_exceeded', 'quote_future_skew',
  'quote_event_out_of_order', 'quote_spread_exceeded', 'quote_reference_drift_exceeded'
"""


def upgrade() -> None:
    op.execute(
        """
        LOCK TABLE trading_runtime_state, trading_intents IN SHARE ROW EXCLUSIVE MODE;
        DO $cutover$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM trading_runtime_state WHERE id = 1 AND control = 'PAUSED') THEN
            RAISE EXCEPTION 'intent_quote_authority_requires_paused';
          END IF;
          IF EXISTS (
            SELECT 1 FROM trading_intents
             WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
          ) THEN
            RAISE EXCEPTION 'intent_quote_authority_requires_no_recovery_obligation';
          END IF;
        END
        $cutover$
        """
    )
    op.execute(
        """
        ALTER TABLE trading_intents
          ADD COLUMN adopted_at_ms BIGINT,
          ADD COLUMN entry_fence_requested_at_ms BIGINT,
          ADD COLUMN submission_fence_version TEXT,
          ADD COLUMN submission_quantity NUMERIC,
          ADD COLUMN entry_quote_q1 JSONB,
          ADD COLUMN entry_quote_q2 JSONB,
          ADD COLUMN entry_submitted_at_ms BIGINT,
          ADD COLUMN entry_accepted_at_ms BIGINT
        """
    )
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_reason_check")
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_rejected_flat_check")
    op.execute(
        f"""
        ALTER TABLE trading_intents
          ADD CONSTRAINT trading_intents_reason_check CHECK (
            reason_code IS NULL OR reason_code IN (
              'intent_expired', 'runtime_not_ready', 'external_exposure',
              'blacklisted', 'capability_mismatch', 'market_unacceptable',
              'quantity_unexecutable', 'risk_denied',
              'entry_outcome_unknown', 'protection_unproven',
              'close_outcome_unknown', 'operator_intervention',
              {_QUOTE_REASONS}
            )
          ),
          ADD CONSTRAINT trading_intents_rejected_flat_check CHECK (
            terminal_outcome <> 'REJECTED' OR (
              actual_quantity IS NULL
              AND opened_at_ms IS NULL
              AND position_id IS NULL
              AND reason_code IN (
                'runtime_not_ready', 'external_exposure', 'blacklisted',
                'capability_mismatch', 'market_unacceptable',
                'quantity_unexecutable', 'risk_denied',
                {_QUOTE_REASONS}
              )
              AND (entry_fenced_at_ms IS NULL OR flat_verified_at_ms IS NOT NULL)
            )
          )
        """
    )
    op.execute(
        """
        ALTER TABLE trading_intents
          ADD CONSTRAINT trading_intents_quote_q1_audit_check CHECK (
            entry_quote_q1 IS NULL OR (
              jsonb_typeof(entry_quote_q1) = 'object'
              AND octet_length(entry_quote_q1::text) <= 2048
              AND entry_quote_q1 ->> 'snapshot_version' IN (
                'execution_quote_snapshot_v1', 'execution_quote_rejection_v1'
              )
              AND entry_quote_q1 ->> 'stage' = 'Q1'
              AND entry_quote_q1 ->> 'reason' IS NOT NULL
              AND (
                entry_quote_q1 ->> 'reason' <> 'accepted'
                OR entry_quote_q1 ?& ARRAY[
                  'instrument_id', 'side', 'side_price', 'bid', 'ask',
                  'ts_event_ns', 'ts_init_ns', 'evaluated_at_ns', 'stream_generation',
                  'receive_age_ns', 'event_age_ns', 'source_latency_ns',
                  'spread_bps', 'reference_drift_bps'
                ]
              )
            )
          ),
          ADD CONSTRAINT trading_intents_quote_q2_audit_check CHECK (
            entry_quote_q2 IS NULL OR (
              jsonb_typeof(entry_quote_q2) = 'object'
              AND octet_length(entry_quote_q2::text) <= 2048
              AND entry_quote_q2 ->> 'snapshot_version' IN (
                'execution_quote_snapshot_v1', 'execution_quote_rejection_v1'
              )
              AND entry_quote_q2 ->> 'stage' = 'Q2'
              AND entry_quote_q2 ->> 'reason' IS NOT NULL
              AND (
                entry_quote_q2 ->> 'reason' <> 'accepted'
                OR entry_quote_q2 ?& ARRAY[
                  'instrument_id', 'side', 'side_price', 'bid', 'ask',
                  'ts_event_ns', 'ts_init_ns', 'evaluated_at_ns', 'stream_generation',
                  'receive_age_ns', 'event_age_ns', 'source_latency_ns',
                  'spread_bps', 'reference_drift_bps'
                ]
              )
            )
          ),
          ADD CONSTRAINT trading_intents_submission_fence_v1_check CHECK (
            (submission_fence_version IS NULL AND submission_quantity IS NULL)
            OR (
              submission_fence_version = 'submission_fence_v1'
              AND submission_quantity > 0
              AND submission_quantity * (entry_quote_q1 ->> 'side_price')::NUMERIC
                    <= target_notional_usd
              AND entry_client_order_id IS NOT NULL
              AND entry_fenced_at_ms IS NOT NULL
              AND entry_quote_q1 ->> 'snapshot_version' = 'execution_quote_snapshot_v1'
              AND entry_quote_q1 ->> 'reason' = 'accepted'
            )
          ),
          ADD CONSTRAINT trading_intents_q2_submission_check CHECK (
            (entry_quote_q2 IS NULL AND entry_submitted_at_ms IS NULL AND entry_accepted_at_ms IS NULL)
            OR (
              submission_fence_version = 'submission_fence_v1'
              AND entry_quote_q2 IS NOT NULL
              AND (
                (entry_quote_q2 ->> 'reason' = 'accepted')
                OR (
                  entry_quote_q2 ->> 'reason' = reason_code
                  AND execution_state = 'TERMINAL'
                  AND terminal_outcome = 'REJECTED'
                  AND entry_submitted_at_ms IS NULL
                  AND entry_accepted_at_ms IS NULL
                )
              )
              AND (entry_submitted_at_ms IS NULL OR entry_quote_q2 ->> 'reason' = 'accepted')
              AND (entry_accepted_at_ms IS NULL OR entry_submitted_at_ms IS NOT NULL)
            )
          ),
          ADD CONSTRAINT trading_intents_execution_clock_order_check CHECK (
            (adopted_at_ms IS NULL OR adopted_at_ms >= created_at_ms)
            AND (entry_fence_requested_at_ms IS NULL OR (
              adopted_at_ms IS NOT NULL AND entry_fence_requested_at_ms >= adopted_at_ms
            ))
            AND (submission_fence_version IS NULL OR (
              entry_fence_requested_at_ms IS NOT NULL
              AND entry_fenced_at_ms >= entry_fence_requested_at_ms
            ))
            AND (entry_submitted_at_ms IS NULL OR entry_submitted_at_ms >= entry_fenced_at_ms)
            AND (entry_accepted_at_ms IS NULL OR entry_accepted_at_ms >= entry_submitted_at_ms)
          )
        """
    )
    op.execute(
        """
        GRANT UPDATE (
          adopted_at_ms, entry_fence_requested_at_ms, submission_fence_version,
          submission_quantity, entry_quote_q1, entry_quote_q2,
          entry_submitted_at_ms, entry_accepted_at_ms
        ) ON trading_intents TO tracefold_nautilus
        """
    )


def downgrade() -> None:
    raise RuntimeError("intent_quote_authority_downgrade_unsupported")
