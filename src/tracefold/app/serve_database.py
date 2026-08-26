from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import BoundedSemaphore
from typing import Any

from psycopg_pool import PoolClosed, PoolTimeout

from tracefold.app.repository_session import RepositorySession, repositories_for_connection
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.client import create_pool, with_password_from_file

_SERVE_POOL_SIZE = 7  # 6 ordinary read permits + 1 control permit
_SERVE_CHECKOUT_TIMEOUT_SECONDS = 0.250
_SERVE_STATEMENT_TIMEOUT_SECONDS = 1.0
_SERVE_SESSION_CONFIG = {
    "jit": "off",
    "max_parallel_workers_per_gather": "0",
    "work_mem": "8MB",
}
_SERVE_LANE_CAPACITIES = {
    "ordinary": 6,
    "control": 1,
}
_SERVE_PERMIT_TIMEOUT_SECONDS = 0.050


class ServeDatabaseBusy(RuntimeError):
    pass


@dataclass(slots=True)
class ServeDatabase:
    """The public serving database boundary.

    Connections default to read-only and nothing on this pool ever opens a
    read-write transaction: the two ReviewDesk mutations that used to were
    removed with the console page they served (#256).  `tracefold news review
    submit` is the one remaining writer of the append-only review fact tables
    and opens its own connection under the same role.
    """

    api_pool: Any
    telemetry: TelemetryRegistry | None = field(default_factory=TelemetryRegistry)
    admission: dict[str, BoundedSemaphore] = field(
        default_factory=lambda: {lane: BoundedSemaphore(capacity) for lane, capacity in _SERVE_LANE_CAPACITIES.items()}
    )

    @classmethod
    def create(cls, settings: Any, *, telemetry: TelemetryRegistry | None = None) -> ServeDatabase:
        postgres = settings.storage.postgres
        dsn = with_password_from_file(
            settings.postgres_dsn("serve"),
            settings.postgres_password_file("serve"),
        )
        pool = create_pool(
            dsn,
            min_size=1,
            max_size=_SERVE_POOL_SIZE,
            connect_timeout_seconds=postgres.connect_timeout_seconds,
            application_name="tracefold_serve",
            statement_timeout_seconds=_SERVE_STATEMENT_TIMEOUT_SECONDS,
            lock_timeout_seconds=0.250,
            read_only=True,
            idle_in_transaction_session_timeout_seconds=5.0,
        )
        pool.wait(timeout=float(postgres.connect_timeout_seconds))
        return cls(
            api_pool=pool,
            telemetry=telemetry if telemetry is not None else TelemetryRegistry(),
        )

    @contextmanager
    def api_session(self, lane: str = "ordinary") -> Iterator[RepositorySession]:
        try:
            gate = self.admission[lane]
        except KeyError as exc:
            raise ValueError(f"serve_database_lane_invalid:{lane}") from exc
        started = time.perf_counter()
        if not gate.acquire(timeout=_SERVE_PERMIT_TIMEOUT_SECONDS):
            if self.telemetry is not None:
                self.telemetry.record_pool_wait(
                    f"serve_{lane}_permit",
                    (time.perf_counter() - started) * 1000,
                )
            raise ServeDatabaseBusy(f"serve_database_busy:{lane}")
        try:
            permit_acquired_at = time.perf_counter()
            if self.telemetry is not None:
                self.telemetry.record_pool_wait(
                    f"serve_{lane}_permit",
                    (permit_acquired_at - started) * 1000,
                )
            try:
                with self.api_pool.connection(timeout=_SERVE_CHECKOUT_TIMEOUT_SECONDS) as conn:
                    if self.telemetry is not None:
                        self.telemetry.record_pool_wait(
                            "serve",
                            (time.perf_counter() - permit_acquired_at) * 1000,
                        )
                    for name, value in _SERVE_SESSION_CONFIG.items():
                        _set_config(conn, name, value)
                    yield repositories_for_connection(conn)
            except (PoolClosed, PoolTimeout) as exc:
                raise ServeDatabaseBusy(f"serve_database_pool_busy:{lane}") from exc
        finally:
            gate.release()

    async def aclose(self) -> None:
        await _close_pool(self.api_pool)


def _set_config(conn: Any, name: str, value: str) -> None:
    conn.execute("SELECT set_config(%s, %s, false)", (str(name), str(value)))


async def _close_pool(pool: Any) -> None:
    result = pool.close()
    if result is not None:
        raise RuntimeError("db_pool_close_must_be_sync")
