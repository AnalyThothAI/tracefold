"""Every News Program endpoint is complete and fallback routing is all-or-none."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tracefold.platform.config.models import LlmConfig, news_model_availability


def _availability(llm: LlmConfig):
    return news_model_availability(SimpleNamespace(llm=llm))  # type: ignore[arg-type]


def test_partial_fallback_triple_fails_validation() -> None:
    with pytest.raises(ValidationError, match="llm_fallback_configuration_incomplete"):
        LlmConfig(
            api_key="k",
            base_url="http://192.168.0.2:8080/v1",
            news_triage_model="qwen3.8-27b",
            news_fallbacks=[{"api_key": "d", "base_url": "https://api.deepseek.com/v1"}],
        )


def test_fallback_without_primary_fails_validation() -> None:
    with pytest.raises(ValidationError, match="llm_fallback_without_primary"):
        LlmConfig(
            news_fallbacks=[{"api_key": "d", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}]
        )


def test_availability_reports_primary_and_fallback_models() -> None:
    llm = LlmConfig(
        api_key="k",
        base_url="http://192.168.0.2:8080/v1/",
        news_triage_model="qwen3.8-27b",
        news_fallbacks=[{"api_key": "d", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}],
    )
    models = _availability(llm)
    assert models.triage_configured and models.triage_model == "qwen3.8-27b"
    assert models.triage_fallback_models == ("deepseek-chat",)
    assert models.reader_card_fallback_models == ("deepseek-chat",)
    assert models.reader_card_fallback_dedicated == (False,)
    assert llm.base_url == "http://192.168.0.2:8080/v1"


def test_availability_preserves_the_operator_order_of_three_complete_fallback_routes() -> None:
    llm = LlmConfig(
        api_key="minimax-key",
        base_url="https://api.minimaxi.com/v1",
        news_triage_model="MiniMax-M3",
        news_fallbacks=[
            {
                "api_key": "minimax-key",
                "base_url": "https://api.minimaxi.com/v1",
                "model": "MiniMax-M2.7",
            },
            {
                "api_key": "deepseek-key",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-pro",
            },
            {
                "api_key": "deepseek-key",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-flash",
            },
        ],
    )

    models = _availability(llm)

    assert models.triage_fallback_models == (
        "MiniMax-M2.7",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    )
    assert models.reader_card_fallback_models == models.triage_fallback_models
    assert models.reader_card_fallback_dedicated == (False, False, False)


def test_availability_without_fallback_is_unchanged() -> None:
    llm = LlmConfig(api_key="k", base_url="https://api.deepseek.com/v1", news_triage_model="deepseek-chat")
    models = _availability(llm)
    assert models.triage_model == "deepseek-chat" and models.triage_fallback_models == ()
    assert models.reader_card_model == "deepseek-chat"
    assert models.reader_card_dedicated is False
    assert models.program_configured is True
    assert LlmConfig().news_fallbacks == ()


def test_the_compiler_tariff_key_is_gone_rather_than_ignored() -> None:
    """#202 §6.2 deletes the tariff with the metered proxy that reserved against it.

    `LlmConfig` forbids unknown keys, so an operator YAML still carrying the block fails to load with the
    key named — which is the intended migration signal, not a silently ignored setting. The offline
    optimizer charges an unpriced call at the operator's declared `--max-call-cost-microusd` instead.
    """

    with pytest.raises(ValidationError, match="news_compiler_tariff"):
        LlmConfig(news_compiler_tariff={"tariff_id": "provider-contract-2026-08"})


def test_reader_fallback_requires_the_event_fallback_route() -> None:
    with pytest.raises(ValidationError, match="llm_reader_card_fallback_without_event_fallback"):
        LlmConfig(
            api_key="k",
            base_url="https://triage.test/v1",
            news_triage_model="triage-model",
            news_fallbacks=[
                {
                    "reader_card": {
                        "api_key": "reader-key",
                        "base_url": "https://reader-fallback.test/v1",
                        "model": "reader-fallback-model",
                    }
                }
            ],
        )


def test_availability_reports_dedicated_reader_fallback_endpoint() -> None:
    llm = LlmConfig(
        api_key="k",
        base_url="https://triage.test/v1",
        news_triage_model="triage-model",
        news_fallbacks=[
            {
                "api_key": "event-fallback-key",
                "base_url": "https://event-fallback.test/v1",
                "model": "event-fallback-model",
                "reader_card": {
                    "api_key": "reader-fallback-key",
                    "base_url": "https://reader-fallback.test/v1",
                    "model": "reader-fallback-model",
                },
            }
        ],
    )

    models = _availability(llm)

    assert models.triage_fallback_models == ("event-fallback-model",)
    assert models.reader_card_fallback_models == ("reader-fallback-model",)
    assert models.reader_card_fallback_dedicated == (True,)


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
