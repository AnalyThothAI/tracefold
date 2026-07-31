from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tracefold.platform.config.settings import Settings
from tracefold.platform.workers.worker_base import WorkerBase


@dataclass(frozen=True, slots=True)
class WorkerFactoryContext:
    settings: Settings
    db: Any
    telemetry: Any
    asset_market: Any | None
    collector: WorkerBase | None
    collector_enabled: bool
    resources: Any
    provider_governor: Any
    runtime_id: str
    inactive_statuses: dict[str, dict[str, Any]]


WorkerFactory = Callable[
    [WorkerFactoryContext],
    Mapping[str, WorkerBase],
]


def mark_inactive(
    ctx: WorkerFactoryContext,
    name: str,
    *,
    effective_status: str,
    reason: str | None = None,
) -> None:
    if name in ctx.inactive_statuses:
        raise ValueError(f"worker_inactive_duplicate:{name}")
    ctx.inactive_statuses[name] = _inactive_status(
        effective_status=effective_status,
        reason=_redacted_reason(reason) if reason else None,
    )


def _inactive_status(
    *,
    effective_status: str,
    reason: str | None = None,
) -> dict[str, Any]:
    if effective_status not in {"disabled", "unavailable"}:
        raise ValueError("inactive_worker_status_invalid")
    return {
        "enabled": effective_status == "unavailable",
        "running": False,
        "effective_status": effective_status,
        "unavailable_reason": reason,
        "last_started_at_ms": None,
        "last_finished_at_ms": None,
        "last_result": None,
        "last_error": None,
        "iteration_duration_p99_ms": None,
    }


def _redacted_reason(reason: str) -> str:
    value = str(reason or "").strip().lower()
    allowed = []
    for char in value:
        if char.isalnum() or char == "_":
            allowed.append(char)
        elif char in {"-", ".", " "}:
            allowed.append("_")
    return "".join(allowed).strip("_") or "unavailable"


__all__ = [
    "WorkerFactory",
    "WorkerFactoryContext",
    "mark_inactive",
]
