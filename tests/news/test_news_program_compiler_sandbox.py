from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.compiler.sandbox import (
    CompilerSandboxLaunchReceipt,
    CompilerSandboxPolicy,
    environment_manifest_sha256,
    scrubbed_compiler_environment,
    validate_compiler_environment,
    verify_sandbox_output_directory,
)


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
        # The launcher addresses the volume by its name alone, and the receipt is now checked against
        # exactly that: `canonical_sha({"name": <volume>})`, not a hash of the whole socket payload.
        "socket_volume_sha256": canonical_sha({"name": volume_name}),
        "egress_network_sha256": network_sha,
        "compiler_container_removed": True,
        "proxy_container_removed": True,
        "init_container_removed": True,
        "socket_volume_removed": True,
        "egress_network_removed": True,
    }
    receipt = CompilerSandboxLaunchReceipt.issue(
        policy=policy.model_dump(mode="json"),
        input_bundle_sha256="1" * 64,
        image_preflight=image_preflight_payload,
        compiler_image_digest=image,
        proxy_image_digest=image,
        socket_volume=socket_volume_payload,
        lifecycle=lifecycle_payload,
        boundary_command=command_payload,
        boundary_inspect=inspect_payload,
        # Stated by the launcher, never defaulted by `issue`: a failed launch has incomplete actuals and
        # must still be able to build the receipt its failure is reported with.
        boundary_actuals_available=True,
        environment=environment_payload,
        mount_manifest=mount_payload,
        egress_manifest=egress_payload,
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


def test_compiler_sandbox_policy_and_launch_receipt_fail_closed_on_boundary_tampering() -> None:
    """The policy is still self-addressed; the launch receipt is addressed by the record that embeds it.

    The receipt's own `launch_receipt_sha256` and its eight `*_payload`/`*_sha256` preimage pairs are gone,
    so a tamper that used to be caught by re-hashing is now caught by `compile_record_sha256` — see
    `test_compile_record_fails_closed_on_every_tampered_embedded_payload`. What stays here is the part no
    digest ever proved: the boundary *semantics* read out of the retained daemon payloads.
    """

    policy, receipt = _valid_sandbox_launch_receipt()

    assert CompilerSandboxPolicy.model_validate(policy.model_dump(mode="json")) == policy
    assert CompilerSandboxLaunchReceipt.model_validate(receipt.model_dump(mode="json")) == receipt

    tampered_policy = policy.model_dump(mode="json")
    tampered_policy["max_cpu_seconds"] = 3_600
    with pytest.raises(ValidationError, match="sandbox_policy_hash_mismatch"):
        CompilerSandboxPolicy.model_validate(tampered_policy)

    semantically_tampered = receipt.model_dump(mode="json")
    semantically_tampered["boundary_inspect"]["compiler"]["pids_limit"] = 2
    with pytest.raises(ValidationError, match="boundary_semantics_invalid"):
        CompilerSandboxLaunchReceipt.model_validate(semantically_tampered)

    relabelled_volume = receipt.model_dump(mode="json")
    relabelled_volume["socket_volume"]["name"] = "tracefold-compiler-other-socket"
    with pytest.raises(ValidationError, match="boundary_semantics_invalid"):
        CompilerSandboxLaunchReceipt.model_validate(relabelled_volume)

    uncleaned = receipt.model_dump(mode="json")
    uncleaned["socket_volume_removed"] = False
    uncleaned["lifecycle"]["socket_volume_removed"] = False
    with pytest.raises(ValidationError, match="cleanup_incomplete"):
        CompilerSandboxLaunchReceipt.model_validate(uncleaned)

    lying_lifecycle = receipt.model_dump(mode="json")
    lying_lifecycle["lifecycle"]["egress_network_removed"] = False
    with pytest.raises(ValidationError, match="lifecycle_claim_invalid"):
        CompilerSandboxLaunchReceipt.model_validate(lying_lifecycle)

    failed = receipt.model_dump(mode="json")
    failed["termination"] = "failed"
    failed["exit_code"] = 1
    failed["output_root_sha256"] = None
    failed["boundary_actuals_available"] = False
    failed["boundary_inspect"]["compiler"] = None
    parsed_failure = CompilerSandboxLaunchReceipt.model_validate(failed)
    assert parsed_failure.boundary_actuals_available is False

    false_claim = dict(failed)
    false_claim["boundary_actuals_available"] = True
    with pytest.raises(ValidationError, match="boundary_actuals_claim_invalid"):
        CompilerSandboxLaunchReceipt.model_validate(false_claim)


def test_sandbox_launch_receipt_rejects_a_success_that_did_not_actually_succeed() -> None:
    """Three outcome invariants no digest ever covered, and no test guarded until #193.

    A receipt claiming `succeeded` is what makes the launcher hand the runner's patch to the record, so
    every part of that claim has to be self-consistent: the process exited 0, an output root was verified,
    and the daemon was actually observed. The inverse matters as much — a run that did not succeed may
    not carry an output root, because that root is what the patch is read against.
    """

    _, receipt = _valid_sandbox_launch_receipt()

    non_zero_exit = receipt.model_dump(mode="json")
    non_zero_exit["exit_code"] = 7
    with pytest.raises(ValidationError, match="success_receipt_invalid"):
        CompilerSandboxLaunchReceipt.model_validate(non_zero_exit)

    no_output_root = receipt.model_dump(mode="json")
    no_output_root["output_root_sha256"] = None
    with pytest.raises(ValidationError, match="success_receipt_invalid"):
        CompilerSandboxLaunchReceipt.model_validate(no_output_root)

    unobserved = receipt.model_dump(mode="json")
    unobserved["boundary_actuals_available"] = False
    unobserved["boundary_inspect"]["compiler"] = None
    with pytest.raises(ValidationError, match="success_actuals_unavailable"):
        CompilerSandboxLaunchReceipt.model_validate(unobserved)

    failed_with_output = receipt.model_dump(mode="json")
    failed_with_output["termination"] = "timed_out"
    failed_with_output["exit_code"] = None
    with pytest.raises(ValidationError, match="failed_output_forbidden"):
        CompilerSandboxLaunchReceipt.model_validate(failed_with_output)


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
from tracefold.news.learning.compiler.sandbox import install_compiler_sandbox_guards

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
    # One file, since #193: the receipts carry the typed patch, so the runner no longer writes it twice.
    (output / "runner_receipts.json").write_text('{"receipts":"ok"}', encoding="utf-8")
    policy = CompilerSandboxPolicy.issue(max_output_bytes=1_024)

    expected = canonical_sha({"runner_receipts.json": canonical_sha({"document": '{"receipts":"ok"}'})})
    assert verify_sandbox_output_directory(output, policy=policy) == expected

    (output / "patch.json").write_text('{"patch":"ok"}', encoding="utf-8")
    with pytest.raises(ValueError, match="output_files_invalid"):
        verify_sandbox_output_directory(output, policy=policy)
    (output / "patch.json").unlink()

    (output / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="output_files_invalid"):
        verify_sandbox_output_directory(output, policy=policy)


def test_the_compile_source_seal_is_computable_from_the_package() -> None:
    """PR8-A regression: the seal resolved its roots by counting `__file__` parents.

    Moving `program_compiler_source.py` to `learning/compiler/source_identity.py` changed that depth, so
    `_NEWS_ROOT` became `news/learning` and every call raised `news_program_compile_source_tree_invalid`
    — the launcher, the runner and the sidecar all compute this before a compile may start. Nothing in
    the suite called it, so the whole compiler boundary went dark without a single red test.
    """

    from tracefold.news.learning.compiler.source_identity import compiler_source_sha256, proxy_source_sha256

    compiler = compiler_source_sha256()
    proxy = proxy_source_sha256()
    assert len(compiler) == 64 and len(proxy) == 64
    # Two schemas over the same tree: equal inputs, deliberately different addresses.
    assert compiler != proxy
    # Deterministic — the host and the container must agree on it across processes.
    assert compiler_source_sha256() == compiler
    assert proxy_source_sha256() == proxy


def test_the_compiler_dependency_lock_identity_matches_the_source_lock() -> None:
    """#193: the lock attestation moved off the Program artifact onto the compiler boundary.

    `_verify_image_payload_before_secrets` compares this constant against the `uv.lock` copied out of the
    compiler image before any secret is staged, so a repository lock bump that does not update it fails
    every compile at image preflight. The Program artifact no longer carries a dependency lock at all —
    a wheel has no `uv.lock`, and the Program's behavior never depended on it — so this drift test lives
    beside the source seal that the same preflight computes.
    """

    from tracefold.news.learning.compiler.source_identity import COMPILER_DEPENDENCY_LOCK_SHA256

    repository_root = Path(__file__).resolve().parents[2]
    assert hashlib.sha256((repository_root / "uv.lock").read_bytes()).hexdigest() == COMPILER_DEPENDENCY_LOCK_SHA256
