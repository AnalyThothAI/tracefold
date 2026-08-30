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

    # Freeze the source identity at OI-ledger insertion.  The old projection recovered these fields
    # from a mutable Event leader and the currently active learning epoch, so an unrelated later
    # merge, rollback, or deployment could rewrite a historical evidence population.
    op.execute(
        """
        ALTER TABLE news_oi_signals
          ADD COLUMN source_item_id TEXT,
          ADD COLUMN source_venue TEXT,
          ADD COLUMN available_at_ms BIGINT,
          ADD COLUMN learning_epoch TEXT;
        WITH frozen AS (
          SELECT signal.event_id, signal.metric_version,
                 (SELECT member.item_id
                    FROM news_event_members member
                   WHERE member.event_id = signal.event_id
                   ORDER BY member.joined_at_ms, member.item_id
                   LIMIT 1) AS source_item_id,
                 COALESCE(
                   (SELECT epoch.epoch_id
                      FROM news_learning_epochs epoch
                     WHERE epoch.starts_at_ms <= signal.created_at_ms
                     ORDER BY epoch.starts_at_ms DESC, epoch.epoch_id
                     LIMIT 1),
                   'unproven'
                 ) AS learning_epoch
            FROM news_oi_signals signal
        )
        UPDATE news_oi_signals signal
           SET source_item_id = frozen.source_item_id,
               source_venue = item.provider_metadata ->> 'source',
               available_at_ms = signal.created_at_ms,
               learning_epoch = frozen.learning_epoch
          FROM frozen
          JOIN news_items item ON item.item_id = frozen.source_item_id
         WHERE signal.event_id = frozen.event_id
           AND signal.metric_version = frozen.metric_version;
        ALTER TABLE news_oi_signals
          ALTER COLUMN source_item_id SET NOT NULL,
          ALTER COLUMN available_at_ms SET NOT NULL,
          ALTER COLUMN learning_epoch SET NOT NULL,
          ADD CONSTRAINT news_oi_signals_source_item_fk
            FOREIGN KEY (source_item_id) REFERENCES news_items(item_id) ON DELETE CASCADE,
          ADD CONSTRAINT news_oi_signals_available_clock_check
            CHECK (available_at_ms >= observed_at_ms AND available_at_ms >= created_at_ms),
          ADD CONSTRAINT news_oi_signals_learning_epoch_nonempty CHECK (learning_epoch <> '');
        """
    )
    op.execute(
        """
        ALTER TABLE workers_runtime
          ADD COLUMN runtime_revision TEXT,
          ADD COLUMN image_digest TEXT;
        UPDATE workers_runtime SET runtime_revision = 'UNVERSIONED', image_digest = 'UNVERSIONED';
        ALTER TABLE workers_runtime
          ALTER COLUMN runtime_revision SET NOT NULL,
          ALTER COLUMN image_digest SET NOT NULL,
          ADD CONSTRAINT workers_runtime_release_identity_nonempty
            CHECK (runtime_revision <> '' AND image_digest <> '');
        ALTER TABLE trading_candidate_gate_decisions ADD COLUMN release_revision TEXT;
        UPDATE trading_candidate_gate_decisions SET release_revision = 'UNVERSIONED';
        ALTER TABLE trading_candidate_gate_decisions
          ALTER COLUMN release_revision SET NOT NULL,
          ADD CONSTRAINT trading_candidate_gate_release_nonempty CHECK (release_revision <> '');
        """
    )
    op.execute(
        """
        CREATE FUNCTION store_trading_venue_catalog_snapshot(
          p_digest TEXT,
          p_binding TEXT,
          p_captured_at_ms BIGINT,
          p_stale_after_ms BIGINT,
          p_instrument_count INTEGER,
          p_payload JSONB,
          p_now_ms BIGINT
        ) RETURNS TABLE(identity_valid BOOLEAN, activated_binding TEXT)
        LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
          PERFORM pg_advisory_xact_lock(hashtextextended(p_digest, 0));
          INSERT INTO trading_venue_catalog_snapshots (
            snapshot_sha256, binding, captured_at_ms, stale_after_ms,
            provider_instrument_count, payload, created_at_ms
          ) VALUES (
            p_digest, p_binding, p_captured_at_ms, p_stale_after_ms,
            p_instrument_count, p_payload, p_now_ms
          )
          ON CONFLICT (snapshot_sha256) DO NOTHING;

          SELECT EXISTS (
            SELECT 1
              FROM trading_venue_catalog_snapshots existing
             WHERE existing.snapshot_sha256 = p_digest
               AND existing.binding = p_binding
               AND existing.captured_at_ms = p_captured_at_ms
               AND existing.stale_after_ms = p_stale_after_ms
               AND existing.provider_instrument_count = p_instrument_count
               AND existing.payload = p_payload
          ) INTO identity_valid;

          activated_binding := NULL;
          IF identity_valid THEN
            UPDATE trading_binding_runtime AS runtime
               SET catalog_state = 'ready',
                   catalog_snapshot_sha256 = p_digest,
                   catalog_captured_at_ms = p_captured_at_ms,
                   capability_state = CASE
                     WHEN runtime.capability_snapshot_sha256 IS NULL THEN 'missing'
                     WHEN EXISTS (
                       SELECT 1 FROM trading_execution_capability_snapshots capability
                        WHERE capability.snapshot_sha256 = runtime.capability_snapshot_sha256
                          AND capability.catalog_snapshot_sha256 = p_digest
                     ) THEN runtime.capability_state
                     ELSE 'stale'
                   END,
                   reason = CASE
                     WHEN credential_state = 'unconfigured' THEN 'credentials_unconfigured'
                     WHEN credential_state = 'invalid' THEN 'credentials_invalid'
                     WHEN runtime_state = 'stopped' THEN 'binding_adapter_unavailable'
                     WHEN runtime_state <> 'ready' THEN 'binding_unready'
                     ELSE NULL
                   END,
                   updated_at_ms = p_now_ms
             WHERE runtime.binding = p_binding
         RETURNING runtime.binding INTO activated_binding;
          END IF;
          RETURN NEXT;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION store_trading_venue_catalog_snapshot("
        "TEXT, TEXT, BIGINT, BIGINT, INTEGER, JSONB, BIGINT) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION store_trading_venue_catalog_snapshot("
        "TEXT, TEXT, BIGINT, BIGINT, INTEGER, JSONB, BIGINT) TO tracefold_workers"
    )

    op.execute(
        """
        CREATE FUNCTION trading_evidence_now_ms() RETURNS BIGINT
        LANGUAGE sql VOLATILE AS $$
          SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::BIGINT
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION trading_evidence_now_ms() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION trading_evidence_now_ms() TO tracefold_workers")

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
          recorded_at_ms BIGINT NOT NULL DEFAULT
            (trading_evidence_now_ms()),
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
          CONSTRAINT trading_evidence_created_at_check
            CHECK (created_at_ms > 0 AND recorded_at_ms >= created_at_ms),
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
          future_batch_count INTEGER;
        BEGIN
          NEW.recorded_at_ms := trading_evidence_now_ms();
          IF NEW.receipt_kind = 'DISCOVERY_CORPUS' THEN
            RETURN NEW;
          END IF;
          SELECT * INTO parent_row
            FROM trading_evidence_clock_receipts
           WHERE receipt_sha256 = NEW.parent_receipt_sha256;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'trading_evidence_parent_missing';
          END IF;
          IF NEW.recorded_at_ms <= parent_row.recorded_at_ms THEN
            RAISE EXCEPTION 'trading_evidence_parent_clock_invalid';
          END IF;
          IF NEW.receipt_kind = 'CANDIDATE_DECISION' AND (
            parent_row.receipt_kind <> 'DISCOVERY_CORPUS'
            OR parent_row.artifact_sha256 <> NEW.corpus_sha256
          ) THEN
            RAISE EXCEPTION 'trading_evidence_candidate_parent_invalid';
          END IF;
          IF NEW.receipt_kind = 'CANDIDATE_DECISION'
            AND NEW.terminal = 'CANDIDATE_LOCKED'
            AND NEW.recorded_at_ms >= (NEW.payload #>> '{evidence,statistics,future_start_ms}')::BIGINT
          THEN
            RAISE EXCEPTION 'trading_evidence_candidate_recorded_after_future_start';
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
          IF NEW.receipt_kind = 'FUTURE_CAPTURE' THEN
            SELECT count(*) INTO future_batch_count
              FROM trading_evidence_future_capture_batches
             WHERE protocol_sha256 = NEW.protocol_sha256;
            IF parent_row.receipt_kind <> 'CANDIDATE_DECISION'
              OR parent_row.terminal <> 'CANDIDATE_LOCKED'
              OR parent_row.binding <> NEW.binding
              OR parent_row.corpus_sha256 <> NEW.corpus_sha256
              OR parent_row.protocol_sha256 <> NEW.protocol_sha256
              OR (NEW.payload #>> '{receipt,batch_count}')::INTEGER <> future_batch_count
            THEN
              RAISE EXCEPTION 'trading_evidence_future_capture_parent_invalid';
            END IF;
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
        CREATE TABLE trading_evidence_future_capture_batches (
          protocol_sha256 TEXT NOT NULL,
          batch_start_ms BIGINT NOT NULL,
          batch_end_ms BIGINT NOT NULL,
          captured_at_ms BIGINT NOT NULL,
          recorded_at_ms BIGINT NOT NULL DEFAULT
            (trading_evidence_now_ms()),
          capture_lag_ms BIGINT NOT NULL,
          batch_sha256 TEXT NOT NULL UNIQUE,
          candidate_receipt_sha256 TEXT NOT NULL
            REFERENCES trading_evidence_clock_receipts(receipt_sha256) ON DELETE RESTRICT,
          binding TEXT NOT NULL,
          source_count INTEGER NOT NULL,
          late_source_count INTEGER NOT NULL,
          catalog_missing_count INTEGER NOT NULL,
          collector_connected BOOLEAN NOT NULL,
          missing_source_bps INTEGER NOT NULL,
          late_source_bps INTEGER NOT NULL,
          catalog_missing_bps INTEGER NOT NULL,
          bar_continuity_bps INTEGER NOT NULL,
          funding_continuity_bps INTEGER NOT NULL,
          artifact_integrity_sha256 TEXT NOT NULL,
          payload JSONB NOT NULL,
          PRIMARY KEY (protocol_sha256, batch_start_ms),
          CONSTRAINT trading_future_batch_protocol_sha_check
            CHECK (protocol_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_future_batch_sha_check CHECK (batch_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_future_batch_binding_check
            CHECK (binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')),
          CONSTRAINT trading_future_batch_clock_check
            CHECK (batch_start_ms >= 0 AND batch_end_ms > batch_start_ms
              AND captured_at_ms >= batch_end_ms
              AND recorded_at_ms >= captured_at_ms
              AND capture_lag_ms = captured_at_ms - batch_end_ms),
          CONSTRAINT trading_future_batch_count_check CHECK (
            source_count >= 0 AND late_source_count >= 0 AND catalog_missing_count >= 0
            AND late_source_count <= source_count AND catalog_missing_count <= source_count
            AND missing_source_bps BETWEEN 0 AND 10000
            AND late_source_bps BETWEEN 0 AND 10000
            AND catalog_missing_bps BETWEEN 0 AND 10000
            AND bar_continuity_bps BETWEEN 0 AND 10000
            AND funding_continuity_bps BETWEEN 0 AND 10000
          ),
          CONSTRAINT trading_future_batch_integrity_sha_check
            CHECK (artifact_integrity_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_future_batch_payload_check CHECK (
            payload ->> 'batch_version' = 'future_capture_batch_v1'
            AND payload ->> 'protocol_sha256' = protocol_sha256
            AND payload ->> 'candidate_receipt_sha256' = candidate_receipt_sha256
            AND payload ->> 'binding' = binding
            AND (payload ->> 'batch_start_ms')::BIGINT = batch_start_ms
            AND (payload ->> 'batch_end_ms')::BIGINT = batch_end_ms
            AND (payload ->> 'captured_at_ms')::BIGINT = captured_at_ms
            AND (payload ->> 'capture_lag_ms')::BIGINT = capture_lag_ms
            AND (payload ->> 'source_count')::INTEGER = source_count
            AND (payload ->> 'late_source_count')::INTEGER = late_source_count
            AND (payload ->> 'catalog_missing_count')::INTEGER = catalog_missing_count
            AND (payload #>> '{health,collector_connected}')::BOOLEAN = collector_connected
            AND (payload #>> '{health,missing_source_bps}')::INTEGER = missing_source_bps
            AND (payload #>> '{health,late_source_bps}')::INTEGER = late_source_bps
            AND (payload #>> '{health,catalog_missing_bps}')::INTEGER = catalog_missing_bps
            AND (payload #>> '{health,bar_continuity_bps}')::INTEGER = bar_continuity_bps
            AND (payload #>> '{health,funding_continuity_bps}')::INTEGER = funding_continuity_bps
            AND payload #>> '{health,artifact_integrity_sha256}' = artifact_integrity_sha256
            AND jsonb_array_length(payload -> 'sources') = source_count
          )
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_trading_future_capture_batch() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          candidate trading_evidence_clock_receipts%ROWTYPE;
          future_start BIGINT;
          future_end BIGINT;
          capture_interval BIGINT;
          maximum_lag BIGINT;
          expected_start BIGINT;
          expected_end BIGINT;
        BEGIN
          NEW.recorded_at_ms := trading_evidence_now_ms();
          IF EXISTS (
            SELECT 1 FROM trading_evidence_future_capture_batches existing
             WHERE existing.protocol_sha256 = NEW.protocol_sha256
               AND existing.batch_start_ms = NEW.batch_start_ms
               AND existing.batch_sha256 = NEW.batch_sha256
               AND existing.payload = NEW.payload
          ) THEN
            RETURN NEW;
          END IF;
          SELECT * INTO candidate
            FROM trading_evidence_clock_receipts
           WHERE receipt_sha256 = NEW.candidate_receipt_sha256
           FOR UPDATE;
          IF NOT FOUND OR candidate.receipt_kind <> 'CANDIDATE_DECISION'
            OR candidate.terminal <> 'CANDIDATE_LOCKED'
            OR candidate.protocol_sha256 <> NEW.protocol_sha256
            OR candidate.binding <> NEW.binding
          THEN
            RAISE EXCEPTION 'trading_future_batch_candidate_invalid';
          END IF;
          future_start := (candidate.payload #>> '{evidence,statistics,future_start_ms}')::BIGINT;
          future_end := (candidate.payload #>> '{evidence,statistics,future_end_ms}')::BIGINT;
          capture_interval := (candidate.payload #>> '{evidence,statistics,capture_interval_ms}')::BIGINT;
          maximum_lag := (candidate.payload #>> '{evidence,statistics,maximum_capture_lag_ms}')::BIGINT;
          SELECT COALESCE(max(batch_end_ms), future_start) INTO expected_start
            FROM trading_evidence_future_capture_batches
           WHERE protocol_sha256 = NEW.protocol_sha256;
          expected_end := least(expected_start + capture_interval, future_end);
          IF NEW.batch_start_ms <> expected_start OR NEW.batch_end_ms <> expected_end
            OR NEW.recorded_at_ms < expected_end
            OR NEW.recorded_at_ms > expected_end + maximum_lag
          THEN
            RAISE EXCEPTION 'trading_future_batch_clock_invalid';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_future_capture_batch BEFORE INSERT "
        "ON trading_evidence_future_capture_batches FOR EACH ROW "
        "EXECUTE FUNCTION validate_trading_future_capture_batch()"
    )
    op.execute(
        "CREATE TRIGGER trg_trading_future_capture_batches_append_only BEFORE UPDATE OR DELETE "
        "ON trading_evidence_future_capture_batches FOR EACH ROW "
        "EXECUTE FUNCTION reject_trading_append_only_mutation()"
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

    op.execute(
        """
        CREATE TABLE trading_production_release_registrations (
          release_sha256 TEXT PRIMARY KEY,
          window_sha256 TEXT NOT NULL UNIQUE,
          release_tag TEXT NOT NULL,
          git_commit_sha TEXT NOT NULL,
          oci_image_digest TEXT NOT NULL,
          window_start_ms BIGINT NOT NULL,
          window_end_ms BIGINT NOT NULL,
          workers_runtime_id UUID NOT NULL,
          workers_runtime_revision TEXT NOT NULL,
          workers_image_digest TEXT NOT NULL,
          workers_started_at_ms BIGINT NOT NULL,
          serve_runtime_id UUID NOT NULL,
          serve_runtime_revision TEXT NOT NULL,
          serve_image_digest TEXT NOT NULL,
          serve_started_at_ms BIGINT NOT NULL,
          serve_measured_at_ms BIGINT NOT NULL,
          registered_at_ms BIGINT NOT NULL DEFAULT
            (trading_evidence_now_ms()),
          payload JSONB NOT NULL,
          CONSTRAINT trading_release_registration_release_sha_check
            CHECK (release_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_release_registration_window_sha_check
            CHECK (window_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_release_registration_git_sha_check
            CHECK (git_commit_sha ~ '^[0-9a-f]{40}$'),
          CONSTRAINT trading_release_registration_clock_check CHECK (
            window_end_ms > window_start_ms
            AND workers_started_at_ms <= registered_at_ms
            AND serve_started_at_ms <= serve_measured_at_ms
            AND serve_measured_at_ms <= registered_at_ms
            AND registered_at_ms < window_start_ms
          ),
          CONSTRAINT trading_release_registration_runtime_identity_check CHECK (
            workers_runtime_revision = git_commit_sha
            AND workers_image_digest = oci_image_digest
            AND serve_runtime_revision = git_commit_sha
            AND serve_image_digest = oci_image_digest
          ),
          CONSTRAINT trading_release_registration_payload_check CHECK (
            payload ->> 'registration_version' = 'production_release_registration_v1'
            AND payload ->> 'release_sha256' = release_sha256
            AND payload ->> 'window_sha256' = window_sha256
            AND payload #>> '{release,release_tag}' = release_tag
            AND payload #>> '{release,git_commit_sha}' = git_commit_sha
            AND payload #>> '{release,oci_image_digest}' = oci_image_digest
            AND (payload #>> '{release,acceptance_window,start_ms}')::BIGINT = window_start_ms
            AND (payload #>> '{release,acceptance_window,end_ms}')::BIGINT = window_end_ms
            AND (payload #>> '{serve_runtime,runtime_id}')::UUID = serve_runtime_id
            AND payload #>> '{serve_runtime,runtime_revision}' = serve_runtime_revision
            AND payload #>> '{serve_runtime,image_digest}' = serve_image_digest
            AND (payload #>> '{serve_runtime,started_at_ms}')::BIGINT = serve_started_at_ms
            AND (payload #>> '{serve_runtime,measured_at_ms}')::BIGINT = serve_measured_at_ms
          )
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_production_release_registrations_append_only BEFORE UPDATE OR DELETE "
        "ON trading_production_release_registrations FOR EACH ROW "
        "EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )
    op.execute(
        """
        CREATE FUNCTION stamp_trading_release_registration() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          NEW.registered_at_ms := trading_evidence_now_ms();
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_release_registration_clock BEFORE INSERT "
        "ON trading_production_release_registrations FOR EACH ROW "
        "EXECUTE FUNCTION stamp_trading_release_registration()"
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_terminal_intent_revival() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.execution_state = 'TERMINAL' AND NEW.execution_state <> 'TERMINAL' THEN
            RAISE EXCEPTION 'trading_terminal_intent_revival_forbidden';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_terminal_intent_revival BEFORE UPDATE ON trading_intents "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_terminal_intent_revival()"
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

    op.execute(
        "REVOKE ALL ON trading_evidence_clock_receipts, trading_evidence_future_capture_batches, "
        "trading_nautilus_runtime_starts, trading_production_release_registrations FROM PUBLIC"
    )
    for role in ("tracefold_serve", "tracefold_workers", "tracefold_nautilus"):
        op.execute(f"GRANT SELECT ON trading_evidence_clock_receipts TO {role}")
        op.execute(f"GRANT SELECT ON trading_evidence_future_capture_batches TO {role}")
        op.execute(f"GRANT SELECT ON trading_nautilus_runtime_starts TO {role}")
        op.execute(f"GRANT SELECT ON trading_production_release_registrations TO {role}")
    op.execute("GRANT INSERT ON trading_evidence_clock_receipts TO tracefold_workers")
    op.execute("GRANT INSERT ON trading_evidence_future_capture_batches TO tracefold_workers")
    op.execute("GRANT INSERT ON trading_nautilus_runtime_starts TO tracefold_nautilus")
    op.execute("GRANT INSERT ON trading_production_release_registrations TO tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("trading_evidence_clock_v1_downgrade_unsupported")
