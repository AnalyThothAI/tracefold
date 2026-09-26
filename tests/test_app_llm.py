from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from dspy.lm15 import Message, Request, Response, Usage, response_to_events

from tracefold.app import learning_runtime
from tracefold.app.llm import configured_lm_endpoint
from tracefold.app.workers.wiring import news as workers
from tracefold.news.updates.dspy_backend import CopySignature
from tracefold.platform.config.models import LlmRequestConfig, Settings


def _llm(*, api_key: str, base_url: str) -> SimpleNamespace:
    """One partial `llm` section, carrying the request envelope every real endpoint model has."""

    return SimpleNamespace(api_key=api_key, base_url=base_url, request=LlmRequestConfig())


def test_deepseek_v4_disables_thinking_for_structured_tool_calls() -> None:
    settings = SimpleNamespace(llm=_llm(api_key="test-key", base_url="https://models.test/v1"))
    endpoint = configured_lm_endpoint(
        settings,
        model_name="openai/deepseek-v4-flash",
    )

    assert endpoint.model_name == "openai/deepseek-v4-flash"
    assert endpoint.model_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_non_deepseek_model_does_not_receive_provider_specific_thinking_flag() -> None:
    settings = SimpleNamespace(llm=_llm(api_key="test-key", base_url="https://models.test/v1"))
    endpoint = configured_lm_endpoint(
        settings,
        model_name="openai/gpt-5.4-mini",
    )

    assert "extra_body" not in endpoint.model_kwargs


def test_qwen_disables_thinking_via_chat_template_kwargs() -> None:
    settings = SimpleNamespace(llm=_llm(api_key="test-key", base_url="https://models.test/v1"))
    endpoint = configured_lm_endpoint(settings, model_name="qwen3.8-27b")
    assert endpoint.model_name == "openai/qwen3.8-27b"
    assert endpoint.model_kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_qwen_thinking_alias_is_called_without_a_disable_override() -> None:
    settings = SimpleNamespace(llm=_llm(api_key="test-key", base_url="https://models.test/v1"))

    endpoint = configured_lm_endpoint(settings, model_name="qwen3.8-27b:thinking")

    assert endpoint.model_name == "openai/qwen3.8-27b:thinking"
    assert endpoint.model_kwargs == {}
    assert endpoint.structured_output == "prompt_json"


class _CaptureEngine:
    """A provider double below the real DSPy JSON adapter: records the normalized request it is sent."""

    def __init__(self) -> None:
        self.requests: list[Request] = []

    def complete(self, request: Request) -> Response:
        self.requests.append(request)
        answer = {"result": {"headline_zh": "标题", "lines": [{"claim_ref": "c1", "text_zh": "内容。"}]}}
        return Response(
            id=None,
            model=request.model,
            message=Message.assistant(json.dumps(answer, ensure_ascii=False)),
            finish_reason="stop",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    def stream(self, request: Request) -> Iterator[Any]:
        return iter(response_to_events(self.complete(request)))

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("model", "base_url", "expected_mode", "expected_format", "expected_extra"),
    [
        (
            "qwen3.8-27b",
            "https://qwen.test/v1",
            "json_schema",
            "schema",
            {"chat_template_kwargs": {"enable_thinking": False}},
        ),
        (
            "deepseek-v4-flash",
            "https://deepseek.test/v1",
            "json_object",
            "object",
            {"thinking": {"type": "disabled"}},
        ),
    ],
)
def test_configured_provider_capability_shapes_the_actual_native_dspy_request(
    model: str,
    base_url: str,
    expected_mode: str,
    expected_format: str,
    expected_extra: dict[str, Any],
) -> None:
    settings = SimpleNamespace(llm=_llm(api_key="request-shape-secret", base_url=base_url))
    endpoint = configured_lm_endpoint(settings, model_name=model)
    # The production LM carries the provider extras to LiteLLM as request kwargs.
    production = learning_runtime.generative_lm(endpoint, max_tokens=2048, timeout=5.0)
    assert production.kwargs["extra_body"] == expected_extra
    assert "request-shape-secret" not in repr(production.kwargs.get("extra_body"))
    # The same structured-output capability, below the real DSPy JSON adapter, shapes the request.
    engine = _CaptureEngine()
    request: dict[str, Any] = {"cache": False, "num_retries": 0, "max_tokens": 2048}
    if endpoint.temperature is not None:
        request["temperature"] = endpoint.temperature
    lm = learning_runtime.GenerativeLM(
        endpoint.model_name, structured_output=endpoint.structured_output, engine=engine, **request
    )

    with dspy.context(adapter=dspy.JSONAdapter()):
        prediction = dspy.Predict(CopySignature)(selected_claims_json="[]", lm=lm)

    assert prediction.result.headline_zh == "标题"
    assert endpoint.structured_output == expected_mode
    assert len(engine.requests) == 1
    sent = engine.requests[0]
    if expected_format == "schema":
        assert isinstance(sent.config.response_format, dict)
        assert sent.config.response_format["type"] == "json_schema"
        assert isinstance(sent.config.response_format["schema"], dict)
    elif expected_format == "object":
        assert sent.config.response_format == {"type": "json_object"}
    else:
        assert sent.config.response_format is None
    visible = repr(sent.messages)
    assert "selected_claims_json" in visible
    assert "request-shape-secret" not in visible
    assert base_url not in visible


def test_kimi_coding_endpoint_has_no_hidden_compatibility_profile() -> None:
    settings = SimpleNamespace(llm=_llm(api_key="test-key", base_url="https://api.kimi.com/coding/v1"))

    endpoint = configured_lm_endpoint(settings, model_name="k3")

    assert endpoint.model_kwargs == {}
    assert endpoint.temperature == 0.0
    assert endpoint.structured_output == "json_schema"


def test_operator_can_describe_a_custom_openai_compatible_request_without_endpoint_detection() -> None:
    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "test-key",
                "base_url": "http://127.0.0.1:8080/v1",
                "news_triage_model": "my-local-model",
                "request": {
                    "send_temperature": False,
                    "structured_output": "prompt_json",
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                },
            }
        }
    )

    endpoint = configured_lm_endpoint(settings, model_name="my-local-model")

    assert endpoint.temperature is None
    assert endpoint.structured_output == "prompt_json"
    assert endpoint.model_kwargs == {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


def test_endpoint_override_targets_the_fallback_gateway() -> None:
    settings = SimpleNamespace(llm=_llm(api_key="local-key", base_url="http://192.168.0.2:8080/v1"))
    endpoint = configured_lm_endpoint(
        settings,
        model_name="deepseek-chat",
        api_key="remote-key",
        base_url="https://api.deepseek.com/v1",
    )
    assert endpoint.model_name == "openai/deepseek-chat"
    assert endpoint.api_base == "https://api.deepseek.com/v1"
    assert endpoint.api_key == "remote-key"
    assert "remote-key" not in repr(endpoint)
    assert "api.deepseek.com" not in repr(endpoint)
    assert "api_base" not in repr(endpoint)


class _StartupBus:
    def __init__(
        self,
        *,
        url: str,
        name_prefix: str,
        connect_timeout_seconds: float,
        management_url: str | None = None,
        telemetry: Any | None = None,
    ) -> None:
        del url, connect_timeout_seconds, management_url, telemetry
        self.prefix = name_prefix
        self.connected = False
        self.settle_timeout_seconds: float | None = None
        self.policies_verified = False

    async def connect(self) -> None:
        self.connected = True

    async def verify_policies(self, *, settle_timeout_seconds: float | None = None) -> dict[str, Any]:
        self.settle_timeout_seconds = settle_timeout_seconds
        self.policies_verified = True
        return {"verified": []}


def _startup_settings() -> Settings:
    return Settings(
        llm={
            "api_key": "test-key",
            "base_url": "https://models.test/v1",
            "news_triage_model": "test-model",
        },
        news={
            "broker": {"url": "amqp://guest:guest@broker.test:5672/"},
            "venues": {"enabled": False},
        },
    )


def test_news_wiring_verifies_the_broker_policy_with_the_bounded_settle_before_consuming(monkeypatch: Any) -> None:
    """#400: the attach runs the bounded settle; a one-shot read dies inside the statistics interval."""

    from tracefold.integrations.rabbitmq import POLICY_EFFECTIVE_TIMEOUT_SECONDS

    monkeypatch.setattr("tracefold.integrations.rabbitmq.RabbitMQBus", _StartupBus)
    bus = asyncio.run(workers._connect_news_bus(_startup_settings()))

    assert bus.connected is True
    assert bus.policies_verified is True
    assert bus.settle_timeout_seconds == POLICY_EFFECTIVE_TIMEOUT_SECONDS


def _news_settings(**llm: Any) -> Settings:
    return Settings(
        llm={
            "api_key": "news-key",
            "base_url": "https://triage.test/v1",
            "news_triage_model": "triage-model",
            **llm,
        }
    )


def test_unconfigured_news_models_compose_no_runtime_route() -> None:
    assert learning_runtime.compose_news_models(Settings()) is None
    invalid_reader = Settings.model_construct(
        llm=Settings().llm.model_copy(
            update={
                "api_key": "news-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "triage-model",
                "news_reader_card": Settings().llm.news_reader_card.model_copy(
                    update={"api_key": "k", "base_url": "ftp://reader.test", "model": "reader"}
                ),
            }
        )
    )
    assert learning_runtime.compose_news_models(invalid_reader) is None


def test_extraction_and_judgment_share_the_triage_route_and_cards_use_the_reader_route() -> None:
    models = learning_runtime.compose_news_models(
        _news_settings(
            news_triage_fallback={"api_key": "fb-key", "base_url": "https://fallback.test/v1", "model": "fb-model"},
            news_reader_card={"api_key": "reader-key", "base_url": "https://reader.test/v1", "model": "reader"},
        )
    )
    assert models is not None
    assert models.extraction.primary is models.judgment.primary
    assert models.extraction.fallback is models.judgment.fallback
    assert models.extraction.fallback is not None and models.extraction.fallback.model_name == "openai/fb-model"
    assert models.card.primary.model_name == "openai/reader"
    # No dedicated reader fallback: the card route inherits the extraction fallback, as before.
    assert models.card.fallback is models.extraction.fallback
    extraction = models.extraction.lms()
    assert [lm.model for lm in extraction] == ["openai/triage-model", "openai/fb-model"]
    assert all(lm.kwargs["max_tokens"] == learning_runtime.EXTRACTION_MAX_TOKENS for lm in extraction)
    assert all(lm.num_retries == 0 and lm.cache is False for lm in extraction)
    assert [lm.kwargs["max_tokens"] for lm in models.card.lms()] == [learning_runtime.CARD_MAX_TOKENS] * 2
    assert models.news_judgment is None
    assert models.status()["judgment_backend"] == "generated"
    assert models.status()["judgment_model"] == "triage-model"


def test_route_identities_are_secret_free_and_ignore_key_rotation() -> None:
    before = learning_runtime.compose_news_models(_news_settings(api_key="key-before"))
    after = learning_runtime.compose_news_models(_news_settings(api_key="key-after"))
    other = learning_runtime.compose_news_models(_news_settings(news_triage_model="other-model"))
    assert before is not None and after is not None and other is not None
    assert before.extraction.identity == after.extraction.identity
    assert before.program_identity == after.program_identity
    assert "key-before" not in repr(before.status())
    assert other.extraction.identity != before.extraction.identity
    assert other.program_identity != before.program_identity


def test_news_jev_is_its_own_route_and_trading_semantics_never_enables_it() -> None:
    jev = {"api_key": "jev-key", "base_url": "https://openrouter.ai/api/", "model": "jev-1.13"}
    trading_only = learning_runtime.compose_news_models(_news_settings(trading_semantics=jev))
    native = learning_runtime.compose_news_models(_news_settings(news_judgment=jev))
    assert trading_only is not None and native is not None
    assert trading_only.news_judgment is None
    assert native.news_judgment is not None
    assert native.news_judgment.base_url == "https://openrouter.ai/api"
    assert "jev-key" not in repr(native.news_judgment)
    assert native.status()["judgment_backend"] == "native"
    assert native.status()["judgment_model"] == "jev-1.13"
    assert native.program_identity != trading_only.program_identity


def test_generative_lm_states_the_structured_output_capability_of_its_endpoint() -> None:
    models = learning_runtime.compose_news_models(_news_settings(request={"structured_output": "json_object"}))
    assert models is not None
    (lm,) = models.extraction.lms()
    assert lm.supported_params == {"response_format"}
    assert lm.supports_response_schema is False
    prompt_only = learning_runtime.generative_lm(
        configured_lm_endpoint(_news_settings(request={"structured_output": "prompt_json"}), model_name="m"),
        max_tokens=10,
        timeout=1.0,
    )
    assert prompt_only.supported_params == set()


def test_the_runtime_manifest_names_the_configured_program_and_image() -> None:
    settings = _news_settings()
    digest = "sha256:" + "1" * 64
    first = learning_runtime.news_runtime_manifest_sha(settings, image_digest=digest, runtime_revision="r")
    again = learning_runtime.news_runtime_manifest_sha(settings, image_digest=digest, runtime_revision="r")
    image = learning_runtime.news_runtime_manifest_sha(
        settings, image_digest="sha256:" + "2" * 64, runtime_revision="r"
    )
    unconfigured = learning_runtime.news_runtime_manifest_sha(Settings(), image_digest=digest, runtime_revision="r")
    assert first == again
    assert len({first, image, unconfigured}) == 3
    identity = SimpleNamespace(image_digest=digest, runtime_revision="r")
    assert workers.configured_runtime_manifest_sha(settings, identity=identity) == first


def test_route_identities_canonicalize_equivalent_endpoint_urls() -> None:
    explicit_port = learning_runtime.compose_news_models(_news_settings(base_url="https://TRIAGE.TEST:443/v1/"))
    canonical = learning_runtime.compose_news_models(_news_settings(base_url="https://triage.test/v1"))
    assert explicit_port is not None and canonical is not None
    assert explicit_port.extraction.identity == canonical.extraction.identity
    assert explicit_port.program_identity == canonical.program_identity


def test_operator_request_controls_are_assigned_per_route() -> None:
    models = learning_runtime.compose_news_models(
        _news_settings(
            request={"send_temperature": False, "structured_output": "prompt_json"},
            news_reader_card={
                "api_key": "reader-key",
                "base_url": "https://reader.test/v1",
                "model": "reader-model",
                "request": {"temperature": 0.4, "send_temperature": True},
            },
        )
    )
    assert models is not None
    assert models.extraction.primary.temperature is None
    assert models.extraction.primary.structured_output == "prompt_json"
    assert models.card.primary.temperature == 0.4
    assert models.card.primary.structured_output == "json_schema"


def test_a_dedicated_reader_fallback_is_its_own_secret_free_route() -> None:
    models = learning_runtime.compose_news_models(
        _news_settings(
            news_triage_fallback={
                "api_key": "event-fallback-key",
                "base_url": "https://event-fallback.test/v1",
                "model": "event-fallback-model",
            },
            news_reader_card_fallback={
                "api_key": "reader-fallback-key",
                "base_url": "https://reader-fallback.test/v1",
                "model": "reader-fallback-model",
            },
        )
    )
    assert models is not None
    assert models.card.fallback is not None and models.extraction.fallback is not None
    assert models.card.fallback.model_name == "openai/reader-fallback-model"
    assert models.card.identity != models.extraction.identity
    rendered = repr((models.card.identity, models.extraction.identity, models.status()))
    for secret in ("event-fallback.test", "reader-fallback.test", "event-fallback-key", "reader-fallback-key"):
        assert secret not in rendered
