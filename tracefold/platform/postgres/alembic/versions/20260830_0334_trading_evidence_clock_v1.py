"""Trading Production V3 evidence clock and promotion hard link (#377).

Revision ID: 20260830_0334
Revises: 20260830_0333
"""

from __future__ import annotations

from alembic import op

revision = "20260830_0334"
down_revision = "20260830_0333"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A grant written before the future-evidence FK would be unprovable.  Do not invent a report or
    # weaken the new columns to preserve one: the first real promotion has not happened yet.
    op.execute(
        """
        LOCK TABLE trading_runtime_state, trading_production_promotion_grants
          IN SHARE ROW EXCLUSIVE MODE;
        DO $cutover$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM trading_runtime_state WHERE id = 1 AND control = 'PAUSED') THEN
            RAISE EXCEPTION 'trading_evidence_clock_cutover_requires_paused';
          END IF;
          IF EXISTS (SELECT 1 FROM trading_production_promotion_grants) THEN
            RAISE EXCEPTION 'trading_evidence_clock_cutover_unbound_promotion_grant';
          END IF;
        END
        $cutover$
        """
    )

    op.execute(
        """
        CREATE TABLE trading_evidence_clock_receipts (
          receipt_sha256 TEXT PRIMARY KEY,
          receipt_kind TEXT NOT NULL,
          terminal TEXT NOT NULL,
          binding TEXT,
          parent_receipt_sha256 TEXT REFERENCES trading_evidence_clock_receipts(receipt_sha256)
            ON DELETE RESTRICT,
          artifact_sha256 TEXT NOT NULL UNIQUE,
          corpus_sha256 TEXT NOT NULL,
          protocol_sha256 TEXT,
          created_at_ms BIGINT NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_evidence_receipt_sha_check
            CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_evidence_artifact_sha_check
            CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_evidence_corpus_sha_check
            CHECK (corpus_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_evidence_protocol_sha_check
            CHECK (protocol_sha256 IS NULL OR protocol_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_evidence_binding_check
            CHECK (binding IS NULL OR binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')),
          CONSTRAINT trading_evidence_created_at_check CHECK (created_at_ms > 0),
          CONSTRAINT trading_evidence_kind_shape_check CHECK (
            (receipt_kind = 'DISCOVERY_CORPUS'
              AND terminal = 'SOURCE_FEATURE_DISCOVERY_CORPUS_V1_SEALED'
              AND binding IS NULL AND parent_receipt_sha256 IS NULL
              AND artifact_sha256 = corpus_sha256 AND protocol_sha256 IS NULL)
            OR (receipt_kind = 'CANDIDATE_DECISION'
              AND terminal IN ('CANDIDATE_LOCKED', 'NO_CANDIDATE')
              AND binding IS NOT NULL AND parent_receipt_sha256 IS NOT NULL
              AND ((terminal = 'CANDIDATE_LOCKED' AND protocol_sha256 IS NOT NULL
                    AND artifact_sha256 = protocol_sha256)
                OR (terminal = 'NO_CANDIDATE' AND protocol_sha256 IS NULL)))
            OR (receipt_kind = 'FUTURE_CAPTURE'
              AND terminal = 'FUTURE_CAPTURE_SEALED'
              AND binding IS NOT NULL AND parent_receipt_sha256 IS NOT NULL
              AND protocol_sha256 IS NOT NULL)
            OR (receipt_kind = 'FUTURE_DRAIN'
              AND terminal = 'FUTURE_DRAIN_SEALED'
              AND binding IS NOT NULL AND parent_receipt_sha256 IS NOT NULL
              AND protocol_sha256 IS NOT NULL)
            OR (receipt_kind = 'FUTURE_RESULT'
              AND terminal IN ('PROMOTE', 'HOLD', 'INSUFFICIENT_EVIDENCE')
              AND binding IS NOT NULL AND parent_receipt_sha256 IS NOT NULL
              AND protocol_sha256 IS NOT NULL)
          ),
          CONSTRAINT trading_evidence_payload_check CHECK (
            payload ->> 'receipt_sha256' = receipt_sha256
            AND payload ->> 'receipt_kind' = receipt_kind
            AND payload ->> 'terminal' = terminal
            AND payload ->> 'artifact_sha256' = artifact_sha256
            AND payload ->> 'corpus_sha256' = corpus_sha256
            AND (binding IS NULL OR payload ->> 'binding' = binding)
            AND (parent_receipt_sha256 IS NULL
              OR payload ->> 'parent_receipt_sha256' = parent_receipt_sha256)
            AND (protocol_sha256 IS NULL OR payload ->> 'protocol_sha256' = protocol_sha256)
            AND payload -> 'receipt' ->> 'artifact_sha256' = artifact_sha256
            AND (payload -> 'receipt' ->> 'created_at_ms')::BIGINT = created_at_ms
            AND (receipt_kind <> 'FUTURE_RESULT'
              OR payload -> 'receipt' ->> 'report_sha256' = artifact_sha256)
          )
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_trading_evidence_parent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          parent_row trading_evidence_clock_receipts%ROWTYPE;
        BEGIN
          IF NEW.receipt_kind = 'DISCOVERY_CORPUS' THEN
            RETURN NEW;
          END IF;
          SELECT * INTO parent_row
            FROM trading_evidence_clock_receipts
           WHERE receipt_sha256 = NEW.parent_receipt_sha256;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'trading_evidence_parent_missing';
          END IF;
          IF NEW.created_at_ms <= parent_row.created_at_ms THEN
            RAISE EXCEPTION 'trading_evidence_parent_clock_invalid';
          END IF;
          IF NEW.receipt_kind = 'CANDIDATE_DECISION' AND (
            parent_row.receipt_kind <> 'DISCOVERY_CORPUS'
            OR parent_row.artifact_sha256 <> NEW.corpus_sha256
          ) THEN
            RAISE EXCEPTION 'trading_evidence_candidate_parent_invalid';
          END IF;
          IF NEW.receipt_kind = 'FUTURE_DRAIN' AND (
            parent_row.receipt_kind <> 'FUTURE_CAPTURE'
            OR parent_row.terminal <> 'FUTURE_CAPTURE_SEALED'
            OR parent_row.binding <> NEW.binding
            OR parent_row.corpus_sha256 <> NEW.corpus_sha256
            OR parent_row.protocol_sha256 <> NEW.protocol_sha256
            OR NEW.payload -> 'receipt' ->> 'capture_receipt_sha256'
              IS DISTINCT FROM parent_row.receipt_sha256
            OR NEW.payload -> 'receipt' ->> 'candidate_receipt_sha256'
              IS DISTINCT FROM parent_row.parent_receipt_sha256
            OR NEW.payload -> 'receipt' ->> 'capture_sha256'
              IS DISTINCT FROM parent_row.artifact_sha256
          ) THEN
            RAISE EXCEPTION 'trading_evidence_future_drain_parent_invalid';
          END IF;
          IF NEW.receipt_kind = 'FUTURE_CAPTURE' AND (
            parent_row.receipt_kind <> 'CANDIDATE_DECISION'
            OR parent_row.terminal <> 'CANDIDATE_LOCKED'
            OR parent_row.binding <> NEW.binding
            OR parent_row.corpus_sha256 <> NEW.corpus_sha256
            OR parent_row.protocol_sha256 <> NEW.protocol_sha256
          ) THEN
            RAISE EXCEPTION 'trading_evidence_future_capture_parent_invalid';
          END IF;
          IF NEW.receipt_kind = 'FUTURE_RESULT' AND (
            parent_row.receipt_kind <> 'FUTURE_DRAIN'
            OR parent_row.terminal <> 'FUTURE_DRAIN_SEALED'
            OR parent_row.binding <> NEW.binding
            OR parent_row.corpus_sha256 <> NEW.corpus_sha256
            OR parent_row.protocol_sha256 <> NEW.protocol_sha256
            OR NEW.payload -> 'evidence' ->> 'candidate_receipt_sha256'
              IS DISTINCT FROM parent_row.payload -> 'receipt' ->> 'candidate_receipt_sha256'
            OR NEW.payload -> 'evidence' ->> 'future_capture_sha256'
              IS DISTINCT FROM parent_row.payload -> 'receipt' ->> 'capture_sha256'
            OR NEW.payload -> 'evidence' ->> 'future_drain_sha256'
              IS DISTINCT FROM parent_row.artifact_sha256
          ) THEN
            RAISE EXCEPTION 'trading_evidence_future_parent_invalid';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_evidence_parent BEFORE INSERT ON trading_evidence_clock_receipts "
        "FOR EACH ROW EXECUTE FUNCTION validate_trading_evidence_parent()"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_trading_evidence_candidate_per_corpus_binding "
        "ON trading_evidence_clock_receipts (corpus_sha256, binding) "
        "WHERE receipt_kind = 'CANDIDATE_DECISION'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_trading_evidence_future_capture_protocol_once "
        "ON trading_evidence_clock_receipts (protocol_sha256) "
        "WHERE receipt_kind = 'FUTURE_CAPTURE'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_trading_evidence_future_drain_protocol_once "
        "ON trading_evidence_clock_receipts (protocol_sha256) "
        "WHERE receipt_kind = 'FUTURE_DRAIN'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_trading_evidence_future_protocol_once "
        "ON trading_evidence_clock_receipts (protocol_sha256) "
        "WHERE receipt_kind = 'FUTURE_RESULT'"
    )
    op.execute(
        "CREATE TRIGGER trg_trading_evidence_clock_receipts_append_only BEFORE UPDATE OR DELETE "
        "ON trading_evidence_clock_receipts FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )

    op.execute(
        """
        CREATE TABLE trading_nautilus_runtime_starts (
          start_sha256 TEXT PRIMARY KEY,
          runtime_id UUID NOT NULL UNIQUE,
          runtime_revision TEXT NOT NULL,
          image_digest TEXT NOT NULL,
          nautilus_version TEXT NOT NULL,
          nautilus_source_git_commit TEXT NOT NULL,
          nautilus_wheel_identity TEXT NOT NULL,
          started_at_ms BIGINT NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_nautilus_start_sha_check
            CHECK (start_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_nautilus_source_sha_check
            CHECK (nautilus_source_git_commit ~ '^[0-9a-f]{40}$'),
          CONSTRAINT trading_nautilus_start_clock_check CHECK (started_at_ms > 0),
          CONSTRAINT trading_nautilus_start_identity_check CHECK (
            length(runtime_revision) > 0 AND length(image_digest) > 0
            AND length(nautilus_version) > 0 AND length(nautilus_wheel_identity) > 0
            AND payload ->> 'start_version' = 'nautilus_runtime_start_v1'
            AND (payload ->> 'runtime_id')::UUID = runtime_id
            AND payload ->> 'runtime_revision' = runtime_revision
            AND payload ->> 'image_digest' = image_digest
            AND payload ->> 'nautilus_version' = nautilus_version
            AND payload ->> 'nautilus_source_git_commit' = nautilus_source_git_commit
            AND payload ->> 'nautilus_wheel_identity' = nautilus_wheel_identity
            AND (payload ->> 'started_at_ms')::BIGINT = started_at_ms
          )
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_nautilus_runtime_starts_append_only BEFORE UPDATE OR DELETE "
        "ON trading_nautilus_runtime_starts FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )

    op.execute("ALTER TABLE trading_production_promotion_grants ADD COLUMN sealed_corpus_sha256 TEXT NOT NULL")
    op.execute("ALTER TABLE trading_production_promotion_grants ADD COLUMN locked_future_report_sha256 TEXT NOT NULL")
    op.execute(
        "ALTER TABLE trading_production_promotion_grants ADD CONSTRAINT trading_promotion_grant_corpus_sha_check "
        "CHECK (sealed_corpus_sha256 ~ '^[0-9a-f]{64}$')"
    )
    op.execute(
        "ALTER TABLE trading_production_promotion_grants ADD CONSTRAINT trading_promotion_grant_future_sha_check "
        "CHECK (locked_future_report_sha256 ~ '^[0-9a-f]{64}$')"
    )
    op.execute(
        "ALTER TABLE trading_production_promotion_grants ADD CONSTRAINT trading_promotion_grant_future_fk "
        "FOREIGN KEY (locked_future_report_sha256) "
        "REFERENCES trading_evidence_clock_receipts(artifact_sha256) ON DELETE RESTRICT"
    )
    op.execute(
        "ALTER TABLE trading_production_promotion_grants ADD CONSTRAINT trading_promotion_grant_evidence_payload_check "
        "CHECK (payload ->> 'sealed_corpus_sha256' = sealed_corpus_sha256 "
        "AND payload ->> 'locked_future_report_sha256' = locked_future_report_sha256 "
        "AND jsonb_typeof(payload -> 'allowed_capability_entry_ids') = 'array' "
        "AND jsonb_array_length(payload -> 'allowed_capability_entry_ids') = 1)"
    )
    op.execute(
        """
        CREATE FUNCTION validate_trading_promotion_future_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          result_row trading_evidence_clock_receipts%ROWTYPE;
        BEGIN
          SELECT * INTO result_row
            FROM trading_evidence_clock_receipts
           WHERE artifact_sha256 = NEW.locked_future_report_sha256;
          IF NOT FOUND
            OR result_row.receipt_kind <> 'FUTURE_RESULT'
            OR result_row.terminal <> 'PROMOTE'
            OR result_row.binding <> NEW.binding
            OR result_row.corpus_sha256 <> NEW.sealed_corpus_sha256
          THEN
            RAISE EXCEPTION 'trading_promotion_future_evidence_invalid';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_promotion_future_evidence "
        "BEFORE INSERT ON trading_production_promotion_grants FOR EACH ROW "
        "EXECUTE FUNCTION validate_trading_promotion_future_evidence()"
    )

    op.execute("REVOKE ALL ON trading_evidence_clock_receipts, trading_nautilus_runtime_starts FROM PUBLIC")
    for role in ("tracefold_serve", "tracefold_workers", "tracefold_nautilus"):
        op.execute(f"GRANT SELECT ON trading_evidence_clock_receipts TO {role}")
        op.execute(f"GRANT SELECT ON trading_nautilus_runtime_starts TO {role}")
    op.execute("GRANT INSERT ON trading_evidence_clock_receipts TO tracefold_workers")
    op.execute("GRANT INSERT ON trading_nautilus_runtime_starts TO tracefold_nautilus")


def downgrade() -> None:
    raise RuntimeError("trading_evidence_clock_v1_downgrade_unsupported")
