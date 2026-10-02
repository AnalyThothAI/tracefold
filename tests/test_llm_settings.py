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
    assert models.configured and models.extraction_model == "qwen3.8-27b"
    assert models.extraction_fallback_model == "deepseek-chat"
    assert models.card_fallback_model == "deepseek-chat"
    assert models.card_fallback_dedicated is False
    assert llm.base_url == "http://192.168.0.2:8080/v1"


def test_availability_without_fallback_is_unchanged() -> None:
    llm = LlmConfig(api_key="k", base_url="https://api.deepseek.com/v1", news_triage_model="deepseek-chat")
    models = _availability(llm)
    assert models.extraction_model == "deepseek-chat" and models.extraction_fallback_model is None
    assert models.generated_judgment_model == "deepseek-chat"
    assert models.card_model == "deepseek-chat"
    assert models.card_dedicated is False
    assert models.configured is True
    assert LlmConfig().news_triage_fallback.configured is False


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

    assert models.extraction_fallback_model == "event-fallback-model"
    assert models.card_fallback_model == "reader-fallback-model"
    assert models.card_fallback_dedicated is True


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

    assert models.extraction_model is not None
    assert models.card_model == "reader-model"
    assert models.card_dedicated is True
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

    assert models.extraction_model is not None
    assert models.card_model is None
    assert models.configured is False
    assert "reader-secret" not in repr(llm.news_reader_card)


def test_news_judgment_is_all_or_none_and_validates_its_url() -> None:
    with pytest.raises(ValidationError, match="news_judgment_configuration_incomplete"):
        LlmConfig(news_judgment={"base_url": "https://openrouter.ai/api", "model": "jev-1.13"})
    with pytest.raises(ValidationError, match="news_judgment_base_url_invalid"):
        LlmConfig(news_judgment={"api_key": "k", "base_url": "openrouter.ai/api", "model": "jev-1.13"})
    configured = LlmConfig(
        news_judgment={"api_key": "k", "base_url": "https://api.typesafe.ai/", "model": "jev-1.13.0"}
    )
    assert configured.news_judgment.configured is True
    assert configured.news_judgment.base_url == "https://api.typesafe.ai"
    assert "k" not in repr(configured.news_judgment).replace("jev", "")
    assert LlmConfig().news_judgment.configured is False


def test_news_judgment_is_reported_and_retired_trading_semantics_is_rejected() -> None:
    route = {"api_key": "k", "base_url": "https://openrouter.ai/api", "model": "jev-1.13"}
    news = LlmConfig(api_key="k", base_url="https://api.deepseek.com/v1", news_triage_model="m", news_judgment=route)
    assert _availability(news).news_judgment_model == "jev-1.13"
    with pytest.raises(ValidationError, match="trading_semantics"):
        LlmConfig.model_validate({"trading_semantics": route})


@pytest.mark.parametrize(
    ("extra_body", "path"),
    [
        ({"access_token": "sk-abcdefghijklmnopqrstu"}, "extra_body.access_token"),
        ({"provider": {"Authorization": "Bearer sk-abcdefghijklmnopqrstu"}}, "extra_body.provider.Authorization"),
        ({"routes": [{"api-key": "sk-abcdefghijklmnopqrstu"}]}, "extra_body.routes[].api-key"),
        ({"client_secret": "sk-abcdefghijklmnopqrstu"}, "extra_body.client_secret"),
    ],
)
def test_a_credential_in_the_request_body_is_refused_without_echoing_it(extra_body: dict, path: str) -> None:
    with pytest.raises(ValidationError) as caught:
        LlmConfig.model_validate({"request": {"extra_body": extra_body}})

    assert f"llm_request_extra_body_secret:{path}" in str(caught.value)
    assert "sk-abcdefghijklmnopqrstu" not in str(caught.value)


def test_ordinary_provider_extensions_are_not_mistaken_for_credentials() -> None:
    config = LlmConfig.model_validate(
        {"request": {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20}}}
    )
    assert config.request.extra_body["chat_template_kwargs"] == {"enable_thinking": False}


def test_news_reader_judgment_takes_its_key_from_a_file_only_and_is_all_or_none() -> None:
    route = {"api_key_file": "news_reader_judgment_key", "base_url": "https://api.typesafe.ai/", "model": "jev-1.13"}
    with pytest.raises(ValidationError, match="news_reader_judgment_api_key_inline"):
        LlmConfig(news_reader_judgment={**route, "api_key": "inline-secret"})
    with pytest.raises(ValidationError, match="news_reader_judgment_configuration_incomplete"):
        LlmConfig(news_reader_judgment={"api_key_file": "k", "base_url": "https://api.typesafe.ai"})
    with pytest.raises(ValidationError, match="news_reader_judgment_base_url_invalid"):
        LlmConfig(news_reader_judgment={**route, "base_url": "api.typesafe.ai"})
    configured = LlmConfig(news_reader_judgment=route).news_reader_judgment
    assert configured.configured is True and configured.base_url == "https://api.typesafe.ai"
    assert LlmConfig().news_reader_judgment.configured is False


def test_news_reader_judgment_is_never_inferred_from_news_judgment() -> None:
    route = {"api_key": "k", "base_url": "https://openrouter.ai/api", "model": "jev-1.13"}
    llm = LlmConfig(news_judgment=route)
    assert llm.news_reader_judgment.configured is False
    with pytest.raises(ValidationError, match="trading_semantics"):
        LlmConfig.model_validate({"trading_semantics": route})


def test_news_embedding_owns_its_complete_route_and_rejects_the_retired_setting() -> None:
    route = {
        "api_key_file": "news_embedding_api_key",
        "base_url": "http://news-embedding:8080/v1/",
        "model": "tested-model",
        "max_batch_size": 2,
    }
    configured = LlmConfig(news_embedding=route).news_embedding
    assert configured.configured is True and configured.base_url == "http://news-embedding:8080/v1"
    assert configured.max_batch_size == 2
    assert (
        LlmConfig(
            api_key="generative-key", base_url="https://generative.test/v1", news_triage_model="generative-model"
        ).news_embedding.configured
        is False
    )
    for missing in ("api_key_file", "base_url", "model"):
        with pytest.raises(ValidationError, match="news_embedding_configuration_incomplete"):
            LlmConfig(news_embedding={key: value for key, value in route.items() if key != missing})
    with pytest.raises(ValidationError, match="news_embedding_api_key_inline") as caught:
        LlmConfig(news_embedding={**route, "api_key": "private-embedding-key"})
    assert "private-embedding-key" not in str(caught.value)
    with pytest.raises(ValidationError, match="news_embedding_base_url_invalid"):
        LlmConfig(news_embedding={**route, "base_url": "news-embedding:8080/v1"})
    for max_batch_size in (0, 33):
        with pytest.raises(ValidationError):
            LlmConfig(news_embedding={**route, "max_batch_size": max_batch_size})
    with pytest.raises(ValidationError, match="news_embedding_model"):
        LlmConfig.model_validate({"news_embedding_model": "retired-model"})


def test_the_judgment_model_names_a_model_on_the_extraction_endpoint() -> None:
    """#770: the semantic judgments may ask a deterministic variant of the same weights by its own name."""

    llm = LlmConfig(
        api_key="k",
        base_url="http://192.168.0.2:8080/v1",
        news_triage_model="qwen3.8-27b",
        news_triage_judgment_model="  qwen3.8-27b:judge ",
    )
    models = _availability(llm)
    assert models.extraction_model == "qwen3.8-27b"
    assert models.generated_judgment_model == "qwen3.8-27b:judge"
    assert models.card_model == "qwen3.8-27b"
    assert LlmConfig(news_triage_judgment_model="  ").news_triage_judgment_model is None


def test_a_judgment_model_without_the_primary_endpoint_fails_validation() -> None:
    with pytest.raises(ValidationError, match="llm_judgment_model_without_primary"):
        LlmConfig(news_triage_judgment_model="qwen3.8-27b:judge")
