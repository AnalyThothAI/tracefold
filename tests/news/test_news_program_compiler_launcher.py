from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

from tracefold.app.cli.parser import build_parser
from tracefold.news.agents import program_compiler_launcher as launcher_module
from tracefold.news.agents.program_compiler_launcher import (
    PROXY_MODULE,
    RUNNER_MODULE,
    ProgramCompilerLauncher,
)
from tracefold.news.agents.program_compiler_proxy import (
    CompilerModelProxyGrant,
    CompilerProviderEndpointSecret,
    CompilerProxySecretConfig,
)
from tracefold.news.agents.program_compiler_runner import _build_compiler_proxy_runtime
from tracefold.news.agents.program_compiler_sandbox import CompilerSandboxPolicy
from tracefold.news.agents.program_compiler_security import (
    CompileBudgetV3,
    CompilerProxyTariff,
    seal_compile_input,
)
from tracefold.news.artifact_identity import canonical_json, canonical_sha


def _secret_config() -> CompilerProxySecretConfig:
    return CompilerProxySecretConfig(
        task=CompilerProviderEndpointSecret(
            model="provider/task",
            api_base="https://task.example/v1",
            api_key="private-key",
            timeout=20,
            max_tokens=512,
            temperature=0,
        ),
        reflection=CompilerProviderEndpointSecret(
            model="provider/reflection",
            api_base="https://reflection.example/v1",
            api_key="private-key",
            timeout=300,
            max_tokens=32_000,
            temperature=1,
        ),
        metric_judge=CompilerProviderEndpointSecret(
            model="provider/judge",
            api_base="https://judge.example/v1",
            api_key="private-key",
            timeout=120,
            max_tokens=4_096,
            temperature=0,
        ),
        tariff=_tariff(),
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


def _input(
    *,
    max_task_model_calls: int = 8,
    max_reflection_model_calls: int = 4,
    max_metric_judge_model_calls: int = 6,
) -> tuple[str, str, CompilerProxySecretConfig]:
    secrets = _secret_config()
    payload = {
        "role": "development",
        "learning_epoch": "program_v6",
        "learning_epoch_started_at_ms": 1_800_000_000_000,
        "agent_cohort": {
            "bundle_sha": "a" * 64,
            "learning_epoch": "program_v6",
            "program_version": "news_semantic_program_v4",
            "program_sha256": "6" * 64,
            "runtime_model_bindings_sha256": "8" * 64,
        },
        "cases": [{"case_id": "case-1", "cluster_id": "cluster-1"}],
    }
    task = secrets.task.binding("task")
    reflection = secrets.reflection.binding("reflection")
    metric_judge = secrets.metric_judge.binding("metric_judge")
    grant = CompilerModelProxyGrant.issue(
        task=task,
        reflection=reflection,
        metric_judge=metric_judge,
        max_task_model_calls=max_task_model_calls,
        max_reflection_model_calls=max_reflection_model_calls,
        max_metric_judge_model_calls=max_metric_judge_model_calls,
        max_cost_microusd=100,
        tariff=secrets.tariff,
        proxy_config_sha256=secrets.secret_free_config_sha256,
        proxy_source_sha256="4" * 64,
    )
    bundle = seal_compile_input(
        dataset_sha=canonical_sha({"kind": "dataset", "payload": payload}),
        dataset_payload=payload,
        episodes=({"case_id": "case-1", "cluster_id": "cluster-1"},),
        review_rubric_version="news_review_v4",
        parent_program_sha256="6" * 64,
        parent_state_sha256="7" * 64,
        stable_bundle_sha256="a" * 64,
        target_runtime_manifest_sha256="8" * 64,
        eligible_demo_bank_root_sha256="9" * 64,
        task=task,
        reflection=reflection,
        metric_judge=metric_judge,
        proxy_grant_sha256=grant.grant_sha256,
        proxy_config_sha256=secrets.secret_free_config_sha256,
        tariff_sha256=secrets.tariff_sha256,
        proxy_tariff=secrets.tariff,
        compiler_source_sha256="1" * 64,
        proxy_source_sha256="4" * 64,
        compiler_lock_sha256="2" * 64,
        sandbox_policy_sha256=CompilerSandboxPolicy.issue(wall_timeout_seconds=60).policy_sha256,
        compiler_image_digest="sha256:" + "3" * 64,
        budget=CompileBudgetV3(
            max_metric_calls=3,
            max_task_model_calls=max_task_model_calls,
            max_reflection_model_calls=max_reflection_model_calls,
            max_metric_judge_model_calls=max_metric_judge_model_calls,
            max_cost_microusd=100,
            max_call_cost_microusd=grant.max_call_cost_microusd,
            seed=17,
        ),
    )
    return canonical_json(bundle.model_dump(mode="json")), bundle.bundle_sha256, secrets


def _launcher(tmp_path: Path) -> ProgramCompilerLauncher:
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    docker.chmod(0o700)
    return ProgramCompilerLauncher(
        policy=CompilerSandboxPolicy.issue(wall_timeout_seconds=60),
        compiler_source_sha256="1" * 64,
        compiler_lock_sha256="2" * 64,
        compiler_image="sha256:" + "3" * 64,
        proxy_source_sha256="4" * 64,
        docker_executable=str(docker),
    )


def test_commands_use_named_socket_volume_and_keep_proxy_and_runner_mounts_disjoint(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    paths = launcher_module._StagingPaths(tmp_path)
    compiler = launcher._compiler_command(
        paths=paths,
        volume_name="tracefold-compiler-test-socket",
        container_name="tracefold-compiler-test-runner",
    )
    proxy = launcher._proxy_command(
        paths=paths,
        volume_name="tracefold-compiler-test-socket",
        network_name="tracefold-compiler-test-egress",
        container_name="tracefold-compiler-test-proxy",
    )

    compiler_text = " ".join(compiler)
    proxy_text = " ".join(proxy)
    assert "--network none" in compiler_text
    assert "type=volume,src=tracefold-compiler-test-socket" in compiler_text
    assert "dst=/run/tracefold/proxy,readonly" in compiler_text
    assert "/run/tracefold/secrets" not in compiler_text
    assert "/run/tracefold/proxy-receipt" not in compiler_text
    assert RUNNER_MODULE in compiler_text
    assert "--network tracefold-compiler-test-egress" in proxy_text
    assert "/run/tracefold/secrets" in proxy_text
    assert "/run/tracefold/input" not in proxy_text
    assert "/run/tracefold/output" not in proxy_text
    assert PROXY_MODULE in proxy_text
    assert "/var/run/docker.sock" not in compiler_text + proxy_text


def test_launch_rejects_mismatched_secret_binding_before_docker(tmp_path: Path) -> None:
    document, bundle_sha, original = _input()
    other = CompilerProviderEndpointSecret(
        model="provider/task",
        api_base="https://other.example/v1",
        api_key="private-key",
        timeout=20,
        max_tokens=512,
        temperature=0,
    )
    secrets = CompilerProxySecretConfig(
        task=other,
        reflection=original.reflection,
        metric_judge=original.metric_judge,
        tariff=_tariff(),
    )
    with pytest.raises(ValueError, match="proxy_binding_mismatch"):
        _launcher(tmp_path).launch(
            input_document=document,
            input_bundle_sha256=bundle_sha,
            proxy_secret_config=secrets,
        )


def test_cli_bundle_launcher_and_runner_keep_all_three_role_bindings_end_to_end(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "news",
            "learning",
            "compile",
            "--development",
            "d" * 64,
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--out",
            str(tmp_path / "compile.json"),
            "--compiler-image",
            "sha256:" + "3" * 64,
            "--max-metric-calls",
            "3",
            "--max-task-model-calls",
            "7",
            "--max-reflection-model-calls",
            "5",
            "--max-metric-judge-model-calls",
            "11",
            "--max-cost-microusd",
            "100",
        ]
    )
    document, bundle_sha, _ = _input(
        max_task_model_calls=args.max_task_model_calls,
        max_reflection_model_calls=args.max_reflection_model_calls,
        max_metric_judge_model_calls=args.max_metric_judge_model_calls,
    )
    canonical_input, bundle = launcher_module._validated_input_document(document, expected_sha256=bundle_sha)
    assert canonical_input == document

    with tempfile.TemporaryDirectory(prefix="tf-runner-", dir="/tmp") as root:
        proxy_path = Path(root) / "p.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(proxy_path))
            grant, task_lm, reflection_lm, judge = _build_compiler_proxy_runtime(bundle, proxy_path)

    assert (
        grant.max_task_model_calls,
        grant.max_reflection_model_calls,
        grant.max_metric_judge_model_calls,
    ) == (7, 5, 11)
    assert task_lm.tracefold_compiler_role_binding == bundle.task
    assert reflection_lm.tracefold_compiler_role_binding == bundle.reflection
    assert judge.identity["execution"]["role_binding"] == bundle.metric_judge.model_dump(mode="json")
    assert judge.identity["execution"]["max_model_calls"] == 11


def test_launcher_accepts_only_content_addressed_image_references(tmp_path: Path) -> None:
    _launcher(tmp_path)
    policy = CompilerSandboxPolicy.issue(wall_timeout_seconds=60)
    kwargs = {
        "policy": policy,
        "compiler_source_sha256": "1" * 64,
        "compiler_lock_sha256": "2" * 64,
        "proxy_source_sha256": "4" * 64,
        "docker_executable": str(tmp_path / "docker"),
    }
    with pytest.raises(ValueError, match="compiler_image_unpinned"):
        ProgramCompilerLauncher(**kwargs, compiler_image="tracefold-compiler:latest")


def test_missing_cidfile_cleanup_uses_the_random_exact_container_name(monkeypatch: Any) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_best_effort(*command: str) -> bool:
        calls.append(command)
        return True

    monkeypatch.setattr(launcher_module, "_docker_best_effort", fake_best_effort)
    assert launcher_module._remove_exact_container(
        "/docker",
        None,
        fallback_name="tracefold-compiler-exact-random-runner",
        attempted=True,
    )
    assert calls == [("/docker", "rm", "--force", "tracefold-compiler-exact-random-runner")]


@pytest.mark.integration
def test_docker_desktop_named_volume_unix_socket_network_none_and_cleanup(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker unavailable")
    image = subprocess.run(
        (docker, "image", "inspect", "python:3.13-slim", "--format", "{{.Id}}"),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not image.startswith("sha256:"):
        pytest.skip("local python:3.13-slim image unavailable")
    suffix = uuid.uuid4().hex[:20]
    volume = f"tracefold-compiler-it-{suffix}-socket"
    network = f"tracefold-compiler-it-{suffix}-egress"
    sidecar = f"tracefold-compiler-it-{suffix}-proxy"
    runner = f"tracefold-compiler-it-{suffix}-runner"
    receipt = tmp_path / "receipt"
    receipt.mkdir()
    sidecar_cid: str | None = None
    runner_cid: str | None = None
    try:
        subprocess.run((docker, "network", "create", network), check=True, capture_output=True)
        subprocess.run((docker, "volume", "create", volume), check=True, capture_output=True)
        subprocess.run(
            (
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--mount",
                f"type=volume,src={volume},dst=/v",
                "--entrypoint",
                "python",
                image,
                "-c",
                "import os;os.chmod('/v',0o733)",
            ),
            check=True,
            capture_output=True,
        )
        server = (
            "import os,socket;"
            "p='/v/compiler.sock';s=socket.socket(socket.AF_UNIX);s.bind(p);os.chmod(p,0o600);"
            "open('/receipt/ready','w').write('ready');s.listen(1);c,_=s.accept();"
            "c.sendall(c.recv(4));c.close();s.close()"
        )
        sidecar_cid = subprocess.run(
            (
                docker,
                "run",
                "-d",
                "--name",
                sidecar,
                "--network",
                network,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "32",
                "--memory",
                "1073741824",
                "--memory-swap",
                "1073741824",
                "--cpus",
                "1.0",
                "--ipc",
                "none",
                "--log-driver",
                "none",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--mount",
                f"type=volume,src={volume},dst=/v",
                "--mount",
                f"type=bind,src={receipt},dst=/receipt",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=67108864",  # noqa: S108
                "--tmpfs",
                "/run/tracefold/home:rw,noexec,nosuid,nodev,size=1048576",
                "--entrypoint",
                "python",
                image,
                "-c",
                server,
            ),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        for _ in range(100):
            if (receipt / "ready").exists():
                break
            import time

            time.sleep(0.05)
        assert (receipt / "ready").exists()
        proxy_boundary = launcher_module._docker_container_boundary_payload(
            docker,
            sidecar_cid,
            expected_name=sidecar,
            expected_image=image,
            expected_network=network,
            expected_pids_limit=32,
            expected_memory_bytes=1_073_741_824,
            expected_ulimits={},
            expected_mounts={
                "/receipt": ("bind", True, str(receipt)),
                "/v": ("volume", True, volume),
            },
        )
        assert proxy_boundary["readonly_rootfs"] is True
        assert proxy_boundary["pids_limit"] == 32
        client = (
            "import socket;"
            "s=socket.socket(socket.AF_UNIX);s.connect('/v/compiler.sock');s.sendall(b'ping');"
            "assert s.recv(4)==b'ping';s.close();"
            "n=socket.socket(socket.AF_INET);n.settimeout(.2);"
            "\ntry:n.connect(('1.1.1.1',443));raise SystemExit(9)\nexcept OSError:pass"
        )
        subprocess.run(
            (
                docker,
                "run",
                "--name",
                runner,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "1",
                "--memory",
                "134217728",
                "--memory-swap",
                "134217728",
                "--cpus",
                "1.0",
                "--ulimit",
                "nofile=64:64",
                "--ulimit",
                "fsize=1048576:1048576",
                "--ulimit",
                "cpu=60:60",
                "--ipc",
                "none",
                "--log-driver",
                "none",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--mount",
                f"type=volume,src={volume},dst=/v,readonly",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=67108864",  # noqa: S108
                "--tmpfs",
                "/run/tracefold/home:rw,noexec,nosuid,nodev,size=1048576",
                "--entrypoint",
                "python",
                image,
                "-c",
                client,
            ),
            check=True,
            capture_output=True,
        )
        runner_cid = subprocess.run(
            (docker, "container", "inspect", runner, "--format", "{{.Id}}"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        runner_boundary = launcher_module._docker_container_boundary_payload(
            docker,
            runner_cid,
            expected_name=runner,
            expected_image=image,
            expected_network="none",
            expected_pids_limit=1,
            expected_memory_bytes=134_217_728,
            expected_ulimits={
                "cpu": (60, 60),
                "fsize": (1_048_576, 1_048_576),
                "nofile": (64, 64),
            },
            expected_mounts={"/v": ("volume", False, volume)},
        )
        assert runner_boundary["network_sha256"] == canonical_sha({"network": "none"})
        assert runner_boundary["ulimits"]["cpu"] == [60, 60]
    finally:
        subprocess.run(
            (docker, "rm", "--force", runner_cid or runner),
            check=False,
            capture_output=True,
        )
        if sidecar_cid:
            subprocess.run((docker, "rm", "--force", sidecar_cid), check=False, capture_output=True)
        else:
            subprocess.run((docker, "rm", "--force", sidecar), check=False, capture_output=True)
        subprocess.run((docker, "volume", "rm", volume), check=False, capture_output=True)
        subprocess.run((docker, "network", "rm", network), check=False, capture_output=True)
    assert subprocess.run((docker, "volume", "inspect", volume), check=False, capture_output=True).returncode != 0
    assert subprocess.run((docker, "network", "inspect", network), check=False, capture_output=True).returncode != 0
