from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tracefold.app.worker_status import manifest_worker_statuses

_PROVIDER_CONNECTION_STATES = frozenset(
    {
        "authenticating",
        "circuit_open",
        "connected",
        "connecting",
        "degraded_recoverable",
        "disabled",
        "disconnected",
        "failed",
        "failed_terminal",
        "running",
        "streaming",
        "subscribed",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    workers: dict[str, dict[str, Any]]
    collector: dict[str, Any]
    provider_states: dict[str, dict[str, Any]]
    startup_db_status: dict[str, Any]
    composition: dict[str, Any]
    degradation_reasons: tuple[str, ...]

    @classmethod
    def startup(
        cls,
        *,
        startup_db_status: Mapping[str, Any],
        composition: Mapping[str, Any],
    ) -> RuntimeSnapshot:
        return cls(
            workers={},
            collector={},
            provider_states={},
            startup_db_status=dict(startup_db_status),
            composition=dict(composition),
            degradation_reasons=(),
        )


def capture_runtime_snapshot(runtime: Any) -> RuntimeSnapshot:
    cached = runtime.snapshot
    if not isinstance(cached, RuntimeSnapshot):
        raise TypeError("runtime_snapshot_required")

    collector = _collector_details(runtime.collector)
    workers = manifest_worker_statuses(runtime.supervisor.status_payload())
    provider_states = {
        "gmgn_direct_ws": _provider_connection_state(runtime.collector.upstream_client),
        "okx_dex_ws": _provider_connection_state(runtime.providers.asset_market.stream_dex_market),
    }
    reasons = _worker_degradation_reasons(workers, runtime.supervisor.tasks)
    reasons.extend(_provider_degradation_reasons(provider_states))
    return RuntimeSnapshot(
        workers=workers,
        collector=collector,
        provider_states=provider_states,
        startup_db_status=dict(cached.startup_db_status),
        composition=dict(cached.composition),
        degradation_reasons=tuple(dict.fromkeys(str(reason) for reason in reasons)),
    )


def _collector_details(collector: Any) -> dict[str, Any]:
    payload = collector.status.to_dict()
    if not isinstance(payload, Mapping):
        raise TypeError("collector_status_payload_must_be_dict")
    return dict(payload)


def _provider_connection_state(provider: Any | None) -> dict[str, Any]:
    if provider is None:
        return {"state": "disabled", "last_state_change_at_ms": None}
    try:
        payload = provider.connection_state_payload()
    except AttributeError:
        return {
            "state": "failed",
            "last_state_change_at_ms": None,
            "error": "provider_connection_state_contract_missing",
        }
    except Exception as exc:
        return {
            "state": "failed",
            "last_state_change_at_ms": None,
            "error": type(exc).__name__,
        }
    if not isinstance(payload, Mapping):
        return {
            "state": "failed",
            "last_state_change_at_ms": None,
            "error": "provider_connection_state_payload_not_mapping",
        }
    state = payload.get("state")
    if not isinstance(state, str) or state not in _PROVIDER_CONNECTION_STATES:
        return {
            "state": "failed",
            "last_state_change_at_ms": None,
            "error": "provider_connection_state_invalid",
        }
    if "last_state_change_at_ms" not in payload:
        return {
            "state": "failed",
            "last_state_change_at_ms": None,
            "error": "provider_connection_state_timestamp_missing",
        }
    return dict(payload)


def _provider_degradation_reasons(provider_states: Mapping[str, Mapping[str, Any]]) -> list[str]:
    healthy_states = {"authenticating", "connected", "connecting", "running", "streaming", "subscribed"}
    reasons: list[str] = []
    for name, payload in provider_states.items():
        state = str(payload.get("state") or "unknown")
        if state == "disabled" or state in healthy_states:
            continue
        reasons.append(f"provider:{name}:{state}")
    return reasons


def _worker_degradation_reasons(
    workers: Mapping[str, Mapping[str, Any]],
    tasks: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    for name, status in workers.items():
        effective_status = str(status["effective_status"])
        if effective_status == "disabled":
            continue
        if effective_status == "unavailable":
            reasons.append(f"worker:{name}:unavailable:{status.get('unavailable_reason') or 'unavailable'}")
            continue
        task = tasks.get(name)
        if task is not None and task.done() and not task.cancelled():
            error = task.exception()
            if error is not None:
                reasons.append(f"worker:{name}:errored:{error}")
                continue
        if effective_status == "degraded":
            reasons.append(f"worker:{name}:degraded")
            continue
        if effective_status == "stopped":
            continue
        if effective_status == "failed":
            error = status.get("last_error")
            reasons.append(f"worker:{name}:errored:{error}" if error else f"worker:{name}:failed")
    return reasons


__all__ = ["RuntimeSnapshot", "capture_runtime_snapshot"]
