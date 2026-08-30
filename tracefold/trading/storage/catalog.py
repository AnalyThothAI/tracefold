"""Durable Decision Plane, binding facts, and venue catalogue snapshots (#350)."""

from __future__ import annotations

from typing import Any

from ..catalog import VenueInstrumentCatalogSnapshotV1
from ..contracts import DecisionRuntimeV1, VenueBinding, VenueBindingRuntimeV1
from .sql_values import _dumps


class CatalogStorage:
    conn: Any

    # ---------------------------------------------------------------- decision
    def decision_runtime(self) -> DecisionRuntimeV1 | None:
        row = self.conn.execute(
            "SELECT state, heartbeat_at_ms, reason, updated_at_ms FROM trading_decision_runtime WHERE id = 1"
        ).fetchone()
        return DecisionRuntimeV1(**dict(row)) if row is not None else None

    def set_decision_runtime(
        self,
        *,
        state: str,
        heartbeat_at_ms: int | None,
        reason: str | None,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_decision_runtime
               SET state = %(state)s,
                   heartbeat_at_ms = %(heartbeat)s,
                   reason = %(reason)s,
                   updated_at_ms = %(now)s
             WHERE id = 1
         RETURNING id
            """,
            {
                "state": state,
                "heartbeat": None if heartbeat_at_ms is None else int(heartbeat_at_ms),
                "reason": reason,
                "now": int(now_ms),
            },
        ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- bindings
    def binding_runtime_rows(self, *, now_ms: int) -> list[VenueBindingRuntimeV1]:
        rows = self.conn.execute(
            """
            SELECT runtime.binding, runtime.credential_state, runtime.credential_fingerprint,
                   runtime.runtime_state, runtime.account_state, runtime.account_generation,
                   CASE
                     WHEN runtime.catalog_state = 'ready'
                      AND snapshot.stale_after_ms IS NOT NULL
                      AND runtime.catalog_captured_at_ms + snapshot.stale_after_ms <= %(now)s
                     THEN 'stale'
                     ELSE runtime.catalog_state
                   END AS catalog_state,
                   runtime.catalog_snapshot_sha256, runtime.catalog_captured_at_ms,
                   runtime.capability_state, runtime.capability_snapshot_sha256,
                   runtime.capability_compiled_at_ms, runtime.capability_compile_error,
                   runtime.execution_binding_sha256, runtime.active_arm_receipt_sha256,
                   runtime.heartbeat_at_ms, runtime.reason, runtime.updated_at_ms
              FROM trading_binding_runtime runtime
              LEFT JOIN trading_venue_catalog_snapshots snapshot
                ON snapshot.snapshot_sha256 = runtime.catalog_snapshot_sha256
             ORDER BY runtime.binding
            """,
            {"now": int(now_ms)},
        ).fetchall()
        return [VenueBindingRuntimeV1(**dict(row)) for row in rows]

    def binding_runtime(self, *, binding: VenueBinding, now_ms: int) -> VenueBindingRuntimeV1 | None:
        row = self.conn.execute(
            """
            SELECT runtime.binding, runtime.credential_state, runtime.credential_fingerprint,
                   runtime.runtime_state, runtime.account_state, runtime.account_generation,
                   CASE
                     WHEN runtime.catalog_state = 'ready'
                      AND snapshot.stale_after_ms IS NOT NULL
                      AND runtime.catalog_captured_at_ms + snapshot.stale_after_ms <= %(now)s
                     THEN 'stale'
                     ELSE runtime.catalog_state
                   END AS catalog_state,
                   runtime.catalog_snapshot_sha256, runtime.catalog_captured_at_ms,
                   runtime.capability_state, runtime.capability_snapshot_sha256,
                   runtime.capability_compiled_at_ms, runtime.capability_compile_error,
                   runtime.execution_binding_sha256, runtime.active_arm_receipt_sha256,
                   runtime.heartbeat_at_ms, runtime.reason, runtime.updated_at_ms
              FROM trading_binding_runtime runtime
              LEFT JOIN trading_venue_catalog_snapshots snapshot
                ON snapshot.snapshot_sha256 = runtime.catalog_snapshot_sha256
             WHERE runtime.binding = %(binding)s
            """,
            {"binding": binding, "now": int(now_ms)},
        ).fetchone()
        return VenueBindingRuntimeV1(**dict(row)) if row is not None else None

    def set_binding_runtime(
        self,
        *,
        binding: VenueBinding,
        credential_state: str,
        credential_fingerprint: str | None,
        runtime_state: str,
        account_state: str,
        heartbeat_at_ms: int | None,
        reason: str | None,
        now_ms: int,
    ) -> bool:
        current = self.conn.execute(
            "SELECT credential_state, credential_fingerprint FROM trading_binding_runtime "
            "WHERE binding = %s FOR UPDATE",
            (binding,),
        ).fetchone()
        if current is None:
            return False
        # Every Workers start re-projects credentials and is an activation boundary. A restart, a new
        # Key or a fingerprint change can therefore never inherit RUNNING capital from an old process.
        capital = self.conn.execute("SELECT control FROM trading_runtime_state WHERE id = 1 FOR UPDATE").fetchone()
        if capital is None:
            raise RuntimeError("trading_runtime_state_missing")
        entering_paused = capital["control"] != "PAUSED"
        self.conn.execute(
            "UPDATE trading_runtime_state SET control = 'PAUSED', "
            "arm_epoch = arm_epoch + CASE WHEN %s THEN 1 ELSE 0 END, updated_at_ms = %s WHERE id = 1",
            (entering_paused, int(now_ms)),
        )
        if entering_paused:
            self.conn.execute(
                "UPDATE trading_binding_runtime SET active_arm_receipt_sha256 = NULL, updated_at_ms = %s",
                (int(now_ms),),
            )
        if credential_state == "unconfigured" and binding == "BINANCE_USDM":
            recovery = self.conn.execute(
                "SELECT 1 FROM trading_intents "
                "WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW') LIMIT 1"
            ).fetchone()
            if recovery is not None:
                reason = "recovery_blocked_credentials_missing"
        row = self.conn.execute(
            """
            UPDATE trading_binding_runtime AS runtime
               SET credential_state = %(credential_state)s,
                   credential_fingerprint = %(credential_fingerprint)s,
                   account_generation = CASE
                     WHEN (credential_state, credential_fingerprint)
                          IS DISTINCT FROM (%(credential_state)s, %(credential_fingerprint)s)
                     THEN account_generation + 1 ELSE account_generation END,
                   execution_binding_sha256 = CASE
                     WHEN (credential_state, credential_fingerprint)
                          IS DISTINCT FROM (%(credential_state)s, %(credential_fingerprint)s)
                     THEN NULL ELSE execution_binding_sha256 END,
                   active_arm_receipt_sha256 = NULL,
                   runtime_state = %(runtime_state)s,
                   account_state = %(account_state)s,
                   heartbeat_at_ms = %(heartbeat)s,
                   reason = %(reason)s,
                   updated_at_ms = %(now)s
             WHERE binding = %(binding)s
         RETURNING binding
            """,
            {
                "binding": binding,
                "credential_state": credential_state,
                "credential_fingerprint": credential_fingerprint,
                "runtime_state": runtime_state,
                "account_state": account_state,
                "heartbeat": None if heartbeat_at_ms is None else int(heartbeat_at_ms),
                "reason": reason,
                "now": int(now_ms),
            },
        ).fetchone()
        return row is not None

    def binding_execution_runtime(
        self,
        *,
        binding: VenueBinding,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT capital.control, runtime.binding, runtime.runtime_state, runtime.account_state,
                   runtime.capability_state, runtime.capability_snapshot_sha256,
                   runtime.execution_binding_sha256, runtime.heartbeat_at_ms
              FROM trading_runtime_state capital
              JOIN trading_binding_runtime runtime ON runtime.binding = %s
             WHERE capital.id = 1
            """
            + (" FOR UPDATE OF capital, runtime" if for_update else ""),
            (binding,),
        ).fetchone()
        return None if row is None else dict(row)

    def set_binding_execution_runtime(
        self,
        *,
        binding: VenueBinding,
        heartbeat_at_ms: int,
        ready: bool,
        readiness_reason: str | None,
        unexpected_exposure: bool,
        now_ms: int,
    ) -> None:
        updated = self.conn.execute(
            """
            UPDATE trading_binding_runtime
               SET runtime_state = CASE WHEN %(ready)s THEN 'ready' ELSE 'faulted' END,
                   account_state = CASE
                     WHEN %(unexpected)s THEN 'exposure_present'
                     WHEN %(ready)s THEN 'reconciled_flat'
                     ELSE account_state
                   END,
                   heartbeat_at_ms = %(heartbeat)s,
                   reason = %(reason)s,
                   updated_at_ms = %(now)s
             WHERE binding = %(binding)s
         RETURNING binding
            """,
            {
                "binding": binding,
                "heartbeat": int(heartbeat_at_ms),
                "ready": bool(ready),
                "reason": readiness_reason,
                "unexpected": bool(unexpected_exposure),
                "now": int(now_ms),
            },
        ).fetchone()
        if updated is None:
            raise RuntimeError(f"trading_binding_runtime_missing:{binding}")

    def set_binding_bootstrap_account_zero(
        self,
        *,
        binding: VenueBinding,
        verified_at_ms: int | None,
        now_ms: int,
        expected_capability_snapshot_sha256: str | None,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_binding_runtime runtime
               SET account_state = CASE WHEN %(verified)s IS NULL THEN 'unknown' ELSE 'reconciled_flat' END,
                   heartbeat_at_ms = %(now)s,
                   reason = CASE WHEN %(verified)s IS NULL THEN 'account_reconciliation_unproven'
                                 ELSE 'bootstrap_account_flat' END,
                   updated_at_ms = %(now)s
              FROM trading_runtime_state capital
             WHERE runtime.binding = %(binding)s
               AND capital.id = 1
               AND capital.control = 'PAUSED'
               AND runtime.capability_snapshot_sha256 IS NOT DISTINCT FROM %(expected)s
               AND NOT EXISTS (
                 SELECT 1 FROM trading_intents
                  WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
               )
         RETURNING runtime.binding
            """,
            {
                "binding": binding,
                "verified": None if verified_at_ms is None else int(verified_at_ms),
                "now": int(now_ms),
                "expected": expected_capability_snapshot_sha256,
            },
        ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- catalogues
    def store_venue_catalog_snapshot(
        self,
        *,
        snapshot: VenueInstrumentCatalogSnapshotV1,
        now_ms: int,
    ) -> None:
        digest = snapshot.snapshot_sha256
        payload = snapshot.model_dump(mode="json")
        self.conn.execute(
            """
            INSERT INTO trading_venue_catalog_snapshots (
              snapshot_sha256, binding, captured_at_ms, stale_after_ms,
              provider_instrument_count, payload, created_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (snapshot_sha256) DO NOTHING
            """,
            (
                digest,
                snapshot.binding,
                int(snapshot.captured_at_ms),
                int(snapshot.stale_after_ms),
                int(snapshot.provider_instrument_count),
                _dumps(payload),
                int(now_ms),
            ),
        )
        stored = self.conn.execute(
            "SELECT payload FROM trading_venue_catalog_snapshots WHERE snapshot_sha256 = %s",
            (digest,),
        ).fetchone()
        if stored is None or stored["payload"] != payload:
            raise RuntimeError("venue_catalog_snapshot_identity_conflict")
        updated = self.conn.execute(
            """
            UPDATE trading_binding_runtime AS runtime
               SET catalog_state = 'ready',
                   catalog_snapshot_sha256 = %(digest)s,
                   catalog_captured_at_ms = %(captured)s,
                   capability_state = CASE
                     WHEN runtime.capability_snapshot_sha256 IS NULL THEN 'missing'
                     WHEN EXISTS (
                       SELECT 1 FROM trading_execution_capability_snapshots capability
                        WHERE capability.snapshot_sha256 = runtime.capability_snapshot_sha256
                          AND capability.catalog_snapshot_sha256 = %(digest)s
                     ) THEN runtime.capability_state
                     ELSE 'stale'
                   END,
                   reason = CASE
                     WHEN credential_state = 'unconfigured' THEN 'credentials_unconfigured'
                     WHEN credential_state = 'invalid' THEN 'credentials_invalid'
                     WHEN runtime_state = 'stopped' THEN 'binding_adapter_unavailable'
                     WHEN runtime_state != 'ready' THEN 'binding_unready'
                     ELSE NULL
                   END,
                   updated_at_ms = %(now)s
             WHERE binding = %(binding)s
         RETURNING binding
            """,
            {
                "binding": snapshot.binding,
                "digest": digest,
                "captured": int(snapshot.captured_at_ms),
                "now": int(now_ms),
            },
        ).fetchone()
        if updated is None:
            raise RuntimeError("venue_catalog_binding_missing")

    def mark_venue_catalog_unavailable(self, *, binding: VenueBinding, reason: str, now_ms: int) -> None:
        updated = self.conn.execute(
            """
            UPDATE trading_binding_runtime
               SET catalog_state = CASE WHEN catalog_snapshot_sha256 IS NULL THEN 'error' ELSE 'stale' END,
                   reason = %(reason)s,
                   updated_at_ms = %(now)s
             WHERE binding = %(binding)s
         RETURNING binding
            """,
            {"binding": binding, "reason": reason, "now": int(now_ms)},
        ).fetchone()
        if updated is None:
            raise RuntimeError("venue_catalog_binding_missing")

    def active_venue_catalog(self, *, binding: VenueBinding) -> VenueInstrumentCatalogSnapshotV1 | None:
        row = self.conn.execute(
            """
            SELECT snapshot.payload, runtime.catalog_snapshot_sha256
              FROM trading_binding_runtime runtime
              LEFT JOIN trading_venue_catalog_snapshots snapshot
                ON snapshot.snapshot_sha256 = runtime.catalog_snapshot_sha256
             WHERE runtime.binding = %s
            """,
            (binding,),
        ).fetchone()
        if row is None or row["payload"] is None:
            return None
        snapshot = VenueInstrumentCatalogSnapshotV1.model_validate(row["payload"])
        if snapshot.snapshot_sha256 != row["catalog_snapshot_sha256"]:
            raise RuntimeError("venue_catalog_snapshot_digest_mismatch")
        return snapshot


__all__ = ["CatalogStorage"]
