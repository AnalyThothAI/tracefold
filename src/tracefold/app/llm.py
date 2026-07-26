from __future__ import annotations

from typing import Any

from langchain_litellm import ChatLiteLLM


def configured_chat_model(settings: Any, worker_settings: Any) -> tuple[ChatLiteLLM, str]:
    effective_model = litellm_proxy_model_name(
        worker_settings.model,
        base_url=settings.llm.base_url,
    )
    return (
        ChatLiteLLM(
            model=effective_model,
            api_key=settings.llm.api_key,
            api_base=settings.llm.base_url,
            temperature=0,
            max_tokens=worker_settings.max_tokens,
            max_retries=0,
            request_timeout=worker_settings.model_request_timeout_seconds,
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


__all__ = ["configured_chat_model", "litellm_proxy_model_name", "llm_is_configured"]
