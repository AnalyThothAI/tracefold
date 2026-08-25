from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tests.news.test_news_program_compiler_sandbox import _valid_sandbox_launch_receipt
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.compiler.sandbox import CompilerSandboxLaunchReceipt
from tracefold.news.learning.compiler.security import (
    CompileBudgetV3,
    CompileInputBundle,
    CompilerBuildAttestation,
    CompileRecordV1,
    CompilerProxyCall,
    CompilerProxyExecution,
    CompilerProxyTariff,
    CompilerRole,
    CompileSpend,
    GepaRunResult,
    ModelExecutionIdentity,
    endpoint_fingerprint,
    gepa_metric_call_ceiling,
    seal_compile_input,
    validate_compile_record,
)

_PARENT_PROGRAM_SHA256 = "a" * 64
_PROGRAM_SHA256 = "b" * 64
_RUNTIME_MANIFEST_SHA256 = "c" * 64
_STABLE_BUNDLE_SHA256 = "e" * 64
# The three source identities the sandbox fixture's image preflight payload reports.
_HOST_SOURCE_SHA256 = "2" * 64
_HOST_PROXY_SOURCE_SHA256 = "5" * 64
_HOST_LOCK_SHA256 = "3" * 64
_IMAGE_DIGEST = "sha256:" + "4" * 64


def _dataset_payload() -> dict[str, Any]:
    return {
        "dataset_version": "news_learning_dataset_v1",
        "role": "development",
        "learning_epoch": "program_v7",
        "learning_epoch_started_at_ms": 1_800_000_000_000,
        "agent_cohort": {
            "bundle_sha": _STABLE_BUNDLE_SHA256,
            "learning_epoch": "program_v7",
            "program_version": "news_semantic_program_v5",
            "program_sha256": _PARENT_PROGRAM_SHA256,
            "runtime_model_bindings_sha256": _RUNTIME_MANIFEST_SHA256,
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


def _identity(role: CompilerRole, **overrides: Any) -> ModelExecutionIdentity:
    """One role's whole execution contract, issued at the code-owned ceilings."""

    defaults: dict[CompilerRole, dict[str, Any]] = {
        "task": {
            "model": "provider/task",
            "api_base": "https://task.example/v1",
            "max_output_tokens": 512,
            "timeout_seconds": 20.0,
            "temperature": 0.0,
        },
        "reflection": {
            "model": "provider/reflection",
            "api_base": "https://reflection.example/v1",
            "max_output_tokens": 32_000,
            "timeout_seconds": 300.0,
            "temperature": 1.0,
        },
        "metric_judge": {
            "model": "provider/judge",
            "api_base": "https://judge.example/v1",
            "max_output_tokens": 4_096,
            "timeout_seconds": 120.0,
            "temperature": 0.0,
        },
    }
    return ModelExecutionIdentity.issue(role=role, model_kwargs={}, **{**defaults[role], **overrides})


def _budget(**overrides: Any) -> CompileBudgetV3:
    return CompileBudgetV3(
        **{
            "max_metric_calls": 3,
            "max_task_model_calls": 8,
            "max_reflection_model_calls": 4,
            "max_metric_judge_model_calls": 6,
            "max_cost_microusd": 100,
            "max_call_cost_microusd": 10,
            "seed": 17,
            **overrides,
        }
    )


def _sealed_bundle() -> CompileInputBundle:
    payload = _dataset_payload()
    return seal_compile_input(
        dataset_sha=canonical_sha({"kind": "dataset", "payload": payload}),
        dataset_payload=payload,
        episodes=_episodes(),
        review_rubric_version="news_review_v4",
        parent_program_sha256=_PARENT_PROGRAM_SHA256,
        stable_bundle_sha256=_STABLE_BUNDLE_SHA256,
        target_runtime_manifest_sha256=_RUNTIME_MANIFEST_SHA256,
        task=_identity("task"),
        reflection=_identity("reflection"),
        metric_judge=_identity("metric_judge"),
        proxy_grant_sha256="f" * 64,
        proxy_config_sha256="1" * 64,
        proxy_tariff=_tariff(),
        compiler_source_sha256=_HOST_SOURCE_SHA256,
        proxy_source_sha256=_HOST_PROXY_SOURCE_SHA256,
        compiler_lock_sha256=_HOST_LOCK_SHA256,
        sandbox_policy_sha256="6" * 64,
        compiler_image_digest=_IMAGE_DIGEST,
        budget=_budget(),
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


def test_model_execution_identity_is_endpoint_bound_but_contains_no_endpoint_or_key() -> None:
    """`endpoint_fingerprint` is the one digest that survived the three-level identity chain.

    `endpoint_sha256` -> `model_sha256` -> `binding_sha256` all hashed values printed immediately beside
    them. Only the endpoint URL is genuinely absent from the object, because it names the host a
    credential is presented to, so only its digest still earns its place.
    """

    first = _identity("task", api_base="HTTPS://Task.Example:443/v1/")
    same = _identity("task", api_base="https://task.example/v1")
    other = _identity("task", api_base="https://other.example/v1")

    assert first == same
    assert first.endpoint_fingerprint != other.endpoint_fingerprint
    assert first.endpoint_fingerprint == endpoint_fingerprint("https://task.example/v1")
    serialized = first.model_dump_json()
    assert "task.example" not in serialized
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
def test_model_execution_identity_rejects_credential_and_non_http_url_shapes(api_base: str) -> None:
    with pytest.raises(ValueError, match="endpoint_identity_invalid"):
        _identity("task", api_base=api_base)
    with pytest.raises(ValueError, match="endpoint_identity_invalid"):
        endpoint_fingerprint(api_base)


@pytest.mark.parametrize(
    ("role", "overrides"),
    [
        ("task", {"temperature": 1.0}),
        ("reflection", {"temperature": 0.0}),
        ("reflection", {"max_output_tokens": 8_000}),
        ("reflection", {"timeout_seconds": 60.0}),
        ("metric_judge", {"temperature": 1.0}),
        ("metric_judge", {"max_output_tokens": 8_000}),
        ("metric_judge", {"timeout_seconds": 60.0}),
    ],
)
def test_model_execution_identity_rejects_a_role_contract_the_trusted_side_did_not_choose(
    role: CompilerRole,
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match=f"{role}_role_contract_invalid"):
        _identity(role, **overrides)


def test_sealed_compile_input_recomputes_dataset_and_ordered_projection_roots() -> None:
    bundle = _sealed_bundle()

    assert bundle.corpus.episode_count == 2
    assert bundle.corpus.case_root_sha256 == canonical_sha(["case-1", "case-2"])
    assert bundle.corpus.cluster_root_sha256 == canonical_sha(["cluster-a", "cluster-b"])
    assert bundle.corpus.episode_projection_root_sha256 == canonical_sha(list(_episodes()))
    assert CompileInputBundle.model_validate(bundle.model_dump(mode="json")) == bundle


def test_sealed_compile_input_rejects_forged_dataset_and_episode_membership() -> None:
    payload = _dataset_payload()
    kwargs: dict[str, Any] = {
        "dataset_payload": payload,
        "episodes": _episodes(),
        "parent_program_sha256": _PARENT_PROGRAM_SHA256,
        "stable_bundle_sha256": _STABLE_BUNDLE_SHA256,
        "target_runtime_manifest_sha256": _RUNTIME_MANIFEST_SHA256,
        "task": _identity("task"),
        "reflection": _identity("reflection"),
        "metric_judge": _identity("metric_judge"),
        "proxy_grant_sha256": "f" * 64,
        "proxy_config_sha256": "1" * 64,
        "proxy_tariff": _tariff(),
        "compiler_source_sha256": _HOST_SOURCE_SHA256,
        "proxy_source_sha256": _HOST_PROXY_SOURCE_SHA256,
        "compiler_lock_sha256": _HOST_LOCK_SHA256,
        "review_rubric_version": "news_review_v4",
        "sandbox_policy_sha256": "6" * 64,
        "compiler_image_digest": _IMAGE_DIGEST,
        "budget": _budget(),
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


def _patch_payload() -> dict[str, Any]:
    """The exact patch document the runner retains: the write-set itself, never a digest of it.

    A patch carries no `patch_sha256` field. Its identity is `canonical_sha` of these four keys, computed
    by the record that embeds it, so a fixture shaped like a self-declared digest would be the one shape
    the record must never accept.
    """

    return {
        "schema_version": "news_program_strategy_patch_v1",
        "parent_program_sha256": _PARENT_PROGRAM_SHA256,
        "event_semantics_instruction": "Prefer the concrete magnitude the source states.",
        "reader_card_instruction": "Lead with what changed for the reader.",
    }


def _optimizer_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "schema": "tracefold.news.compile_optimizer_config_receipt.v1",
        "optimizer": {"implementation": "tracefold.news.learning.compiler.root.gepa_optimizer"},
        "constructor_scalar_arguments": {"max_metric_calls": 3, "reflection_minibatch_size": 1},
        "compile_call": {"example_count": 2, "trainset_count": 1, "valset_count": 1},
        **overrides,
    }


def _proxy_call(role: CompilerRole, *, sequence: int = 1, **overrides: Any) -> CompilerProxyCall:
    identity = _identity(role)
    request_bytes = int(overrides.pop("request_bytes", 256))
    values: dict[str, Any] = {
        "role": role,
        "sequence": sequence,
        "request_sha256": canonical_sha({"role": role, "sequence": sequence, "kind": "request"}),
        "response_sha256": canonical_sha({"role": role, "sequence": sequence, "kind": "response"}),
        "responding_model": identity.model,
        "provider_invoked": True,
        "request_bytes": request_bytes,
        "max_output_tokens": identity.max_output_tokens,
        "reserved_cost_microusd": _tariff().worst_case_cost_microusd(
            role=role,
            request_bytes=request_bytes,
            max_output_tokens=identity.max_output_tokens,
        ),
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_tokens": 0,
        "total_tokens": 15,
        "provider_cost_microusd": 1,
        "finish_reason": "stop",
        "error_code": None,
    }
    values.update(overrides)
    return CompilerProxyCall(**values)


def _sandbox_receipt() -> CompilerSandboxLaunchReceipt:
    _, receipt = _valid_sandbox_launch_receipt()
    return receipt


def _grant_sha256() -> str:
    """The grant the sandbox receipt's egress manifest names — the ledger must name the same one."""

    return str(_sandbox_receipt().egress_manifest["proxy_grant_sha256"])


def _usage(*calls: CompilerProxyCall, grant_sha256: str | None = None) -> CompilerProxyExecution:
    def by_role(role: str, attribute: str) -> int:
        return sum(getattr(call, attribute) for call in calls if call.role == role)

    def invoked(role: str) -> int:
        return sum(call.provider_invoked for call in calls if call.role == role)

    def failures(role: str) -> int:
        return sum(call.error_code is not None for call in calls if call.role == role)

    return CompilerProxyExecution(
        grant_sha256=_grant_sha256() if grant_sha256 is None else grant_sha256,
        task_model_calls=invoked("task"),
        reflection_model_calls=invoked("reflection"),
        metric_judge_model_calls=invoked("metric_judge"),
        task_cost_microusd=by_role("task", "provider_cost_microusd"),
        reflection_cost_microusd=by_role("reflection", "provider_cost_microusd"),
        metric_judge_cost_microusd=by_role("metric_judge", "provider_cost_microusd"),
        task_failures=failures("task"),
        reflection_failures=failures("reflection"),
        metric_judge_failures=failures("metric_judge"),
        actual_cost_microusd=sum(call.provider_cost_microusd for call in calls),
        reserved_cost_microusd=sum(call.reserved_cost_microusd for call in calls),
        calls=tuple(calls),
        error_codes=tuple(call.error_code for call in calls if call.error_code is not None),
    )


def _attestation(**overrides: Any) -> CompilerBuildAttestation:
    return CompilerBuildAttestation(
        **{
            "compiler_image_digest": _IMAGE_DIGEST,
            "proxy_image_digest": _IMAGE_DIGEST,
            "host_source_sha256": _HOST_SOURCE_SHA256,
            "host_proxy_source_sha256": _HOST_PROXY_SOURCE_SHA256,
            "host_lock_sha256": _HOST_LOCK_SHA256,
            "image_source_sha256": _HOST_SOURCE_SHA256,
            "image_proxy_source_sha256": _HOST_PROXY_SOURCE_SHA256,
            "image_lock_sha256": _HOST_LOCK_SHA256,
            "container_source_sha256": _HOST_SOURCE_SHA256,
            "container_proxy_source_sha256": _HOST_PROXY_SOURCE_SHA256,
            **overrides,
        }
    )


def _run_result(**overrides: Any) -> GepaRunResult:
    """One optimization, carried whole. The record embeds this object; it does not restate its fields."""

    values: dict[str, Any] = {
        "patch": _patch_payload(),
        "metric": {
            "schema": "tracefold.news.compile_metric_receipt.v1",
            "metric_version": "news_compile_metric_v3",
            "review_rubric_version": "news_review_v4",
        },
        "optimizer_config": _optimizer_payload(),
        # The search path that produced the patch, and the checkpoint it ended on.
        "trajectory": {"schema": "tracefold.news.compile_trajectory_receipt.v1", "best_idx": 1},
        "checkpoint": {
            "schema": "tracefold.news.compile_checkpoint_receipt.v2",
            "factory": "tracefold.news.program.factory_v6",
        },
        # The winner was picked on examples it never trained on, and the model saw the card it was
        # supposed to recognise. Both proofs are computed in the container and now reach the record.
        "split": {
            "schema": "tracefold.news.compile_split_receipt.v1",
            "train_cluster_count": 2,
            "val_cluster_count": 1,
            "disjoint": True,
        },
        "retrieval": {
            "schema": "tracefold.news.compile_retrieval_receipt.v1",
            "episodes": 3,
            "target_visible": 3,
        },
        "failure_cluster_ids": ("cluster-a",),
        "target_dimensions": ("recall",),
        "metric_calls": 4,
        "train_count": 1,
        "val_count": 1,
    }
    values.update(overrides)
    return GepaRunResult.model_validate(values)


def _spend(**overrides: Any) -> CompileSpend:
    values: dict[str, Any] = {
        "task_model_calls": 1,
        "reflection_model_calls": 1,
        "metric_judge_attempts": 2,
        "metric_judge_model_calls": 1,
        "metric_judge_failures": 0,
        "task_cost_microusd": 1,
        "reflection_cost_microusd": 1,
        "metric_judge_cost_microusd": 1,
        "actual_cost_microusd": 3,
    }
    values.update(overrides)
    return CompileSpend.model_validate(values)


def _record_values(**overrides: Any) -> dict[str, Any]:
    """Exactly what the CLI hands `CompileRecordV1.issue`: nested models, not pre-dumped JSON.

    `issue` drafts the record before hashing it, so the digest covers every defaulted field and the
    caller never has to restate `projection_schema_id` or `learning_epoch` to make the root come out
    right. Passing the models themselves is what production does, so the fixture does it too.
    """

    values: dict[str, Any] = {
        "parent_program_sha256": _PARENT_PROGRAM_SHA256,
        "program_sha256": _PROGRAM_SHA256,
        "development_dataset_sha256": canonical_sha({"kind": "dataset", "payload": _dataset_payload()}),
        "learning_epoch_started_at_ms": 1_800_000_000_000,
        "review_rubric_version": "news_review_v4",
        "episode_count": 2,
        "episode_projection_root_sha256": "e" * 64,
        "target_runtime_manifest_sha256": _RUNTIME_MANIFEST_SHA256,
        "task_model": _identity("task"),
        "reflection_model": _identity("reflection"),
        "metric_judge_model": _identity("metric_judge"),
        "run": _run_result(),
        "budget": _budget(),
        "tariff": _tariff(),
        "usage": _usage(_proxy_call("task"), _proxy_call("reflection"), _proxy_call("metric_judge")),
        "spend": _spend(),
        "sandbox": _sandbox_receipt(),
        "compiler_build": _attestation(),
        "created_at_ms": 1_800_000_000_500,
    }
    values.update(overrides)
    return values


def _compile_record(**overrides: Any) -> CompileRecordV1:
    return CompileRecordV1.issue(**_record_values(**overrides))


def _tampered(*path: Any, value: Any) -> dict[str, Any]:
    """One edited payload under a stale root: what `compile_record_sha256` alone must catch."""

    payload = _compile_record().model_dump(mode="json")
    cursor: Any = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    return payload


def _resealed(*path: Any, value: Any) -> dict[str, Any]:
    """The same edit with the root recomputed: what only a semantic validator can still catch."""

    payload = _tampered(*path, value=value)
    payload["compile_record_sha256"] = canonical_sha(
        {key: item for key, item in payload.items() if key != "compile_record_sha256"}
    )
    return payload


def test_compile_record_retains_every_payload_the_receipt_chain_used_to_carry() -> None:
    """The seven chain receipts, the runner receipt, the provenance record and the machine diff, in one.

    `compile_record_sha256` is both the ledger key and the only digest anyone has to check, so what the
    test asserts is retention plus one round trip — the property the chain needed a Merkle root and six
    sibling hashes to state.
    """

    record = _compile_record()

    assert record.development_dataset_sha256 == canonical_sha({"kind": "dataset", "payload": _dataset_payload()})
    assert record.episode_count == 2
    assert record.review_rubric_version == "news_review_v4"
    assert record.run.metric["metric_version"] == "news_compile_metric_v3"
    assert record.run.optimizer_config == _optimizer_payload()
    assert record.run.patch.model_dump(mode="json") == _patch_payload()
    # The search path is carried whole, not as a digest of bytes nothing stores.
    assert record.run.trajectory["schema"] == "tracefold.news.compile_trajectory_receipt.v1"
    assert record.run.checkpoint["schema"] == "tracefold.news.compile_checkpoint_receipt.v2"
    assert record.episode_projection_root_sha256 == "e" * 64
    assert record.sandbox.boundary_command["schema"] == "tracefold.news.compiler_boundary_commands.v2"
    assert record.sandbox.policy["schema_version"] == "tracefold.news.compiler_sandbox_policy.v2"
    assert [call.role for call in record.usage.calls] == ["task", "reflection", "metric_judge"]
    assert record.compile_record_sha256 == canonical_sha(
        record.model_dump(mode="json", exclude={"compile_record_sha256"})
    )
    assert CompileRecordV1.model_validate(record.model_dump(mode="json")) == record


@pytest.mark.parametrize(
    "field",
    [
        "budget",
        "compiler_build",
        "run",
        "sandbox",
        "spend",
        "tariff",
        "usage",
    ],
)
def test_compile_record_cannot_omit_a_payload_the_chain_used_to_require(field: str) -> None:
    """What `news_program_compile_receipt_chain_incomplete` used to say, said by the schema instead."""

    payload = _compile_record().model_dump(mode="json")
    payload.pop(field)
    with pytest.raises(ValidationError):
        CompileRecordV1.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        # was `receipt_sha256` over the patch receipt
        (("run", "patch", "reader_card_instruction"), "Lead with something else."),
        # was `receipt_sha256` over the metric receipt
        (("run", "metric", "metric_version"), "news_compile_metric_v2"),
        # was `receipt_sha256` over the optimizer_config receipt
        (("run", "optimizer_config", "constructor_scalar_arguments", "reflection_minibatch_size"), 2),
        # was `launch_receipt_sha256` (an edited `exit_code` never reaches here: the sandbox receipt's own
        # success invariant rejects it one layer down)
        (("sandbox", "stdout_sha256"), "0" * 64),
        (("sandbox", "wall_time_ms"), 999),
        (("sandbox", "launcher_max_rss_bytes"), 1),
        # was `request_root_sha256` / `response_root_sha256` over the proxy call ledger
        (("usage", "calls", 0, "request_sha256"), "0" * 64),
        (("usage", "calls", 0, "response_sha256"), "0" * 64),
        (("usage", "calls", 0, "responding_model"), "attacker/model"),
        # was the grant cross-binding the deleted `CompilerProxyExecutionReceipt.receipt_sha256` carried
        (("usage", "grant_sha256"), "0" * 64),
        # was `tariff_sha256`, restated in four documents
        (("tariff", "task_output_microusd_per_million"), 1),
        (("budget", "seed"), 18),
        (("run", "trajectory", "best_idx"), 99),
        (("created_at_ms",), 1_800_000_000_600),
        (("compile_record_sha256",), "0" * 64),
    ],
)
def test_compile_record_fails_closed_on_every_tampered_embedded_payload(path: tuple[Any, ...], value: Any) -> None:
    """Each case tampers with the payload one deleted digest used to cover; one root catches all of them."""

    with pytest.raises(ValidationError, match="compile_record_hash_mismatch"):
        CompileRecordV1.model_validate(_tampered(*path, value=value))


def test_compile_record_rejects_a_dropped_proxy_call_the_call_root_used_to_cover() -> None:
    """`call_root_sha256` is gone; the ledger's own per-role sums are what a dropped call now trips."""

    payload = _compile_record().model_dump(mode="json")
    payload["usage"]["calls"] = payload["usage"]["calls"][1:]
    with pytest.raises(ValidationError, match="proxy_execution_accounting_mismatch"):
        CompileRecordV1.model_validate(payload)

    duplicated = _compile_record().model_dump(mode="json")
    duplicated["usage"]["calls"].append(dict(duplicated["usage"]["calls"][0]))
    with pytest.raises(ValidationError, match="proxy_call_sequence_duplicate"):
        CompileRecordV1.model_validate(duplicated)


def test_compile_record_rejects_secret_material_in_any_embedded_payload() -> None:
    with pytest.raises(ValidationError, match="secret_key"):
        _compile_record(run=_run_result(metric={"api_key": "must-not-persist"}))
    with pytest.raises(ValidationError, match="secret_value"):
        _compile_record(run=_run_result(metric={"note": "Bearer abcdefghijklmnopqrstuvwxyz"}))
    with pytest.raises(ValidationError, match="secret_key"):
        _compile_record(run=_run_result(optimizer_config=_optimizer_payload(headers={"authorization": "redacted"})))


def test_compile_record_rejects_a_write_set_or_role_order_the_optimizer_chose() -> None:
    with pytest.raises(ValidationError, match="record_role_order_invalid"):
        _compile_record(task_model=_identity("reflection"))
    with pytest.raises(ValidationError, match="record_no_program_change"):
        _compile_record(program_sha256=_PARENT_PROGRAM_SHA256)
    # The write set is the patch model's own contract now, so a smuggled key and a non-string body are
    # refused by the type rather than by a spelling check the record had to carry.
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _compile_record(run=_run_result(patch={**_patch_payload(), "quality_kernel": "smuggled"}))
    with pytest.raises(ValidationError):
        _compile_record(run=_run_result(patch={**_patch_payload(), "reader_card_instruction": None}))
    # What typing cannot say, and the record still must: which parent this patch was written against.
    with pytest.raises(ValidationError, match="patch_parent_mismatch"):
        _compile_record(run=_run_result(patch={**_patch_payload(), "parent_program_sha256": "0" * 64}))


def test_compiler_build_attestation_accepts_only_a_host_image_and_container_that_agree() -> None:
    """#193 §4.5: the one place in this chain where two independent parties look at the same thing.

    The host hashes its own tree, the launcher hashes what it copied out of the pinned image before any
    secret is staged, and the runner hashes what it can see from inside the container. Recording three
    answers is not redundancy — the agreement is the attestation.
    """

    attestation = _attestation()

    assert attestation.host_source_sha256 == attestation.container_source_sha256
    assert CompilerBuildAttestation.model_validate(attestation.model_dump(mode="json")) == attestation
    assert _compile_record().compiler_build == attestation


@pytest.mark.parametrize(
    "field",
    [
        "image_source_sha256",
        "image_proxy_source_sha256",
        "image_lock_sha256",
        "container_source_sha256",
        "container_proxy_source_sha256",
        "host_source_sha256",
        "host_proxy_source_sha256",
        "host_lock_sha256",
    ],
)
def test_compiler_build_attestation_rejects_a_party_that_disagrees(field: str) -> None:
    with pytest.raises(ValidationError, match="build_attestation_mismatch"):
        _attestation(**{field: "0" * 64})
    # And the record refuses it even when the tamper is resealed under a freshly computed root.
    with pytest.raises(ValidationError, match="build_attestation_mismatch"):
        CompileRecordV1.model_validate(_resealed("compiler_build", field, value="0" * 64))


def test_compile_record_rejects_a_reservation_that_is_not_the_tariff_worst_case() -> None:
    """The per-call arithmetic the deleted `validate_compile_receipt_chain_v3` did across five documents."""

    tariff = _tariff()
    call = _proxy_call("task")
    assert call.reserved_cost_microusd == tariff.worst_case_cost_microusd(
        role="task",
        request_bytes=call.request_bytes,
        max_output_tokens=_identity("task").max_output_tokens,
    )

    # Each ledger below is a valid `CompilerProxyExecution` — its own sums add up. What fails is the
    # record's arithmetic against the tariff, re-issued so the root is never the thing that catches it.
    overstated = _usage(
        _proxy_call("task", reserved_cost_microusd=call.reserved_cost_microusd + 1),
        _proxy_call("reflection"),
        _proxy_call("metric_judge"),
    )
    with pytest.raises(ValidationError, match="record_call_reservation_invalid"):
        _compile_record(usage=overstated)

    # A call reserved at the task rate while claiming an output ceiling the task role was never issued.
    rebound = _usage(
        _proxy_call("task", max_output_tokens=1_024),
        _proxy_call("reflection"),
        _proxy_call("metric_judge"),
    )
    with pytest.raises(ValidationError, match="record_call_binding_mismatch"):
        _compile_record(usage=rebound)


def test_compile_record_rejects_a_per_call_reservation_over_the_budget_ceiling() -> None:
    with pytest.raises(ValidationError, match="record_call_cost_reservation_exceeded"):
        _compile_record(budget=_budget(max_call_cost_microusd=1))


def test_compile_record_rejects_totals_over_the_cost_budget() -> None:
    # Reserved (6) is over the ceiling even though every individual reservation is admissible.
    with pytest.raises(ValidationError, match="record_budget_exceeded"):
        _compile_record(budget=_budget(max_cost_microusd=5, max_call_cost_microusd=5))
    # And so is the money actually spent (3).
    with pytest.raises(ValidationError, match="record_budget_exceeded"):
        _compile_record(budget=_budget(max_cost_microusd=2, max_call_cost_microusd=2))


def test_compile_record_rejects_call_counts_and_metric_calls_over_their_ceilings() -> None:
    usage = _usage(
        _proxy_call("task", sequence=1),
        _proxy_call("task", sequence=2),
        _proxy_call("reflection"),
        _proxy_call("metric_judge"),
    )
    with pytest.raises(ValidationError, match="record_task_call_budget_exceeded"):
        _compile_record(usage=usage, budget=_budget(max_task_model_calls=1))
    # GEPA's sealed ceiling: 3 requested + 1 validation pass + 1 reflection minibatch.
    assert (
        gepa_metric_call_ceiling(max_metric_calls=3, optimizer_config=_optimizer_payload(), expected_example_count=2)
        == 5
    )
    with pytest.raises(ValidationError, match="record_budget_exceeded"):
        _compile_record(run=_run_result(metric_calls=6))
    # A judged call the runner never admitted to attempting.
    with pytest.raises(ValidationError, match="record_budget_exceeded"):
        _compile_record(spend=_spend(metric_judge_attempts=0, metric_judge_model_calls=0))


@pytest.mark.parametrize(
    "overrides",
    [
        {"error_code": "news_program_compile_proxy_model_call_failed"},
        {
            "provider_invoked": False,
            "reserved_cost_microusd": 0,
            "provider_cost_microusd": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "error_code": "news_program_compile_proxy_call_budget_exhausted",
        },
    ],
)
def test_compile_record_rejects_a_task_call_that_failed_or_never_reached_the_provider(
    overrides: dict[str, Any],
) -> None:
    """The ledger must record a refusal; only the record may refuse to be built from one.

    The sidecar writes its ledger whether it served a call or refused it — budget exhausted, sequence
    reused, grant mismatched — and that refusal is the evidence the boundary held, so
    `CompilerProxyExecution` accepts it as long as the arithmetic adds up. A record exists only for a
    compile that finished, so there the same call is fatal. Only the metric judge is exempt: its answer
    is a score component, and the metric already treats an unavailable judgment as zero.
    """

    refused = _usage(_proxy_call("task", **overrides), _proxy_call("reflection"), _proxy_call("metric_judge"))
    assert refused.task_failures == 1
    assert refused.error_codes == (overrides["error_code"],)

    with pytest.raises(ValidationError, match="record_task_call_failed"):
        _compile_record(usage=refused)

    judged = _usage(
        _proxy_call("task"),
        _proxy_call("reflection"),
        _proxy_call("metric_judge", **overrides),
    )
    record = _compile_record(usage=judged)
    assert record.usage.metric_judge_failures == 1


def test_compile_record_binds_the_proxy_ledger_to_the_grant_the_sandbox_admitted() -> None:
    """Re-points the deleted `proxy_identity_sha256`, which used to hold these two together.

    The launcher writes the grant it issued into the egress manifest and the sidecar writes it into the
    ledger. Two documents, one authority — so a record that embeds a ledger from some other grant is not
    a record of the compile the sandbox actually admitted.
    """

    record = _compile_record()

    assert record.sandbox.egress_manifest["proxy_grant_sha256"] == record.usage.grant_sha256
    other_grant = _usage(
        _proxy_call("task"),
        _proxy_call("reflection"),
        _proxy_call("metric_judge"),
        grant_sha256="0" * 64,
    )
    with pytest.raises(ValidationError, match="record_proxy_grant_mismatch"):
        _compile_record(usage=other_grant)
    with pytest.raises(ValidationError, match="record_proxy_grant_mismatch"):
        CompileRecordV1.model_validate(_resealed("sandbox", "egress_manifest", "proxy_grant_sha256", value="0" * 64))


def test_validate_compile_record_rejects_a_record_bound_to_another_candidate() -> None:
    """What the evaluator now checks in place of `validate_compile_receipt_chain_v3`."""

    record = _compile_record()
    identity = {
        "parent_program_sha256": _PARENT_PROGRAM_SHA256,
        "program_sha256": _PROGRAM_SHA256,
        "development_dataset_sha256": canonical_sha({"kind": "dataset", "payload": _dataset_payload()}),
        "target_runtime_manifest_sha256": _RUNTIME_MANIFEST_SHA256,
    }

    assert validate_compile_record(record, **identity) is record
    assert validate_compile_record(record.model_dump(mode="json"), **identity) == record
    for field in identity:
        with pytest.raises(ValueError, match="record_identity_mismatch"):
            validate_compile_record(record, **{**identity, field: "0" * 64})
    with pytest.raises(ValidationError, match="compile_record_hash_mismatch"):
        validate_compile_record(_tampered("run", "metric", "metric_version", value="forged"), **identity)
