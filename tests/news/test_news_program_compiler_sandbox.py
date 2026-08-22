from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracefold.news.agents.program_compiler_sandbox import (
    CompilerSandboxLaunchReceipt,
    CompilerSandboxPolicy,
    environment_manifest_sha256,
    scrubbed_compiler_environment,
    validate_compiler_environment,
    verify_sandbox_output_directory,
)
from tracefold.news.artifact_identity import canonical_sha


def _valid_sandbox_launch_receipt() -> tuple[CompilerSandboxPolicy, CompilerSandboxLaunchReceipt]:
    policy = CompilerSandboxPolicy.issue(
        wall_timeout_seconds=60,
        max_output_bytes=20_000,
        max_rss_bytes=1_000_000_000,
    )
    image = "sha256:" + "4" * 64
    volume_name = "tracefold-compiler-test-socket"
    network_name = "tracefold-compiler-test-egress"
    network_sha = canonical_sha({"name": network_name})
    user = "501:20"
    image_preflight_payload = {
        "schema": "tracefold.news.compiler_image_preflight.v2",
        "image_id": image,
        "compiler_source_sha256": "2" * 64,
        "proxy_source_sha256": "5" * 64,
        "compiler_lock_sha256": "3" * 64,
        "image_code_executed": False,
        "provider_config_mounted": False,
        "network": "none",
        "pull_policy": "never",
    }
    socket_volume_payload = {
        "schema": "tracefold.news.compiler_socket_volume.v2",
        "name": volume_name,
    }
    fixed_options = [
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
    ]
    command_payload = {
        "schema": "tracefold.news.compiler_boundary_commands.v2",
        "volume_init": [
            "docker",
            "run",
            "--network",
            "none",
            *fixed_options,
            "--pids-limit",
            "1",
            image,
        ],
        "proxy": [
            "docker",
            "run",
            "--network",
            network_name,
            *fixed_options,
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
            user,
            image,
        ],
        "compiler": [
            "docker",
            "run",
            "--network",
            "none",
            *fixed_options,
            "--pids-limit",
            "1",
            "--memory",
            "1000000000",
            "--memory-swap",
            "1000000000",
            "--cpus",
            "1.0",
            "--ulimit",
            "nofile=64:64",
            "--ulimit",
            "fsize=262144:262144",
            "--ulimit",
            "cpu=600:600",
            "--ipc",
            "none",
            "--log-driver",
            "none",
            "--user",
            user,
            image,
        ],
    }
    tmpfs = {
        "/tmp": ["nodev", "noexec", "nosuid", "rw", "size=67108864"],  # noqa: S108
        "/run/tracefold/home": ["nodev", "noexec", "nosuid", "rw", "size=1048576"],
    }
    volume_source_sha = canonical_sha({"source": volume_name})

    def mount(destination: str, kind: str, writable: bool, source_sha: str) -> dict[str, object]:
        return {
            "destination": destination,
            "type": kind,
            "writable": writable,
            "source_sha256": source_sha,
        }

    def container(
        *,
        name: str,
        network_sha256: str,
        pids: int,
        memory: int,
        ulimits: dict[str, list[int]],
        mounts: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "kind": "container",
            "container_id_sha256": canonical_sha({"id": name}),
            "name_sha256": canonical_sha({"name": name}),
            "image": image,
            "network_sha256": network_sha256,
            "readonly_rootfs": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": pids,
            "user": user,
            "environment_sha256": "e" * 64,
            "ipc_mode": "none",
            "log_config": {"Type": "none", "Config": {}},
            "tmpfs": tmpfs,
            "memory": memory,
            "memory_swap": memory,
            "nano_cpus": 1_000_000_000,
            "ulimits": ulimits,
            "mounts": mounts,
        }

    inspect_payload = {
        "schema": "tracefold.news.compiler_boundary_inspections.v2",
        "image_preflight": image_preflight_payload,
        "network": {
            "kind": "network",
            "name_sha256": network_sha,
            "daemon_identity_sha256": "a" * 64,
            "driver": "bridge",
            "scope": "local",
        },
        "volume": {
            "kind": "volume",
            "name_sha256": canonical_sha({"name": volume_name}),
            "daemon_identity_sha256": "b" * 64,
            "driver": "local",
            "scope": "local",
        },
        "proxy": container(
            name="proxy",
            network_sha256=network_sha,
            pids=32,
            memory=1_073_741_824,
            ulimits={},
            mounts=[
                mount("/run/tracefold/config", "bind", False, "1" * 64),
                mount("/run/tracefold/secrets", "bind", False, "2" * 64),
                mount("/run/tracefold/proxy-receipt", "bind", True, "3" * 64),
                mount("/run/tracefold/proxy", "volume", True, volume_source_sha),
            ],
        ),
        "compiler": container(
            name="compiler",
            network_sha256=canonical_sha({"network": "none"}),
            pids=1,
            memory=1_000_000_000,
            ulimits={"cpu": [600, 600], "fsize": [262_144, 262_144], "nofile": [64, 64]},
            mounts=[
                mount("/run/tracefold/input", "bind", False, "6" * 64),
                mount("/run/tracefold/output", "bind", True, "7" * 64),
                mount("/run/tracefold/proxy", "volume", False, volume_source_sha),
            ],
        ),
    }
    environment_payload = {
        "schema": "tracefold.news.compiler_container_environment.v2",
        "environment_keys": ["HOME", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8", "TMPDIR"],
        "ambient_forwarded": False,
        "image_environment_cleared_before_package_import": True,
    }
    mount_payload = {
        "schema": "tracefold.news.compiler_mount_manifest.v2",
        "input_bundle_sha256": "1" * 64,
        "compiler_input_read_only": True,
        "compiler_output_private": True,
        "compiler_socket_volume_read_only": True,
        "proxy_socket_volume_read_write": True,
        "proxy_receipt_private": True,
        "proxy_corpus_mounted": False,
        "proxy_compiler_output_mounted": False,
        "provider_config_file_mode": "0400",
        "provider_config_mount_read_only": True,
        "holdout_mounted": False,
        "repository_mounted": False,
        "docker_socket_mounted": False,
        "host_home_mounted": False,
    }
    egress_payload = {
        "schema": "tracefold.news.compiler_egress_manifest.v2",
        "compiler_network": "none",
        "proxy_network_sha256": network_sha,
        "proxy_grant_sha256": "5" * 64,
        "compiler_other_network_allowed": False,
        "trusted_proxy_egress": True,
    }
    lifecycle_payload = {
        "schema": "tracefold.news.compiler_container_lifecycle.v2",
        "compiler_container_sha256": "c" * 64,
        "proxy_container_sha256": "d" * 64,
        "socket_volume_sha256": canonical_sha(socket_volume_payload),
        "egress_network_sha256": network_sha,
        "compiler_container_removed": True,
        "proxy_container_removed": True,
        "init_container_removed": True,
        "socket_volume_removed": True,
        "egress_network_removed": True,
    }
    receipt = CompilerSandboxLaunchReceipt.issue(
        policy_payload=policy.model_dump(mode="json"),
        policy_sha256=policy.policy_sha256,
        input_bundle_sha256="1" * 64,
        compiler_source_sha256="2" * 64,
        compiler_lock_sha256="3" * 64,
        image_preflight_payload=image_preflight_payload,
        image_preflight_sha256=canonical_sha(image_preflight_payload),
        compiler_image_digest=image,
        proxy_image_digest=image,
        proxy_source_sha256="5" * 64,
        proxy_identity_sha256="5" * 64,
        proxy_config_sha256="6" * 64,
        proxy_tariff_sha256="7" * 64,
        proxy_ready_receipt_sha256="8" * 64,
        proxy_execution_receipt_sha256="9" * 64,
        socket_volume_payload=socket_volume_payload,
        socket_volume_sha256=canonical_sha(socket_volume_payload),
        lifecycle_payload=lifecycle_payload,
        lifecycle_sha256=canonical_sha(lifecycle_payload),
        boundary_command_payload=command_payload,
        boundary_command_root_sha256=canonical_sha(command_payload),
        boundary_inspect_payload=inspect_payload,
        boundary_inspect_root_sha256=canonical_sha(inspect_payload),
        boundary_actuals_available=True,
        environment_payload=environment_payload,
        environment_sha256=canonical_sha(environment_payload),
        mount_manifest_payload=mount_payload,
        mount_manifest_sha256=canonical_sha(mount_payload),
        egress_manifest_payload=egress_payload,
        egress_manifest_sha256=canonical_sha(egress_payload),
        holdout_mounted=False,
        db_credentials_present=False,
        ambient_credentials_present=False,
        child_process_allowed=False,
        compiler_container_removed=True,
        proxy_container_removed=True,
        init_container_removed=True,
        socket_volume_removed=True,
        egress_network_removed=True,
        termination="succeeded",
        exit_code=0,
        wall_time_ms=12,
        launcher_cpu_time_ms=8,
        launcher_max_rss_bytes=10_000_000,
        container_actuals_available=False,
        container_max_cpu_seconds=600,
        container_max_rss_bytes=1_000_000_000,
        output_bytes=500,
        output_file_count=2,
        stdout_sha256="9" * 64,
        stderr_sha256="a" * 64,
        output_root_sha256="b" * 64,
    )

    return policy, receipt


def test_compiler_sandbox_policy_and_launch_receipt_are_content_addressed() -> None:
    policy, receipt = _valid_sandbox_launch_receipt()

    assert CompilerSandboxPolicy.model_validate(policy.model_dump(mode="json")) == policy
    assert CompilerSandboxLaunchReceipt.model_validate(receipt.model_dump(mode="json")) == receipt

    tampered = receipt.model_dump(mode="json")
    tampered["exit_code"] = 7
    with pytest.raises(ValidationError, match="launch_hash_mismatch"):
        CompilerSandboxLaunchReceipt.model_validate(tampered)

    tampered = receipt.model_dump(mode="json")
    tampered["boundary_inspect_payload"]["compiler"]["pids_limit"] = 2
    tampered["launch_receipt_sha256"] = canonical_sha(
        {key: value for key, value in tampered.items() if key != "launch_receipt_sha256"}
    )
    with pytest.raises(ValidationError, match="boundary_preimage_hash_mismatch"):
        CompilerSandboxLaunchReceipt.model_validate(tampered)

    semantically_tampered = receipt.model_dump(mode="json")
    semantically_tampered["boundary_inspect_payload"]["compiler"]["pids_limit"] = 2
    semantically_tampered["boundary_inspect_root_sha256"] = canonical_sha(
        semantically_tampered["boundary_inspect_payload"]
    )
    semantically_tampered["launch_receipt_sha256"] = canonical_sha(
        {key: value for key, value in semantically_tampered.items() if key != "launch_receipt_sha256"}
    )
    with pytest.raises(ValidationError, match="boundary_semantics_invalid"):
        CompilerSandboxLaunchReceipt.model_validate(semantically_tampered)

    failed = receipt.model_dump(mode="json")
    failed["termination"] = "failed"
    failed["exit_code"] = 1
    failed["output_root_sha256"] = None
    failed["boundary_actuals_available"] = False
    failed["boundary_inspect_payload"]["compiler"] = None
    failed["boundary_inspect_root_sha256"] = canonical_sha(failed["boundary_inspect_payload"])
    failed["launch_receipt_sha256"] = canonical_sha(
        {key: value for key, value in failed.items() if key != "launch_receipt_sha256"}
    )
    parsed_failure = CompilerSandboxLaunchReceipt.model_validate(failed)
    assert parsed_failure.boundary_actuals_available is False

    false_claim = dict(failed)
    false_claim["boundary_actuals_available"] = True
    false_claim["launch_receipt_sha256"] = canonical_sha(
        {key: value for key, value in false_claim.items() if key != "launch_receipt_sha256"}
    )
    with pytest.raises(ValidationError, match="boundary_actuals_claim_invalid"):
        CompilerSandboxLaunchReceipt.model_validate(false_claim)


def test_scrubbed_compiler_environment_never_copies_parent_credentials(tmp_path: Path) -> None:
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    home.mkdir()
    temporary.mkdir()
    os.environ["OPENAI_API_KEY"] = "ambient-must-not-cross-boundary"
    os.environ["DATABASE_URL"] = "postgresql://ambient-must-not-cross-boundary"
    try:
        environment = scrubbed_compiler_environment(sandbox_home=home, sandbox_tmp=temporary)
    finally:
        os.environ.pop("OPENAI_API_KEY")
        os.environ.pop("DATABASE_URL")

    assert set(environment) == {"HOME", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8", "TMPDIR"}
    assert "ambient-must-not-cross-boundary" not in repr(environment)
    assert len(environment_manifest_sha256(environment)) == 64

    with pytest.raises(ValueError, match="environment_keys_invalid"):
        validate_compiler_environment(
            {**environment, "PGPASSWORD": "unsafe"},
            sandbox_home=home,
            sandbox_tmp=temporary,
        )


def test_python_child_guard_denies_ambient_files_writes_network_and_descendants(tmp_path: Path) -> None:
    sandbox_home = tmp_path / "home"
    sandbox_tmp = tmp_path / "tmp"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    proxy_root = tmp_path / "proxy"
    ambient_root = tmp_path / "ambient"
    for directory in (sandbox_home, sandbox_tmp, input_root, output_root, proxy_root, ambient_root):
        directory.mkdir()
    (input_root / "bundle.json").write_text("{}", encoding="utf-8")
    ambient_file = ambient_root / "holdout.json"
    ambient_file.write_text("must-not-read", encoding="utf-8")
    proxy_socket = proxy_root / "compiler.sock"

    script = f"""
import json
import socket
import subprocess
import sys
from pathlib import Path
from tracefold.news.agents.program_compiler_sandbox import install_compiler_sandbox_guards

input_root = Path({str(input_root)!r})
output_root = Path({str(output_root)!r})
ambient_file = Path({str(ambient_file)!r})
proxy_socket = Path({str(proxy_socket)!r})
install_compiler_sandbox_guards(
    readonly_roots=(input_root, Path(sys.prefix), Path(sys.base_prefix)),
    output_root=output_root,
    proxy_socket=proxy_socket,
)
results = {{}}
results['input_read'] = (input_root / 'bundle.json').read_text() == '{{}}'
try:
    ambient_file.read_text()
except PermissionError as exc:
    results['ambient_read'] = str(exc)
try:
    (input_root / 'mutation').write_text('unsafe')
except PermissionError as exc:
    results['input_write'] = str(exc)
(output_root / 'allowed').write_text('ok')
try:
    subprocess.run([sys.executable, '-c', 'pass'], check=False)
except PermissionError as exc:
    results['child'] = str(exc)
try:
    socket.socket().connect(('127.0.0.1', 9))
except PermissionError as exc:
    results['network'] = str(exc)
results['ambient_env'] = any(key in __import__('os').environ for key in ('DATABASE_URL', 'OPENAI_API_KEY'))
print(json.dumps(results, sort_keys=True))
"""
    environment = scrubbed_compiler_environment(sandbox_home=sandbox_home, sandbox_tmp=sandbox_tmp)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    results = json.loads(result.stdout)
    assert results == {
        "ambient_env": False,
        "ambient_read": "news_program_compile_sandbox_filesystem_denied",
        "child": "news_program_compile_sandbox_child_process_denied",
        "input_read": True,
        "input_write": "news_program_compile_sandbox_filesystem_denied",
        "network": "news_program_compile_sandbox_network_denied",
    }
    assert (output_root / "allowed").read_text(encoding="utf-8") == "ok"
    assert not (input_root / "mutation").exists()


def test_sandbox_output_requires_exact_files_and_budget(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "patch.json").write_text('{"patch":"ok"}', encoding="utf-8")
    (output / "runner_receipts.json").write_text('{"receipts":"ok"}', encoding="utf-8")
    policy = CompilerSandboxPolicy.issue(max_output_bytes=1_024)

    expected = canonical_sha(
        {
            "patch.json": canonical_sha({"document": '{"patch":"ok"}'}),
            "runner_receipts.json": canonical_sha({"document": '{"receipts":"ok"}'}),
        }
    )
    assert verify_sandbox_output_directory(output, policy=policy) == expected

    (output / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="output_files_invalid"):
        verify_sandbox_output_directory(output, policy=policy)
