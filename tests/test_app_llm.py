from __future__ import annotations

from types import SimpleNamespace

from tracefold.app.llm import configured_chat_model


def test_deepseek_v4_disables_thinking_for_structured_tool_calls() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="test-key",
            base_url="https://models.test/v1",
        )
    )
    model, effective_model = configured_chat_model(
        settings,
        model_name="openai/deepseek-v4-flash",
        max_tokens=1_000,
        request_timeout_seconds=30,
    )

    assert effective_model == "openai/deepseek-v4-flash"
    assert model.model_kwargs == {
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    assert model._client_params["extra_body"] == {
        "thinking": {"type": "disabled"},
    }


def test_non_deepseek_model_does_not_receive_provider_specific_thinking_flag() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="test-key",
            base_url="https://models.test/v1",
        )
    )
    model, _ = configured_chat_model(
        settings,
        model_name="openai/gpt-5.4-mini",
        max_tokens=1_000,
        request_timeout_seconds=30,
    )

    assert model.model_kwargs == {}


def test_qwen_disables_thinking_via_chat_template_kwargs() -> None:
    settings = SimpleNamespace(llm=SimpleNamespace(api_key="test-key", base_url="https://models.test/v1"))
    model, effective = configured_chat_model(
        settings, model_name="qwen3.8-27b", max_tokens=700, request_timeout_seconds=30
    )
    assert effective == "openai/qwen3.8-27b"
    assert model.model_kwargs == {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


def test_endpoint_override_targets_the_fallback_gateway() -> None:
    settings = SimpleNamespace(llm=SimpleNamespace(api_key="local-key", base_url="http://192.168.0.2:8080/v1"))
    model, effective = configured_chat_model(
        settings,
        model_name="deepseek-chat",
        max_tokens=700,
        request_timeout_seconds=30,
        api_key="remote-key",
        base_url="https://api.deepseek.com/v1",
    )
    assert effective == "openai/deepseek-chat"
    assert model.api_base == "https://api.deepseek.com/v1"
    assert model.api_key == "remote-key"
