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
        trigger_kind: str,
        strategy_id: str,
        strategy_version: str,
        strategy_config_digest: str,
        mode: str,
        primary_source_key: str,
        supplemental_source_keys: Sequence[str],
        manifest: Mapping[str, Any],
        manifest_sha256: str,
        regime: str | None,
        observed_at_ms: int,
        source_observed_at_ms: int,
        trigger_persisted_at_ms: int,
        now_ms: int,
    ) -> bool:
        """One trigger, one case, and one undecided case per underlying.

        Two unique constraints can refuse this insert and the caller treats both the same way, so the
        `ON CONFLICT` deliberately names no target: `primary_source_key` makes a re-scanned window a
        no-op, and the partial unique index on an in-flight `underlying_key` stops a second concurrent
        thesis for the same issuer. Naming one target would turn the other into an exception in a
        transaction that has already done work.
        """

        cursor = self.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
              strategy_config_digest, mode, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, regime, observed_at_ms, source_observed_at_ms,
              trigger_persisted_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                      'PENDING', %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                case_id,
                underlying_key,
                trigger_kind,
                strategy_id,
                strategy_version,
                strategy_config_digest,
                mode,
                primary_source_key,
                _dumps(list(supplemental_source_keys)),
                _dumps(dict(manifest)),
                manifest_sha256,
                regime,
                int(observed_at_ms),
                int(source_observed_at_ms),
                int(trigger_persisted_at_ms),
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
        """Terminalise a claimed case, and only one that is still undecided.

        `run_id` alone is not enough. A worker holding a valid lease can be inside its model call while
        something else terminalises the case — migration `0309` coalescing duplicates onto the newest
        trigger is exactly that — and without the state predicate the returning worker would write its
        own answer straight over the top, undoing the coalescing and leaving an audit trail that reads
        as a case both superseded and traded (#211).
        """

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
             WHERE case_id = %s AND run_id = %s AND state IN ('PENDING', 'RUNNING')
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

    def underlyings_in_flight(self) -> list[str]:
        """Underlyings whose case is still undecided, in the same predicate as the unique index.

        The index is the authority; this read is what lets the scanner name the reason in its funnel
        instead of discovering it as a swallowed insert conflict.
        """

        rows = self.conn.execute(
            "SELECT DISTINCT underlying_key FROM trading_cases WHERE state IN ('PENDING', 'RUNNING')"
        ).fetchall()
        return [str(row["underlying_key"]) for row in rows]

    def source_keys_since(self, *, observed_at_ms: int) -> list[str]:
        """Trigger identities this lane has already turned into a case, over the scan's own horizon.

        The scanner reads this so a trigger that already produced a case cannot win the per-underlying
        coalescing again and again for the rest of its freshness window, starving the older trigger it
        beat. `has_source_key` remains the authority at the freeze — this read only decides which
        trigger is worth planning, and a race between the two is settled by the unique constraint.
        """

        rows = self.conn.execute(
            "SELECT primary_source_key FROM trading_cases WHERE observed_at_ms >= %s",
            (int(observed_at_ms),),
        ).fetchall()
        return [str(row["primary_source_key"]) for row in rows]

    def has_source_key(self, *, primary_source_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM trading_cases WHERE primary_source_key = %s LIMIT 1", (primary_source_key,)
        ).fetchone()
        return row is not None


__all__ = ["CaseStorage"]
