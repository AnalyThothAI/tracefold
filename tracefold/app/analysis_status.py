"""Read projection for the separate Analysis process and its configured Agent."""

from __future__ import annotations

from typing import Any

_HEARTBEAT_STALE_MS = 15_000


def analysis_status_projection(
    settings: Any,
    runtime: dict[str, Any] | None,
    *,
    now_ms: int,
    last_case_at_ms: int | None,
) -> dict[str, Any]:
    state = "disabled" if not settings.trading.enabled else "unavailable"
    if (
        settings.trading.enabled
        and runtime is not None
        and now_ms - int(runtime["heartbeat_at_ms"]) <= _HEARTBEAT_STALE_MS
    ):
        state = "running" if runtime["model_configured"] else "model_unconfigured"
    return {
        "last_case_at_ms": last_case_at_ms,
        "state": state,
        "active_policy": (runtime["active_policy"] if runtime is not None else settings.trading.analysis.active_policy),
        "model_name": None if runtime is None else runtime["model_name"],
        "publish_signals": (
            bool(runtime["publish_signals"]) if runtime is not None else settings.trading.analysis.publish_signals
        ),
        "config_digest": None if runtime is None else runtime["config_digest"],
        "heartbeat_at_ms": None if runtime is None else int(runtime["heartbeat_at_ms"]),
    }
