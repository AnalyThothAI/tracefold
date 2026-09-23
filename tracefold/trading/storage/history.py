"""Bounded reads of historical Cases and the isolated restore drill seed."""

from __future__ import annotations

from typing import Any

LATEST_CASE_CREATED_AT_SQL = "SELECT max(created_at_ms) AS latest FROM trading_cases"


class HistoricalCaseStorage:
    conn: Any

    def restore_drill_case(self, *, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT case_id, state, manifest_sha256 FROM trading_cases WHERE case_id = %s",
            (case_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def seed_restore_drill_case(self, *, case_id: str) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, primary_source_key,
              manifest, manifest_sha256, state,
              policy_decision, policy_reason, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES (
              %s, 'restore:RESTORE', 'oi', 'restore-source',
              '{"restore":"case","manifest_version":"trading_manifest_v11","market_key":"crypto:perp:RESTORE:USDT"}'::jsonb,
              %s, 'SIGNAL_EMITTED', 'long', 'restore_drill', 10, 10, 10
            )
            """,
            (case_id, "a" * 64),
        )

    def latest_case_created_at_ms(self) -> int | None:
        row = self.conn.execute(LATEST_CASE_CREATED_AT_SQL).fetchone()
        latest = None if row is None else row["latest"]
        return None if latest is None else int(latest)
