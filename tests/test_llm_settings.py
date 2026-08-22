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
    assert models.reader_card_model == "deepseek-chat"
    assert models.reader_card_dedicated is False
    assert models.program_configured is True
    assert LlmConfig().news_triage_fallback.configured is False


def test_partial_reader_card_endpoint_fails_validation() -> None:
    with pytest.raises(ValidationError, match="llm_endpoint_configuration_incomplete"):
        LlmConfig(
            api_key="k",
            base_url="https://triage.test/v1",
            news_triage_model="triage-model",
            news_reader_card={"api_key": "reader-key", "base_url": "https://reader.test/v1"},
        )


def test_reader_card_endpoint_without_primary_fails_validation() -> None:
    with pytest.raises(ValidationError, match="llm_reader_card_without_primary"):
        LlmConfig(
            news_reader_card={
                "api_key": "reader-key",
                "base_url": "https://reader.test/v1",
                "model": "reader-model",
            }
        )


def test_availability_reports_dedicated_reader_card_endpoint() -> None:
    llm = LlmConfig(
        api_key="k",
        base_url="https://triage.test/v1",
        news_triage_model="triage-model",
        news_reader_card={
            "api_key": "reader-key",
            "base_url": "https://reader.test/v1/",
            "model": "reader-model",
        },
    )

    models = _availability(llm)

    assert models.triage_configured is True
    assert models.reader_card_model == "reader-model"
    assert models.reader_card_dedicated is True
    assert llm.news_reader_card.base_url == "https://reader.test/v1"


def test_invalid_dedicated_reader_endpoint_disables_the_whole_program() -> None:
    llm = LlmConfig(
        api_key="triage-key",
        base_url="https://triage.test/v1",
        news_triage_model="shared-model",
        news_reader_card={
            "api_key": "reader-secret",
            "base_url": "ftp://reader.test/v1",
            "model": "shared-model",
        },
    )

    models = _availability(llm)

    assert models.triage_configured is True
    assert models.reader_card_model is None
    assert models.program_configured is False
    assert "reader-secret" not in repr(llm.news_reader_card)
