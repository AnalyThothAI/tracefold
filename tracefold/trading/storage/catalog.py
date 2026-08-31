"""Durable Decision Plane, binding facts, and venue catalogue snapshots (#350)."""

from __future__ import annotations

from typing import Any

from ..adapter_contracts import BINANCE_USDM_ADAPTER_CONTRACT_SHA256

# S608 exemption below composes only fixed catalog capability predicates; all binding values stay bound.
from ..catalog import PreparedVenueCatalogSnapshot
from ..contracts import BINDING_RUNTIME_HEARTBEAT_STALE_AFTER_MS, DecisionRuntimeV1, VenueBinding, VenueBindingRuntimeV1
from ..execution_policy import PROTECTION_CONTRACT_SHA256
from ..quote_authority import QUOTE_CONTRACT_SHA256
from .query_sql import BINDING_RUNTIME_ROWS_SQL


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
            BINDING_RUNTIME_ROWS_SQL,
            {
                "now": int(now_ms),
                "heartbeat_floor": int(now_ms) - BINDING_RUNTIME_HEARTBEAT_STALE_AFTER_MS,
                "adapter_contract": BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
                "quote_contract": QUOTE_CONTRACT_SHA256,
                "protection_contract": PROTECTION_CONTRACT_SHA256,
            },
        ).fetchall()
        return [VenueBindingRuntimeV1(**dict(row)) for row in rows]

    def binding_runtime(self, *, binding: VenueBinding, now_ms: int) -> VenueBindingRuntimeV1 | None:
        return next((row for row in self.binding_runtime_rows(now_ms=now_ms) if row.binding == binding), None)

    def project_binding_credentials(
        self,
        *,
        binding: VenueBinding,
        credential_state: str,
        credential_fingerprint: str | None,
        runtime_state: str,
        heartbeat_at_ms: int | None,
        reason: str | None,
        now_ms: int,
    ) -> bool:
        current = self.conn.execute(
            "SELECT credential_state, credential_fingerprint, account_state FROM trading_binding_runtime "
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
        exposure_present = current["account_state"] == "exposure_present"
        recovery = (
            self.conn.execute(
                "SELECT 1 FROM trading_intents "
                "WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW') LIMIT 1"
            ).fetchone()
            if binding == "BINANCE_USDM"
            else None
        )
        recovery_required = exposure_present or recovery is not None
        current_fingerprint = current["credential_fingerprint"]
        identity_matches = (
            credential_state == "configured"
            and current_fingerprint is not None
            and credential_fingerprint == current_fingerprint
        )
        projected_credential_state = credential_state
        projected_credential_fingerprint = credential_fingerprint
        if recovery_required and not identity_matches:
            projected_credential_fingerprint = current_fingerprint
            if credential_state == "configured":
                projected_credential_state = "invalid"
                reason = (
                    "recovery_blocked_credential_changed"
                    if current_fingerprint is not None
                    else "recovery_blocked_account_identity_unproven"
                )
            elif credential_state == "invalid":
                reason = "recovery_blocked_credentials_invalid"
            else:
                reason = "recovery_blocked_credentials_missing"
        elif recovery_required:
            reason = "binance_demo_recovery_required"
        projection_changed = (
            current["credential_state"],
            current["credential_fingerprint"],
        ) != (projected_credential_state, projected_credential_fingerprint)
        rotate_identity = projection_changed and not recovery_required
        row = self.conn.execute(
            """
            UPDATE trading_binding_runtime AS runtime
               SET credential_state = %(credential_state)s,
                   credential_fingerprint = %(credential_fingerprint)s,
                   account_generation = CASE
                     WHEN %(rotate_identity)s
                     THEN account_generation + 1 ELSE account_generation END,
                   execution_binding_sha256 = CASE
                     WHEN %(rotate_identity)s
                     THEN NULL ELSE execution_binding_sha256 END,
                   active_arm_receipt_sha256 = NULL,
                   runtime_state = %(runtime_state)s,
                   account_state = CASE
                     WHEN account_state = 'exposure_present' THEN account_state
                     WHEN %(rotate_identity)s THEN 'unknown'
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
                "credential_state": projected_credential_state,
                "credential_fingerprint": projected_credential_fingerprint,
                "runtime_state": runtime_state,
                "rotate_identity": rotate_identity,
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
            """  # noqa: S608
            + (" FOR UPDATE OF capital, runtime" if for_update else ""),
            (binding,),
        ).fetchone()
        return None if row is None else dict(row)

    def set_binding_execution_runtime(
        self,
        *,
        binding: VenueBinding,
        expected_capability_snapshot_sha256: str | None,
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
               AND capability_snapshot_sha256 IS NOT DISTINCT FROM %(expected_capability)s
         RETURNING binding
            """,
            {
                "binding": binding,
                "expected_capability": expected_capability_snapshot_sha256,
                "heartbeat": int(heartbeat_at_ms),
                "ready": bool(ready),
                "reason": readiness_reason,
                "unexpected": bool(unexpected_exposure),
                "now": int(now_ms),
            },
        ).fetchone()
        if updated is None:
            raise RuntimeError(f"nautilus_capability_snapshot_changed:{binding}")

    def set_binding_account_reconciliation(
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

    def activate_latest_bootstrap_capability(self, *, binding: VenueBinding, now_ms: int) -> bool:
        """Activate one current Demo capability after bootstrap proved the account flat."""

        if binding != "BINANCE_USDM":
            raise ValueError(f"execution_binding_disabled:{binding}")
        row = self.conn.execute(
            """
            WITH candidate AS (
                SELECT snapshot.snapshot_sha256, snapshot.created_at_ms
                  FROM trading_execution_capability_snapshots snapshot
                  JOIN trading_binding_runtime current
                    ON current.binding = snapshot.binding
                   AND current.catalog_snapshot_sha256 = snapshot.catalog_snapshot_sha256
                 WHERE current.binding = %(binding)s
                   AND snapshot.payload ->> 'adapter_contract_sha256' = %(adapter_contract)s
                   AND snapshot.payload ->> 'quote_contract_sha256' = %(quote_contract)s
                   AND snapshot.payload ->> 'protection_contract_sha256' = %(protection_contract)s
                 ORDER BY snapshot.created_at_ms DESC, snapshot.snapshot_sha256 DESC
                 LIMIT 1
            )
            UPDATE trading_binding_runtime runtime
               SET capability_state = 'ready',
                   capability_snapshot_sha256 = candidate.snapshot_sha256,
                   capability_compiled_at_ms = candidate.created_at_ms,
                   capability_compile_error = NULL,
                   execution_binding_sha256 = NULL,
                   runtime_state = 'stale',
                   reason = 'capability_snapshot_changed',
                   updated_at_ms = %(now)s
              FROM candidate, trading_runtime_state capital
             WHERE runtime.binding = %(binding)s
               AND capital.id = 1
               AND capital.control = 'PAUSED'
               AND runtime.account_state = 'reconciled_flat'
               AND runtime.capability_snapshot_sha256 IS NULL
               AND NOT EXISTS (
                 SELECT 1 FROM trading_intents
                  WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
               )
         RETURNING runtime.binding
            """,
            {
                "adapter_contract": BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
                "quote_contract": QUOTE_CONTRACT_SHA256,
                "protection_contract": PROTECTION_CONTRACT_SHA256,
                "binding": binding,
                "now": int(now_ms),
            },
        ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- catalogues
    def store_venue_catalog_snapshot(
        self,
        *,
        prepared: PreparedVenueCatalogSnapshot,
        now_ms: int,
    ) -> None:
        """Atomically persist and activate one already-materialized immutable snapshot."""

        row = self.conn.execute(
            """
            SELECT identity_valid, activated_binding
              FROM store_trading_venue_catalog_snapshot(
                %(digest)s,
                %(binding)s,
                %(captured)s,
                %(stale_after)s,
                %(instrument_count)s,
                %(payload)s::text::jsonb,
                %(now)s
              )
            """,
            {
                "binding": prepared.binding,
                "captured": int(prepared.captured_at_ms),
                "digest": prepared.snapshot_sha256,
                "instrument_count": int(prepared.provider_instrument_count),
                "now": int(now_ms),
                "payload": prepared.payload_json,
                "stale_after": int(prepared.stale_after_ms),
            },
        ).fetchone()
        if row is None or not bool(row["identity_valid"]):
            raise RuntimeError("venue_catalog_snapshot_identity_conflict")
        if row["activated_binding"] is None:
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


__all__ = ["CatalogStorage"]
