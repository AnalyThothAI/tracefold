from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import dspy
import pytest

from tracefold.news.learning.compiler.proxy import (
    CompilerModelProxyGrant,
    CompilerProviderEndpointSecret,
    CompilerProxyError,
    CompilerProxyLM,
    CompilerProxyRequest,
    CompilerProxySecretConfig,
    CompilerProxyTariff,
    TrustedCompilerModelProxy,
)
from tracefold.news.learning.compiler.security import CompilerEndpointIdentity, CompilerRoleBindingV3
from tracefold.news.learning.judge import CardEquivalenceJudge
from tracefold.news.program.graph import (
    DspyCompileProgram,
    DspyStrictJSONAdapter,
    ExactProviderCallCapture,
    ExactProviderMetadata,
    load_stable_program_artifact,
)


class _ExactFakeLM:
    cache = False
    num_retries = 0

    def __init__(self, identity: CompilerEndpointIdentity, *, cost_microusd: int) -> None:
        self.tracefold_compiler_endpoint_identity = identity
        self.cost_microusd = cost_microusd
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @contextmanager
    def observe_exact_call(self) -> Iterator[ExactProviderCallCapture]:
        capture = ExactProviderCallCapture()
        self._capture = capture
        try:
            yield capture
        finally:
            del self._capture

    def __call__(self, *args: Any, **kwargs: Any) -> list[str]:
        self.calls.append((args, kwargs))
        self._capture.record_metadata(
            ExactProviderMetadata(
                response_model=self.tracefold_compiler_endpoint_identity.model,
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                provider_cost_microusd=self.cost_microusd,
                finish_reason="stop",
            )
        )
        return ["proxy-output"]


@contextmanager
def _short_socket_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="tf-proxy-", dir="/tmp") as root:
        yield Path(root) / "p.sock"


def _grant(*, max_calls: int = 4, max_cost: int = 100, max_request_bytes: int = 1_024) -> CompilerModelProxyGrant:
    tariff = _tariff()
    task_endpoint = CompilerEndpointIdentity.issue(
        model="provider/task-model",
        api_base="https://task.internal.example/v1",
    )
    reflection_endpoint = CompilerEndpointIdentity.issue(
        model="provider/reflection-model",
        api_base="https://reflection.internal.example/v1",
    )
    judge_endpoint = CompilerEndpointIdentity.issue(
        model="provider/judge-model",
        api_base="https://judge.internal.example/v1",
    )
    return CompilerModelProxyGrant.issue(
        task=CompilerRoleBindingV3.issue(
            role="task",
            endpoint=task_endpoint,
            max_output_tokens=512,
            timeout_seconds=20,
            temperature=0,
            model_kwargs={},
        ),
        reflection=CompilerRoleBindingV3.issue(
            role="reflection",
            endpoint=reflection_endpoint,
            max_output_tokens=32_000,
            timeout_seconds=300,
            temperature=1,
            model_kwargs={},
        ),
        metric_judge=CompilerRoleBindingV3.issue(
            role="metric_judge",
            endpoint=judge_endpoint,
            max_output_tokens=4_096,
            timeout_seconds=120,
            temperature=0,
            model_kwargs={},
        ),
        max_task_model_calls=max_calls,
        max_reflection_model_calls=max_calls,
        max_metric_judge_model_calls=max_calls,
        max_cost_microusd=max_cost,
        tariff=tariff,
        proxy_config_sha256="e" * 64,
        proxy_source_sha256="d" * 64,
        max_request_bytes=max_request_bytes,
    )


def _tariff() -> CompilerProxyTariff:
    return CompilerProxyTariff(
        tariff_id="trusted-tariff-v1",
        input_token_overhead=64,
        task_input_microusd_per_million=1,
        task_output_microusd_per_million=20_000,
        reflection_input_microusd_per_million=1,
        reflection_output_microusd_per_million=300,
        metric_judge_input_microusd_per_million=2,
        metric_judge_output_microusd_per_million=3_200,
    )


def test_reflection_endpoint_accepts_the_code_owned_32k_output_ceiling() -> None:
    endpoint = CompilerProviderEndpointSecret(
        model="provider/reflection-model",
        api_base="https://reflection.internal.example/v1",
        api_key="private-key",
        timeout=300,
        max_tokens=32_000,
        temperature=1,
    )

    assert endpoint.max_tokens == 32_000


def test_proxy_accounts_task_reflection_and_metric_judge_as_three_sealed_roles() -> None:
    task_endpoint = CompilerEndpointIdentity.issue(
        model="provider/task-model", api_base="https://task.internal.example/v1"
    )
    reflection_endpoint = CompilerEndpointIdentity.issue(
        model="provider/reflection-model", api_base="https://reflection.internal.example/v1"
    )
    judge_endpoint = CompilerEndpointIdentity.issue(
        model="provider/judge-model", api_base="https://judge.internal.example/v1"
    )
    task = CompilerRoleBindingV3.issue(
        role="task",
        endpoint=task_endpoint,
        max_output_tokens=1_200,
        timeout_seconds=20,
        temperature=0,
        model_kwargs={},
    )
    reflection = CompilerRoleBindingV3.issue(
        role="reflection",
        endpoint=reflection_endpoint,
        max_output_tokens=32_000,
        timeout_seconds=300,
        temperature=1,
        model_kwargs={},
    )
    metric_judge = CompilerRoleBindingV3.issue(
        role="metric_judge",
        endpoint=judge_endpoint,
        max_output_tokens=4_096,
        timeout_seconds=120,
        temperature=0,
        model_kwargs={"extra_body": {"thinking": {"type": "disabled"}}},
    )
    grant = CompilerModelProxyGrant.issue(
        task=task,
        reflection=reflection,
        metric_judge=metric_judge,
        max_task_model_calls=2,
        max_reflection_model_calls=2,
        max_metric_judge_model_calls=2,
        max_cost_microusd=1_000,
        tariff=_tariff(),
        proxy_config_sha256="e" * 64,
        proxy_source_sha256="d" * 64,
        max_request_bytes=1_024,
    )
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=_ExactFakeLM(task_endpoint, cost_microusd=7),
        reflection_lm=_ExactFakeLM(reflection_endpoint, cost_microusd=11),
        metric_judge_lm=_ExactFakeLM(judge_endpoint, cost_microusd=13),
    )

    with _short_socket_path() as path, proxy.serve(path) as socket_path:
        for role in ("task", "reflection", "metric_judge"):
            client = CompilerProxyLM(socket_path=socket_path, grant=grant, role=role)
            assert client(prompt=f"{role} request") == ["proxy-output"]

    receipt = proxy.execution_receipt()
    assert (receipt.task_model_calls, receipt.reflection_model_calls, receipt.metric_judge_model_calls) == (1, 1, 1)
    assert (receipt.task_cost_microusd, receipt.reflection_cost_microusd, receipt.metric_judge_cost_microusd) == (
        7,
        11,
        13,
    )
    assert receipt.actual_cost_microusd == 31
    assert [call.role for call in receipt.calls] == ["task", "reflection", "metric_judge"]


def test_metric_judge_provider_failure_is_unavailable_charged_and_never_cached() -> None:
    class FailingJudgeLM(_ExactFakeLM):
        def __call__(self, *args: Any, **kwargs: Any) -> list[str]:
            super().__call__(*args, **kwargs)
            raise RuntimeError("provider unavailable")

    grant = _grant(max_calls=2, max_request_bytes=32_768)
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=_ExactFakeLM(grant.task.endpoint, cost_microusd=7),
        reflection_lm=_ExactFakeLM(grant.reflection.endpoint, cost_microusd=7),
        metric_judge_lm=FailingJudgeLM(grant.metric_judge.endpoint, cost_microusd=7),
    )
    accepted = {"headline_zh": "原始标题", "why_zh": "原始理由"}
    candidate = {"headline_zh": "不同标题", "why_zh": "不同理由"}

    with _short_socket_path() as path, proxy.serve(path) as socket_path:
        judge = CardEquivalenceJudge(
            CompilerProxyLM(socket_path=socket_path, grant=grant, role="metric_judge"),
            max_tokens=grant.metric_judge.max_output_tokens,
            max_model_calls=grant.max_metric_judge_model_calls,
            require_exact_accounting=True,
        )
        assert judge.equivalence(accepted, candidate).status == "unavailable"
        assert judge.equivalence(accepted, candidate).status == "unavailable"

    receipt = proxy.execution_receipt()
    assert judge.stats == {
        "attempts": 2,
        "model_calls": 2,
        "cache_entries": 0,
        "failures": 2,
        "actual_cost_microusd": 14,
    }
    assert receipt.metric_judge_model_calls == receipt.metric_judge_failures == 2
    assert receipt.metric_judge_cost_microusd == receipt.actual_cost_microusd == 14
    assert receipt.task_failures == receipt.reflection_failures == 0


def test_metric_judge_failure_without_usage_still_counts_the_physical_call() -> None:
    class FailingBeforeMetadataLM(_ExactFakeLM):
        def __call__(self, *args: Any, **kwargs: Any) -> list[str]:
            self.calls.append((args, kwargs))
            raise RuntimeError("provider failed before usage")

    grant = _grant(max_calls=1, max_request_bytes=32_768)
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=_ExactFakeLM(grant.task.endpoint, cost_microusd=7),
        reflection_lm=_ExactFakeLM(grant.reflection.endpoint, cost_microusd=7),
        metric_judge_lm=FailingBeforeMetadataLM(grant.metric_judge.endpoint, cost_microusd=0),
    )

    with _short_socket_path() as path, proxy.serve(path) as socket_path:
        judge = CardEquivalenceJudge(
            CompilerProxyLM(socket_path=socket_path, grant=grant, role="metric_judge"),
            max_tokens=grant.metric_judge.max_output_tokens,
            max_model_calls=grant.max_metric_judge_model_calls,
            require_exact_accounting=True,
        )
        assessment = judge.equivalence(
            {"headline_zh": "原始标题", "why_zh": "原始理由"},
            {"headline_zh": "不同标题", "why_zh": "不同理由"},
        )

    receipt = proxy.execution_receipt()
    assert assessment.status == "unavailable"
    assert judge.stats["model_calls"] == judge.stats["failures"] == 1
    assert judge.stats["actual_cost_microusd"] == 0
    assert receipt.metric_judge_model_calls == receipt.metric_judge_failures == 1
    assert receipt.metric_judge_cost_microusd == 0


def test_unix_proxy_keeps_endpoint_and_credentials_out_of_child_protocol(monkeypatch: Any) -> None:
    grant = _grant()
    task_lm = _ExactFakeLM(grant.task.endpoint, cost_microusd=7)
    reflection_lm = _ExactFakeLM(grant.reflection.endpoint, cost_microusd=11)
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=task_lm,
        reflection_lm=reflection_lm,
        metric_judge_lm=_ExactFakeLM(grant.metric_judge.endpoint, cost_microusd=13),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-proxy")
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-cross-proxy")

    with _short_socket_path() as path, proxy.serve(path) as socket_path:
        task = CompilerProxyLM(socket_path=socket_path, grant=grant, role="task")
        reflection = CompilerProxyLM(
            socket_path=socket_path,
            grant=grant,
            role="reflection",
        )
        with task.observe_exact_call() as task_capture:
            assert task(prompt="safe request") == ["proxy-output"]
        with reflection.observe_exact_call() as reflection_capture:
            assert reflection(messages=[{"role": "user", "content": "safe"}]) == ["proxy-output"]

    assert task_capture.require_exactly_one().provider_cost_microusd == 7
    assert reflection_capture.require_exactly_one().provider_cost_microusd == 11
    receipt = proxy.execution_receipt()
    assert (receipt.task_model_calls, receipt.reflection_model_calls, receipt.actual_cost_microusd) == (1, 1, 18)
    protocol_repr = repr((grant, task_capture.require_exactly_one(), receipt))
    assert "task.internal.example" not in protocol_repr
    assert "reflection.internal.example" not in protocol_repr
    assert "must-not-cross-proxy" not in protocol_repr


def test_proxy_enforces_call_budget_in_trusted_server_before_provider_call() -> None:
    grant = _grant(max_calls=1)
    task_lm = _ExactFakeLM(grant.task.endpoint, cost_microusd=7)
    reflection_lm = _ExactFakeLM(grant.reflection.endpoint, cost_microusd=11)
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=task_lm,
        reflection_lm=reflection_lm,
        metric_judge_lm=_ExactFakeLM(grant.metric_judge.endpoint, cost_microusd=13),
    )

    with _short_socket_path() as path, proxy.serve(path) as socket_path:
        client = CompilerProxyLM(socket_path=socket_path, grant=grant, role="task")
        assert client(prompt="first") == ["proxy-output"]
        with pytest.raises(CompilerProxyError, match="call_budget_exhausted"):
            client(prompt="second")

    assert len(task_lm.calls) == 1
    receipt = proxy.execution_receipt()
    assert receipt.task_model_calls == 1
    assert receipt.error_codes == ("news_program_compile_proxy_call_budget_exhausted",)


def test_proxy_reserves_worst_case_cost_before_each_provider_call() -> None:
    grant = _grant(max_calls=4, max_cost=20)
    task_lm = _ExactFakeLM(grant.task.endpoint, cost_microusd=7)
    reflection_lm = _ExactFakeLM(grant.reflection.endpoint, cost_microusd=7)
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=task_lm,
        reflection_lm=reflection_lm,
        metric_judge_lm=_ExactFakeLM(grant.metric_judge.endpoint, cost_microusd=13),
    )

    with _short_socket_path() as path, proxy.serve(path) as socket_path:
        client = CompilerProxyLM(socket_path=socket_path, grant=grant, role="task")
        assert client(prompt="first") == ["proxy-output"]
        with pytest.raises(CompilerProxyError, match="cost_reservation_exhausted"):
            client(prompt="second")

    assert len(task_lm.calls) == 1
    receipt = proxy.execution_receipt()
    assert receipt.reserved_cost_microusd == receipt.calls[0].reserved_cost_microusd
    assert receipt.calls[0].reserved_cost_microusd == grant.reservation_microusd(
        role="task",
        request_bytes=receipt.calls[0].request_bytes,
    )
    assert receipt.actual_cost_microusd == 7


def test_ephemeral_proxy_secret_config_repr_and_identities_are_secret_free() -> None:
    task = CompilerProviderEndpointSecret(
        model="provider/task-model",
        api_base="https://private-gateway.example/v1",
        api_key="sk-private-value-that-must-not-leak",
        timeout=20,
        max_tokens=512,
        temperature=0,
    )
    reflection = CompilerProviderEndpointSecret(
        model="provider/reflection-model",
        api_base="https://private-gateway.example/v1",
        api_key="sk-private-value-that-must-not-leak",
        timeout=300,
        max_tokens=32_000,
        temperature=1,
    )
    metric_judge = CompilerProviderEndpointSecret(
        model="provider/judge-model",
        api_base="https://private-gateway.example/v1",
        api_key="sk-private-value-that-must-not-leak",
        timeout=120,
        max_tokens=4_096,
        temperature=0,
    )
    config = CompilerProxySecretConfig(
        task=task,
        reflection=reflection,
        metric_judge=metric_judge,
        tariff=_tariff(),
    )

    rendered = repr(config)
    assert "private-gateway" not in rendered
    assert "sk-private" not in rendered
    assert "api_base" not in config.task.identity.model_dump_json()
    assert "api_key" not in config.task.identity.model_dump_json()


def test_proxy_request_rejects_credential_and_endpoint_capability_keys() -> None:
    with pytest.raises(ValueError, match="capability_key"):
        CompilerProxyRequest.issue(
            grant_sha256="a" * 64,
            role="task",
            sequence=1,
            args=(),
            kwargs={"api_key": "not-allowed"},
        )


@pytest.mark.parametrize(
    "forbidden",
    ("max_tokens", "cache", "timeout", "temperature", "tools", "tool_choice"),
)
def test_proxy_request_rejects_optimizer_owned_generation_transport_and_tools(forbidden: str) -> None:
    with pytest.raises(ValueError, match="request_option_forbidden"):
        CompilerProxyRequest.issue(
            grant_sha256="a" * 64,
            role="task",
            sequence=1,
            args=(),
            kwargs={forbidden: 1},
        )
    with pytest.raises(ValueError, match="capability_key"):
        CompilerProxyRequest.issue(
            grant_sha256="a" * 64,
            role="task",
            sequence=1,
            args=(),
            kwargs={"base_url": "https://not-allowed.example"},
        )


def test_proxy_client_strips_only_exact_trusted_dspy_generation_options() -> None:
    grant = _grant()
    task_lm = _ExactFakeLM(grant.task.endpoint, cost_microusd=7)
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=task_lm,
        reflection_lm=_ExactFakeLM(grant.reflection.endpoint, cost_microusd=7),
        metric_judge_lm=_ExactFakeLM(grant.metric_judge.endpoint, cost_microusd=7),
    )
    with _short_socket_path() as path, proxy.serve(path) as socket_path:
        client = CompilerProxyLM(socket_path=socket_path, grant=grant, role="task")
        assert client(
            prompt="safe",
            max_tokens=512,
            temperature=0.0,
            cache=False,
            timeout=20,
            num_retries=0,
        ) == ["proxy-output"]
        for override in (
            {"max_tokens": 10_000},
            {"temperature": 1},
            {"cache": True},
            {"timeout": 200},
            {"num_retries": 1},
        ):
            with pytest.raises(CompilerProxyError, match="request_option_override"):
                client(prompt="unsafe", **override)

    assert task_lm.calls == [((), {"prompt": "safe"})]


def test_real_compile_program_adapter_can_use_proxy_without_forwarding_generation_controls() -> None:
    class ProgramFakeLM(_ExactFakeLM):
        def __init__(self, identity: CompilerEndpointIdentity) -> None:
            super().__init__(identity, cost_microusd=7)
            self.responses = [
                (
                    '{"semantics":{"novelty":"new_fact","restates":-1,"event_type":"filing",'
                    '"assets":[],"direction":"neutral","scope":"single_name","magnitude":1,'
                    '"confidence":0.9,"audience":"us_equity","relevance":{'
                    '"impact_breadth":"single_instrument","tradability":"direct",'
                    '"surprise":"material_vs_expectation","development_delta":"state_change",'
                    '"channels":["earnings_cashflow"],"affected_markets":["single_asset"],'
                    '"reader_value":"realtime"}}}'
                ),
                '{"card":{"headline_zh":"公司提交重要文件","why_zh":"文件改变了事件时间表。"}}',
            ]

        def __call__(self, *args: Any, **kwargs: Any) -> list[str]:
            self.calls.append((args, kwargs))
            self._capture.record_metadata(
                ExactProviderMetadata(
                    response_model=self.tracefold_compiler_endpoint_identity.model,
                    input_tokens=100,
                    output_tokens=50,
                    total_tokens=150,
                    provider_cost_microusd=self.cost_microusd,
                    finish_reason="stop",
                )
            )
            return [self.responses.pop(0)]

    task_endpoint = CompilerEndpointIdentity.issue(
        model="provider/task-model",
        api_base="https://task.internal.example/v1",
    )
    reflection_endpoint = CompilerEndpointIdentity.issue(
        model="provider/reflection-model",
        api_base="https://reflection.internal.example/v1",
    )
    judge_endpoint = CompilerEndpointIdentity.issue(
        model="provider/judge-model",
        api_base="https://judge.internal.example/v1",
    )
    grant = CompilerModelProxyGrant.issue(
        task=CompilerRoleBindingV3.issue(
            role="task",
            endpoint=task_endpoint,
            max_output_tokens=1_200,
            timeout_seconds=20,
            temperature=0,
            model_kwargs={},
        ),
        reflection=CompilerRoleBindingV3.issue(
            role="reflection",
            endpoint=reflection_endpoint,
            max_output_tokens=32_000,
            timeout_seconds=300,
            temperature=1,
            model_kwargs={},
        ),
        metric_judge=CompilerRoleBindingV3.issue(
            role="metric_judge",
            endpoint=judge_endpoint,
            max_output_tokens=4_096,
            timeout_seconds=120,
            temperature=0,
            model_kwargs={},
        ),
        max_task_model_calls=4,
        max_reflection_model_calls=4,
        max_metric_judge_model_calls=4,
        max_cost_microusd=100,
        tariff=_tariff(),
        proxy_config_sha256="e" * 64,
        proxy_source_sha256="d" * 64,
        max_request_bytes=32_768,
    )
    task_lm = ProgramFakeLM(grant.task.endpoint)
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=task_lm,
        reflection_lm=_ExactFakeLM(grant.reflection.endpoint, cost_microusd=7),
        metric_judge_lm=_ExactFakeLM(grant.metric_judge.endpoint, cost_microusd=7),
    )

    with _short_socket_path() as path, proxy.serve(path) as socket_path:
        client = CompilerProxyLM(socket_path=socket_path, grant=grant, role="task")
        program = DspyCompileProgram(load_stable_program_artifact())
        with dspy.context(
            lm=client,
            adapter=DspyStrictJSONAdapter(use_native_function_calling=False),
            track_usage=True,
            disable_history=True,
        ):
            prediction = program(
                evidence_json=("<tracefold-untrusted-event-json-v1>\n{}\n</tracefold-untrusted-event-json-v1>"),
                card_evidence_json=("<tracefold-untrusted-event-json-v1>\n{}\n</tracefold-untrusted-event-json-v1>"),
                told_count=0,
            )

    assert prediction.verdict.headline_zh == "公司提交重要文件"
    assert len(task_lm.calls) == 2
    assert all(set(kwargs) <= {"messages", "response_format"} for _, kwargs in task_lm.calls)


def test_proxy_imputes_the_tariff_reservation_when_the_provider_reports_no_price() -> None:
    """#143: `None` is the normal case, not an anomaly.

    Neither the local llama.cpp endpoint nor DeepSeek returns a price litellm can resolve, so failing closed
    on a missing cost meant a real compile died on its first model call. Charging the grant's own worst-case
    reservation over-charges, which is the safe direction: the budget stops the run early, never late.
    """

    class NoPriceLM(_ExactFakeLM):
        def __call__(self, *args: Any, **kwargs: Any) -> list[str]:
            self.calls.append((args, kwargs))
            self._capture.record_metadata(
                ExactProviderMetadata(
                    input_tokens=3,
                    output_tokens=2,
                    total_tokens=5,
                    provider_cost_microusd=None,
                    finish_reason="stop",
                )
            )
            return ["priced-by-tariff"]

    grant = _grant()
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=NoPriceLM(grant.task.endpoint, cost_microusd=0),
        reflection_lm=_ExactFakeLM(grant.reflection.endpoint, cost_microusd=7),
        metric_judge_lm=_ExactFakeLM(grant.metric_judge.endpoint, cost_microusd=7),
    )
    with _short_socket_path() as path, proxy.serve(path) as socket_path:
        client = CompilerProxyLM(socket_path=socket_path, grant=grant, role="task")
        assert client(prompt="no price is still meterable") == ["priced-by-tariff"]

    receipt = proxy.execution_receipt()
    assert receipt.task_model_calls == 1
    # Charged, not skipped, and charged at the grant's own reservation for this request.
    assert receipt.actual_cost_microusd == receipt.reserved_cost_microusd > 0
    assert not receipt.error_codes


@pytest.mark.parametrize(
    ("metadata", "expected_error"),
    [
        (
            ExactProviderMetadata(
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                provider_cost_microusd=7,
                finish_reason="stop",
            ),
            "provider_usage_unavailable",
        ),
    ],
)
def test_proxy_provider_accounting_failure_never_yields_a_successful_execution_receipt(
    metadata: ExactProviderMetadata,
    expected_error: str,
) -> None:
    class IncompleteAccountingLM(_ExactFakeLM):
        def __call__(self, *args: Any, **kwargs: Any) -> list[str]:
            self.calls.append((args, kwargs))
            self._capture.record_metadata(metadata)
            return ["output-must-be-discarded"]

    grant = _grant()
    task_lm = IncompleteAccountingLM(grant.task.endpoint, cost_microusd=7)
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=task_lm,
        reflection_lm=_ExactFakeLM(grant.reflection.endpoint, cost_microusd=7),
        metric_judge_lm=_ExactFakeLM(grant.metric_judge.endpoint, cost_microusd=7),
    )

    with _short_socket_path() as path, proxy.serve(path) as socket_path:
        client = CompilerProxyLM(socket_path=socket_path, grant=grant, role="task")
        with pytest.raises(CompilerProxyError, match=expected_error):
            client(prompt="accounting must be complete")

    receipt = proxy.execution_receipt()
    assert receipt.error_codes == (f"news_program_compile_proxy_{expected_error}",)
    assert receipt.calls[0].error_code == receipt.error_codes[0]


def test_proxy_rejects_lm_whose_endpoint_identity_does_not_match_grant() -> None:
    grant = _grant()
    wrong = _ExactFakeLM(
        CompilerEndpointIdentity.issue(
            model=grant.task.endpoint.model,
            api_base="https://different.internal.example/v1",
        ),
        cost_microusd=1,
    )
    reflection = _ExactFakeLM(grant.reflection.endpoint, cost_microusd=1)

    with pytest.raises(ValueError, match="endpoint_identity_mismatch"):
        TrustedCompilerModelProxy(
            grant=grant,
            task_lm=wrong,
            reflection_lm=reflection,
            metric_judge_lm=_ExactFakeLM(grant.metric_judge.endpoint, cost_microusd=1),
        )
