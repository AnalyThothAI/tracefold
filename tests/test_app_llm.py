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
    worker = SimpleNamespace(
        model="openai/deepseek-v4-flash",
        max_tokens=1_000,
        model_request_timeout_seconds=30,
    )

    model, effective_model = configured_chat_model(settings, worker)

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
    worker = SimpleNamespace(
        model="openai/gpt-5.4-mini",
        max_tokens=1_000,
        model_request_timeout_seconds=30,
    )

    model, _ = configured_chat_model(settings, worker)

    assert model.model_kwargs == {}
