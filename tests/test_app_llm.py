from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app import learning_runtime
from tracefold.app.llm import configured_lm_endpoint
from tracefold.app.workers.wiring import news as workers
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.evaluate import ArmManifest, CandidateManifest, ProposalReceipt
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.contracts import TriageContext
from tracefold.news.program.resources import candidates as candidate_programs
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.program.transport import ScriptedPredictorAdapter
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
    assert composition.secret_free_slot_identities() == {
        "event_semantics.primary": None,
        "reader_card.primary": None,
        "event_semantics.fallback": None,
        "reader_card.fallback": None,
    }
    assert composition.slot_aliases() == {}
    assert arm.runtime_model_bindings_sha256 == composition.runtime_model_bindings_sha256


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
    created: list[tuple[str, int, ScriptedPredictorAdapter]] = []
    semantics = {
        "novelty": "new_fact",
        "restates": -1,
        "event_type": "listing",
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
    }
    card = {"headline_zh": "比特币将在新交易所上线", "why_zh": "新增交易渠道可扩大现货流动性。"}

    class ScriptedFactory:
        @classmethod
        def from_runtime(cls, **kwargs: Any) -> ScriptedPredictorAdapter:
            model_name = str(kwargs["model_name"])
            step = card if "reader-model" in model_name else semantics
            adapter = ScriptedPredictorAdapter(
                [step],
                model_name=model_name,
                provider="openai",
                model_sha256=str(kwargs["model_sha256"]),
            )
            created.append((model_name, int(kwargs["max_tokens"]), adapter))
            return adapter

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
    judge = composition.semantic_judge(artifact, adapter_type=ScriptedFactory)
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

    stable = SimpleNamespace(program_sha256="a" * 64)
    candidate = SimpleNamespace(
        candidate_arm=SimpleNamespace(program_version=PROGRAM_VERSION, program_sha256="c" * 64),
        proposal_receipt=SimpleNamespace(program_parent_sha256="b" * 64, program_candidate_sha256="c" * 64),
    )

    def unexpected_load(_sha: str) -> Any:
        raise AssertionError("a mismatched parent must be refused before any artifact is loaded")

    monkeypatch.setattr(learning_runtime, "load_program_artifact", unexpected_load)

    with pytest.raises(ValueError, match="news_candidate_program_parent_mismatch"):
        learning_runtime.candidate_program_artifact(candidate, stable)


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
    def __init__(self, *, url: str, name_prefix: str, connect_timeout_seconds: float) -> None:
        del url, connect_timeout_seconds
        self.prefix = name_prefix
        self.connected = False

    async def connect(self) -> None:
        self.connected = True


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
    monkeypatch.setattr(learning_runtime, "load_stable_program_artifact", lambda: stable_artifact)

    def reject_artifact(_program_sha256: str) -> Any:
        raise ValueError("news_program_artifact_hash_mismatch")

    monkeypatch.setattr(learning_runtime, "load_program_artifact", reject_artifact)

    assert (
        learning_runtime.artifact_valid_candidate_bundles(
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
    )
    stable_artifact = SimpleNamespace(program_sha256="b" * 64)
    stable_program = object()
    news = _StartupNewsRepository(
        candidate_manifest_sha=candidate_manifest_sha,
        candidate_bundle_sha=candidate_bundle_sha,
    )
    database = _StartupDatabase(news)
    monkeypatch.setattr("tracefold.integrations.rabbitmq.RabbitMQBus", _StartupBus)
    composition = SimpleNamespace(semantic_judge=lambda _artifact: stable_program)
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
    assert pipeline.triage.judge is stable_program
    assert identity_reads == 1
    manifest = pipeline.triage.runtime_manifest
    assert manifest["image_digest"] == "image"
    assert manifest["runtime_revision"] == "revision"
    assert manifest["manifest_sha"] == learning_runtime.runtime_manifest_sha(
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

    monkeypatch.setattr(learning_runtime, "load_program_artifact", reject_artifact)

    pipeline, news = _wire_startup_test(
        monkeypatch,
        candidate_manifest_sha=candidate.candidate_sha,
        candidate_bundle_sha=candidate.candidate_arm.bundle_sha,
    )

    assert pipeline.triage.canary_arms == {}
    assert news.activation["state"] == "tripped"
    assert news.activation["trip_reason"] == "candidate_artifact_invalid"
    assert int(news.activation["tripped_at_ms"]) > 0
