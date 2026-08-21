from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from tracefold.app import learning_runtime, workers
from tracefold.app.llm import ConfiguredLMEndpoint, configured_lm_endpoint
from tracefold.news import DecidePolicy, canonical_sha
from tracefold.news.agents.programs import candidates as candidate_programs
from tracefold.news.agents.semantic_program import RuntimeModelIdentity
from tracefold.news.candidate_evaluator import ArmManifest, CandidateManifest, ProposalReceipt
from tracefold.platform.config.settings import Settings


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


def test_active_arm_hashes_the_exact_secret_free_runtime_bindings(monkeypatch: Any) -> None:
    artifact = SimpleNamespace(program_version="program-v1", program_sha256="a" * 64)
    availability = SimpleNamespace(
        triage_model="primary-model",
        triage_fallback_model="fallback-model",
    )
    monkeypatch.setattr(learning_runtime, "load_stable_program_artifact", lambda: artifact)
    monkeypatch.setattr(learning_runtime, "news_model_availability", lambda _settings: availability)
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="primary-key",
            base_url="https://primary.test/v1",
            news_triage_model="primary-model",
            news_triage_fallback=SimpleNamespace(
                api_key="fallback-key",
                base_url="https://fallback.test/v1",
            ),
        ),
        news=SimpleNamespace(policy=SimpleNamespace(model_dump=lambda **_kwargs: {"similarity_max": 0.6})),
    )

    arm = learning_runtime.active_arm_manifest(settings)

    primary = RuntimeModelIdentity.issue(provider="openai", model="openai/primary-model").model_dump(mode="json")
    fallback = RuntimeModelIdentity.issue(provider="openai", model="openai/fallback-model").model_dump(mode="json")
    assert arm.runtime_model_bindings_sha256 == canonical_sha(
        {
            "identity_schema": "configured_runtime_binding_v1",
            "event_semantics.primary": primary,
            "reader_card.primary": primary,
            "event_semantics.fallback": fallback,
            "reader_card.fallback": fallback,
        }
    )


def test_worker_composes_an_arm_local_program_with_primary_and_fallback_adapters(monkeypatch: Any) -> None:
    configured: list[dict[str, Any]] = []

    def fake_configured_endpoint(_settings: Any, **kwargs: Any) -> ConfiguredLMEndpoint:
        configured.append(kwargs)
        return ConfiguredLMEndpoint(
            model_name=f"effective:{kwargs['model_name']}",
            api_key=str(kwargs.get("api_key") or "primary-key"),
            api_base=str(kwargs.get("base_url") or "https://primary.test/v1"),
            model_kwargs={},
        )

    class FakeAdapter:
        @classmethod
        def from_runtime(cls, **kwargs: Any) -> Any:
            instance = cls()
            instance.runtime = kwargs
            return instance

    class FakeProgram:
        def __init__(self, artifact: object, *, primary_adapter: Any, fallback_adapter: Any) -> None:
            self.artifact = artifact
            self.primary_adapter = primary_adapter
            self.fallback_adapter = fallback_adapter

    monkeypatch.setattr(workers, "configured_lm_endpoint", fake_configured_endpoint)
    monkeypatch.setattr(workers, "DspyPredictorAdapter", FakeAdapter)
    monkeypatch.setattr(workers, "DspyNewsSemanticProgram", FakeProgram)
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            news_triage_fallback=SimpleNamespace(api_key="fallback-key", base_url="https://fallback.test/v1")
        ),
    )
    models = SimpleNamespace(
        triage_configured=True,
        triage_model="primary-model",
        triage_fallback_model="fallback-model",
    )
    artifact = SimpleNamespace(
        execution=SimpleNamespace(route_deadline_seconds=18),
        event_semantics=SimpleNamespace(max_tokens=600),
        reader_card=SimpleNamespace(max_tokens=720),
    )

    program = workers._configured_semantic_program(settings, artifact, models)

    assert isinstance(program, FakeProgram)
    assert program.artifact is artifact
    assert program.primary_adapter.runtime == {
        "model_name": "effective:primary-model",
        "api_key": "primary-key",
        "api_base": "https://primary.test/v1",
        "timeout": 18.0,
        "max_tokens": 720,
        "model_kwargs": {},
    }
    assert program.fallback_adapter.runtime == {
        "model_name": "effective:fallback-model",
        "api_key": "fallback-key",
        "api_base": "https://fallback.test/v1",
        "timeout": 18.0,
        "max_tokens": 720,
        "model_kwargs": {},
    }
    assert configured == [
        {
            "model_name": "primary-model",
        },
        {
            "model_name": "fallback-model",
            "api_key": "fallback-key",
            "base_url": "https://fallback.test/v1",
        },
    ]


def test_policy_canary_reuses_stable_artifact_without_becoming_a_program_candidate(monkeypatch: Any) -> None:
    stable = SimpleNamespace(program_version="program-v1", program_sha256="a" * 64)
    candidate = SimpleNamespace(
        target="policy",
        candidate_arm=SimpleNamespace(program_version="program-v1", program_sha256="a" * 64),
    )

    def unexpected_load(_sha: str) -> Any:
        raise AssertionError("policy candidate must not load a child artifact")

    monkeypatch.setattr(learning_runtime, "load_program_artifact", unexpected_load)

    assert learning_runtime.candidate_program_artifact(candidate, stable) is stable


class _StartupNewsRepository:
    def __init__(self, *, candidate_manifest_sha: str, candidate_bundle_sha: str) -> None:
        self.activation: dict[str, Any] = {
            "activation_id": "1" * 32,
            "candidate_manifest_sha": candidate_manifest_sha,
            "candidate_bundle_sha": candidate_bundle_sha,
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
    policy = DecidePolicy().as_dict()
    candidate_arm = ArmManifest(
        program_version="program-v2",
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
        candidate_patch_sha="1" * 64,
        declared_target_dimensions=("why_support",),
        program_parent_sha256="b" * 64,
        program_candidate_sha256=candidate_arm.program_sha256,
        program_machine_diff={"reader_card": {"instruction": "changed"}},
        compile_provenance={"mode": "test"},
    )
    return CandidateManifest(
        target="program",
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
        program_version="program-v1",
        program_sha256="b" * 64,
    )
    stable_artifact = SimpleNamespace(
        program_version=stable_arm.program_version,
        program_sha256=stable_arm.program_sha256,
    )
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
        program_version="program-v1",
        program_sha256="b" * 64,
    )
    stable_artifact = SimpleNamespace(program_version="program-v1", program_sha256="b" * 64)
    stable_program = object()
    news = _StartupNewsRepository(
        candidate_manifest_sha=candidate_manifest_sha,
        candidate_bundle_sha=candidate_bundle_sha,
    )
    database = _StartupDatabase(news)
    monkeypatch.setattr("tracefold.integrations.rabbitmq.RabbitMQBus", _StartupBus)
    monkeypatch.setattr(workers, "active_arm_manifest", lambda _settings: stable_arm)
    monkeypatch.setattr(workers, "load_stable_program_artifact", lambda: stable_artifact)
    monkeypatch.setattr(workers, "_configured_semantic_program", lambda *_args: stable_program)
    monkeypatch.setattr(
        workers,
        "runtime_identity",
        lambda: SimpleNamespace(image_digest="image", runtime_revision="revision"),
    )

    bus, pipeline = asyncio.run(
        workers._wire_news_pipeline(
            settings=_startup_settings(),
            db=database,
            finite=SimpleNamespace(),
        )
    )

    assert bus.connected is True
    assert pipeline.triage.judge is stable_program
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
