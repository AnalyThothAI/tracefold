from __future__ import annotations

from typing import Any

from tracefold.platform.postgres.postgres_client import require_transaction


class ProviderCircuitRepository:
    """Durable provider-wide circuit; target attempts remain independent."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def can_attempt(self, *, provider: str, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            SELECT status, next_probe_at_ms
            FROM provider_circuit_state
            WHERE provider = %s
            """,
            (str(provider),),
        ).fetchone()
        if row is None or str(row["status"]) == "closed":
            return True
        return int(row["next_probe_at_ms"] or 0) <= int(now_ms)

    def open(
        self,
        *,
        provider: str,
        error: str,
        now_ms: int,
        retry_ms: int,
    ) -> None:
        require_transaction(self.conn, operation="provider_circuit_open")
        self.conn.execute(
            """
            INSERT INTO provider_circuit_state(
              provider, status, consecutive_failures, opened_at_ms,
              next_probe_at_ms, last_error, updated_at_ms
            )
            VALUES (%s, 'open', 1, %s, %s, %s, %s)
            ON CONFLICT(provider) DO UPDATE SET
              status = 'open',
              consecutive_failures =
                provider_circuit_state.consecutive_failures + 1,
              opened_at_ms = COALESCE(
                provider_circuit_state.opened_at_ms,
                EXCLUDED.opened_at_ms
              ),
              next_probe_at_ms = EXCLUDED.next_probe_at_ms,
              last_error = EXCLUDED.last_error,
              updated_at_ms = EXCLUDED.updated_at_ms
            """,
            (
                str(provider),
                int(now_ms),
                int(now_ms) + int(retry_ms),
                str(error)[:500],
                int(now_ms),
            ),
        )

    def close(self, *, provider: str, now_ms: int) -> None:
        require_transaction(self.conn, operation="provider_circuit_close")
        self.conn.execute(
            """
            INSERT INTO provider_circuit_state(
              provider, status, consecutive_failures, opened_at_ms,
              next_probe_at_ms, last_error, updated_at_ms
            )
            VALUES (%s, 'closed', 0, NULL, NULL, NULL, %s)
            ON CONFLICT(provider) DO UPDATE SET
              status = 'closed',
              consecutive_failures = 0,
              opened_at_ms = NULL,
              next_probe_at_ms = NULL,
              last_error = NULL,
              updated_at_ms = EXCLUDED.updated_at_ms
            WHERE provider_circuit_state.status IS DISTINCT FROM 'closed'
               OR provider_circuit_state.consecutive_failures <> 0
               OR provider_circuit_state.last_error IS NOT NULL
            """,
            (str(provider), int(now_ms)),
        )


__all__ = ["ProviderCircuitRepository"]
