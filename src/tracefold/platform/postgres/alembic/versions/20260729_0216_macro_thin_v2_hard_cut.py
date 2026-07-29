"""Hard-cut Macro to Thin ResearchInput, v2 publications, and official-expiry VX facts."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from alembic import op
from sqlalchemy import text

revision = "20260729_0216"
down_revision = "20260729_0215"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM macro_thesis_runs
            WHERE status = 'running'
              AND leased_until_ms > (extract(epoch FROM clock_timestamp()) * 1000)::bigint
          ) THEN
            RAISE EXCEPTION 'macro_thesis_active_lease_blocks_v2_cutover';
          END IF;
        END
        $$;

        CREATE TABLE macro_research_inputs (
          research_input_id text PRIMARY KEY CHECK (btrim(research_input_id) <> ''),
          evidence_pack_id text NOT NULL
            REFERENCES macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT,
          session_date date NOT NULL UNIQUE,
          cutoff_ms bigint NOT NULL CHECK (cutoff_ms >= 0),
          schema_version text NOT NULL CHECK (schema_version = 'macro_research_input_v1'),
          profile_version text NOT NULL CHECK (btrim(profile_version) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
          input_hash text NOT NULL UNIQUE CHECK (btrim(input_hash) <> '')
        );

        CREATE TRIGGER macro_research_inputs_append_only
        BEFORE UPDATE OR DELETE ON macro_research_inputs
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation();

        ALTER TABLE macro_thesis_runs
          ADD COLUMN research_input_id text
            REFERENCES macro_research_inputs(research_input_id) ON DELETE RESTRICT,
          ADD COLUMN research_input_hash text,
          ADD COLUMN last_gate_category text,
          ADD COLUMN last_candidate_hash text,
          ADD CONSTRAINT macro_thesis_runs_research_input_pair
            CHECK ((research_input_id IS NULL) = (research_input_hash IS NULL)),
          ADD CONSTRAINT macro_thesis_runs_gate_category_check
            CHECK (
              last_gate_category IS NULL
              OR last_gate_category IN (
                'time_identity', 'evidence_closure',
                'contract_validity', 'write_safety'
              )
            );

        ALTER TABLE macro_thesis_publications
          DROP CONSTRAINT macro_thesis_publications_schema_version_check,
          DROP CONSTRAINT macro_thesis_publications_reviewer_draft_hash_check,
          ALTER COLUMN reviewer_invocation_id DROP NOT NULL,
          ALTER COLUMN reviewer_draft_hash DROP NOT NULL,
          ADD CONSTRAINT macro_thesis_publications_schema_version_check
            CHECK (schema_version IN ('macro_thesis_v1', 'macro_thesis_v2')),
          ADD CONSTRAINT macro_thesis_publications_reviewer_version_binding
            CHECK (
              (
                schema_version = 'macro_thesis_v1'
                AND reviewer_invocation_id IS NOT NULL
                AND btrim(reviewer_draft_hash) <> ''
              )
              OR (
                schema_version = 'macro_thesis_v2'
                AND reviewer_invocation_id IS NULL
                AND reviewer_draft_hash IS NULL
              )
            );

        ALTER TABLE macro_live_deltas
          DROP CONSTRAINT macro_live_deltas_schema_version_check,
          DROP CONSTRAINT macro_live_deltas_stable_product_key,
          ADD CONSTRAINT macro_live_deltas_schema_version_check
            CHECK (schema_version IN ('macro_live_delta_v1', 'macro_live_delta_v2'));

        ALTER TABLE macro_outcome_replays
          DROP CONSTRAINT macro_outcome_replays_schema_version_check,
          ADD CONSTRAINT macro_outcome_replays_schema_version_check
            CHECK (schema_version IN ('macro_outcome_replay_v1', 'macro_outcome_replay_v2'));

        ALTER TABLE market_settlements
          ADD COLUMN fact_schema_version text NOT NULL DEFAULT 'market_settlement_v1',
          ADD COLUMN contract_expiration_date date,
          ADD CONSTRAINT market_settlements_fact_schema_check
            CHECK (
              (
                fact_schema_version = 'market_settlement_v1'
                AND contract_expiration_date IS NULL
              )
              OR (
                fact_schema_version = 'market_settlement_v2'
                AND contract_expiration_date IS NOT NULL
              )
            );
        ALTER TABLE market_settlements
          ALTER COLUMN fact_schema_version DROP DEFAULT;

        DELETE FROM macro_module_current;
        ALTER TABLE macro_module_current
          DROP CONSTRAINT macro_module_current_typed_schema_check,
          ADD CONSTRAINT macro_module_current_typed_schema_check
          CHECK (
            payload_json ->> 'schema_version' = CASE module_id
              WHEN 'rates_fed' THEN 'macro_rates_fed_v5'
              WHEN 'economy_inflation' THEN 'macro_economy_inflation_v5'
              WHEN 'liquidity_funding' THEN 'macro_liquidity_funding_v5'
              WHEN 'credit' THEN 'macro_credit_v7'
              WHEN 'volatility' THEN 'macro_volatility_v7'
              WHEN 'cross_asset' THEN 'macro_cross_asset_v7'
              ELSE NULL
            END
          );
        """
    )
    _append_v2_settlement_revisions()
    op.execute(
        """
        DROP TRIGGER IF EXISTS macro_thesis_runs_lifecycle ON macro_thesis_runs;

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
          IF OLD.research_input_id IS NOT NULL AND (
            NEW.research_input_id,
            NEW.research_input_hash
          ) IS DISTINCT FROM (
            OLD.research_input_id,
            OLD.research_input_hash
          ) THEN
            RAISE EXCEPTION 'macro_thesis_run_research_input_immutable';
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
              AND NEW.status = 'pending'
              AND NEW.attempt_count = 0
              AND OLD.research_input_id IS NULL
              AND NEW.research_input_id IS NOT NULL
            )
            OR (
              OLD.status = 'pending'
              AND OLD.attempt_count = 0
              AND NEW.status IN ('config_error', 'failed')
              AND NEW.attempt_count = 0
            )
            OR (
              OLD.status IN ('pending', 'retryable')
              AND NEW.status = 'running'
              AND NEW.research_input_id IS NOT NULL
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

        DELETE FROM checkpoint_writes
        WHERE thread_id LIKE 'research:mep3_%'
           OR thread_id LIKE 'review:mep3_%';
        DELETE FROM checkpoints
        WHERE thread_id LIKE 'research:mep3_%'
           OR thread_id LIKE 'review:mep3_%';
        DELETE FROM checkpoint_blobs
        WHERE thread_id LIKE 'research:mep3_%'
           OR thread_id LIKE 'review:mep3_%';
        """
    )


def _append_v2_settlement_revisions() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        text(
            """
            SELECT *
            FROM market_settlements
            WHERE fact_schema_version = 'market_settlement_v1'
            ORDER BY settlement_id
            """
        )
    ).mappings()
    for row in rows:
        raw = dict(row["raw_data_json"] or {})
        expiration = _official_expiration(raw)
        if expiration is None:
            continue
        payload = {
            "contract_code": row["contract_code"],
            "contract_expiration_date": expiration.isoformat(),
            "dataset_id": row["dataset_id"],
            "fact_schema_version": "market_settlement_v2",
            "instrument_id": row["instrument_id"],
            "open_interest": row["open_interest"],
            "settlement_price": row["settlement_price"],
            "trade_date": row["trade_date"].isoformat(),
            "unit": row["unit"],
            "volume": row["volume"],
        }
        fact_hash = _payload_hash(payload)
        settlement_seed = (
            f"{row['dataset_id']}|{row['instrument_id']}|{row['trade_date']}|"
            f"{row['contract_code']}|market_settlement_v2|{expiration}|{fact_hash}"
        )
        settlement_id = "mktset_" + hashlib.sha256(settlement_seed.encode()).hexdigest()
        connection.execute(
            text(
                """
                INSERT INTO market_settlements(
                  settlement_id, instrument_id, dataset_id, source_id, trade_date,
                  contract_code, settlement_price, open_interest, volume, unit,
                  published_at_ms, received_at_ms, source_url, fact_hash, raw_data_json,
                  fact_schema_version, contract_expiration_date
                )
                VALUES (
                  :settlement_id, :instrument_id, :dataset_id, :source_id, :trade_date,
                  :contract_code, :settlement_price, :open_interest, :volume, :unit,
                  :published_at_ms, :received_at_ms, :source_url, :fact_hash,
                  CAST(:raw_data_json AS jsonb), 'market_settlement_v2', :expiration
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                **dict(row),
                "settlement_id": settlement_id,
                "fact_hash": fact_hash,
                "raw_data_json": json.dumps(raw, sort_keys=True),
                "expiration": expiration,
            },
        )


def _official_expiration(raw: dict[str, Any]) -> date | None:
    for key, value in raw.items():
        normalized = "".join(character for character in str(key).lower() if character.isalnum())
        if normalized != "expirationdate":
            continue
        try:
            return date.fromisoformat(str(value).strip()[:10])
        except ValueError:
            return None
    return None


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def downgrade() -> None:
    raise RuntimeError("20260729_0216 is an irreversible Macro Thin v2 hard cut")
