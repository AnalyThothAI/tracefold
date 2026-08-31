"""Immutable execution bindings and their per-venue active pointers."""

from __future__ import annotations

from typing import Any

from ..bindings import (
    ExecutionBindingV1,
    require_current_execution_contracts,
    require_execution_binding_enabled,
)
from ..capabilities import ExecutionCapabilitySnapshotV2
from ..contracts import VenueBinding
from .sql_values import _dumps


class BindingStorage:
    conn: Any

    def execution_binding(self, binding_sha256: str) -> ExecutionBindingV1 | None:
        row = self.conn.execute(
            "SELECT payload FROM trading_execution_bindings WHERE binding_sha256 = %s",
            (binding_sha256,),
        ).fetchone()
        return None if row is None else ExecutionBindingV1.model_validate(row["payload"])

    def active_execution_binding(self, *, binding: VenueBinding) -> ExecutionBindingV1 | None:
        row = self.conn.execute(
            "SELECT execution_binding_sha256 FROM trading_binding_runtime WHERE binding = %s",
            (binding,),
        ).fetchone()
        digest = None if row is None else row["execution_binding_sha256"]
        return None if digest is None else self.execution_binding(str(digest))

    def append_and_activate_execution_binding(self, value: ExecutionBindingV1) -> bool:
        require_execution_binding_enabled(value.binding)
        require_current_execution_contracts(
            binding=value.binding,
            adapter_contract_sha256=value.adapter_contract_sha256,
            quote_contract_sha256=value.quote_contract_sha256,
            protection_contract_sha256=value.protection_contract_sha256,
        )
        digest = value.binding_sha256
        runtime = self.conn.execute("SELECT control FROM trading_runtime_state WHERE id = 1 FOR UPDATE").fetchone()
        current = self.conn.execute(
            """
            SELECT credential_fingerprint, catalog_snapshot_sha256, capability_snapshot_sha256,
                   account_generation, account_state
              FROM trading_binding_runtime
             WHERE binding = %s
               FOR UPDATE
            """,
            (value.binding,),
        ).fetchone()
        if runtime is None or runtime["control"] != "PAUSED" or current is None:
            return False
        capability_row = self.conn.execute(
            "SELECT payload FROM trading_execution_capability_snapshots WHERE snapshot_sha256 = %s",
            (current["capability_snapshot_sha256"],),
        ).fetchone()
        if capability_row is None:
            return False
        capability = ExecutionCapabilitySnapshotV2.model_validate(capability_row["payload"])
        if (
            current["credential_fingerprint"] != value.credential_fingerprint
            or current["catalog_snapshot_sha256"] != value.catalog_snapshot_sha256
            or current["capability_snapshot_sha256"] != value.capability_snapshot_sha256
            or int(current["account_generation"]) != value.account_generation
            or current["account_state"] != "reconciled_flat"
            or capability.binding != value.binding
            or capability.venue != value.venue
            or capability.catalog_snapshot_sha256 != value.catalog_snapshot_sha256
            or capability.snapshot_sha256 != value.capability_snapshot_sha256
            or capability.adapter_contract_sha256 != value.adapter_contract_sha256
            or capability.quote_contract_sha256 != value.quote_contract_sha256
            or capability.protection_contract_sha256 != value.protection_contract_sha256
            or capability.client_runtime_identity != value.client_runtime_identity
        ):
            return False
        active = self.conn.execute(
            "SELECT 1 FROM trading_intents "
            "WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW') LIMIT 1"
        ).fetchone()
        if active is not None:
            return False
        self.conn.execute(
            """
            INSERT INTO trading_execution_bindings (
              binding_sha256, binding, account_generation, created_at_ms, payload
            ) VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (binding_sha256) DO NOTHING
            """,
            (
                digest,
                value.binding,
                value.account_generation,
                value.created_at_ms,
                _dumps(value.model_dump(mode="json")),
            ),
        )
        self.conn.execute(
            """
            UPDATE trading_binding_runtime
               SET execution_binding_sha256 = %s,
                   updated_at_ms = %s
             WHERE binding = %s
            """,
            (digest, value.created_at_ms, value.binding),
        )
        return True


__all__ = ["BindingStorage"]
