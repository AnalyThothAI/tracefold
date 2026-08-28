"""Immutable successful replay receipts."""

from __future__ import annotations

from typing import Any


class ReplayStorage:
    conn: Any

    def replay_receipt(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM trading_replay_runs WHERE run_id = %s", (run_id,)).fetchone()
        return None if row is None else dict(row)

    def insert_replay_receipt(self, receipt: Any) -> bool:
        row = self.conn.execute(
            """
            INSERT INTO trading_replay_runs (
              run_id, spec_sha256, created_at_ms, terminal_status, artifact_path,
              artifact_sha256, source_count, directional_count, terminal_outcome_count
            ) VALUES (
              %(run_id)s, %(spec_sha256)s, %(created_at_ms)s, %(terminal_status)s,
              %(artifact_path)s, %(artifact_sha256)s, %(source_count)s,
              %(directional_count)s, %(terminal_outcome_count)s
            )
            ON CONFLICT (run_id) DO NOTHING
            RETURNING run_id
            """,
            receipt.model_dump(),
        ).fetchone()
        return row is not None


__all__ = ["ReplayStorage"]
