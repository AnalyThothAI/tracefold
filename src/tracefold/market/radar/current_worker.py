from __future__ import annotations

import time
from typing import Any

from loguru import logger

from tracefold.market.radar.reducer import (
    RadarEvidenceRevision,
    TokenRadarInputOverflow,
    TokenRadarInvariantViolation,
    TokenRadarOutputOverflow,
    enrich_token_radar,
    reduce_token_radar,
)
from tracefold.market.radar.snapshot_repository import (
    TokenRadarCurrentRepository,
    TokenRadarPublicationResult,
)
from tracefold.platform.resource import (
    CpuTaskProcessExpired,
    CpuTaskTimeout,
    ResourceAdmissionTimeout,
    ResourceOperationOverrun,
)

# One native PostgreSQL boundary for every Radar statement. This is a database
# safety limit, not a projection phase or whole-turn budget.
_TOKEN_RADAR_DATABASE_TIMEOUT_SECONDS = 9.0


class TokenRadarCurrentService:
    def __init__(self, *, db: Any, worker_name: str = "token_radar_current") -> None:
        self.db = db
        self.worker_name = worker_name

    def load(self, *, now_ms: int) -> list[RadarEvidenceRevision]:
        with self._session() as repos:
            return TokenRadarCurrentRepository(repos.conn).load_material_inputs(now_ms=now_ms)

    def publish(self, reduced: Any, *, now_ms: int) -> TokenRadarPublicationResult:
        with self._session() as repos:
            return TokenRadarCurrentRepository(repos.conn).publish(
                reduced,
                updated_at_ms=now_ms,
            )

    def load_presentation(self, reduced: Any, *, now_ms: int) -> list[dict[str, Any]]:
        with self._session() as repos:
            return TokenRadarCurrentRepository(repos.conn).load_presentation_facts(
                list(reduced.selected_keys),
                now_ms=now_ms,
            )

    def _session(
        self,
        *,
        timeout_seconds: float = _TOKEN_RADAR_DATABASE_TIMEOUT_SECONDS,
    ) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=timeout_seconds,
            transaction_timeout_seconds=None,
        )


class TokenRadarCurrentProjection:
    """The sole after-completion writer for the complete Token Radar singleton."""

    def __init__(
        self,
        *,
        db: Any,
        cpu: Any,
        clock: Any | None = None,
    ) -> None:
        self.db = db
        self.cpu = cpu
        self.clock = clock or _now_ms
        self.service = TokenRadarCurrentService(db=db)

    async def sample(self) -> None:
        now_ms = int(self.clock())
        try:
            rows = await self.db.run_business(
                "token_radar_current_load",
                self.service.load,
                operation_timeout_seconds=_TOKEN_RADAR_DATABASE_TIMEOUT_SECONDS,
                now_ms=now_ms,
            )
            reduced = await self.cpu.run(
                "token_radar_current_reduce",
                _reduce_token_payload,
                {"rows": rows, "now_ms": now_ms},
                service_timeout_seconds=None,
            )
            presentation_rows = await self.db.run_business(
                "token_radar_current_present",
                self.service.load_presentation,
                reduced,
                operation_timeout_seconds=_TOKEN_RADAR_DATABASE_TIMEOUT_SECONDS,
                now_ms=now_ms,
            )
            reduced = enrich_token_radar(reduced, presentation_rows, now_ms=now_ms)
            await self.db.run_business(
                "token_radar_current_publish",
                self.service.publish,
                reduced,
                operation_timeout_seconds=_TOKEN_RADAR_DATABASE_TIMEOUT_SECONDS,
                now_ms=now_ms,
            )
        except Exception as exc:
            logger.bind(
                error_code=_error_code(exc),
                error_type=type(exc).__name__,
            ).warning("Token Radar sample failed; last successful snapshot retained")


def _reduce_token_payload(payload: dict[str, Any]) -> Any:
    return reduce_token_radar(
        payload["rows"],
        now_ms=int(payload["now_ms"]),
    )


def _error_code(exc: Exception) -> str:
    bounded_codes = (
        CpuTaskProcessExpired,
        CpuTaskTimeout,
        ResourceAdmissionTimeout,
        ResourceOperationOverrun,
        TokenRadarInputOverflow,
        TokenRadarInvariantViolation,
        TokenRadarOutputOverflow,
    )
    if isinstance(exc, bounded_codes):
        text = str(exc).strip()
        if text:
            return text[:160]
    return f"token_radar_turn_failed:{type(exc).__name__}"[:160]


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = [
    "TokenRadarCurrentProjection",
    "TokenRadarCurrentService",
]
