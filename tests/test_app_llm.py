from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from tracefold.app import learning_runtime
from tracefold.app.cli.commands import news_learning_composition
from tracefold.app.llm import configured_lm_endpoint
from tracefold.app.workers.wiring import news as workers
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.evaluate import ArmManifest, CandidateManifest, ProposalReceipt
from tracefold.news.program.artifact import load_stable_program_state
from tracefold.news.program.contracts import TriageContext
from tracefold.news.program.identity import EXECUTION_ENVELOPE_SHA256
from tracefold.news.program.lm import (
    AuditedConfiguredLM,
    LMCallContext,
    LMCallLedger,
    RuntimeModelIdentity,
    ScriptedLM,
    lm_request_projection,
    program_json_adapter,
)
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.program.signatures import EventSemanticsSignature
from tracefold.news.release import runtime as release_runtime
from tracefold.news.triage_rules import DecidePolicy
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
    valid_semantics = {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [],
        "direction": "bullish",
        "scope": "single_name",
        "fact_kind": "state_change",
        "evidence_ref": "c1",
        "confidence": 0.8,
    }
    delegate_kwargs: dict[str, Any] = {
        "max_tokens": 2048,
        **endpoint.model_kwargs,
    }
    if endpoint.temperature is not None:
        delegate_kwargs["temperature"] = endpoint.temperature
    delegate = ScriptedLM(
        [{"semantics": valid_semantics}],
        model=endpoint.model_name,
        structured_output=endpoint.structured_output,
        **delegate_kwargs,
    )
    ledger = LMCallLedger()
    lm = AuditedConfiguredLM(
        delegate,
        structured_output=endpoint.structured_output,
        runtime_identity=RuntimeModelIdentity.issue(provider="openai", model=endpoint.model_name),
        predictor="event_semantics",
        route="primary",
        model_binding="primary",
        ledger=ledger,
    )

    with (
        ledger.scope(LMCallContext(PROGRAM_VERSION, "a" * 64, "b" * 64)),
        dspy.context(adapter=program_json_adapter()),
    ):
        prediction = dspy.Predict(EventSemanticsSignature)(evidence_json="{}", lm=lm)

    assert prediction.semantics.novelty == "new_fact"
    assert endpoint.structured_output == expected_mode
    assert len(delegate.requests) == 1
    request = delegate.requests[0]
    if expected_format == "schema":
        assert isinstance(request.config.response_format, dict)
        assert request.config.response_format["type"] == "json_schema"
        assert isinstance(request.config.response_format["schema"], dict)
    elif expected_format == "object":
        assert request.config.response_format == {"type": "json_object"}
    else:
        assert request.config.response_format is None
    projection = lm_request_projection(request)
    assert projection["config"]["extensions"]["extra_body"] == expected_extra
    visible_request = repr(projection["messages"])
    assert "evidence_json" in visible_request
    assert "request-shape-secret" not in repr(projection)
    assert base_url not in repr(projection)


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


def test_configured_endpoint_rejects_unreviewed_secret_bearing_extra_body_before_call() -> None:
    secret = "sk-abcdefghijklmnopqrstu"
    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "test-key",
                "base_url": "http://127.0.0.1:8080/v1",
                "news_triage_model": "my-local-model",
                "request": {"extra_body": {"access_token": secret}},
            }
        }
    )
    endpoint = configured_lm_endpoint(settings, model_name="my-local-model")
    delegate = ScriptedLM(
        [],
        model=endpoint.model_name,
        **endpoint.model_kwargs,
    )

    with pytest.raises(dspy.LMConfigurationError) as caught:
        AuditedConfiguredLM(
            delegate,
            structured_output=endpoint.structured_output,
            runtime_identity=RuntimeModelIdentity.issue(provider="openai", model=endpoint.model_name),
            predictor="event_semantics",
            route="primary",
            model_binding="event_semantics.primary",
        )

    assert secret not in str(caught.value)


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


def test_unconfigured_news_program_has_a_stable_empty_runtime_identity() -> None:
    """Deterministic News routes must boot even when the semantic Program is unavailable."""

    settings = Settings()

    composition = news_learning_composition.compose_news_program_runtime(settings)
    arm = news_learning_composition.active_arm_manifest(settings, runtime_composition=composition)

    assert composition.program_configured is False
    assert composition.semantic_judge(load_stable_program_state()) is None
    assert composition.secret_free_slot_identities() == {
        "event_semantics.primary": None,
        "taxonomy.primary": None,
        "reader_card.primary": None,
        "event_semantics.fallback": None,
        "taxonomy.fallback": None,
        "reader_card.fallback": None,
    }
    assert composition.slot_aliases() == {}
    assert arm.runtime_model_bindings_sha256 == composition.runtime_model_bindings_sha256


def test_news_runtime_composition_assigns_operator_request_controls_per_role() -> None:
    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "event-key",
                "base_url": "http://127.0.0.1:8080/v1",
                "news_triage_model": "event-model",
                "request": {"send_temperature": False, "structured_output": "prompt_json"},
                "news_reader_card": {
                    "api_key": "reader-key",
                    "base_url": "https://reader.test/v1",
                    "model": "reader-model",
                    "request": {"temperature": 0.4, "send_temperature": True},
                },
            }
        }
    )

    composition = news_learning_composition.compose_news_program_runtime(settings)

    assert composition.event_semantics_primary.temperature is None
    assert composition.event_semantics_primary.structured_output == "prompt_json"
    assert composition.reader_card_primary.temperature == 0.4
    assert composition.reader_card_primary.structured_output == "json_schema"


def test_compile_baseline_uses_native_module_without_production_availability_controls() -> None:
    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "event-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "event-model",
            }
        }
    )
    composition = news_learning_composition.compose_news_program_runtime(settings)
    artifact = load_stable_program_state()

    def scripted_factory(model: str, **kwargs: Any) -> ScriptedLM:
        for setting in ("api_key", "api_base", "timeout"):
            kwargs.pop(setting, None)
        return ScriptedLM([], model=model, **kwargs)

    compile_judge = composition.compile_semantic_judge(artifact, lm_type=scripted_factory)
    runtime_judge = composition.semantic_judge(artifact, lm_type=scripted_factory)

    assert compile_judge is not None
    assert compile_judge.route_deadline_seconds is None
    assert compile_judge.primary_breaker_enabled is False
    assert compile_judge.fallback is None
    assert runtime_judge is not None
    assert runtime_judge.route_deadline_seconds == 20
    assert runtime_judge.primary_breaker_enabled is True


def _compile_route_models(settings: Any) -> dict[str, str]:
    """Which model each Predictor's offline compile call would be sent to."""

    composition = news_learning_composition.compose_news_program_runtime(settings)

    def scripted_factory(model: str, **kwargs: Any) -> ScriptedLM:
        for setting in ("api_key", "api_base", "timeout"):
            kwargs.pop(setting, None)
        return ScriptedLM([], model=model, **kwargs)

    judge = composition.compile_semantic_judge(load_stable_program_state(), lm_type=scripted_factory)
    assert judge is not None
    return {
        "event_semantics": judge.primary.event_semantics.model,
        "taxonomy": judge.primary.taxonomy.model,
        "reader_card": judge.primary.reader_card.model,
    }


def test_compile_binds_each_predictor_to_its_own_production_primary_slot() -> None:
    """#651: offline compile answers on the endpoints production asks that Predictor on.

    With no dedicated ReaderCard endpoint every slot is the EventSemantics alias, which is what the old
    single-endpoint binding happened to produce. With one configured, ReaderCard moves and the other two
    do not — the case the old binding got wrong, because it optimized and scored card copy against a model
    production never asks to write it.
    """

    aliased = Settings.model_validate(
        {
            "llm": {
                "api_key": "event-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "event-model",
            }
        }
    )
    assert _compile_route_models(aliased) == {
        "event_semantics": "openai/event-model",
        "taxonomy": "openai/event-model",
        "reader_card": "openai/event-model",
    }

    dedicated = Settings.model_validate(
        {
            "llm": {
                "api_key": "event-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "event-model",
                "news_reader_card": {
                    "model": "card-model",
                    "api_key": "card-key",
                    "base_url": "https://card.test/v1",
                },
            }
        }
    )
    assert _compile_route_models(dedicated) == {
        "event_semantics": "openai/event-model",
        "taxonomy": "openai/event-model",
        "reader_card": "openai/card-model",
    }


def test_invalid_partial_news_program_configuration_keeps_the_empty_runtime_identity() -> None:
    pristine = news_learning_composition.compose_news_program_runtime(Settings())
    invalid_reader = Settings.model_validate(
        {
            "llm": {
                "api_key": "triage-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "triage-model",
                "news_reader_card": {
                    "api_key": "reader-key",
                    "base_url": "ftp://reader.test/v1",
                    "model": "reader-model",
                },
            }
        }
    )

    partial = news_learning_composition.compose_news_program_runtime(invalid_reader)

    assert partial.program_configured is False
    assert partial.secret_free_slot_identities() == pristine.secret_free_slot_identities()
    assert partial.slot_aliases() == pristine.slot_aliases() == {}
    assert partial.runtime_model_bindings_sha256 == pristine.runtime_model_bindings_sha256


def test_active_arm_uses_the_composed_secret_free_runtime_bindings() -> None:
    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "primary-key",
                "base_url": "https://primary.test/v1",
                "news_triage_model": "primary-model",
                "news_triage_fallback": {
                    "api_key": "fallback-key",
                    "base_url": "https://fallback.test/v1",
                    "model": "fallback-model",
                },
            }
        }
    )

    composition = news_learning_composition.compose_news_program_runtime(settings)
    arm = news_learning_composition.active_arm_manifest(settings, runtime_composition=composition)
    slots = composition.secret_free_slot_identities()

    assert arm.runtime_model_bindings_sha256 == composition.runtime_model_bindings_sha256
    assert slots["event_semantics.primary"] == slots["taxonomy.primary"] == slots["reader_card.primary"]
    assert slots["event_semantics.fallback"] == slots["taxonomy.fallback"] == slots["reader_card.fallback"]
    assert composition.slot_aliases() == {
        "taxonomy.primary": "event_semantics.primary",
        "taxonomy.fallback": "event_semantics.fallback",
        "reader_card.primary": "event_semantics.primary",
        "reader_card.fallback": "event_semantics.fallback",
    }
    assert "primary-key" not in repr(slots) and "fallback-key" not in repr(slots)
    assert "primary.test" not in repr(slots) and "fallback.test" not in repr(slots)


def test_different_reader_backend_changes_same_named_model_identity() -> None:
    inherited = Settings.model_validate(
        {
            "llm": {
                "api_key": "triage-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "shared-model",
            }
        }
    )
    dedicated = Settings.model_validate(
        {
            "llm": {
                "api_key": "triage-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "shared-model",
                "news_reader_card": {
                    "api_key": "reader-key",
                    "base_url": "https://reader.test/v1",
                    "model": "shared-model",
                },
            }
        }
    )

    inherited_composition = news_learning_composition.compose_news_program_runtime(inherited)
    dedicated_composition = news_learning_composition.compose_news_program_runtime(dedicated)
    inherited_slots = inherited_composition.secret_free_slot_identities()
    dedicated_slots = dedicated_composition.secret_free_slot_identities()

    assert inherited_slots["event_semantics.primary"] == dedicated_slots["event_semantics.primary"]
    assert inherited_slots["reader_card.primary"] != dedicated_slots["reader_card.primary"]
    assert inherited_composition.runtime_model_bindings_sha256 != dedicated_composition.runtime_model_bindings_sha256


def test_runtime_binding_identity_ignores_credential_rotation() -> None:
    def settings_with_key(key: str) -> Settings:
        return Settings.model_validate(
            {
                "llm": {
                    "api_key": key,
                    "base_url": "https://triage.test/v1",
                    "news_triage_model": "shared-model",
                    "news_reader_card": {
                        "api_key": f"reader-{key}",
                        "base_url": "https://reader.test/v1",
                        "model": "shared-model",
                    },
                }
            }
        )

    before = news_learning_composition.compose_news_program_runtime(settings_with_key("key-before"))
    after = news_learning_composition.compose_news_program_runtime(settings_with_key("key-after"))

    assert before.slot_aliases() == after.slot_aliases() == {"taxonomy.primary": "event_semantics.primary"}
    assert before.secret_free_slot_identities() == after.secret_free_slot_identities()
    assert before.runtime_model_bindings_sha256 == after.runtime_model_bindings_sha256


def test_runtime_binding_identity_canonicalizes_equivalent_endpoint_urls() -> None:
    def settings_with_endpoint(base_url: str) -> Settings:
        return Settings.model_validate(
            {
                "llm": {
                    "api_key": "same-key",
                    "base_url": base_url,
                    "news_triage_model": "shared-model",
                }
            }
        )

    explicit_default_port = news_learning_composition.compose_news_program_runtime(
        settings_with_endpoint("https://TRIAGE.TEST:443/v1/")
    )
    canonical = news_learning_composition.compose_news_program_runtime(settings_with_endpoint("https://triage.test/v1"))

    assert explicit_default_port.secret_free_slot_identities() == canonical.secret_free_slot_identities()
    assert explicit_default_port.runtime_model_bindings_sha256 == canonical.runtime_model_bindings_sha256


def test_dedicated_reader_fallback_has_its_own_explicit_slot_identity() -> None:
    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "triage-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "triage-model",
                "news_triage_fallback": {
                    "api_key": "event-fallback-key",
                    "base_url": "https://event-fallback.test/v1",
                    "model": "event-fallback-model",
                },
                "news_reader_card_fallback": {
                    "api_key": "reader-fallback-key",
                    "base_url": "https://reader-fallback.test/v1",
                    "model": "reader-fallback-model",
                },
            }
        }
    )

    composition = news_learning_composition.compose_news_program_runtime(settings)
    slots = composition.secret_free_slot_identities()

    assert slots["event_semantics.fallback"] != slots["reader_card.fallback"]
    assert composition.slot_aliases() == {
        "taxonomy.primary": "event_semantics.primary",
        "taxonomy.fallback": "event_semantics.fallback",
        "reader_card.primary": "event_semantics.primary",
    }
    rendered = repr(slots)
    assert "event-fallback.test" not in rendered
    assert "reader-fallback.test" not in rendered
    assert "event-fallback-key" not in rendered
    assert "reader-fallback-key" not in rendered


def test_invalid_requested_reader_fallback_disables_the_whole_fallback_route() -> None:
    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "triage-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "triage-model",
                "news_triage_fallback": {
                    "api_key": "event-fallback-key",
                    "base_url": "https://event-fallback.test/v1",
                    "model": "event-fallback-model",
                },
                "news_reader_card_fallback": {
                    "api_key": "reader-fallback-key",
                    "base_url": "ftp://reader-fallback.test/v1",
                    "model": "reader-fallback-model",
                },
            }
        }
    )

    composition = news_learning_composition.compose_news_program_runtime(settings)

    assert composition.program_configured is True
    assert composition.event_semantics_fallback is not None
    assert composition.reader_card_fallback is None
    assert composition.secret_free_slot_identities()["reader_card.fallback"] is None


def test_dedicated_reader_endpoint_produces_exact_three_model_trace() -> None:
    created: list[tuple[str, int, ScriptedLM]] = []
    artifact = load_stable_program_state()
    semantics = {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [{"symbol": "BTC", "market_type": "spot", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "fact_kind": "state_change",
        "evidence_ref": "c1",
        "confidence": 0.8,
    }
    taxonomy = {
        "subject_codes": ["medtop:20001279"],
        "event_family": "market_access",
        "change_state": "announced",
        "assertion_status": "confirmed",
    }
    card = {"headline_zh": "比特币将在新交易所上线", "why_zh": "新增交易渠道可扩大现货流动性。"}

    def scripted_factory(model: str, **kwargs: Any) -> ScriptedLM:
        # The taxonomy slot is an alias of the triage endpoint, so the same model name is created twice
        # with two ceilings; the ceiling tells the two apart.
        if "reader-model" in model:
            step: dict[str, Any] = {"card": card}
        elif int(kwargs["max_tokens"]) == artifact.taxonomy.max_tokens:
            step = {"taxonomy": taxonomy}
        else:
            step = {"semantics": semantics}
        lm = ScriptedLM([step], model=model)
        created.append((model, int(kwargs["max_tokens"]), lm))
        return lm

    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "triage-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "triage-model",
                "news_reader_card": {
                    "api_key": "reader-key",
                    "base_url": "https://reader.test/v1",
                    "model": "reader-model",
                },
            }
        }
    )
    artifact = load_stable_program_state()
    composition = news_learning_composition.compose_news_program_runtime(settings)
    judge = composition.semantic_judge(artifact, lm_type=scripted_factory)
    assert judge is not None
    context = TriageContext.from_card(
        {
            "event_id": "event-1",
            "evidence_version": 1,
            "evidence_sha256": "e" * 64,
            "focus_fact_id": "fact-1",
            "leader_title": "BTC listed on Example Exchange",
            "raw_first_line": "$BTC listing",
            "leader_description": "Trading starts tomorrow.",
            "opened_at_ms": 1_000_000,
            "grounded_assets": ["BTC"],
            "asset_class": "crypto",
            "storyline_key": "asset:BTC",
        },
        watchlist=(),
        told_rows=(),
        now_ms=1_010_000,
        queue_lag_ms=0,
    )

    judgment = asyncio.run(judge.judge(context))

    assert judgment.usage.physical_call_count == 3
    assert [(call.predictor, call.model) for call in judgment.trace.calls] == [
        ("event_semantics", "openai/triage-model"),
        ("taxonomy", "openai/triage-model"),
        ("reader_card", "openai/reader-model"),
    ]
    slots = composition.secret_free_slot_identities()
    event_identity = slots["event_semantics.primary"]
    taxonomy_identity = slots["taxonomy.primary"]
    reader_identity = slots["reader_card.primary"]
    assert event_identity is not None and taxonomy_identity is not None and reader_identity is not None
    assert [call.runtime_binding_sha256 for call in judgment.trace.calls] == [
        event_identity["binding_sha256"],
        taxonomy_identity["binding_sha256"],
        reader_identity["binding_sha256"],
    ]
    assert [(model, cap) for model, cap, _adapter in created] == [
        ("openai/triage-model", artifact.event_semantics.max_tokens),
        ("openai/triage-model", artifact.taxonomy.max_tokens),
        ("openai/reader-model", artifact.reader_card.max_tokens),
    ]
    assert (
        composition.runtime_model_bindings_sha256
        == news_learning_composition.active_arm_manifest(
            settings,
            runtime_composition=composition,
        ).runtime_model_bindings_sha256
    )


def test_a_candidate_whose_parent_is_not_the_running_stable_never_resolves_an_artifact(monkeypatch: Any) -> None:
    """#202 §1.3 removed the policy candidate, and with it the branch that reused the stable artifact.

    What is left is one rule: a candidate resolves to the image-carried artifact its own receipt says
    descends from the running stable, or it resolves to nothing. A mismatch must be refused before the
    artifact is loaded, not after — loading is what an unverified lineage would smuggle behavior through.
    """

    stable = SimpleNamespace(
        bundle_sha="d" * 64,
        program_version=PROGRAM_VERSION,
        program_sha256="a" * 64,
    )
    stable_artifact = SimpleNamespace(program_sha256=stable.program_sha256)
    candidate = SimpleNamespace(
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=SimpleNamespace(program_version=PROGRAM_VERSION, program_sha256="c" * 64),
        proposal_receipt=SimpleNamespace(program_parent_sha256="b" * 64, program_candidate_sha256="c" * 64),
    )

    def unexpected_load(_sha: str) -> Any:
        raise AssertionError("a mismatched parent must be refused before any artifact is loaded")

    monkeypatch.setattr(release_runtime, "load_program_state", unexpected_load)

    with pytest.raises(ValueError, match="news_candidate_program_parent_mismatch"):
        release_runtime.candidate_program_artifact(candidate, stable, stable_artifact=stable_artifact)


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


def _program_candidate_document() -> CandidateManifest:
    """One image-carried Program candidate whose lineage lives on its own proposal receipt.

    `program_version` is code-owned now, so a candidate that names anything but the running
    `PROGRAM_VERSION` is rejected before its artifact is ever looked up — which would hide the
    artifact-rejection behavior these tests exist to prove.
    """

    policy = DecidePolicy().as_dict()
    candidate_arm = ArmManifest(
        program_version=PROGRAM_VERSION,
        program_sha256="c" * 64,
        envelope_sha256=EXECUTION_ENVELOPE_SHA256,
        runtime_model_bindings_sha256="d" * 64,
        retrieval_sha256="e" * 64,
        policy=policy,
        policy_sha256=canonical_sha(policy),
    )
    receipt = ProposalReceipt.issue(
        development_dataset_sha="f" * 64,
        optimizer_cluster_ids=("cluster-1",),
        generator_kind="human",
        registered_at_ms=1,
        declared_target_dimensions=("why_support",),
        development_episode_projection_root_sha256="e" * 64,
        program_parent_sha256="b" * 64,
        program_candidate_sha256=candidate_arm.program_sha256,
        prompt_candidate_sha256="1" * 64,
    )
    return CandidateManifest(
        parent_stable_sha="a" * 64,
        candidate_arm=candidate_arm,
        hypothesis="Test an image-carried child Program.",
        target_dimensions=("why_support",),
        development_dataset_sha="f" * 64,
        proposal_receipt=receipt,
    )


def test_canary_control_excludes_a_manifest_whose_program_artifact_cannot_load(monkeypatch: Any) -> None:
    candidate = _program_candidate_document()
    stable_arm = SimpleNamespace(
        bundle_sha=candidate.parent_stable_sha,
        program_version=PROGRAM_VERSION,
        program_sha256="b" * 64,
    )
    stable_artifact = SimpleNamespace(program_sha256=stable_arm.program_sha256)
    monkeypatch.setattr(release_runtime, "load_stable_program_state", lambda: stable_artifact)

    def reject_artifact(_program_sha256: str) -> Any:
        raise ValueError("news_program_artifact_hash_mismatch")

    monkeypatch.setattr(release_runtime, "load_program_state", reject_artifact)

    assert (
        release_runtime.artifact_valid_candidate_bundles(
            stable_arm,
            {candidate.candidate_sha: candidate},
        )
        == {}
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
