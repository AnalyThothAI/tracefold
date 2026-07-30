from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tracefold.platform.config.settings import Settings
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


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


WorkerFactory = Callable[[WorkerFactoryContext], Mapping[str, WorkerBase]]


class InactiveWorker(WorkerBase):
    def __init__(
        self,
        *,
        name: str,
        settings: Any,
        db: Any,
        telemetry: Any,
        effective_status: str,
        unavailable_reason: str | None = None,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self._inactive_status = str(effective_status)
        self._inactive_reason = str(unavailable_reason) if unavailable_reason else None

    @property
    def effective_status(self) -> str:
        return self._inactive_status

    @property
    def unavailable_reason(self) -> str | None:
        return self._inactive_reason

    async def run_once(self) -> WorkerResult:
        return WorkerResult(skipped=1, notes={"reason": self.effective_status})


def disabled_worker(ctx: WorkerFactoryContext, name: str) -> WorkerBase:
    return InactiveWorker(
        name=name,
        settings=_worker_settings(ctx.settings, name, enabled=False),
        db=ctx.db,
        telemetry=ctx.telemetry,
        effective_status="disabled",
    )


def intentionally_not_started_worker(ctx: WorkerFactoryContext, name: str) -> WorkerBase:
    return InactiveWorker(
        name=name,
        settings=_worker_settings(ctx.settings, name, enabled=False),
        db=ctx.db,
        telemetry=ctx.telemetry,
        effective_status="intentionally_not_started",
    )


def unavailable_worker(ctx: WorkerFactoryContext, name: str, reason: str) -> WorkerBase:
    return InactiveWorker(
        name=name,
        settings=_worker_settings(ctx.settings, name, enabled=True),
        db=ctx.db,
        telemetry=ctx.telemetry,
        effective_status="unavailable",
        unavailable_reason=_redacted_reason(reason),
    )


def _redacted_reason(reason: str) -> str:
    value = str(reason or "").strip().lower()
    allowed = []
    for char in value:
        if char.isalnum() or char == "_":
            allowed.append(char)
        elif char in {"-", ".", " "}:
            allowed.append("_")
    return "".join(allowed).strip("_") or "unavailable"


def _worker_settings(settings: Settings, name: str, *, enabled: bool) -> Any:
    config = getattr(settings.workers, name)
    if config.enabled == enabled:
        return config
    model_copy = getattr(config, "model_copy", None)
    if not callable(model_copy):
        raise RuntimeError(f"worker_settings_model_copy_required:{name}")
    return model_copy(update={"enabled": enabled})


__all__ = [
    "InactiveWorker",
    "WorkerFactory",
    "WorkerFactoryContext",
    "disabled_worker",
    "intentionally_not_started_worker",
    "unavailable_worker",
]
