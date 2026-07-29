"""Complete Macro Thesis lifecycle and stable Live Delta contracts."""

from __future__ import annotations

from alembic import op

revision = "20260728_0213"
down_revision = "20260728_0212"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS macro_thesis_runs_lifecycle
          ON macro_thesis_runs;
        DROP TRIGGER IF EXISTS macro_live_deltas_append_only
          ON macro_live_deltas;

        WITH ranked AS (
          SELECT
            live_delta_id,
            row_number() OVER (
              PARTITION BY publication_id
              ORDER BY evaluated_at_ms DESC, live_delta_id DESC
            ) AS position
          FROM macro_live_deltas
        )
        DELETE FROM macro_live_deltas AS deltas
        USING ranked
        WHERE deltas.live_delta_id = ranked.live_delta_id
          AND ranked.position > 1;
        UPDATE macro_live_deltas
        SET live_delta_id = 'mld:' || publication_id;

        ALTER TABLE macro_thesis_runs
          DROP CONSTRAINT macro_thesis_runs_status_check;
        UPDATE macro_thesis_runs
        SET status = 'not_published'
        WHERE status = 'blocked';
        ALTER TABLE macro_thesis_runs
          ADD CONSTRAINT macro_thesis_runs_status_check
          CHECK (status IN (
            'pending', 'running', 'retryable', 'failed',
            'config_error', 'not_published', 'published'
          ));

        ALTER TABLE macro_live_deltas
          ADD CONSTRAINT macro_live_deltas_stable_product_key
          CHECK (live_delta_id = 'mld:' || publication_id);
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_macro_thesis_run_lifecycle()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'macro_thesis_run_delete_forbidden';
          END IF;
          IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'pending' OR NEW.attempt_count <> 0 THEN
              RAISE EXCEPTION 'macro_thesis_run_initial_state_invalid';
            END IF;
            RETURN NEW;
          END IF;
          IF (
            NEW.session_date,
            NEW.cutoff_ms,
            NEW.evidence_pack_id,
            NEW.evidence_pack_hash,
            NEW.max_attempts,
            NEW.created_at_ms
          ) IS DISTINCT FROM (
            OLD.session_date,
            OLD.cutoff_ms,
            OLD.evidence_pack_id,
            OLD.evidence_pack_hash,
            OLD.max_attempts,
            OLD.created_at_ms
          ) THEN
            RAISE EXCEPTION 'macro_thesis_run_frozen_fields_immutable';
          END IF;
          IF OLD.status IN (
            'failed', 'config_error', 'not_published', 'published'
          ) THEN
            RAISE EXCEPTION 'macro_thesis_run_terminal';
          END IF;
          IF NOT (
            (
              OLD.status = 'pending'
              AND OLD.attempt_count = 0
              AND NEW.status = 'config_error'
              AND NEW.attempt_count = 0
            )
            OR (
              OLD.status IN ('pending', 'retryable')
              AND NEW.status = 'running'
            )
            OR (
              OLD.status = 'running'
              AND NEW.status IN (
                'running', 'retryable', 'failed', 'config_error',
                'not_published', 'published'
              )
            )
          ) THEN
            RAISE EXCEPTION 'macro_thesis_run_transition_invalid:%->%',
              OLD.status, NEW.status;
          END IF;
          IF NEW.attempt_count < OLD.attempt_count THEN
            RAISE EXCEPTION 'macro_thesis_run_attempt_count_decrease';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE TRIGGER macro_thesis_runs_lifecycle
        BEFORE INSERT OR UPDATE OR DELETE ON macro_thesis_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_macro_thesis_run_lifecycle();
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260728_0213 is an irreversible Macro Thesis contract completion")
