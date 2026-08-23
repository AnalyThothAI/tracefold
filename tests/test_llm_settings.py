"""Every News Program endpoint is complete and fallback routing is all-or-none."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tracefold.platform.config.models import LlmCompilerTariffConfig, LlmConfig, news_model_availability


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
    assert models.reader_card_fallback_model == "deepseek-chat"
    assert models.reader_card_fallback_dedicated is False
    assert llm.base_url == "http://192.168.0.2:8080/v1"


def test_availability_without_fallback_is_unchanged() -> None:
    llm = LlmConfig(api_key="k", base_url="https://api.deepseek.com/v1", news_triage_model="deepseek-chat")
    models = _availability(llm)
    assert models.triage_model == "deepseek-chat" and models.triage_fallback_model is None
    assert models.reader_card_model == "deepseek-chat"
    assert models.reader_card_dedicated is False
    assert models.program_configured is True
    assert LlmConfig().news_triage_fallback.configured is False
    assert LlmConfig().news_compiler_tariff.configured is False


def test_compiler_tariff_is_complete_positive_and_secret_free() -> None:
    with pytest.raises(ValidationError, match="llm_news_compiler_tariff_incomplete"):
        LlmCompilerTariffConfig(tariff_id="provider-contract-2026-08")
    with pytest.raises(ValidationError):
        LlmCompilerTariffConfig(
            tariff_id="provider-contract-2026-08",
            input_token_overhead=1024,
            task_input_microusd_per_million=0,
            task_output_microusd_per_million=1,
            reflection_input_microusd_per_million=1,
            reflection_output_microusd_per_million=1,
            metric_judge_input_microusd_per_million=1,
            metric_judge_output_microusd_per_million=1,
        )

    tariff = LlmCompilerTariffConfig(
        tariff_id="provider-contract-2026-08",
        input_token_overhead=1024,
        task_input_microusd_per_million=300_000,
        task_output_microusd_per_million=1_200_000,
        reflection_input_microusd_per_million=300_000,
        reflection_output_microusd_per_million=1_200_000,
        metric_judge_input_microusd_per_million=400_000,
        metric_judge_output_microusd_per_million=1_600_000,
    )

    assert tariff.configured is True
    assert tariff.tariff_id == "provider-contract-2026-08"
    assert tariff.metric_judge_output_microusd_per_million == 1_600_000


def test_reader_fallback_requires_the_event_fallback_route() -> None:
    with pytest.raises(ValidationError, match="llm_reader_card_fallback_without_event_fallback"):
        LlmConfig(
            api_key="k",
            base_url="https://triage.test/v1",
            news_triage_model="triage-model",
            news_reader_card_fallback={
                "api_key": "reader-key",
                "base_url": "https://reader-fallback.test/v1",
                "model": "reader-fallback-model",
            },
        )


def test_availability_reports_dedicated_reader_fallback_endpoint() -> None:
    llm = LlmConfig(
        api_key="k",
        base_url="https://triage.test/v1",
        news_triage_model="triage-model",
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

    models = _availability(llm)

    assert models.triage_fallback_model == "event-fallback-model"
    assert models.reader_card_fallback_model == "reader-fallback-model"
    assert models.reader_card_fallback_dedicated is True


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
    rendered = repr(llm)
    assert "reader-key" not in rendered
    assert "reader.test" not in rendered
    assert "base_url" not in repr(llm.news_reader_card)


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
