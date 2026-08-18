from __future__ import annotations

from typing import Any

from langchain_litellm import ChatLiteLLM


def configured_chat_model(
    settings: Any,
    *,
    model_name: str,
    request_timeout_seconds: float,
    max_tokens: int,
    thinking: bool = False,
) -> tuple[ChatLiteLLM, str]:
    effective_model = litellm_proxy_model_name(
        model_name,
        base_url=settings.llm.base_url,
    )
    model_kwargs = _provider_model_kwargs(effective_model, thinking=thinking)
    return (
        ChatLiteLLM(
            model=effective_model,
            api_key=settings.llm.api_key,
            api_base=settings.llm.base_url,
            temperature=0,
            max_tokens=max_tokens,
            max_retries=0,
            request_timeout=request_timeout_seconds,
            model_kwargs=model_kwargs,
        ),
        effective_model,
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
    return {}


__all__ = [
    "configured_chat_model",
    "litellm_proxy_model_name",
    "llm_is_configured",
]
