"""Append-only persistence for the Production V3 evidence clock."""

from __future__ import annotations

import json
from typing import Any

from ..contracts import canonical_sha256
from ..evidence_clock import (
    CandidateDecisionReceiptV1,
    CandidateDecisionV1,
    CandidateLockedV1,
    DiscoveryCorpusReceiptV1,
    FutureCaptureBatchV1,
    FutureCaptureReceiptV1,
    FutureDrainReceiptV1,
    FutureHoldoutResultReceiptV1,
    FutureHoldoutResultV1,
    PreparedFutureCaptureBatch,
)
from ..evidence_verification import NautilusRuntimeStartV1, PreparedProductionReleaseRegistration
from .sql_values import _dumps


class EvidenceStorage:
    conn: Any

    def register_production_release(self, prepared: PreparedProductionReleaseRegistration) -> dict[str, Any]:
        """Bind a future window to the current Workers and observed Serve process before it starts."""

        inserted = self.conn.execute(
            """
            INSERT INTO trading_production_release_registrations (
              release_sha256, window_sha256, release_tag, git_commit_sha, oci_image_digest,
              window_start_ms, window_end_ms,
              workers_runtime_id, workers_runtime_revision, workers_image_digest, workers_started_at_ms,
              serve_runtime_id, serve_runtime_revision, serve_image_digest,
              serve_started_at_ms, serve_measured_at_ms, payload
            )
            SELECT
              %(release_sha)s, %(window_sha)s, %(release_tag)s, %(git_commit)s, %(image)s,
              %(window_start)s, %(window_end)s,
              runtime_id, runtime_revision, image_digest, started_at_ms,
              %(serve_id)s, %(serve_revision)s, %(serve_image)s,
              %(serve_started)s, %(serve_measured)s, %(payload)s::text::jsonb
              FROM workers_runtime
             WHERE singleton_key
               AND lifecycle_state = 'running'
               AND runtime_revision = %(git_commit)s
               AND image_digest = %(image)s
               AND heartbeat_at_ms >= trading_evidence_now_ms() - 15000
               AND trading_evidence_now_ms() < %(window_start)s
            ON CONFLICT (release_sha256) DO NOTHING
            RETURNING release_sha256
            """,
            {
                "release_sha": prepared.release_sha256,
                "window_sha": prepared.window_sha256,
                "release_tag": prepared.release_tag,
                "git_commit": prepared.git_commit_sha,
                "image": prepared.oci_image_digest,
                "window_start": prepared.window_start_ms,
                "window_end": prepared.window_end_ms,
                "serve_id": prepared.serve_runtime_id,
                "serve_revision": prepared.serve_runtime_revision,
                "serve_image": prepared.serve_image_digest,
                "serve_started": prepared.serve_started_at_ms,
                "serve_measured": prepared.serve_measured_at_ms,
                "payload": prepared.payload_json,
            },
        ).fetchone()
        persisted = self.production_release_registration(prepared.release_sha256)
        if persisted is None:
            raise RuntimeError("evidence_release_registration_runtime_mismatch")
        if str(persisted["window_sha256"]) != prepared.window_sha256 or persisted["payload"] != json.loads(
            prepared.payload_json
        ):
            raise RuntimeError("evidence_release_registration_identity_conflict")
        return {**persisted, "inserted": inserted is not None}

    def production_release_registration(self, release_sha256: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM trading_production_release_registrations WHERE release_sha256 = %s",
            (release_sha256,),
        ).fetchone()
        return None if row is None else dict(row)

    def append_nautilus_runtime_start(self, value: NautilusRuntimeStartV1) -> bool:
        payload = value.model_dump(mode="json")
        inserted = self.conn.execute(
            """
            INSERT INTO trading_nautilus_runtime_starts (
              start_sha256, runtime_id, runtime_revision, image_digest,
              nautilus_version, nautilus_source_git_commit, nautilus_wheel_identity,
              started_at_ms, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (start_sha256) DO NOTHING
            RETURNING start_sha256
            """,
            (
                value.start_sha256,
                value.runtime_id,
                value.runtime_revision,
                value.image_digest,
                value.nautilus_version,
                value.nautilus_source_git_commit,
                value.nautilus_wheel_identity,
                value.started_at_ms,
                _dumps(payload),
            ),
        ).fetchone()
        persisted = self.conn.execute(
            "SELECT payload FROM trading_nautilus_runtime_starts WHERE start_sha256 = %s",
            (value.start_sha256,),
        ).fetchone()
        if persisted is None or persisted["payload"] != payload:
            raise RuntimeError("nautilus_runtime_start_identity_conflict")
        return inserted is not None

    def append_future_capture_batch(self, prepared: PreparedFutureCaptureBatch) -> bool:
        row = self.conn.execute(
            """
            WITH inserted AS (
              INSERT INTO trading_evidence_future_capture_batches (
                protocol_sha256, batch_start_ms, batch_end_ms, captured_at_ms,
                capture_lag_ms, batch_sha256, candidate_receipt_sha256, binding,
                source_count, late_source_count, catalog_missing_count,
                collector_connected, missing_source_bps, late_source_bps,
                catalog_missing_bps, bar_continuity_bps, funding_continuity_bps,
                artifact_integrity_sha256, payload
              ) VALUES (
                %(protocol)s, %(start)s, %(end)s, %(captured)s, %(lag)s, %(batch)s,
                %(candidate)s, %(binding)s, %(sources)s, %(late)s, %(catalog_missing)s,
                %(collector_connected)s, %(missing_bps)s, %(late_bps)s,
                %(catalog_missing_bps)s, %(bar_bps)s, %(funding_bps)s, %(integrity_sha)s,
                %(payload)s::text::jsonb
              )
              ON CONFLICT (protocol_sha256, batch_start_ms) DO NOTHING
              RETURNING batch_sha256
            ), verified AS (
              SELECT batch_sha256, true AS inserted FROM inserted
              UNION ALL
              SELECT existing.batch_sha256, false AS inserted
                FROM trading_evidence_future_capture_batches existing
               WHERE existing.protocol_sha256 = %(protocol)s
                 AND existing.batch_start_ms = %(start)s
                 AND existing.batch_sha256 = %(batch)s
                 AND existing.payload = %(payload)s::text::jsonb
                 AND NOT EXISTS (SELECT 1 FROM inserted)
            )
            SELECT batch_sha256, inserted FROM verified
            """,
            {
                "protocol": prepared.protocol_sha256,
                "start": prepared.batch_start_ms,
                "end": prepared.batch_end_ms,
                "captured": prepared.captured_at_ms,
                "lag": prepared.capture_lag_ms,
                "batch": prepared.batch_sha256,
                "candidate": prepared.candidate_receipt_sha256,
                "binding": prepared.binding,
                "sources": prepared.source_count,
                "late": prepared.late_source_count,
                "catalog_missing": prepared.catalog_missing_count,
                "collector_connected": prepared.collector_connected,
                "missing_bps": prepared.missing_source_bps,
                "late_bps": prepared.late_source_bps,
                "catalog_missing_bps": prepared.catalog_missing_bps,
                "bar_bps": prepared.bar_continuity_bps,
                "funding_bps": prepared.funding_continuity_bps,
                "integrity_sha": prepared.artifact_integrity_sha256,
                "payload": prepared.payload_json,
            },
        ).fetchone()
        if row is None:
            raise RuntimeError("evidence_future_capture_batch_identity_conflict")
        return bool(row["inserted"])

    def future_capture_batches(self, protocol_sha256: str) -> tuple[FutureCaptureBatchV1, ...]:
        rows = self.conn.execute(
            "SELECT payload FROM trading_evidence_future_capture_batches "
            "WHERE protocol_sha256 = %s ORDER BY batch_start_ms",
            (protocol_sha256,),
        ).fetchall()
        return tuple(FutureCaptureBatchV1.model_validate(row["payload"]) for row in rows)

    def append_discovery_corpus_receipt(self, value: DiscoveryCorpusReceiptV1) -> bool:
        payload = self._payload(
            receipt_sha256=value.receipt_sha256,
            receipt_kind="DISCOVERY_CORPUS",
            terminal=value.terminal,
            binding=None,
            parent_receipt_sha256=None,
            artifact_sha256=value.artifact_sha256,
            corpus_sha256=value.corpus_sha256,
            protocol_sha256=None,
            receipt=value.model_dump(mode="json"),
            evidence=None,
        )
        return self._append_receipt(payload=payload, created_at_ms=value.created_at_ms)

    def append_candidate_decision_receipt(
        self,
        receipt: CandidateDecisionReceiptV1,
        decision: CandidateDecisionV1,
    ) -> bool:
        artifact_sha256 = canonical_sha256(decision.model_dump(mode="json"))
        if (
            receipt.terminal != decision.terminal
            or receipt.binding != decision.binding
            or receipt.sealed_corpus_sha256 != decision.sealed_corpus_sha256
            or receipt.artifact_sha256 != artifact_sha256
            or receipt.protocol_sha256 != decision.protocol_sha256
        ):
            raise ValueError("evidence_candidate_receipt_mismatch")
        corpus = self._receipt_for_artifact(receipt.sealed_corpus_sha256, kind="DISCOVERY_CORPUS")
        if corpus is None:
            raise ValueError("evidence_candidate_corpus_receipt_missing")
        payload = self._payload(
            receipt_sha256=receipt.receipt_sha256,
            receipt_kind="CANDIDATE_DECISION",
            terminal=receipt.terminal,
            binding=receipt.binding,
            parent_receipt_sha256=str(corpus["receipt_sha256"]),
            artifact_sha256=receipt.artifact_sha256,
            corpus_sha256=receipt.sealed_corpus_sha256,
            protocol_sha256=receipt.protocol_sha256,
            receipt=receipt.model_dump(mode="json"),
            evidence=decision.model_dump(mode="json"),
        )
        return self._append_receipt(payload=payload, created_at_ms=receipt.created_at_ms)

    def append_future_holdout_result_receipt(
        self,
        receipt: FutureHoldoutResultReceiptV1,
        result: FutureHoldoutResultV1,
    ) -> bool:
        if (
            receipt.terminal != result.terminal
            or receipt.binding != result.binding
            or receipt.candidate_receipt_sha256 != result.candidate_receipt_sha256
            or receipt.protocol_sha256 != result.protocol_sha256
            or receipt.sealed_corpus_sha256 != result.sealed_corpus_sha256
            or receipt.report_sha256 != result.report_sha256
            or receipt.artifact_sha256 != result.report_sha256
        ):
            raise ValueError("evidence_future_receipt_mismatch")
        drain = self._receipt_for_artifact(result.future_drain_sha256, kind="FUTURE_DRAIN")
        if drain is None:
            raise ValueError("evidence_future_drain_receipt_missing")
        drain_receipt = FutureDrainReceiptV1.model_validate(drain["payload"]["receipt"])
        if (
            drain["terminal"] != "FUTURE_DRAIN_SEALED"
            or drain["binding"] != receipt.binding
            or drain["corpus_sha256"] != receipt.sealed_corpus_sha256
            or drain["protocol_sha256"] != receipt.protocol_sha256
            or drain_receipt.candidate_receipt_sha256 != receipt.candidate_receipt_sha256
            or drain_receipt.capture_sha256 != result.future_capture_sha256
            or drain_receipt.drain_sha256 != result.future_drain_sha256
        ):
            raise ValueError("evidence_future_drain_receipt_mismatch")
        payload = self._payload(
            receipt_sha256=receipt.receipt_sha256,
            receipt_kind="FUTURE_RESULT",
            terminal=receipt.terminal,
            binding=receipt.binding,
            parent_receipt_sha256=str(drain["receipt_sha256"]),
            artifact_sha256=receipt.artifact_sha256,
            corpus_sha256=receipt.sealed_corpus_sha256,
            protocol_sha256=receipt.protocol_sha256,
            receipt=receipt.model_dump(mode="json"),
            evidence=result.model_dump(mode="json"),
        )
        return self._append_receipt(payload=payload, created_at_ms=receipt.created_at_ms)

    def append_future_drain_receipt(self, receipt: FutureDrainReceiptV1) -> bool:
        capture = self.evidence_clock_receipt(receipt.capture_receipt_sha256)
        if capture is None or capture["receipt_kind"] != "FUTURE_CAPTURE":
            raise ValueError("evidence_future_capture_receipt_missing")
        if (
            capture["terminal"] != "FUTURE_CAPTURE_SEALED"
            or capture["binding"] != receipt.binding
            or capture["corpus_sha256"] != receipt.sealed_corpus_sha256
            or capture["protocol_sha256"] != receipt.protocol_sha256
            or capture["parent_receipt_sha256"] != receipt.candidate_receipt_sha256
            or capture["artifact_sha256"] != receipt.capture_sha256
        ):
            raise ValueError("evidence_future_capture_receipt_mismatch")
        payload = self._payload(
            receipt_sha256=receipt.receipt_sha256,
            receipt_kind="FUTURE_DRAIN",
            terminal=receipt.terminal,
            binding=receipt.binding,
            parent_receipt_sha256=receipt.capture_receipt_sha256,
            artifact_sha256=receipt.artifact_sha256,
            corpus_sha256=receipt.sealed_corpus_sha256,
            protocol_sha256=receipt.protocol_sha256,
            receipt=receipt.model_dump(mode="json"),
            evidence=None,
        )
        return self._append_receipt(payload=payload, created_at_ms=receipt.created_at_ms)

    def append_future_capture_receipt(self, receipt: FutureCaptureReceiptV1) -> bool:
        candidate = self.evidence_clock_receipt(receipt.candidate_receipt_sha256)
        if candidate is None or candidate["receipt_kind"] != "CANDIDATE_DECISION":
            raise ValueError("evidence_future_candidate_receipt_missing")
        if (
            candidate["terminal"] != "CANDIDATE_LOCKED"
            or candidate["binding"] != receipt.binding
            or candidate["corpus_sha256"] != receipt.sealed_corpus_sha256
            or candidate["protocol_sha256"] != receipt.protocol_sha256
        ):
            raise ValueError("evidence_future_candidate_receipt_mismatch")
        payload = self._payload(
            receipt_sha256=receipt.receipt_sha256,
            receipt_kind="FUTURE_CAPTURE",
            terminal=receipt.terminal,
            binding=receipt.binding,
            parent_receipt_sha256=receipt.candidate_receipt_sha256,
            artifact_sha256=receipt.artifact_sha256,
            corpus_sha256=receipt.sealed_corpus_sha256,
            protocol_sha256=receipt.protocol_sha256,
            receipt=receipt.model_dump(mode="json"),
            evidence=None,
        )
        return self._append_receipt(payload=payload, created_at_ms=receipt.created_at_ms)

    def evidence_clock_receipt(self, receipt_sha256: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM trading_evidence_clock_receipts WHERE receipt_sha256 = %s",
            (receipt_sha256,),
        ).fetchone()
        return None if row is None else dict(row)

    def evidence_clock_receipt_for_artifact(self, artifact_sha256: str, *, kind: str) -> dict[str, Any] | None:
        return self._receipt_for_artifact(artifact_sha256, kind=kind)

    def future_holdout_result_for_artifact(self, artifact_sha256: str) -> FutureHoldoutResultV1 | None:
        row = self._receipt_for_artifact(artifact_sha256, kind="FUTURE_RESULT")
        if row is None:
            return None
        return FutureHoldoutResultV1.model_validate(row["payload"]["evidence"])

    def future_holdout_receipt_for_protocol(self, protocol_sha256: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM trading_evidence_clock_receipts
            WHERE protocol_sha256 = %s AND receipt_kind = 'FUTURE_RESULT'
            """,
            (protocol_sha256,),
        ).fetchone()
        return None if row is None else dict(row)

    def future_drain_receipt_for_protocol(self, protocol_sha256: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM trading_evidence_clock_receipts
            WHERE protocol_sha256 = %s AND receipt_kind = 'FUTURE_DRAIN'
            """,
            (protocol_sha256,),
        ).fetchone()
        return None if row is None else dict(row)

    def future_capture_receipt_for_protocol(self, protocol_sha256: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM trading_evidence_clock_receipts
            WHERE protocol_sha256 = %s AND receipt_kind = 'FUTURE_CAPTURE'
            """,
            (protocol_sha256,),
        ).fetchone()
        return None if row is None else dict(row)

    def locked_candidate_for_receipt(self, receipt_sha256: str) -> CandidateLockedV1 | None:
        row = self.evidence_clock_receipt(receipt_sha256)
        if row is None or row["receipt_kind"] != "CANDIDATE_DECISION" or row["terminal"] != "CANDIDATE_LOCKED":
            return None
        return CandidateLockedV1.model_validate(row["payload"]["evidence"])

    def _receipt_for_artifact(self, artifact_sha256: str, *, kind: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM trading_evidence_clock_receipts WHERE artifact_sha256 = %s AND receipt_kind = %s",
            (artifact_sha256, kind),
        ).fetchone()
        return None if row is None else dict(row)

    def _append_receipt(self, *, payload: dict[str, Any], created_at_ms: int) -> bool:
        if int(created_at_ms) != int(payload["receipt"]["created_at_ms"]):
            raise ValueError("evidence_receipt_created_at_mismatch")
        inserted = self.conn.execute(
            """
            INSERT INTO trading_evidence_clock_receipts (
              receipt_sha256, receipt_kind, terminal, binding, parent_receipt_sha256,
              artifact_sha256, corpus_sha256, protocol_sha256, created_at_ms, payload
            ) VALUES (
              %(receipt_sha256)s, %(receipt_kind)s, %(terminal)s, %(binding)s,
              %(parent_receipt_sha256)s, %(artifact_sha256)s, %(corpus_sha256)s,
              %(protocol_sha256)s, %(created_at_ms)s, %(payload)s::jsonb
            )
            ON CONFLICT (receipt_sha256) DO NOTHING
            RETURNING receipt_sha256
            """,
            {**payload, "created_at_ms": int(created_at_ms), "payload": _dumps(payload)},
        ).fetchone()
        persisted = self.evidence_clock_receipt(str(payload["receipt_sha256"]))
        if persisted is None or persisted["payload"] != payload:
            raise RuntimeError("evidence_receipt_identity_conflict")
        return inserted is not None

    @staticmethod
    def _payload(
        *,
        receipt_sha256: str,
        receipt_kind: str,
        terminal: str,
        binding: str | None,
        parent_receipt_sha256: str | None,
        artifact_sha256: str,
        corpus_sha256: str,
        protocol_sha256: str | None,
        receipt: dict[str, Any],
        evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "receipt_sha256": receipt_sha256,
            "receipt_kind": receipt_kind,
            "terminal": terminal,
            "binding": binding,
            "parent_receipt_sha256": parent_receipt_sha256,
            "artifact_sha256": artifact_sha256,
            "corpus_sha256": corpus_sha256,
            "protocol_sha256": protocol_sha256,
            "receipt": receipt,
        }
        if evidence is not None:
            payload["evidence"] = evidence
        return payload


__all__ = ["EvidenceStorage"]
