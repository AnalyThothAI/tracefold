from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.news.test_news_program_gepa_real import compiler_development_corpus
from tracefold.news.artifact_identity import canonical_json, canonical_sha
from tracefold.news.learning.compiler.launcher import ProgramCompilerLauncher
from tracefold.news.learning.compiler.proxy import (
    CompilerModelProxyGrant,
    CompilerProviderEndpointSecret,
    CompilerProxySecretConfig,
)
from tracefold.news.learning.compiler.sandbox import CompilerSandboxPolicy
from tracefold.news.learning.compiler.security import (
    CompileBudgetV3,
    CompilerProxyTariff,
    CompilerRunnerReceiptsV3,
    seal_compile_input,
)
from tracefold.news.learning.compiler.source_identity import compiler_source_sha256, proxy_source_sha256
from tracefold.news.program.artifact import ProgramStrategyPatchV1, load_stable_program_artifact
from tracefold.news.program.runtime import PROGRAM_VERSION

ROOT = Path(__file__).resolve().parents[2]
PROVIDER_SCRIPT = Path(__file__).with_name("scripted_openai_provider.py")
pytestmark = [pytest.mark.integration, pytest.mark.compiler_smoke]


@pytest.fixture(scope="module")
def compiler_runtime() -> Iterator[tuple[str, str, str]]:
    docker = shutil.which("docker")
    if docker is None:
        message = "compiler smoke requires Docker"
        if os.environ.get("TRACEFOLD_TEST_EVIDENCE") == "1":
            pytest.fail(message, pytrace=False)
        pytest.skip(message + " (local convenience skip; not verification evidence)")
    available = subprocess.run((docker, "info"), capture_output=True, check=False, timeout=15)
    if available.returncode != 0:
        message = "compiler smoke requires a reachable Docker daemon"
        if os.environ.get("TRACEFOLD_TEST_EVIDENCE") == "1":
            pytest.fail(message, pytrace=False)
        pytest.skip(message + " (local convenience skip; not verification evidence)")

    revision = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=ROOT, capture_output=True, check=True, text=True
    ).stdout.strip()
    tag = f"tracefold-compiler-smoke:{revision[:12]}"
    subprocess.run(
        (
            docker,
            "build",
            "--target",
            "compiler",
            "--build-arg",
            f"TRACEFOLD_BUILD_REVISION={revision}",
            "--tag",
            tag,
            ".",
        ),
        cwd=ROOT,
        check=True,
        timeout=1_200,
    )
    image_id = subprocess.run(
        (docker, "image", "inspect", tag, "--format", "{{.Id}}"),
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    assert image_id.startswith("sha256:")

    provider = f"tracefold-compiler-smoke-provider-{uuid.uuid4().hex[:16]}"
    subprocess.run(
        (
            docker,
            "run",
            "-d",
            "--name",
            provider,
            "--network",
            "bridge",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            f"type=bind,src={PROVIDER_SCRIPT},dst=/scripted_openai_provider.py,readonly",
            "--entrypoint",
            "python",
            image_id,
            "/scripted_openai_provider.py",
        ),
        capture_output=True,
        check=True,
        timeout=30,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            logs = subprocess.run(
                (docker, "logs", provider), capture_output=True, check=False, text=True, timeout=5
            ).stdout
            if "scripted-provider-ready" in logs:
                break
            time.sleep(0.05)
        else:
            pytest.fail("scripted compiler provider did not become ready", pytrace=False)
        yield docker, image_id, provider
    finally:
        subprocess.run((docker, "rm", "--force", provider), capture_output=True, check=False, timeout=30)


def _docker_wrapper(tmp_path: Path, *, docker: str, provider: str) -> Path:
    state = tmp_path / "launcher-network"
    wrapper = tmp_path / "docker"
    wrapper.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                "set -eu",
                f"real={shlex.quote(docker)}",
                f"provider={shlex.quote(provider)}",
                f"state={shlex.quote(str(state))}",
                'if [ "$1" = network ] && [ "$2" = create ]; then',
                '  "$real" "$@"',
                '  eval "network=\\${$#}"',
                '  printf "%s" "$network" > "$state"',
                "  exit 0",
                "fi",
                'if [ "$1" = volume ] && [ "$2" = create ] && [ -f "$state" ]; then',
                '  network=$(sed -n "1p" "$state")',
                '  "$real" network connect "$network" "$provider" >/dev/null',
                "fi",
                'if [ "$1" = network ] && [ "$2" = rm ]; then',
                "  network=$3",
                '  "$real" network disconnect --force "$network" "$provider" >/dev/null 2>&1 || true',
                "fi",
                'exec "$real" "$@"',
                "",
            )
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return wrapper


def _secrets(provider: str) -> CompilerProxySecretConfig:
    endpoint = f"http://{provider}:8080/v1"
    tariff = CompilerProxyTariff(
        tariff_id="compiler-smoke-v2",
        input_token_overhead=1_000,
        task_input_microusd_per_million=1,
        task_output_microusd_per_million=1,
        reflection_input_microusd_per_million=1,
        reflection_output_microusd_per_million=1,
        metric_judge_input_microusd_per_million=1,
        metric_judge_output_microusd_per_million=1,
    )
    return CompilerProxySecretConfig(
        task=CompilerProviderEndpointSecret(
            model="openai/scripted-task",
            api_base=endpoint,
            api_key="compiler-smoke-key",
            timeout=20,
            max_tokens=1_200,
            temperature=0,
        ),
        reflection=CompilerProviderEndpointSecret(
            model="openai/scripted-reflection",
            api_base=endpoint,
            api_key="compiler-smoke-key",
            timeout=300,
            max_tokens=32_000,
            temperature=1,
        ),
        metric_judge=CompilerProviderEndpointSecret(
            model="openai/scripted-judge",
            api_base=endpoint,
            api_key="compiler-smoke-key",
            timeout=120,
            max_tokens=4_096,
            temperature=0,
        ),
        tariff=tariff,
    )


def _sealed_input(
    *, image_id: str, secrets: CompilerProxySecretConfig, policy: CompilerSandboxPolicy
) -> tuple[str, str]:
    episodes = compiler_development_corpus()
    parent = load_stable_program_artifact()
    stable_bundle = canonical_sha({"kind": "compiler_smoke_stable_bundle"})
    runtime_manifest = canonical_sha({"kind": "compiler_smoke_runtime_manifest"})
    cases = [
        {
            "case_id": episode.case_id,
            "cluster_id": episode.cluster_id,
            "evidence_sha256": "a" * 64,
            "review_id": f"compiler-smoke-review-{index}",
        }
        for index, episode in enumerate(episodes)
    ]
    dataset_payload = {
        "role": "development",
        "learning_epoch": "program_v7",
        "learning_epoch_started_at_ms": 1_800_000_000_000,
        "agent_cohort": {
            "bundle_sha": stable_bundle,
            "learning_epoch": "program_v7",
            "program_version": PROGRAM_VERSION,
            "program_sha256": parent.program_sha256,
            "runtime_model_bindings_sha256": runtime_manifest,
        },
        "cases": cases,
    }
    dataset_sha = canonical_sha({"kind": "dataset", "payload": dataset_payload})
    task = secrets.task.binding("task")
    reflection = secrets.reflection.binding("reflection")
    judge = secrets.metric_judge.binding("metric_judge")
    grant = CompilerModelProxyGrant.issue(
        task=task,
        reflection=reflection,
        metric_judge=judge,
        max_task_model_calls=400,
        max_reflection_model_calls=40,
        max_metric_judge_model_calls=400,
        max_cost_microusd=400_000,
        tariff=secrets.tariff,
        proxy_config_sha256=secrets.secret_free_config_sha256,
        proxy_source_sha256=proxy_source_sha256(),
    )
    budget = CompileBudgetV3(
        max_metric_calls=40,
        max_task_model_calls=400,
        max_reflection_model_calls=40,
        max_metric_judge_model_calls=400,
        max_cost_microusd=400_000,
        max_call_cost_microusd=grant.max_call_cost_microusd,
        seed=190,
    )
    bundle = seal_compile_input(
        dataset_sha=dataset_sha,
        dataset_payload=dataset_payload,
        episodes=episodes,
        review_rubric_version="news_review_v4",
        parent_program_sha256=parent.program_sha256,
        stable_bundle_sha256=stable_bundle,
        target_runtime_manifest_sha256=runtime_manifest,
        task=task,
        reflection=reflection,
        metric_judge=judge,
        proxy_grant_sha256=grant.grant_sha256,
        proxy_config_sha256=secrets.secret_free_config_sha256,
        tariff_sha256=secrets.tariff_sha256,
        proxy_tariff=secrets.tariff,
        compiler_source_sha256=compiler_source_sha256(),
        proxy_source_sha256=proxy_source_sha256(),
        compiler_lock_sha256=hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        sandbox_policy_sha256=policy.policy_sha256,
        compiler_image_digest=image_id,
        budget=budget,
    )
    return canonical_json(bundle.model_dump(mode="json")), bundle.bundle_sha256


def _assert_every_runner_call_has_a_proxy_leaf(
    runner: CompilerRunnerReceiptsV3, *, proxy_task: int, proxy_reflection: int, proxy_judge: int
) -> None:
    if (
        runner.task_model_calls,
        runner.reflection_model_calls,
        runner.metric_judge_model_calls,
    ) != (proxy_task, proxy_reflection, proxy_judge):
        raise ValueError("compiler smoke found an unrecorded model request")


def _sandbox_policy() -> CompilerSandboxPolicy:
    values = CompilerSandboxPolicy.issue(wall_timeout_seconds=300, max_cpu_seconds=240).model_dump(
        mode="json", exclude={"policy_sha256"}
    )
    values.update(max_stdout_bytes=1_048_576, max_stderr_bytes=1_048_576)
    return CompilerSandboxPolicy(**values, policy_sha256=canonical_sha(values))


def test_production_launcher_runs_real_runner_proxy_and_typed_outputs(
    compiler_runtime: tuple[str, str, str], tmp_path: Path
) -> None:
    docker, image_id, provider = compiler_runtime
    policy = _sandbox_policy()
    secrets = _secrets(provider)
    input_document, input_sha = _sealed_input(image_id=image_id, secrets=secrets, policy=policy)
    launcher = ProgramCompilerLauncher(
        policy=policy,
        compiler_source_sha256=compiler_source_sha256(),
        proxy_source_sha256=proxy_source_sha256(),
        compiler_lock_sha256=hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        compiler_image=image_id,
        docker_executable=str(_docker_wrapper(tmp_path, docker=docker, provider=provider)),
    )

    result = launcher.launch(
        input_document=input_document,
        input_bundle_sha256=input_sha,
        proxy_secret_config=secrets,
    )
    patch = ProgramStrategyPatchV1.model_validate_json(result.patch_document)
    runner = CompilerRunnerReceiptsV3.model_validate_json(result.runner_receipts_document)
    proxy = result.proxy_execution_receipt

    assert patch.parent_program_sha256 == load_stable_program_artifact().program_sha256
    # The optimizer's whole write-set is these two advisories; a run that changed neither compiled nothing.
    assert patch.event_semantics_instruction.strip() or patch.reader_card_instruction.strip()
    assert result.launch_receipt.termination == "succeeded"
    assert result.launch_receipt.compiler_container_removed
    assert result.launch_receipt.proxy_container_removed
    assert result.launch_receipt.socket_volume_removed
    assert result.launch_receipt.egress_network_removed
    assert result.launch_receipt.boundary_actuals_available
    assert proxy.calls and all(call.provider_invoked for call in proxy.calls)
    _assert_every_runner_call_has_a_proxy_leaf(
        runner,
        proxy_task=proxy.task_model_calls,
        proxy_reflection=proxy.reflection_model_calls,
        proxy_judge=proxy.metric_judge_model_calls,
    )

    tampered = runner.model_dump(mode="json")
    tampered["task_model_calls"] += 1
    with pytest.raises(ValidationError, match="runner_receipt_hash_mismatch"):
        CompilerRunnerReceiptsV3.model_validate(tampered)
    forged = runner.model_copy(update={"task_model_calls": runner.task_model_calls + 1})
    with pytest.raises(ValueError, match="unrecorded model request"):
        _assert_every_runner_call_has_a_proxy_leaf(
            forged,
            proxy_task=proxy.task_model_calls,
            proxy_reflection=proxy.reflection_model_calls,
            proxy_judge=proxy.metric_judge_model_calls,
        )
