from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from pydantic import BaseModel

from tracefold.app import learning_runtime
from tracefold.app.llm import configured_lm_endpoint
from tracefold.app.workers.wiring import news as workers
from tracefold.news.artifact_identity import canonical_sha, runtime_manifest_sha
from tracefold.news.learning.evaluate import ArmManifest, CandidateManifest, ProposalReceipt
from tracefold.news.program.artifact import load_stable_program_artifact
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
from tracefold.news.program.resources import candidates as candidate_programs
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.program.signatures import EventSemanticsSignature
from tracefold.news.release import runtime as release_runtime
from tracefold.news.release.canary import (
    CANARY_ELIGIBILITY_PROFILE_SHA,
    CANARY_ROLLING_PROFILE_SHA,
    CANARY_SELECTOR_VERSION,
)
from tracefold.news.triage_rules import DecidePolicy
from tracefold.platform.config.models import Settings


def test_deepseek_v4_disables_thinking_for_structured_tool_calls() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="test-key",
            base_url="https://models.test/v1",
        )
    )
    endpoint = configured_lm_endpoint(
        settings,
        model_name="openai/deepseek-v4-flash",
    )

    assert endpoint.model_name == "openai/deepseek-v4-flash"
    assert endpoint.model_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_non_deepseek_model_does_not_receive_provider_specific_thinking_flag() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="test-key",
            base_url="https://models.test/v1",
        )
    )
    endpoint = configured_lm_endpoint(
        settings,
        model_name="openai/gpt-5.4-mini",
    )

    assert "extra_body" not in endpoint.model_kwargs


def test_qwen_disables_thinking_via_chat_template_kwargs() -> None:
    settings = SimpleNamespace(llm=SimpleNamespace(api_key="test-key", base_url="https://models.test/v1"))
    endpoint = configured_lm_endpoint(settings, model_name="qwen3.8-27b")
    assert endpoint.model_name == "openai/qwen3.8-27b"
    assert endpoint.model_kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_minimax_m3_disables_thinking_for_structured_outputs() -> None:
    settings = SimpleNamespace(llm=SimpleNamespace(api_key="test-key", base_url="https://api.minimaxi.com/v1"))

    endpoint = configured_lm_endpoint(settings, model_name="MiniMax-M3")

    assert endpoint.model_name == "openai/MiniMax-M3"
    assert endpoint.model_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert endpoint.temperature == 1.0
    assert endpoint.structured_output == "prompt_json"
    assert endpoint.model_kwargs["top_p"] == 0.95


def test_minimax_m3_can_explicitly_keep_thinking_enabled() -> None:
    settings = SimpleNamespace(llm=SimpleNamespace(api_key="test-key", base_url="https://api.minimaxi.com/v1"))

    endpoint = configured_lm_endpoint(settings, model_name="MiniMax-M3", thinking=True)

    assert "extra_body" not in endpoint.model_kwargs


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
        (
            "MiniMax-M3",
            "https://minimax.test/v1",
            "prompt_json",
            "prompt",
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
    settings = SimpleNamespace(llm=SimpleNamespace(api_key="request-shape-secret", base_url=base_url))
    endpoint = configured_lm_endpoint(settings, model_name=model)
    valid_semantics = {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 1,
        "confidence": 0.8,
        "audience": "crypto",
        "relevance": {
            "impact_breadth": "single_instrument",
            "tradability": "direct",
            "surprise": "unscheduled",
            "development_delta": "state_change",
            "channels": ["exchange_access"],
            "affected_markets": ["single_asset"],
            "reader_value": "realtime",
        },
        "taxonomy": {
            "subject_codes": ["medtop:20001279"],
            "event_family": "market_access",
            "change_state": "announced",
            "assertion_status": "confirmed",
        },
    }
    delegate_kwargs: dict[str, Any] = {
        "api_key": endpoint.api_key,
        "api_base": endpoint.api_base,
        "timeout": 20.0,
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
        assert isinstance(request.config.response_format, type)
        assert issubclass(request.config.response_format, BaseModel)
    elif expected_format == "object":
        assert request.config.response_format == {"type": "json_object"}
    else:
        assert request.config.response_format is None
    projection = lm_request_projection(request)
    assert projection["config"]["extensions"]["extra_body"] == expected_extra
    visible_request = repr(projection["messages"])
    assert "Visible event_status.told index" in visible_request
    assert "request-shape-secret" not in repr(projection)
    assert base_url not in repr(projection)


def test_kimi_coding_endpoint_has_no_hidden_compatibility_profile() -> None:
    settings = SimpleNamespace(llm=SimpleNamespace(api_key="test-key", base_url="https://api.kimi.com/coding/v1"))

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
        api_key=endpoint.api_key,
        api_base=endpoint.api_base,
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
    settings = SimpleNamespace(llm=SimpleNamespace(api_key="local-key", base_url="http://192.168.0.2:8080/v1"))
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

    composition = learning_runtime.compose_news_program_runtime(settings)
    arm = learning_runtime.active_arm_manifest(settings, runtime_composition=composition)

    assert composition.program_configured is False
    assert composition.semantic_judge(load_stable_program_artifact()) is None
    assert composition.progression_verifier() is None
    with pytest.raises(ValueError, match="news_taxonomy_shadow_model_not_configured"):
        composition.taxonomy_shadow_program()
    assert composition.secret_free_slot_identities() == {
        "event_semantics.primary": None,
        "reader_card.primary": None,
        "event_semantics.fallback": None,
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

    composition = learning_runtime.compose_news_program_runtime(settings)

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
    composition = learning_runtime.compose_news_program_runtime(settings)
    artifact = load_stable_program_artifact()

    def scripted_factory(model: str, **kwargs: Any) -> ScriptedLM:
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


def test_news_runtime_composes_progression_review_from_the_event_model_endpoint() -> None:
    created: list[dict[str, Any]] = []

    def scripted_factory(model: str, **kwargs: Any) -> ScriptedLM:
        created.append({"model": model, **kwargs})
        return ScriptedLM(
            [{"review": {"related": False, "candidate_i": -1, "reason_zh": "没有同一事件链。"}}],
            model=model,
        )

    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "event-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "triage-model",
            }
        }
    )

    verifier = learning_runtime.compose_news_program_runtime(settings).progression_verifier(lm_type=scripted_factory)

    assert verifier is not None
    assert created[0]["model"] == "openai/triage-model"
    assert created[0]["max_tokens"] == 512
    assert created[0]["timeout"] == 12.0


def test_news_runtime_composes_bounded_taxonomy_shadow_from_the_event_model_endpoint() -> None:
    created: list[dict[str, Any]] = []

    def scripted_factory(model: str, **kwargs: Any) -> ScriptedLM:
        created.append({"model": model, **kwargs})
        return ScriptedLM([], model=model)

    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "event-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "triage-model",
            }
        }
    )

    program = learning_runtime.compose_news_program_runtime(settings).taxonomy_shadow_program(lm_type=scripted_factory)

    assert program.model_binding == "taxonomy-shadow-v2"
    assert len(program.shadow_program_sha256) == 64
    assert len(program.model_binding_sha256) == 64
    assert created == [
        {
            "model": "openai/triage-model",
            "api_key": "event-key",
            "api_base": "https://triage.test/v1",
            "timeout": 20.0,
            "max_tokens": 800,
            "cache": False,
            "num_retries": 0,
            "temperature": 0.0,
        }
    ]


def test_invalid_partial_news_program_configuration_keeps_the_empty_runtime_identity() -> None:
    pristine = learning_runtime.compose_news_program_runtime(Settings())
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

    partial = learning_runtime.compose_news_program_runtime(invalid_reader)

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

    composition = learning_runtime.compose_news_program_runtime(settings)
    arm = learning_runtime.active_arm_manifest(settings, runtime_composition=composition)
    slots = composition.secret_free_slot_identities()

    assert arm.runtime_model_bindings_sha256 == composition.runtime_model_bindings_sha256
    assert slots["event_semantics.primary"] == slots["reader_card.primary"]
    assert slots["event_semantics.fallback"] == slots["reader_card.fallback"]
    assert composition.slot_aliases() == {
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

    inherited_composition = learning_runtime.compose_news_program_runtime(inherited)
    dedicated_composition = learning_runtime.compose_news_program_runtime(dedicated)
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

    before = learning_runtime.compose_news_program_runtime(settings_with_key("key-before"))
    after = learning_runtime.compose_news_program_runtime(settings_with_key("key-after"))

    assert before.slot_aliases() == after.slot_aliases() == {}
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

    explicit_default_port = learning_runtime.compose_news_program_runtime(
        settings_with_endpoint("https://TRIAGE.TEST:443/v1/")
    )
    canonical = learning_runtime.compose_news_program_runtime(settings_with_endpoint("https://triage.test/v1"))

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

    composition = learning_runtime.compose_news_program_runtime(settings)
    slots = composition.secret_free_slot_identities()

    assert slots["event_semantics.fallback"] != slots["reader_card.fallback"]
    assert composition.slot_aliases() == {"reader_card.primary": "event_semantics.primary"}
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

    composition = learning_runtime.compose_news_program_runtime(settings)

    assert composition.program_configured is True
    assert composition.event_semantics_fallback is not None
    assert composition.reader_card_fallback is None
    assert composition.secret_free_slot_identities()["reader_card.fallback"] is None


def test_dedicated_reader_endpoint_produces_exact_two_model_trace() -> None:
    created: list[tuple[str, int, ScriptedLM]] = []
    semantics = {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [{"symbol": "BTC", "market_type": "spot", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 1,
        "confidence": 0.8,
        "audience": "crypto",
        "relevance": {
            "impact_breadth": "single_instrument",
            "tradability": "direct",
            "surprise": "unscheduled",
            "development_delta": "state_change",
            "channels": ["exchange_access"],
            "affected_markets": ["single_asset"],
            "reader_value": "realtime",
        },
        "taxonomy": {
            "subject_codes": ["medtop:20001279"],
            "event_family": "market_access",
            "change_state": "announced",
            "assertion_status": "confirmed",
        },
    }
    card = {"headline_zh": "比特币将在新交易所上线", "why_zh": "新增交易渠道可扩大现货流动性。"}

    def scripted_factory(model: str, **kwargs: Any) -> ScriptedLM:
        step: dict[str, Any] = {"card": card} if "reader-model" in model else {"semantics": semantics}
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
    artifact = load_stable_program_artifact()
    composition = learning_runtime.compose_news_program_runtime(settings)
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

    assert judgment.usage.physical_call_count == 2
    assert [(call.predictor, call.model) for call in judgment.trace.calls] == [
        ("event_semantics", "openai/triage-model"),
        ("reader_card", "openai/reader-model"),
    ]
    slots = composition.secret_free_slot_identities()
    event_identity = slots["event_semantics.primary"]
    reader_identity = slots["reader_card.primary"]
    assert event_identity is not None and reader_identity is not None
    assert [call.runtime_binding_sha256 for call in judgment.trace.calls] == [
        event_identity["binding_sha256"],
        reader_identity["binding_sha256"],
    ]
    assert [(model, cap) for model, cap, _adapter in created] == [
        ("openai/triage-model", artifact.event_semantics.max_tokens),
        ("openai/reader-model", artifact.reader_card.max_tokens),
    ]
    assert (
        composition.runtime_model_bindings_sha256
        == learning_runtime.active_arm_manifest(
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

    monkeypatch.setattr(release_runtime, "load_program_artifact", unexpected_load)

    with pytest.raises(ValueError, match="news_candidate_program_parent_mismatch"):
        release_runtime.candidate_program_artifact(candidate, stable, stable_artifact=stable_artifact)


class _StartupNewsRepository:
    def __init__(self, *, candidate_manifest_sha: str, candidate_bundle_sha: str) -> None:
        self.activation: dict[str, Any] = {
            "activation_id": "1" * 32,
            "candidate_manifest_sha": candidate_manifest_sha,
            "candidate_bundle_sha": candidate_bundle_sha,
            "selector_version": CANARY_SELECTOR_VERSION,
            "eligibility_profile_sha": CANARY_ELIGIBILITY_PROFILE_SHA,
            "rolling_profile_sha": CANARY_ROLLING_PROFILE_SHA,
            "state": "active",
        }

    def active_canary(self) -> dict[str, Any] | None:
        return dict(self.activation) if self.activation["state"] == "active" else None

    def canary_status(self) -> dict[str, Any]:
        return {
            "state": self.activation["state"],
            "activation": dict(self.activation),
            "assignments": {"stable": 0, "candidate": 0},
        }

    def transition_canary(
        self,
        *,
        activation_id: str,
        target_state: str,
        reason: str,
        now_ms: int,
    ) -> bool:
        assert activation_id == self.activation["activation_id"]
        self.activation.update(state=target_state, trip_reason=reason, tripped_at_ms=now_ms)
        return True


class _StartupRepositories:
    def __init__(self, news: _StartupNewsRepository) -> None:
        self.news = news

    @contextmanager
    def transaction(self) -> Any:
        yield


class _StartupLane:
    async def run_business(
        self,
        _name: str,
        function: Any,
        *,
        operation_timeout_seconds: float,
    ) -> Any:
        del operation_timeout_seconds
        return function()


class _StartupDatabase:
    def __init__(self, news: _StartupNewsRepository) -> None:
        self.repos = _StartupRepositories(news)
        self.lane = _StartupLane()

    def heavy_business(self) -> _StartupLane:
        return self.lane

    @contextmanager
    def worker_session(self, _name: str, _timeout_seconds: float) -> Any:
        yield self.repos

    async def run_news(
        self,
        _name: str,
        function: Any,
        /,
        *args: Any,
        operation_timeout_seconds: float,
        **kwargs: Any,
    ) -> Any:
        del operation_timeout_seconds
        return function(*args, **kwargs)


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
        failure_cluster_ids=("cluster-1",),
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
    monkeypatch.setattr(release_runtime, "load_stable_program_artifact", lambda: stable_artifact)

    def reject_artifact(_program_sha256: str) -> Any:
        raise ValueError("news_program_artifact_hash_mismatch")

    monkeypatch.setattr(release_runtime, "load_program_artifact", reject_artifact)

    assert (
        release_runtime.artifact_valid_candidate_bundles(
            stable_arm,
            {candidate.candidate_sha: candidate},
        )
        == {}
    )


def _wire_startup_test(
    monkeypatch: Any,
    *,
    candidate_manifest_sha: str,
    candidate_bundle_sha: str,
) -> tuple[Any, _StartupNewsRepository]:
    stable_arm = SimpleNamespace(
        bundle_sha="a" * 64,
        program_version=PROGRAM_VERSION,
        program_sha256="b" * 64,
        envelope_sha256=EXECUTION_ENVELOPE_SHA256,
    )
    stable_artifact = SimpleNamespace(program_sha256="b" * 64, schema_version="news_program_strategy_artifact_v1")
    stable_program = object()
    progression_verifier = object()
    news = _StartupNewsRepository(
        candidate_manifest_sha=candidate_manifest_sha,
        candidate_bundle_sha=candidate_bundle_sha,
    )
    database = _StartupDatabase(news)
    monkeypatch.setattr("tracefold.integrations.rabbitmq.RabbitMQBus", _StartupBus)
    composition = SimpleNamespace(
        semantic_judge=lambda _artifact: stable_program,
        progression_verifier=lambda: progression_verifier,
    )
    monkeypatch.setattr(workers, "compose_news_program_runtime", lambda _settings: composition)
    monkeypatch.setattr(workers, "active_arm_manifest", lambda _settings, **_kwargs: stable_arm)
    monkeypatch.setattr(workers, "load_stable_program_artifact", lambda: stable_artifact)
    identity_reads = 0

    def read_runtime_identity() -> SimpleNamespace:
        nonlocal identity_reads
        identity_reads += 1
        return SimpleNamespace(image_digest="image", runtime_revision="revision")

    monkeypatch.setattr(workers, "runtime_identity", read_runtime_identity)

    bus, pipeline = asyncio.run(
        workers._wire_news_pipeline(
            settings=_startup_settings(),
            db=database,
            finite=SimpleNamespace(),
        )
    )

    assert bus.connected is True
    # The attach must run the bounded settle before consuming: a one-shot read dies inside the
    # management statistics interval on every fresh broker volume (#400).
    from tracefold.integrations.rabbitmq import POLICY_EFFECTIVE_TIMEOUT_SECONDS

    assert bus.policies_verified is True
    assert bus.settle_timeout_seconds == POLICY_EFFECTIVE_TIMEOUT_SECONDS
    assert pipeline.triage.judge is stable_program
    assert pipeline.deliverer._progression_verifier is progression_verifier
    assert identity_reads == 1
    manifest = pipeline.triage.runtime_manifest
    assert manifest["image_digest"] == "image"
    assert manifest["runtime_revision"] == "revision"
    assert manifest["manifest_sha"] == runtime_manifest_sha(
        stable_bundle_sha=stable_arm.bundle_sha,
        candidate_shas=manifest["candidate_shas"],
        image_digest=manifest["image_digest"],
        runtime_revision=manifest["runtime_revision"],
    )
    return pipeline, news


def test_worker_startup_isolates_bad_candidate_schema_and_trips_active_activation(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        candidate_programs,
        "COMPILED_CANDIDATE_DOCUMENTS",
        ({"target": "program", "candidate_arm": {"program_sha256": "c" * 64}},),
    )

    pipeline, news = _wire_startup_test(
        monkeypatch,
        candidate_manifest_sha="d" * 64,
        candidate_bundle_sha="e" * 64,
    )

    assert pipeline.triage.canary_arms == {}
    assert news.activation["state"] == "tripped"
    assert news.activation["trip_reason"] == "candidate_manifest_missing_or_invalid"
    assert int(news.activation["tripped_at_ms"]) > 0


def test_worker_startup_isolates_bad_candidate_artifact_and_trips_active_activation(monkeypatch: Any) -> None:
    candidate = _program_candidate_document()
    monkeypatch.setattr(
        candidate_programs,
        "COMPILED_CANDIDATE_DOCUMENTS",
        (candidate.model_dump(mode="json"),),
    )

    def reject_artifact(_program_sha256: str) -> Any:
        raise ValueError("news_program_artifact_hash_mismatch")

    monkeypatch.setattr(release_runtime, "load_program_artifact", reject_artifact)

    pipeline, news = _wire_startup_test(
        monkeypatch,
        candidate_manifest_sha=candidate.candidate_sha,
        candidate_bundle_sha=candidate.candidate_arm.bundle_sha,
    )

    assert pipeline.triage.canary_arms == {}
    assert news.activation["state"] == "tripped"
    assert news.activation["trip_reason"] == "candidate_artifact_invalid"
    assert int(news.activation["tripped_at_ms"]) > 0
