from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ConfiguredLMEndpoint:
    """Provider-neutral runtime binding; the semantic Program owns the concrete LM Adapter."""

    model_name: str
    api_key: str = field(repr=False)
    api_base: str = field(repr=False)
    model_kwargs: dict[str, Any]


def configured_lm_endpoint(
    settings: Any,
    *,
    model_name: str,
    thinking: bool = False,
    api_key: str | None = None,
    base_url: str | None = None,
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
    return ConfiguredLMEndpoint(
        model_name=effective_model,
        api_key=str(endpoint_key),
        api_base=str(endpoint_url),
        model_kwargs=model_kwargs,
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
    if leaf.startswith("qwen") and not thinking:
        # Qwen3 on llama.cpp / vLLM thinks by default and spends the whole ``max_tokens`` budget on reasoning
        # before the tool call; ``chat_template_kwargs`` is the OpenAI-compatible switch both servers honour.
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    return {}


__all__ = [
    "ConfiguredLMEndpoint",
    "configured_lm_endpoint",
    "litellm_proxy_model_name",
    "llm_is_configured",
]
