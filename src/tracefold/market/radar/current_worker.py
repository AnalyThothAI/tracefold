from __future__ import annotations

import asyncio
import time
from typing import Any

from tracefold.market.radar.constants import TOKEN_RADAR_REDUCER_BUDGET_SECONDS
from tracefold.market.radar.reducer import (
    TokenRadarBudgetExceeded,
    TokenRadarInputOverflow,
    TokenRadarOutputOverflow,
    reduce_token_radar,
)
from tracefold.market.radar.snapshot_repository import (
    TokenRadarCurrentRepository,
    TokenRadarPublicationResult,
)
from tracefold.market.radar.stocks_current import (
    STOCKS_RADAR_REDUCER_BUDGET_SECONDS,
    StocksRadarCurrentRepository,
    StocksRadarInputOverflow,
    reduce_stocks_radar,
)
from tracefold.platform.resource import (
    CpuTaskTimeout,
    ResourceAdmissionTimeout,
    ResourceOperationOverrun,
)

_TOKEN_RADAR_LOAD_TIMEOUT_SECONDS = 3.0
_TOKEN_RADAR_COMPUTE_TIMEOUT_SECONDS = 1.5
_TOKEN_RADAR_PUBLISH_TIMEOUT_SECONDS = 0.5
_TOKEN_RADAR_FAILURE_TIMEOUT_SECONDS = 0.5
_STOCKS_RADAR_LOAD_TIMEOUT_SECONDS = 1.5
_STOCKS_RADAR_PUBLISH_TIMEOUT_SECONDS = 1.0


class TokenRadarCurrentService:
    def __init__(self, *, db: Any, worker_name: str = "token_radar_current") -> None:
        self.db = db
        self.worker_name = worker_name

    def load(
        self,
        *,
        now_ms: int,
        session_timeout_seconds: float = _TOKEN_RADAR_LOAD_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]:
        with self._session(timeout_seconds=session_timeout_seconds) as repos:
            return TokenRadarCurrentRepository(repos.conn).load_material_inputs(now_ms=now_ms)

    def publish(
        self,
        reduced: Any,
        *,
        now_ms: int,
        session_timeout_seconds: float = _TOKEN_RADAR_PUBLISH_TIMEOUT_SECONDS,
    ) -> TokenRadarPublicationResult:
        with self._session(timeout_seconds=session_timeout_seconds) as repos:
            return TokenRadarCurrentRepository(repos.conn).publish(
                reduced,
                evaluation_at_ms=now_ms,
            )

    def mark_failed(
        self,
        *,
        now_ms: int,
        error_code: str,
        session_timeout_seconds: float = _TOKEN_RADAR_FAILURE_TIMEOUT_SECONDS,
    ) -> int:
        with self._session(timeout_seconds=session_timeout_seconds) as repos:
            return TokenRadarCurrentRepository(repos.conn).record_failure(
                evaluation_at_ms=now_ms,
                error_code=error_code,
            )

    def _session(self, *, timeout_seconds: float) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=timeout_seconds,
            transaction_timeout_seconds=timeout_seconds,
        )


class TokenRadarCurrentProjection:
    """The sole 30-second writer for the compact Token Radar singleton."""

    def __init__(
        self,
        *,
        db: Any,
        cpu: Any,
        telemetry: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self.db = db
        self.cpu = cpu
        self.clock = clock or _now_ms
        self.telemetry = telemetry
        self.service = TokenRadarCurrentService(db=db)

    async def sample(self) -> None:
        started = time.monotonic()
        deadline = time.monotonic() + TOKEN_RADAR_REDUCER_BUDGET_SECONDS
        now_ms = int(self.clock())
        outcome: str | None = None
        deadline_missed = False
        try:
            async with asyncio.timeout(TOKEN_RADAR_REDUCER_BUDGET_SECONDS):
                outcome, deadline_missed = await self._sample_with_deadline(
                    now_ms=now_ms,
                    deadline=deadline,
                )
        except TimeoutError:
            outcome = outcome or "deadline_miss"
            deadline_missed = True
            await self._mark_failed(
                "token_radar_sample_budget_exceeded",
                now_ms=now_ms,
                deadline=time.monotonic() + _TOKEN_RADAR_FAILURE_TIMEOUT_SECONDS,
            )
        finally:
            if deadline_missed:
                self._record_deadline_miss()
            if outcome is not None:
                self._record_job(outcome)
            if self.telemetry is not None:
                self.telemetry.record_processing_seconds("token_radar_current", time.monotonic() - started)
                self.telemetry.mark_last_run("token_radar_current")

    async def _sample_with_deadline(self, *, now_ms: int, deadline: float) -> tuple[str, bool]:
        try:
            load_timeout = _phase_timeout(
                deadline,
                cap=_TOKEN_RADAR_LOAD_TIMEOUT_SECONDS,
                reserve_seconds=(_TOKEN_RADAR_COMPUTE_TIMEOUT_SECONDS + _TOKEN_RADAR_PUBLISH_TIMEOUT_SECONDS),
            )
            rows = await self.db.run_business(
                "token_radar_current_load",
                self.service.load,
                operation_timeout_seconds=load_timeout,
                now_ms=now_ms,
                session_timeout_seconds=load_timeout,
            )
            self._set_rows("input", len(rows))
            compute_timeout = _phase_timeout(
                deadline,
                cap=_TOKEN_RADAR_COMPUTE_TIMEOUT_SECONDS,
                reserve_seconds=_TOKEN_RADAR_PUBLISH_TIMEOUT_SECONDS,
            )
            reduced = await self.cpu.run(
                "token_radar_current_reduce",
                _reduce_token_payload,
                {"rows": rows, "now_ms": now_ms, "budget_seconds": compute_timeout},
                service_timeout_seconds=compute_timeout,
            )
            self._set_rows("eligible", reduced.eligible_rows)
            self._set_rows("public", len(reduced.snapshot["items"]))
            self._set_bytes("input", reduced.input_bytes)
            self._set_bytes("output", reduced.output_bytes)
            publish_timeout = _phase_timeout(
                deadline,
                cap=_TOKEN_RADAR_PUBLISH_TIMEOUT_SECONDS,
            )
            result = await self.db.run_business(
                "token_radar_current_publish",
                self.service.publish,
                reduced,
                operation_timeout_seconds=publish_timeout,
                now_ms=now_ms,
                session_timeout_seconds=publish_timeout,
            )
            outcome = str(result["status"])
            if self.telemetry is not None:
                self.telemetry.record_projection_cache("token_radar_current", outcome)
            return outcome, False
        except ResourceOperationOverrun:
            raise
        except (
            CpuTaskTimeout,
            ResourceAdmissionTimeout,
            TokenRadarBudgetExceeded,
        ) as exc:
            await self._mark_failed(_error_code(exc), now_ms=now_ms, deadline=deadline)
            return "failed", True
        except (TokenRadarInputOverflow, TokenRadarOutputOverflow) as exc:
            await self._mark_failed(_error_code(exc), now_ms=now_ms, deadline=deadline)
            return "failed", False

    async def _mark_failed(self, error_code: str, *, now_ms: int, deadline: float) -> None:
        try:
            timeout = _phase_timeout(
                deadline,
                cap=_TOKEN_RADAR_FAILURE_TIMEOUT_SECONDS,
            )
            await self.db.run_business(
                "token_radar_current_fail",
                self.service.mark_failed,
                operation_timeout_seconds=timeout,
                now_ms=now_ms,
                error_code=error_code,
                session_timeout_seconds=timeout,
            )
        except ResourceOperationOverrun:
            raise
        except (ResourceAdmissionTimeout, TokenRadarBudgetExceeded):
            return

    def _set_rows(self, stage: str, rows: int) -> None:
        if self.telemetry is not None:
            self.telemetry.set_projection_rows("token_radar_current", stage, rows)

    def _set_bytes(self, direction: str, byte_count: int) -> None:
        if self.telemetry is not None:
            self.telemetry.set_projection_bytes(
                "token_radar_current",
                direction,
                byte_count,
            )

    def _record_job(self, status: str) -> None:
        if self.telemetry is not None:
            self.telemetry.record_job("token_radar_current", status)

    def _record_deadline_miss(self) -> None:
        if self.telemetry is not None:
            self.telemetry.record_projection_deadline_miss("token_radar_current", "radar")


class StocksRadarCurrentService:
    def __init__(self, *, db: Any, worker_name: str = "stocks_radar_current") -> None:
        self.db = db
        self.worker_name = worker_name

    def load(self, *, now_ms: int) -> list[dict[str, Any]]:
        with self._session() as repos:
            return StocksRadarCurrentRepository(repos.conn).load_material_inputs(now_ms=now_ms)

    def publish(self, reduced: Any, *, now_ms: int) -> int:
        with self._session() as repos:
            return StocksRadarCurrentRepository(repos.conn).publish(reduced, now_ms=now_ms)

    def _session(self) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=2.0,
            transaction_timeout_seconds=2.0,
        )


class StocksRadarCurrentProjection:
    """Independent fixed-period Stocks writer over material facts."""

    def __init__(self, *, db: Any, cpu: Any, clock: Any | None = None) -> None:
        self.db = db
        self.cpu = cpu
        self.clock = clock or _now_ms
        self.service = StocksRadarCurrentService(db=db)

    async def sample(self) -> None:
        now_ms = int(self.clock())
        try:
            rows = await self.db.run_business(
                "stocks_radar_current_load",
                self.service.load,
                operation_timeout_seconds=_STOCKS_RADAR_LOAD_TIMEOUT_SECONDS,
                now_ms=now_ms,
            )
            reduced = await self.cpu.run(
                "stocks_radar_current_reduce",
                _reduce_stocks_payload,
                {"rows": rows, "now_ms": now_ms},
                service_timeout_seconds=STOCKS_RADAR_REDUCER_BUDGET_SECONDS,
            )
            await self.db.run_business(
                "stocks_radar_current_publish",
                self.service.publish,
                reduced,
                operation_timeout_seconds=_STOCKS_RADAR_PUBLISH_TIMEOUT_SECONDS,
                now_ms=now_ms,
            )
        except (
            CpuTaskTimeout,
            ResourceAdmissionTimeout,
            StocksRadarInputOverflow,
        ):
            return


class RadarCurrentProjectionCycle:
    """One cadence owner that gives Token deterministic CPU priority over Stocks."""

    def __init__(
        self,
        *,
        token: TokenRadarCurrentProjection,
        stocks: StocksRadarCurrentProjection,
    ) -> None:
        self.token = token
        self.stocks = stocks

    async def initialize(self) -> None:
        """Publish Token Radar before competing startup reconciliation begins."""

        await self.token.sample()

    async def sample(self) -> None:
        await self.token.sample()
        await self.stocks.sample()


def _reduce_token_payload(payload: dict[str, Any]) -> Any:
    budget_seconds = float(payload["budget_seconds"])
    return reduce_token_radar(
        payload["rows"],
        now_ms=int(payload["now_ms"]),
        deadline_monotonic=time.monotonic() + budget_seconds,
    )


def _reduce_stocks_payload(payload: dict[str, Any]) -> Any:
    return reduce_stocks_radar(payload["rows"], now_ms=int(payload["now_ms"]))


def _error_code(exc: Exception) -> str:
    if isinstance(exc, CpuTaskTimeout):
        return "token_radar_reducer_budget_exceeded"
    text = str(exc).strip()
    return text or type(exc).__name__


def _phase_timeout(
    deadline: float,
    *,
    cap: float,
    reserve_seconds: float = 0.0,
) -> float:
    available = deadline - time.monotonic() - reserve_seconds
    if available <= 0:
        raise TokenRadarBudgetExceeded("token_radar_sample_budget_exceeded")
    return max(0.001, min(float(cap), available))


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = [
    "RadarCurrentProjectionCycle",
    "StocksRadarCurrentProjection",
    "StocksRadarCurrentService",
    "TokenRadarCurrentProjection",
    "TokenRadarCurrentService",
]
