"""Persistence for immutable Trading candidates and their decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .sql_values import _dumps


class CaseStorage:
    conn: Any

    def insert_case(
        self,
        *,
        case_id: str,
        underlying_key: str,
        case_kind: str,
        mode: str,
        primary_source_key: str,
        supplemental_source_keys: Sequence[str],
        manifest: Mapping[str, Any],
        manifest_sha256: str,
        regime: str | None,
        observed_at_ms: int,
        now_ms: int,
    ) -> bool:
        """One source fact, one case. A re-scanned window is a no-op, not a duplicate."""

        cursor = self.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, case_kind, mode, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, regime, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, 'PENDING', %s, %s, %s, %s)
            ON CONFLICT (primary_source_key) DO NOTHING
            """,
            (
                case_id,
                underlying_key,
                case_kind,
                mode,
                primary_source_key,
                _dumps(list(supplemental_source_keys)),
                _dumps(dict(manifest)),
                manifest_sha256,
                regime,
                int(observed_at_ms),
                int(now_ms),
                int(now_ms),
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def claim_case(self, *, run_id: str, lease_ms: int, now_ms: int) -> dict[str, Any] | None:
        """Take the oldest claimable case under a short lease.

        A `RUNNING` case whose lease expired may be reclaimed: re-running an undecided case is safe.
        Re-sending a prepared or submitted economic intent is not, and the proposal/order uniqueness —
        not the lease — is what prevents that.
        """

        row = self.conn.execute(
            """
            UPDATE trading_cases
               SET state = 'RUNNING',
                   run_id = %s,
                   lease_expires_at_ms = %s,
                   attempt_count = attempt_count + 1,
                   updated_at_ms = %s
             WHERE case_id = (
                     SELECT case_id FROM trading_cases
                      WHERE state = 'PENDING'
                         OR (state = 'RUNNING' AND coalesce(lease_expires_at_ms, 0) < %s)
                      ORDER BY created_at_ms, case_id
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                   )
         RETURNING *
            """,
            (run_id, int(now_ms) + int(lease_ms), int(now_ms), int(now_ms)),
        ).fetchone()
        return dict(row) if row is not None else None

    def settle_case(
        self,
        *,
        case_id: str,
        run_id: str,
        state: str,
        policy_decision: str | None,
        policy_reason: str | None,
        program_version: str | None = None,
        program_sha256: str | None = None,
        program_output: Mapping[str, Any] | None = None,
        regime: str | None = None,
        now_ms: int,
    ) -> bool:
        """Terminalise a claimed case. A stale `run_id` cannot write: its lease belongs to someone else."""

        cursor = self.conn.execute(
            """
            UPDATE trading_cases
               SET state = %s,
                   policy_decision = %s,
                   policy_reason = %s,
                   program_version = coalesce(%s, program_version),
                   program_sha256 = coalesce(%s, program_sha256),
                   program_output = coalesce(%s::jsonb, program_output),
                   regime = coalesce(%s, regime),
                   decided_at_ms = %s,
                   updated_at_ms = %s
             WHERE case_id = %s AND run_id = %s
            """,
            (
                state,
                policy_decision,
                policy_reason,
                program_version,
                program_sha256,
                None if program_output is None else _dumps(dict(program_output)),
                regime,
                int(now_ms),
                int(now_ms),
                case_id,
                run_id,
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def case(self, *, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM trading_cases WHERE case_id = %s", (case_id,)).fetchone()
        return dict(row) if row is not None else None

    def cases(self, *, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if state:
            rows = self.conn.execute(
                "SELECT * FROM trading_cases WHERE state = %s ORDER BY created_at_ms DESC LIMIT %s",
                (state, int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trading_cases ORDER BY created_at_ms DESC LIMIT %s", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def has_source_key(self, *, primary_source_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM trading_cases WHERE primary_source_key = %s LIMIT 1", (primary_source_key,)
        ).fetchone()
        return row is not None


__all__ = ["CaseStorage"]
