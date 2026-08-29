"""Immutable V2 execution capabilities and one active pointer per closed binding."""

from __future__ import annotations

from typing import Any

from ..blacklist import Blacklist, BlacklistSnapshotV1
from ..capabilities import ExecutionCapabilitySnapshotV2
from ..contracts import VenueBinding
from .sql_values import _dumps


class CapabilityStorage:
    conn: Any

    def execution_capability_snapshot(self, snapshot_sha256: str) -> ExecutionCapabilitySnapshotV2 | None:
        row = self.conn.execute(
            "SELECT payload FROM trading_execution_capability_snapshots "
            "WHERE snapshot_sha256 = %s AND payload ->> 'snapshot_version' = 'execution_capability_snapshot_v2'",
            (snapshot_sha256,),
        ).fetchone()
        return None if row is None else ExecutionCapabilitySnapshotV2.model_validate(row["payload"])

    def active_execution_capability_snapshot(
        self,
        *,
        binding: VenueBinding,
        for_update: bool = False,
    ) -> ExecutionCapabilitySnapshotV2 | None:
        row = self.conn.execute(
            "SELECT capability_snapshot_sha256 FROM trading_binding_runtime WHERE binding = %s"
            + (" FOR UPDATE" if for_update else ""),
            (binding,),
        ).fetchone()
        digest = None if row is None else row["capability_snapshot_sha256"]
        return None if digest is None else self.execution_capability_snapshot(str(digest))

    def replay_authority_snapshot(
        self,
        *,
        binding: VenueBinding,
        now_ms: int,
    ) -> tuple[ExecutionCapabilitySnapshotV2, BlacklistSnapshotV1]:
        """Read one binding capability and the global deny-list from one statement snapshot."""

        row = self.conn.execute(
            """
            SELECT s.payload, r.blacklist_revision,
                   EXISTS (
                     SELECT 1 FROM trading_symbol_blacklist expired
                      WHERE expired.expires_at_ms IS NOT NULL AND expired.expires_at_ms <= %(now)s
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
              JOIN trading_binding_runtime binding ON binding.binding = %(binding)s
              JOIN trading_execution_capability_snapshots s
                ON s.snapshot_sha256 = binding.capability_snapshot_sha256
               AND s.payload ->> 'snapshot_version' = 'execution_capability_snapshot_v2'
              LEFT JOIN trading_symbol_blacklist b
                ON b.expires_at_ms IS NULL OR b.expires_at_ms > %(now)s
             WHERE r.id = 1
             GROUP BY s.payload, r.blacklist_revision
            """,
            {"binding": binding, "now": int(now_ms)},
        ).fetchone()
        if row is None:
            raise RuntimeError(f"execution_capability_snapshot_unavailable:{binding}")
        if bool(row["has_unmaterialized_expiry"]):
            raise RuntimeError("blacklist_expiry_not_materialized")
        snapshot = ExecutionCapabilitySnapshotV2.model_validate(row["payload"])
        blacklist = Blacklist.from_rows(row["blacklist_rows"]).snapshot(
            revision=int(row["blacklist_revision"]),
            now_ms=now_ms,
        )
        return snapshot, blacklist

    def append_and_activate_execution_capability_snapshot(
        self,
        snapshot: ExecutionCapabilitySnapshotV2,
        *,
        created_at_ms: int,
    ) -> bool:
        """Append complete truth, then activate only while globally paused and account-flat."""

        digest = snapshot.snapshot_sha256
        self.conn.execute(
            """
            INSERT INTO trading_execution_capability_snapshots (
              snapshot_sha256, created_at_ms, execution_environment,
              binding, venue, catalog_snapshot_sha256, catalog_instrument_count,
              included_count, excluded_count, partition_sha256, payload
            ) VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (snapshot_sha256) DO NOTHING
            """,
            (
                digest,
                int(created_at_ms),
                snapshot.binding,
                snapshot.venue,
                snapshot.catalog_snapshot_sha256,
                snapshot.catalog_instrument_count,
                snapshot.included_count,
                snapshot.excluded_count,
                snapshot.partition_sha256,
                _dumps(snapshot.model_dump(mode="json")),
            ),
        )
        runtime = self.conn.execute("SELECT control FROM trading_runtime_state WHERE id = 1 FOR UPDATE").fetchone()
        binding = self.conn.execute(
            """
            SELECT account_state, catalog_snapshot_sha256, capability_snapshot_sha256
              FROM trading_binding_runtime
             WHERE binding = %s
               FOR UPDATE
            """,
            (snapshot.binding,),
        ).fetchone()
        if runtime is None or runtime["control"] != "PAUSED" or binding is None:
            return False
        if binding["catalog_snapshot_sha256"] != snapshot.catalog_snapshot_sha256:
            return False
        if binding["account_state"] != "reconciled_flat":
            return False
        if binding["capability_snapshot_sha256"] == digest:
            return True
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
            UPDATE trading_binding_runtime
               SET capability_state = 'ready',
                   capability_snapshot_sha256 = %s,
                   capability_compiled_at_ms = %s,
                   capability_compile_error = NULL,
                   execution_binding_sha256 = NULL,
                   runtime_state = CASE WHEN runtime_state = 'ready' THEN 'stale' ELSE runtime_state END,
                   reason = 'capability_snapshot_changed',
                   updated_at_ms = %s
             WHERE binding = %s
            """,
            (digest, int(created_at_ms), int(created_at_ms), snapshot.binding),
        )
        return True

    def mark_execution_capability_compile_error(
        self,
        *,
        binding: VenueBinding,
        reason: str,
        now_ms: int,
    ) -> None:
        if not reason or len(reason) > 128:
            raise ValueError("execution_capability_compile_error_invalid")
        self.conn.execute(
            """
            UPDATE trading_binding_runtime
               SET capability_state = 'error',
                   capability_compile_error = %s,
                   updated_at_ms = %s
             WHERE binding = %s
            """,
            (reason, int(now_ms), binding),
        )

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
            materialized = self.conn.execute("SELECT materialize_trading_blacklist_expiry() AS revision").fetchone()
            if materialized is None:
                raise RuntimeError("blacklist_expiry_materialization_failed")
            revision = int(materialized["revision"])
            remaining = self.conn.execute(
                "SELECT EXISTS (SELECT 1 FROM trading_symbol_blacklist "
                "WHERE expires_at_ms IS NOT NULL AND expires_at_ms <= %s) AS blocked",
                (int(now_ms),),
            ).fetchone()
            if remaining is None or bool(remaining["blocked"]):
                raise RuntimeError("blacklist_expiry_not_materialized")
        rows = self.conn.execute(
            """
            SELECT base_symbol, reason, created_at_ms, expires_at_ms
              FROM trading_symbol_blacklist
             ORDER BY base_symbol
            """
        ).fetchall()
        return Blacklist.from_rows(rows).snapshot(revision=revision, now_ms=now_ms)


__all__ = ["CapabilityStorage"]
