from __future__ import annotations

from types import SimpleNamespace

from tracefold.app.runtime_capabilities import macro_document_analysis_runtime


def test_enabled_document_analysis_without_gateway_is_truthfully_unconfigured() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key=None,
            base_url=None,
            macro_document_analysis_enabled=True,
            macro_document_analysis_model="policy-evidence-model",
        )
    )

    assert macro_document_analysis_runtime(settings) == {
        "state": "unconfigured",
        "enabled": True,
        "configured": False,
        "worker_active": False,
        "model": "policy-evidence-model",
    }
