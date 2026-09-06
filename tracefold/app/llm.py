from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

StructuredOutputMode = Literal["json_schema", "json_object", "prompt_json"]


@dataclass(frozen=True, slots=True)
class ConfiguredLMEndpoint:
    """Provider-neutral runtime binding; the semantic Program owns the concrete LM Adapter."""

    model_name: str
    api_key: str = field(repr=False)
    api_base: str = field(repr=False)
    model_kwargs: dict[str, Any]
    temperature: float | None = 0.0
    structured_output: StructuredOutputMode = "json_schema"


def configured_lm_endpoint(
    settings: Any,
    *,
    model_name: str,
    thinking: bool = False,
    api_key: str | None = None,
    base_url: str | None = None,
    request_config: Any | None = None,
) -> ConfiguredLMEndpoint:
    """Resolve one direct endpoint without importing a model framework.

    The explicit override composes the semantic Program's fallback route. Retry, cache, token and deadline policy
    live behind the Program's Adapter Seam rather than in this application configuration helper.
    """

    endpoint_key = api_key if api_key is not None else settings.llm.api_key
    endpoint_url = base_url if base_url is not None else settings.llm.base_url
    effective_model = litellm_proxy_model_name(
        model_name,
        base_url=endpoint_url,
    )
    model_kwargs = _provider_model_kwargs(effective_model, thinking=thinking)
    temperature, structured_output = _provider_request_defaults(effective_model)
    # `LlmRequestConfig` is a required field of every endpoint model, so the envelope is always
    # there to read: an absent one was never a shape this configuration can have (#589 P-F15).
    request = request_config if request_config is not None else settings.llm.request
    if request.send_temperature is False:
        temperature = None
    elif request.send_temperature is True:
        temperature = float(request.temperature)
    if request.structured_output != "auto":
        structured_output = request.structured_output
    configured_extra = dict(request.extra_body)
    if configured_extra:
        provider_extra = dict(model_kwargs.get("extra_body") or {})
        model_kwargs["extra_body"] = {**provider_extra, **configured_extra}
    return ConfiguredLMEndpoint(
        model_name=effective_model,
        api_key=str(endpoint_key),
        api_base=str(endpoint_url),
        model_kwargs=model_kwargs,
        temperature=temperature,
        structured_output=structured_output,
    )


def llm_is_configured(settings: Any) -> bool:
    return bool(settings.llm.api_key and settings.llm.base_url)


def litellm_proxy_model_name(model_name: str, *, base_url: str) -> str:
    normalized = str(model_name or "").strip()
    if "/" in normalized or not str(base_url or "").strip():
        return normalized
    return f"openai/{normalized}"


def _provider_model_kwargs(model_name: str, *, thinking: bool = False) -> dict[str, Any]:
    leaf = str(model_name or "").rsplit("/", maxsplit=1)[-1].lower()
    if leaf.startswith("deepseek-v4-") and thinking:
        return {"extra_body": {"thinking": {"type": "enabled"}}}
    if leaf.startswith("deepseek-v4-"):
        # This gateway enables thinking by default, while function-calling
        # structured output requires tool_choice. This is an OpenAI-compatible extension,
        # so it must be placed in the HTTP request body via ``extra_body``;
        # a top-level LiteLLM kwarg is filtered before the proxy request.
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    if leaf.startswith("qwen") and leaf.endswith(":thinking"):
        return {}
    if leaf.startswith("qwen") and not thinking:
        # Qwen3 on llama.cpp / vLLM thinks by default and spends the whole ``max_tokens`` budget on reasoning
        # before the tool call; ``chat_template_kwargs`` is the OpenAI-compatible switch both servers honour.
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    return {}


def _provider_request_defaults(model_name: str) -> tuple[float | None, StructuredOutputMode]:
    leaf = str(model_name or "").rsplit("/", maxsplit=1)[-1].lower()
    if leaf.startswith("qwen") and leaf.endswith(":thinking"):
        return 0.0, "prompt_json"
    if leaf.startswith("deepseek"):
        return 0.0, "json_object"
    return 0.0, "json_schema"


__all__ = [
    "ConfiguredLMEndpoint",
    "StructuredOutputMode",
    "configured_lm_endpoint",
    "litellm_proxy_model_name",
    "llm_is_configured",
]
