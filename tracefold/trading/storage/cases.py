"""Claiming and reading immutable Trading Cases.

Writing one is `LaneStorage.create_case`, and terminalising one is `LaneStorage.settle_case`: both are
atomic compositions with something else, so they live beside the transaction they belong to rather
than here.
"""

from __future__ import annotations

from typing import Any


class CaseStorage:
    conn: Any

    def seed_restore_drill_case(self, *, case_id: str) -> None:
        """Seed one current Signal Case for the isolated application restore drill."""

        self.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, primary_source_key,
              supplemental_source_keys, manifest, manifest_sha256, state,
              policy_decision, policy_reason, observed_at_ms, created_at_ms, updated_at_ms,
              strategy_id, strategy_version, strategy_config_digest,
              capital_disposition, capital_reason
            ) VALUES (
              %s, 'restore:RESTORE', 'news', 'restore-source', '[]'::jsonb,
              '{"restore":"case","manifest_version":"trading_manifest_v10","market_key":"crypto:perp:RESTORE:USDT"}'::jsonb,
              %s, 'SIGNAL_EMITTED', 'long', 'restore_drill',
              10, 10, 10, 'restore_strategy', 'restore_v1', %s, 'not_applicable', NULL
            )
            """,
            (case_id, "a" * 64, "b" * 64),
        )

    def claim_case(self, *, run_id: str, lease_ms: int, now_ms: int) -> dict[str, Any] | None:
        """Take the oldest claimable Case under a short lease.

        A `RUNNING` Case whose lease expired may be reclaimed: re-running an undecided Case is safe,
        and the state predicate on the terminal transition — not the lease — is what prevents two
        workers handing the same Case over twice.
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


__all__ = ["CaseStorage"]
