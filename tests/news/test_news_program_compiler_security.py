from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tracefold.news.agents.program_compiler_security import (
    CompileBudgetV3,
    CompileInputBundle,
    CompileReceiptChain,
    CompilerEndpointIdentity,
    CompilerProxyTariff,
    CompilerRoleBindingV3,
    ContentAddressedCompileReceipt,
    gepa_metric_call_ceiling,
    seal_compile_input,
)
from tracefold.news.artifact_identity import canonical_sha


def _dataset_payload() -> dict[str, Any]:
    return {
        "dataset_version": "news_learning_dataset_v1",
        "role": "development",
        "learning_epoch": "program_v6",
        "learning_epoch_started_at_ms": 1_800_000_000_000,
        "agent_cohort": {
            "bundle_sha": "e" * 64,
            "learning_epoch": "program_v6",
            "program_version": "news_semantic_program_v4",
            "program_sha256": "a" * 64,
            "runtime_model_bindings_sha256": "c" * 64,
        },
        "cases": [
            {"case_id": "case-1", "cluster_id": "cluster-a"},
            {"case_id": "case-2", "cluster_id": "cluster-b"},
        ],
    }


def _episodes() -> tuple[dict[str, Any], ...]:
    return (
        {"case_id": "case-1", "cluster_id": "cluster-a", "accepted_review": {"should_push": "must_push"}},
        {"case_id": "case-2", "cluster_id": "cluster-b", "accepted_review": {"should_push": "must_hold"}},
    )


def _tariff() -> CompilerProxyTariff:
    return CompilerProxyTariff(
        tariff_id="trusted-v1",
        input_token_overhead=64,
        task_input_microusd_per_million=10,
        task_output_microusd_per_million=20,
        reflection_input_microusd_per_million=10,
        reflection_output_microusd_per_million=20,
        metric_judge_input_microusd_per_million=10,
        metric_judge_output_microusd_per_million=20,
    )


def _role_bindings() -> tuple[CompilerRoleBindingV3, CompilerRoleBindingV3, CompilerRoleBindingV3]:
    return (
        CompilerRoleBindingV3.issue(
            role="task",
            endpoint=CompilerEndpointIdentity.issue(model="provider/task", api_base="https://task.example/v1"),
            max_output_tokens=512,
            timeout_seconds=20,
            temperature=0,
            model_kwargs={},
        ),
        CompilerRoleBindingV3.issue(
            role="reflection",
            endpoint=CompilerEndpointIdentity.issue(
                model="provider/reflection", api_base="https://reflection.example/v1"
            ),
            max_output_tokens=32_000,
            timeout_seconds=300,
            temperature=1,
            model_kwargs={},
        ),
        CompilerRoleBindingV3.issue(
            role="metric_judge",
            endpoint=CompilerEndpointIdentity.issue(model="provider/judge", api_base="https://judge.example/v1"),
            max_output_tokens=4_096,
            timeout_seconds=120,
            temperature=0,
            model_kwargs={},
        ),
    )


def _sealed_bundle() -> CompileInputBundle:
    payload = _dataset_payload()
    task, reflection, metric_judge = _role_bindings()
    tariff = _tariff()
    return seal_compile_input(
        dataset_sha=canonical_sha({"kind": "dataset", "payload": payload}),
        dataset_payload=payload,
        episodes=_episodes(),
        review_rubric_version="news_review_v4",
        parent_program_sha256="a" * 64,
        parent_state_sha256="b" * 64,
        stable_bundle_sha256="e" * 64,
        target_runtime_manifest_sha256="c" * 64,
        eligible_demo_bank_root_sha256="d" * 64,
        task=task,
        reflection=reflection,
        metric_judge=metric_judge,
        proxy_grant_sha256="f" * 64,
        proxy_config_sha256="1" * 64,
        tariff_sha256=tariff.tariff_sha256,
        proxy_tariff=tariff,
        compiler_source_sha256="3" * 64,
        proxy_source_sha256="4" * 64,
        compiler_lock_sha256="5" * 64,
        sandbox_policy_sha256="6" * 64,
        compiler_image_digest="sha256:" + "7" * 64,
        budget=CompileBudgetV3(
            max_metric_calls=3,
            max_task_model_calls=8,
            max_reflection_model_calls=4,
            max_metric_judge_model_calls=6,
            max_cost_microusd=100,
            max_call_cost_microusd=10,
            seed=17,
        ),
    )


def test_gepa_metric_ceiling_rejects_an_inflated_untrusted_split() -> None:
    optimizer_config = {
        "constructor_scalar_arguments": {
            "max_metric_calls": 20,
            "reflection_minibatch_size": 3,
        },
        "compile_call": {
            "example_count": 6,
            "trainset_count": 4,
            "valset_count": 2,
        },
    }

    assert (
        gepa_metric_call_ceiling(
            max_metric_calls=20,
            optimizer_config=optimizer_config,
            expected_example_count=6,
        )
        == 25
    )
    optimizer_config["compile_call"]["valset_count"] = 5
    with pytest.raises(ValueError, match="optimizer_metric_budget_invalid"):
        gepa_metric_call_ceiling(
            max_metric_calls=20,
            optimizer_config=optimizer_config,
            expected_example_count=6,
        )


def test_compiler_endpoint_identity_is_endpoint_bound_but_contains_no_endpoint_or_key() -> None:
    first = CompilerEndpointIdentity.issue(model="openai/model-a", api_base="HTTPS://Compiler.Example:443/v1/")
    same = CompilerEndpointIdentity.issue(model="openai/model-a", api_base="https://compiler.example/v1")
    other = CompilerEndpointIdentity.issue(model="openai/model-a", api_base="https://other.example/v1")

    assert first == same
    assert first.binding_sha256 != other.binding_sha256
    serialized = first.model_dump_json()
    assert "compiler.example" not in serialized
    assert "api_base" not in serialized
    assert "api_key" not in serialized


@pytest.mark.parametrize(
    "api_base",
    [
        "https://user:password@compiler.example/v1",
        "https://compiler.example/v1?token=unsafe",
        "https://compiler.example/v1#fragment",
        "file:///tmp/model",
        "compiler.example/v1",
    ],
)
def test_compiler_endpoint_identity_rejects_credential_and_non_http_url_shapes(api_base: str) -> None:
    with pytest.raises(ValueError, match="endpoint_identity_invalid"):
        CompilerEndpointIdentity.issue(model="openai/model-a", api_base=api_base)


def test_sealed_compile_input_recomputes_dataset_and_ordered_projection_roots() -> None:
    bundle = _sealed_bundle()

    assert bundle.corpus.episode_count == 2
    assert bundle.corpus.case_root_sha256 == canonical_sha(["case-1", "case-2"])
    assert bundle.corpus.cluster_root_sha256 == canonical_sha(["cluster-a", "cluster-b"])
    assert bundle.corpus.episode_projection_root_sha256 == canonical_sha(list(_episodes()))
    assert CompileInputBundle.model_validate(bundle.model_dump(mode="json")) == bundle


def test_sealed_compile_input_rejects_forged_dataset_and_episode_membership() -> None:
    payload = _dataset_payload()
    tariff = _tariff()
    task, reflection, metric_judge = _role_bindings()
    kwargs = {
        "dataset_payload": payload,
        "episodes": _episodes(),
        "parent_program_sha256": "a" * 64,
        "parent_state_sha256": "b" * 64,
        "stable_bundle_sha256": "e" * 64,
        "target_runtime_manifest_sha256": "c" * 64,
        "eligible_demo_bank_root_sha256": "d" * 64,
        "task": task,
        "reflection": reflection,
        "metric_judge": metric_judge,
        "proxy_grant_sha256": "f" * 64,
        "proxy_config_sha256": "1" * 64,
        "tariff_sha256": tariff.tariff_sha256,
        "proxy_tariff": tariff,
        "compiler_source_sha256": "3" * 64,
        "proxy_source_sha256": "4" * 64,
        "compiler_lock_sha256": "5" * 64,
        "review_rubric_version": "news_review_v4",
        "sandbox_policy_sha256": "6" * 64,
        "compiler_image_digest": "sha256:" + "7" * 64,
        "budget": CompileBudgetV3(
            max_metric_calls=3,
            max_task_model_calls=8,
            max_reflection_model_calls=4,
            max_metric_judge_model_calls=6,
            max_cost_microusd=100,
            max_call_cost_microusd=10,
            seed=17,
        ),
    }
    with pytest.raises(ValueError, match="dataset_hash_mismatch"):
        seal_compile_input(dataset_sha="f" * 64, **kwargs)

    dataset_sha = canonical_sha({"kind": "dataset", "payload": payload})
    with pytest.raises(ValueError, match="dataset_episode_membership_mismatch"):
        seal_compile_input(dataset_sha=dataset_sha, **{**kwargs, "episodes": tuple(reversed(_episodes()))})
    with pytest.raises(ValueError, match="dataset_episode_membership_mismatch"):
        seal_compile_input(dataset_sha=dataset_sha, **{**kwargs, "episodes": _episodes()[:1]})


def test_sealed_compile_input_rejects_tampered_projection_root_and_bundle_hash() -> None:
    payload = _sealed_bundle().model_dump(mode="json")
    payload["corpus"]["episode_projection_root_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="episode_projection_root_mismatch"):
        CompileInputBundle.model_validate(payload)

    payload = _sealed_bundle().model_dump(mode="json")
    payload["bundle_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="input_bundle_hash_mismatch"):
        CompileInputBundle.model_validate(payload)


def _receipt_chain() -> CompileReceiptChain:
    return CompileReceiptChain.issue(
        [
            ContentAddressedCompileReceipt.issue("corpus", _sealed_bundle().corpus),
            ContentAddressedCompileReceipt.issue("metric", {"metric_sha256": "1" * 64}),
            ContentAddressedCompileReceipt.issue("optimizer_config", {"optimizer_sha256": "2" * 64}),
            ContentAddressedCompileReceipt.issue("trajectory", {"trajectory_sha256": "3" * 64}),
            ContentAddressedCompileReceipt.issue("checkpoint", {"checkpoint_sha256": "4" * 64}),
            ContentAddressedCompileReceipt.issue(
                "sandbox_launch",
                {
                    "holdout_mounted": False,
                    "db_credentials_present": False,
                    "ambient_credentials_present": False,
                },
            ),
            ContentAddressedCompileReceipt.issue("patch", {"patch_sha256": "5" * 64}),
        ]
    )


def test_compile_receipt_chain_retains_and_rehashes_every_required_payload() -> None:
    chain = _receipt_chain()

    assert len(chain.receipts) == 7
    assert chain.payload("patch") == {"patch_sha256": "5" * 64}
    assert CompileReceiptChain.model_validate(chain.model_dump(mode="json")) == chain


def test_compile_receipt_chain_rejects_missing_tampered_or_secret_payload() -> None:
    receipts = list(_receipt_chain().receipts)
    with pytest.raises(ValidationError, match="receipt_chain_incomplete"):
        CompileReceiptChain.issue(receipts[:-1])

    payload = receipts[0].model_dump(mode="json")
    payload["payload"]["episode_count"] = 999
    with pytest.raises(ValidationError, match="receipt_hash_mismatch"):
        ContentAddressedCompileReceipt.model_validate(payload)

    with pytest.raises(ValidationError, match="secret_key"):
        ContentAddressedCompileReceipt.issue("patch", {"api_key": "must-not-persist"})
    with pytest.raises(ValidationError, match="secret_value"):
        ContentAddressedCompileReceipt.issue("patch", {"note": "Bearer abcdefghijklmnopqrstuvwxyz"})
