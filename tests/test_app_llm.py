from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tracefold.app import workers
from tracefold.app.llm import ConfiguredLMEndpoint, configured_lm_endpoint


def test_deepseek_v4_disables_thinking_for_structured_tool_calls() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="test-key",
            base_url="https://models.test/v1",
        )
    )
    endpoint = configured_lm_endpoint(
        settings,
        model_name="openai/deepseek-v4-flash",
    )

    assert endpoint.model_name == "openai/deepseek-v4-flash"
    assert endpoint.model_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_non_deepseek_model_does_not_receive_provider_specific_thinking_flag() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="test-key",
            base_url="https://models.test/v1",
        )
    )
    endpoint = configured_lm_endpoint(
        settings,
        model_name="openai/gpt-5.4-mini",
    )

    assert "extra_body" not in endpoint.model_kwargs


def test_qwen_disables_thinking_via_chat_template_kwargs() -> None:
    settings = SimpleNamespace(llm=SimpleNamespace(api_key="test-key", base_url="https://models.test/v1"))
    endpoint = configured_lm_endpoint(settings, model_name="qwen3.8-27b")
    assert endpoint.model_name == "openai/qwen3.8-27b"
    assert endpoint.model_kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_endpoint_override_targets_the_fallback_gateway() -> None:
    settings = SimpleNamespace(llm=SimpleNamespace(api_key="local-key", base_url="http://192.168.0.2:8080/v1"))
    endpoint = configured_lm_endpoint(
        settings,
        model_name="deepseek-chat",
        api_key="remote-key",
        base_url="https://api.deepseek.com/v1",
    )
    assert endpoint.model_name == "openai/deepseek-chat"
    assert endpoint.api_base == "https://api.deepseek.com/v1"
    assert endpoint.api_key == "remote-key"


def test_worker_composes_an_arm_local_program_with_primary_and_fallback_adapters(monkeypatch: Any) -> None:
    configured: list[dict[str, Any]] = []

    def fake_configured_endpoint(_settings: Any, **kwargs: Any) -> ConfiguredLMEndpoint:
        configured.append(kwargs)
        return ConfiguredLMEndpoint(
            model_name=f"effective:{kwargs['model_name']}",
            api_key=str(kwargs.get("api_key") or "primary-key"),
            api_base=str(kwargs.get("base_url") or "https://primary.test/v1"),
            model_kwargs={},
        )

    class FakeAdapter:
        @classmethod
        def from_runtime(cls, **kwargs: Any) -> Any:
            instance = cls()
            instance.runtime = kwargs
            return instance

    class FakeProgram:
        def __init__(self, artifact: object, *, primary_adapter: Any, fallback_adapter: Any) -> None:
            self.artifact = artifact
            self.primary_adapter = primary_adapter
            self.fallback_adapter = fallback_adapter

    monkeypatch.setattr(workers, "configured_lm_endpoint", fake_configured_endpoint)
    monkeypatch.setattr(workers, "DspyPredictorAdapter", FakeAdapter)
    monkeypatch.setattr(workers, "DspyNewsSemanticProgram", FakeProgram)
    settings = SimpleNamespace(
        news=SimpleNamespace(triage=SimpleNamespace(deadline_seconds=20)),
        llm=SimpleNamespace(
            news_triage_fallback=SimpleNamespace(api_key="fallback-key", base_url="https://fallback.test/v1")
        ),
    )
    models = SimpleNamespace(
        triage_configured=True,
        triage_model="primary-model",
        triage_fallback_model="fallback-model",
    )
    artifact = SimpleNamespace(
        execution=SimpleNamespace(route_deadline_seconds=18),
        event_semantics=SimpleNamespace(max_tokens=600),
        reader_card=SimpleNamespace(max_tokens=720),
    )

    program = workers._configured_semantic_program(settings, artifact, models)

    assert isinstance(program, FakeProgram)
    assert program.artifact is artifact
    assert program.primary_adapter.runtime == {
        "model_name": "effective:primary-model",
        "api_key": "primary-key",
        "api_base": "https://primary.test/v1",
        "timeout": 18.0,
        "max_tokens": 720,
        "model_kwargs": {},
    }
    assert program.fallback_adapter.runtime == {
        "model_name": "effective:fallback-model",
        "api_key": "fallback-key",
        "api_base": "https://fallback.test/v1",
        "timeout": 18.0,
        "max_tokens": 720,
        "model_kwargs": {},
    }
    assert configured == [
        {
            "model_name": "primary-model",
        },
        {
            "model_name": "fallback-model",
            "api_key": "fallback-key",
            "base_url": "https://fallback.test/v1",
        },
    ]
