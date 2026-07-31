from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tracefold.app.worker_manifest import manifest_by_name

_EFFECTIVE_STATUSES = frozenset({"disabled", "unavailable", "degraded", "running", "stopped", "failed"})
_WORKER_STATUS_FIELDS = frozenset(
    {
        "enabled",
        "running",
        "effective_status",
        "unavailable_reason",
        "last_started_at_ms",
        "last_finished_at_ms",
        "last_result",
        "last_error",
        "iteration_duration_p99_ms",
    }
)


def manifest_worker_statuses(payload: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    manifests = manifest_by_name()
    missing_workers = set(manifests) - set(payload)
    unknown_workers = set(payload) - set(manifests)
    if missing_workers or unknown_workers:
        raise ValueError(
            f"worker_status_manifest_mismatch:missing={sorted(missing_workers)}:unknown={sorted(unknown_workers)}"
        )
    workers: dict[str, dict[str, Any]] = {}
    for name in manifests:
        status = dict(payload[name])
        missing_fields = _WORKER_STATUS_FIELDS - set(status)
        unknown_fields = set(status) - _WORKER_STATUS_FIELDS
        if missing_fields or unknown_fields:
            raise ValueError(
                f"worker_status_fields_mismatch:{name}:"
                f"missing={sorted(missing_fields)}:unknown={sorted(unknown_fields)}"
            )
        status["effective_status"] = effective_worker_status(status)
        if status.get("unavailable_reason") is not None:
            status["unavailable_reason"] = str(status["unavailable_reason"])
        workers[name] = status
    return workers


def effective_worker_status(status: Mapping[str, Any]) -> str:
    explicit = status.get("effective_status")
    if isinstance(explicit, str) and explicit in _EFFECTIVE_STATUSES:
        return explicit
    raise ValueError("worker_effective_status_required")
