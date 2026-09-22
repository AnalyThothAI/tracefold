"""The Workers watchdog's alert ledger, `platform_watchdog_alerts` (#680 PR-2).

One row per watched condition, written only by the Workers singleton. It is bookkeeping about what the
operator was told, never a business fact: nothing reads it to decide a trade, and dropping it costs one
repeated alert per active condition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

WATCHDOG_ALERTS_SQL: Final = """
    SELECT condition_key, active, opened_at_ms, notified_at_ms, clear_since_ms
      FROM platform_watchdog_alerts
"""
_DETAIL_MAX: Final = 2_000


@dataclass(frozen=True, slots=True)
class WatchdogAlertState:
    """What the ledger holds about one condition.

    `active` with `opened_at_ms` is the current episode. `notified_at_ms` is the last message about it
    that the provider accepted, and `None` until one did, so an alert that failed to send is retried
    rather than recorded as told. `clear_since_ms` is when an active condition first read clear; the
    episode closes -- and the recovery message goes out -- only once it has stayed clear.
    """

    condition_key: str
    active: bool
    opened_at_ms: int
    notified_at_ms: int | None = None
    clear_since_ms: int | None = None


class WatchdogAlertRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def states(self) -> dict[str, WatchdogAlertState]:
        rows = self.conn.execute(WATCHDOG_ALERTS_SQL).fetchall()
        return {
            str(row["condition_key"]): WatchdogAlertState(
                condition_key=str(row["condition_key"]),
                active=bool(row["active"]),
                opened_at_ms=int(row["opened_at_ms"]),
                notified_at_ms=None if row["notified_at_ms"] is None else int(row["notified_at_ms"]),
                clear_since_ms=None if row["clear_since_ms"] is None else int(row["clear_since_ms"]),
            )
            for row in rows
        }

    def save(self, state: WatchdogAlertState, *, detail: str, now_ms: int) -> None:
        self.conn.execute(
            """
            INSERT INTO platform_watchdog_alerts (
              condition_key, active, opened_at_ms, notified_at_ms, clear_since_ms, detail, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (condition_key) DO UPDATE
               SET active = EXCLUDED.active,
                   opened_at_ms = EXCLUDED.opened_at_ms,
                   notified_at_ms = EXCLUDED.notified_at_ms,
                   clear_since_ms = EXCLUDED.clear_since_ms,
                   detail = EXCLUDED.detail,
                   updated_at_ms = EXCLUDED.updated_at_ms
            """,
            (
                state.condition_key,
                state.active,
                int(state.opened_at_ms),
                state.notified_at_ms,
                state.clear_since_ms,
                detail[:_DETAIL_MAX],
                int(now_ms),
            ),
        )


__all__ = ["WATCHDOG_ALERTS_SQL", "WatchdogAlertRepository", "WatchdogAlertState"]
