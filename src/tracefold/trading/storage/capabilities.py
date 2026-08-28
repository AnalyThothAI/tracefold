"""Immutable execution capability snapshots and their one active pointer."""

from __future__ import annotations

from typing import Any

from ..candidate.blacklist import Blacklist, BlacklistSnapshotV1
from ..capabilities import ExecutionCapabilitySnapshotV1
from .sql_values import _dumps

_NAUTILUS_ZERO_PROOF_MAX_AGE_MS = 15_000


class CapabilityStorage:
    conn: Any

    def execution_capability_snapshot(self, snapshot_sha256: str) -> ExecutionCapabilitySnapshotV1 | None:
        row = self.conn.execute(
            "SELECT payload FROM trading_execution_capability_snapshots WHERE snapshot_sha256 = %s",
            (snapshot_sha256,),
        ).fetchone()
        return None if row is None else ExecutionCapabilitySnapshotV1.model_validate(row["payload"])

    def active_execution_capability_snapshot(self, *, for_update: bool = False) -> ExecutionCapabilitySnapshotV1 | None:
        row = self.conn.execute(
            "SELECT active_capability_snapshot_sha256 FROM trading_runtime_state WHERE id = 1"
            + (" FOR UPDATE" if for_update else "")
        ).fetchone()
        digest = None if row is None else row["active_capability_snapshot_sha256"]
        return None if digest is None else self.execution_capability_snapshot(str(digest))

    def replay_authority_snapshot(
        self,
        *,
        now_ms: int,
    ) -> tuple[ExecutionCapabilitySnapshotV1, BlacklistSnapshotV1]:
        """Read the active capability and blacklist from one PostgreSQL statement snapshot."""

        row = self.conn.execute(
            """
            SELECT s.payload, r.blacklist_revision,
                   EXISTS (
                     SELECT 1 FROM trading_symbol_blacklist expired
                      WHERE expired.expires_at_ms IS NOT NULL AND expired.expires_at_ms <= %s
                   ) AS has_unmaterialized_expiry,
                   COALESCE(
                     jsonb_agg(
                       jsonb_build_object(
                         'base_symbol', b.base_symbol,
                         'reason', b.reason,
                         'created_at_ms', b.created_at_ms,
                         'expires_at_ms', b.expires_at_ms
                       ) ORDER BY b.base_symbol
                     ) FILTER (WHERE b.base_symbol IS NOT NULL),
                     '[]'::jsonb
                   ) AS blacklist_rows
              FROM trading_runtime_state r
              JOIN trading_execution_capability_snapshots s
                ON s.snapshot_sha256 = r.active_capability_snapshot_sha256
              LEFT JOIN trading_symbol_blacklist b
                ON b.expires_at_ms IS NULL OR b.expires_at_ms > %s
             WHERE r.id = 1
             GROUP BY s.payload, r.blacklist_revision
            """,
            (int(now_ms), int(now_ms)),
        ).fetchone()
        if row is None:
            raise RuntimeError("execution_capability_snapshot_unavailable")
        if bool(row["has_unmaterialized_expiry"]):
            raise RuntimeError("blacklist_expiry_not_materialized")
        snapshot = ExecutionCapabilitySnapshotV1.model_validate(row["payload"])
        blacklist = Blacklist.from_rows(row["blacklist_rows"]).snapshot(
            revision=int(row["blacklist_revision"]),
            now_ms=now_ms,
        )
        return snapshot, blacklist

    def append_and_activate_execution_capability_snapshot(
        self,
        snapshot: ExecutionCapabilitySnapshotV1,
        *,
        created_at_ms: int,
    ) -> bool:
        digest = snapshot.snapshot_sha256
        self.conn.execute(
            """
            INSERT INTO trading_execution_capability_snapshots (
              snapshot_sha256, created_at_ms, execution_environment,
              included_count, excluded_count, payload
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (snapshot_sha256) DO NOTHING
            """,
            (
                digest,
                int(created_at_ms),
                snapshot.execution_environment,
                len(snapshot.included),
                len(snapshot.excluded),
                _dumps(snapshot.model_dump(mode="json")),
            ),
        )
        runtime = self.conn.execute(
            """
            SELECT control, active_capability_snapshot_sha256, nautilus_heartbeat_at_ms,
                   nautilus_ready, nautilus_unexpected_exposure
              FROM trading_runtime_state WHERE id = 1 FOR UPDATE
            """
        ).fetchone()
        if runtime is None or runtime["control"] != "PAUSED":
            return False
        current = runtime["active_capability_snapshot_sha256"]
        if current == digest:
            return True
        heartbeat_at_ms = runtime["nautilus_heartbeat_at_ms"]
        proof_is_fresh = heartbeat_at_ms is not None and int(heartbeat_at_ms) >= (
            int(created_at_ms) - _NAUTILUS_ZERO_PROOF_MAX_AGE_MS
        )
        if not runtime["nautilus_ready"] or runtime["nautilus_unexpected_exposure"] or not proof_is_fresh:
            return False
        nonterminal = self.conn.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM trading_intents
               WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
            ) AS blocked
            """
        ).fetchone()
        if nonterminal is None or bool(nonterminal["blocked"]):
            return False
        self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET active_capability_snapshot_sha256 = %s,
                   active_capability_included_count = %s,
                   nautilus_ready = false,
                   nautilus_readiness_reason = 'capability_snapshot_changed',
                   updated_at_ms = %s
             WHERE id = 1
            """,
            (digest, len(snapshot.included), int(created_at_ms)),
        )
        return True

    def intent_admission_evidence(
        self,
        *,
        instrument_id: str,
        underlying_key: str,
        now_ms: int,
    ) -> tuple[ExecutionCapabilitySnapshotV1, BlacklistSnapshotV1] | None:
        snapshot = self.active_execution_capability_snapshot(for_update=True)
        if snapshot is None:
            return None
        capability = snapshot.included.get(instrument_id)
        if capability is None or capability.underlying_key != underlying_key or not capability.executable:
            return None
        blacklist = self.blacklist_snapshot(now_ms=now_ms, materialize_expiry=True)
        if any(row.underlying_key == underlying_key for row in blacklist.active_rows):
            return None
        return snapshot, blacklist

    def blacklist_snapshot(self, *, now_ms: int, materialize_expiry: bool) -> BlacklistSnapshotV1:
        runtime = self.conn.execute(
            "SELECT blacklist_revision FROM trading_runtime_state WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if runtime is None:
            raise RuntimeError("trading_runtime_state_missing")
        revision = int(runtime["blacklist_revision"])
        expired = self.conn.execute(
            "SELECT count(*) AS n FROM trading_symbol_blacklist "
            "WHERE expires_at_ms IS NOT NULL AND expires_at_ms <= %s",
            (int(now_ms),),
        ).fetchone()
        expired_n = 0 if expired is None else int(expired["n"])
        if expired_n:
            if not materialize_expiry:
                raise RuntimeError("blacklist_expiry_not_materialized")
            self.conn.execute(
                "DELETE FROM trading_symbol_blacklist WHERE expires_at_ms IS NOT NULL AND expires_at_ms <= %s",
                (int(now_ms),),
            )
            revision += 1
            self.conn.execute(
                "UPDATE trading_runtime_state SET blacklist_revision = %s, updated_at_ms = %s WHERE id = 1",
                (revision, int(now_ms)),
            )
        rows = self.conn.execute(
            """
            SELECT base_symbol, reason, created_at_ms, expires_at_ms
              FROM trading_symbol_blacklist
             ORDER BY base_symbol
            """
        ).fetchall()
        return Blacklist.from_rows(rows).snapshot(revision=revision, now_ms=now_ms)


__all__ = ["CapabilityStorage"]
