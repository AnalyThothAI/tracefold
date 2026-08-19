"""LlmConfig: the primary triple and the Triage fallback endpoint (issue #65) are each all-or-nothing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tracefold.platform.config.settings import LlmConfig, news_model_availability


def _availability(llm: LlmConfig):
    return news_model_availability(SimpleNamespace(llm=llm))  # type: ignore[arg-type]


def test_partial_fallback_triple_fails_validation() -> None:
    with pytest.raises(ValidationError, match="llm_fallback_configuration_incomplete"):
        LlmConfig(
            api_key="k",
            base_url="http://192.168.0.2:8080/v1",
            news_triage_model="qwen3.8-27b",
            news_triage_fallback={"api_key": "d", "base_url": "https://api.deepseek.com/v1"},
        )


def test_fallback_without_primary_fails_validation() -> None:
    with pytest.raises(ValidationError, match="llm_fallback_without_primary"):
        LlmConfig(
            news_triage_fallback={"api_key": "d", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}
        )


def test_availability_reports_primary_and_fallback_models() -> None:
    llm = LlmConfig(
        api_key="k",
        base_url="http://192.168.0.2:8080/v1/",
        news_triage_model="qwen3.8-27b",
        news_triage_fallback={"api_key": "d", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    )
    models = _availability(llm)
    assert models.triage_configured and models.triage_model == "qwen3.8-27b"
    assert models.triage_fallback_model == "deepseek-chat"
    assert llm.base_url == "http://192.168.0.2:8080/v1"


def test_availability_without_fallback_is_unchanged() -> None:
    llm = LlmConfig(api_key="k", base_url="https://api.deepseek.com/v1", news_triage_model="deepseek-chat")
    models = _availability(llm)
    assert models.triage_model == "deepseek-chat" and models.triage_fallback_model is None
    assert LlmConfig().news_triage_fallback.configured is False
