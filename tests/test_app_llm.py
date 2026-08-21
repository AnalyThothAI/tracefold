from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tracefold.app import learning_runtime, workers
from tracefold.app.llm import ConfiguredLMEndpoint, configured_lm_endpoint
from tracefold.news import canonical_sha
from tracefold.news.agents.semantic_program import RuntimeModelIdentity


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


def test_active_arm_hashes_the_exact_secret_free_runtime_bindings(monkeypatch: Any) -> None:
    artifact = SimpleNamespace(program_version="program-v1", program_sha256="a" * 64)
    availability = SimpleNamespace(
        triage_model="primary-model",
        triage_fallback_model="fallback-model",
    )
    monkeypatch.setattr(learning_runtime, "load_stable_program_artifact", lambda: artifact)
    monkeypatch.setattr(learning_runtime, "news_model_availability", lambda _settings: availability)
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="primary-key",
            base_url="https://primary.test/v1",
            news_triage_model="primary-model",
            news_triage_fallback=SimpleNamespace(
                api_key="fallback-key",
                base_url="https://fallback.test/v1",
            ),
        ),
        news=SimpleNamespace(policy=SimpleNamespace(model_dump=lambda **_kwargs: {"similarity_max": 0.6})),
    )

    arm = learning_runtime.active_arm_manifest(settings)

    primary = RuntimeModelIdentity.issue(provider="openai", model="openai/primary-model").model_dump(mode="json")
    fallback = RuntimeModelIdentity.issue(provider="openai", model="openai/fallback-model").model_dump(mode="json")
    assert arm.runtime_model_bindings_sha256 == canonical_sha(
        {
            "identity_schema": "configured_runtime_binding_v1",
            "event_semantics.primary": primary,
            "reader_card.primary": primary,
            "event_semantics.fallback": fallback,
            "reader_card.fallback": fallback,
        }
    )


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


def test_policy_canary_reuses_stable_artifact_without_becoming_a_program_candidate(monkeypatch: Any) -> None:
    stable = SimpleNamespace(program_version="program-v1", program_sha256="a" * 64)
    candidate = SimpleNamespace(
        target="policy",
        candidate_arm=SimpleNamespace(program_version="program-v1", program_sha256="a" * 64),
    )

    def unexpected_load(_sha: str) -> Any:
        raise AssertionError("policy candidate must not load a child artifact")

    monkeypatch.setattr(workers, "load_program_artifact", unexpected_load)

    assert workers._candidate_program_artifact(candidate, stable) is stable
