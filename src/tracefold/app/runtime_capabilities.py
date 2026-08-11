from __future__ import annotations

from typing import Any


def macro_document_analysis_runtime(settings: Any) -> dict[str, Any]:
    """Return secret-free config admission, not worker process liveness."""

    configured = bool(settings.llm.api_key and settings.llm.base_url)
    enabled = bool(settings.llm.macro_document_analysis_enabled)
    state = "active" if enabled and configured else "unconfigured" if enabled else "disabled"
    return {
        "state": state,
        "enabled": enabled,
        "configured": configured,
        "worker_active": enabled and configured,
        "model": settings.llm.macro_document_analysis_model,
    }


__all__ = ["macro_document_analysis_runtime"]
