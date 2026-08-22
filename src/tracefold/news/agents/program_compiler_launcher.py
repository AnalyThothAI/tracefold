"""Trusted Docker boundary for the cold GEPA compiler.

The optimizer and credential-owning model proxy run in separate containers and
share only a random, single-use Docker volume containing one Unix socket.  The
optimizer has ``--network none`` and never receives provider or database
credentials; the proxy receives neither corpus nor optimizer output.  Exact
resource cleanup precedes release of any candidate patch.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_json, canonical_sha
from .program_compiler_proxy import (
    CompilerModelProxyGrant,
    CompilerProxyExecutionReceipt,
    CompilerProxyReadyReceipt,
    CompilerProxySecretConfig,
)
from .program_compiler_sandbox import (
    CompilerSandboxLaunchReceipt,
    CompilerSandboxPolicy,
    verify_sandbox_output_directory,
)
from .program_compiler_security import CompileInputBundle
from .program_compiler_source import compiler_source_sha256, proxy_source_sha256

RUNNER_MODULE: Literal["tracefold.news.agents.program_compiler_runner"] = (
    "tracefold.news.agents.program_compiler_runner"
)
PROXY_MODULE: Literal["tracefold.news.agents.program_compiler_proxy_sidecar"] = (
    "tracefold.news.agents.program_compiler_proxy_sidecar"
)
_RUNNER_BOOTSTRAP = (
    "import os,runpy;os.environ.clear();"
    "os.environ.update({'HOME':'/run/tracefold/home','TMPDIR':'/tmp','LANG':'C.UTF-8',"
    "'LC_ALL':'C.UTF-8','PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'});"
    f"runpy.run_module('{RUNNER_MODULE}',run_name='__main__')"
)
_PROXY_BOOTSTRAP = (
    "import os,runpy;os.environ.clear();"
    "os.environ.update({'HOME':'/run/tracefold/home','TMPDIR':'/tmp','LANG':'C.UTF-8',"
    "'LC_ALL':'C.UTF-8','PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'});"
    f"runpy.run_module('{PROXY_MODULE}',run_name='__main__')"
)
_VOLUME_INIT = "import os;os.chmod('/run/tracefold/proxy',0o733)"
_SAFE_DOCKER_ENV = {"HOME": "/tmp", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}  # noqa: S108
_READY_TIMEOUT_SECONDS = 30.0


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompilerLauncherResult(_ExactModel):
    patch_document: str = Field(repr=False)
    runner_receipts_document: str = Field(repr=False)
    output_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proxy_execution_receipt: CompilerProxyExecutionReceipt
    launch_receipt: CompilerSandboxLaunchReceipt


class CompilerSandboxFailure(RuntimeError):
    """A failed child never yields optimizer output, only a trusted receipt."""

    def __init__(self, code: str, *, receipt: CompilerSandboxLaunchReceipt) -> None:
        self.code = code
        self.receipt = receipt
        super().__init__(code)


@dataclass(frozen=True)
class _BoundedCommandResult:
    exit_code: int
    termination: Literal["succeeded", "failed", "timed_out", "killed"]
    stdout: bytes
    stderr: bytes


class ProgramCompilerLauncher:
    """Launch the sole compiler runner and its fixed trusted proxy sidecar."""

    def __init__(
        self,
        *,
        policy: CompilerSandboxPolicy,
        compiler_source_sha256: str,
        compiler_lock_sha256: str,
        compiler_image: str,
        proxy_source_sha256: str,
        docker_executable: str | None = None,
    ) -> None:
        self._policy = policy
        self._compiler_source_sha256 = _require_sha(compiler_source_sha256, code="compiler_source")
        self._compiler_lock_sha256 = _require_sha(compiler_lock_sha256, code="compiler_lock")
        self._compiler_image, self._compiler_image_digest = _pinned_image(compiler_image)
        self._proxy_source_sha256 = _require_sha(proxy_source_sha256, code="proxy_source")
        self._docker_executable = _docker_executable(docker_executable)

    def launch(
        self,
        *,
        input_document: str,
        input_bundle_sha256: str,
        proxy_secret_config: CompilerProxySecretConfig,
    ) -> CompilerLauncherResult:
        """Run both containers; fail closed unless output and cleanup verify."""

        bundle_sha = _require_sha(input_bundle_sha256, code="input_bundle")
        canonical_input, bundle = _validated_input_document(input_document, expected_sha256=bundle_sha)
        grant = CompilerModelProxyGrant.issue(
            task_endpoint=bundle.task_endpoint,
            reflection_endpoint=bundle.reflection_endpoint,
            max_model_calls=bundle.budget.max_task_model_calls,
            max_cost_microusd=bundle.budget.max_cost_microusd,
            tariff=bundle.proxy_tariff,
            task_max_output_tokens=bundle.task_max_output_tokens,
            reflection_max_output_tokens=bundle.reflection_max_output_tokens,
            task_timeout_seconds=bundle.task_timeout_seconds,
            reflection_timeout_seconds=bundle.reflection_timeout_seconds,
            proxy_config_sha256=bundle.proxy_config_sha256,
            proxy_source_sha256=bundle.proxy_source_sha256,
        )
        if (
            grant.grant_sha256 != bundle.proxy_grant_sha256
            or proxy_secret_config.task.identity != bundle.task_endpoint
            or proxy_secret_config.reflection.identity != bundle.reflection_endpoint
            or proxy_secret_config.secret_free_config_sha256 != bundle.proxy_config_sha256
            or proxy_secret_config.tariff_sha256 != bundle.tariff_sha256
            or proxy_secret_config.tariff != bundle.proxy_tariff
            or proxy_secret_config.task.max_tokens != bundle.task_max_output_tokens
            or proxy_secret_config.reflection.max_tokens != bundle.reflection_max_output_tokens
            or proxy_secret_config.task.timeout != bundle.task_timeout_seconds
            or proxy_secret_config.reflection.timeout != bundle.reflection_timeout_seconds
            or grant.max_call_cost_microusd != bundle.budget.max_call_cost_microusd
            or bundle.compiler_source_sha256 != self._compiler_source_sha256
            or bundle.proxy_source_sha256 != self._proxy_source_sha256
            or bundle.compiler_lock_sha256 != self._compiler_lock_sha256
            or bundle.sandbox_policy_sha256 != self._policy.policy_sha256
            or bundle.compiler_image_digest != self._compiler_image_digest
        ):
            raise ValueError("news_program_compile_launcher_proxy_binding_mismatch")

        started = time.monotonic()
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        prefix = f"tracefold-compiler-{secrets.token_hex(12)}"
        volume_name = prefix + "-socket"
        network_name = prefix + "-egress"
        proxy_name = prefix + "-proxy"
        compiler_name = prefix + "-runner"
        init_name = prefix + "-init"
        preflight_name = prefix + "-preflight"

        with tempfile.TemporaryDirectory(prefix="tracefold-compiler-launch-") as staging_raw:
            staging = Path(staging_raw).resolve(strict=True)
            image_preflight_payload = _verify_image_payload_before_secrets(
                docker=self._docker_executable,
                image=self._compiler_image,
                expected_compiler_source_sha256=self._compiler_source_sha256,
                expected_proxy_source_sha256=self._proxy_source_sha256,
                expected_lock_sha256=self._compiler_lock_sha256,
                staging=staging,
                container_name=preflight_name,
            )
            image_preflight_sha = canonical_sha(image_preflight_payload)
            paths = _prepare_staging(
                staging,
                canonical_input=canonical_input,
                policy=self._policy,
                grant=grant,
                proxy_secret_config=proxy_secret_config,
            )
            environment_payload = {
                "schema": "tracefold.news.compiler_container_environment.v2",
                "environment_keys": [
                    "HOME",
                    "LANG",
                    "LC_ALL",
                    "PYTHONDONTWRITEBYTECODE",
                    "PYTHONUTF8",
                    "TMPDIR",
                ],
                "ambient_forwarded": False,
                "image_environment_cleared_before_package_import": True,
            }
            environment_sha = canonical_sha(environment_payload)
            socket_volume_payload = {
                "schema": "tracefold.news.compiler_socket_volume.v2",
                "name": volume_name,
            }
            socket_volume_sha = canonical_sha(socket_volume_payload)
            mount_manifest_payload = {
                "schema": "tracefold.news.compiler_mount_manifest.v2",
                "input_bundle_sha256": bundle_sha,
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
            mount_manifest_sha = canonical_sha(mount_manifest_payload)
            egress_manifest_payload = {
                "schema": "tracefold.news.compiler_egress_manifest.v2",
                "compiler_network": "none",
                "proxy_network_sha256": canonical_sha({"name": network_name}),
                "proxy_grant_sha256": grant.grant_sha256,
                "compiler_other_network_allowed": False,
                "trusted_proxy_egress": True,
            }
            egress_manifest_sha = canonical_sha(egress_manifest_payload)
            init_command = self._volume_init_command(volume_name=volume_name, container_name=init_name)
            proxy_command = self._proxy_command(
                paths=paths,
                volume_name=volume_name,
                network_name=network_name,
                container_name=proxy_name,
            )
            compiler_command = self._compiler_command(
                paths=paths,
                volume_name=volume_name,
                container_name=compiler_name,
            )
            boundary_command_payload = {
                "schema": "tracefold.news.compiler_boundary_commands.v2",
                "volume_init": list(init_command),
                "proxy": list(proxy_command),
                "compiler": list(compiler_command),
            }
            boundary_command_root_sha = canonical_sha(boundary_command_payload)
            compiler_cid: str | None = None
            proxy_cid: str | None = None
            compiler_removed = proxy_removed = init_removed = volume_removed = network_removed = False
            compiler_started = proxy_started = False
            ready: CompilerProxyReadyReceipt | None = None
            proxy_execution: CompilerProxyExecutionReceipt | None = None
            termination: Literal["succeeded", "failed", "timed_out", "killed"] = "failed"
            exit_code: int | None = None
            failure_code = "news_program_compile_sandbox_failed"
            output_root_sha: str | None = None
            network_inspect_payload: dict[str, Any] | None = None
            volume_inspect_payload: dict[str, Any] | None = None
            proxy_inspect_payload: dict[str, Any] | None = None
            compiler_inspect_payload: dict[str, Any] | None = None
            stdout = b""
            stderr = b""
            try:
                _docker_checked(self._docker_executable, "network", "create", "--driver", "bridge", network_name)
                network_inspect_payload = _docker_named_resource_inspect_payload(
                    self._docker_executable,
                    "network",
                    network_name,
                )
                _docker_checked(self._docker_executable, "volume", "create", volume_name)
                volume_inspect_payload = _docker_named_resource_inspect_payload(
                    self._docker_executable,
                    "volume",
                    volume_name,
                )
                _docker_checked(*init_command)
                proxy_started = True
                _docker_checked(*proxy_command)
                proxy_cid = _read_container_id(paths.proxy_cid)
                proxy_inspect_payload = _docker_container_boundary_payload(
                    self._docker_executable,
                    proxy_cid,
                    expected_name=proxy_name,
                    expected_image=self._compiler_image_digest,
                    expected_network=network_name,
                    expected_pids_limit=32,
                    expected_memory_bytes=1_073_741_824,
                    expected_ulimits={},
                    expected_mounts={
                        "/run/tracefold/config": ("bind", False, str(paths.proxy_config)),
                        "/run/tracefold/secrets": ("bind", False, str(paths.proxy_secrets)),
                        "/run/tracefold/proxy-receipt": ("bind", True, str(paths.proxy_receipt)),
                        "/run/tracefold/proxy": ("volume", True, volume_name),
                    },
                )
                ready = _wait_for_proxy_ready(paths.proxy_receipt / "ready.json", grant=grant)
                compiler_started = True
                runner_result = _bounded_command(
                    compiler_command,
                    timeout_seconds=self._policy.wall_timeout_seconds,
                    max_stdout_bytes=self._policy.max_stdout_bytes,
                    max_stderr_bytes=self._policy.max_stderr_bytes,
                    on_abort=lambda: _kill_exact_container_attempt(
                        self._docker_executable,
                        _optional_container_id(paths.compiler_cid),
                        fallback_name=compiler_name,
                    ),
                )
                exit_code = runner_result.exit_code
                termination = runner_result.termination
                stdout = runner_result.stdout
                stderr = runner_result.stderr
                compiler_cid = _optional_container_id(paths.compiler_cid)
                if compiler_cid is not None:
                    compiler_inspect_payload = _docker_container_boundary_payload(
                        self._docker_executable,
                        compiler_cid,
                        expected_name=compiler_name,
                        expected_image=self._compiler_image_digest,
                        expected_network="none",
                        expected_pids_limit=self._policy.max_processes,
                        expected_memory_bytes=self._policy.max_rss_bytes,
                        expected_ulimits={
                            "cpu": (self._policy.max_cpu_seconds, self._policy.max_cpu_seconds),
                            "fsize": (
                                max(
                                    self._policy.max_output_bytes,
                                    self._policy.max_stdout_bytes,
                                    self._policy.max_stderr_bytes,
                                ),
                            )
                            * 2,
                            "nofile": (self._policy.max_open_files, self._policy.max_open_files),
                        },
                        expected_mounts={
                            "/run/tracefold/input": ("bind", False, str(paths.compiler_input)),
                            "/run/tracefold/output": ("bind", True, str(paths.output)),
                            "/run/tracefold/proxy": ("volume", False, volume_name),
                        },
                    )
                if termination == "killed":
                    failure_code = "news_program_compile_sandbox_output_log_budget_exceeded"
                if not _docker_best_effort(self._docker_executable, "stop", "--time", "10", proxy_cid):
                    termination = "failed"
                    failure_code = "news_program_compile_proxy_stop_failed"
                proxy_execution = _load_proxy_execution(paths.proxy_receipt / "execution.json", grant=grant)
                if termination == "succeeded":
                    output_root_sha = verify_sandbox_output_directory(paths.output, policy=self._policy)
            except subprocess.TimeoutExpired:
                termination = "timed_out"
                failure_code = "news_program_compile_proxy_ready_timed_out"
            except (OSError, TypeError, ValueError, subprocess.SubprocessError):
                termination = "failed"
                failure_code = "news_program_compile_launcher_boundary_failed"
            finally:
                compiler_cid = compiler_cid or _optional_container_id(paths.compiler_cid)
                proxy_cid = proxy_cid or _optional_container_id(paths.proxy_cid)
                compiler_removed = _remove_exact_container(
                    self._docker_executable,
                    compiler_cid,
                    fallback_name=compiler_name,
                    attempted=compiler_started,
                )
                proxy_removed = _remove_exact_container(
                    self._docker_executable,
                    proxy_cid,
                    fallback_name=proxy_name,
                    attempted=proxy_started,
                )
                init_removed = _remove_exact_container(
                    self._docker_executable,
                    None,
                    fallback_name=init_name,
                    attempted=True,
                )
                volume_removed = _remove_exact_named_resource(
                    self._docker_executable,
                    "volume",
                    volume_name,
                )
                network_removed = _remove_exact_named_resource(
                    self._docker_executable,
                    "network",
                    network_name,
                )

            usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
            output_files = tuple(paths.output.iterdir())
            output_bytes = sum(child.lstat().st_size for child in output_files if stat.S_ISREG(child.lstat().st_mode))
            ready_hash = ready.ready_sha256 if ready is not None else canonical_sha({"status": "proxy_not_ready"})
            execution_hash = (
                proxy_execution.receipt_sha256
                if proxy_execution is not None
                else canonical_sha({"status": "proxy_execution_unavailable"})
            )
            boundary_inspect_payload = {
                "schema": "tracefold.news.compiler_boundary_inspections.v2",
                "image_preflight": image_preflight_payload,
                "network": network_inspect_payload,
                "volume": volume_inspect_payload,
                "proxy": proxy_inspect_payload,
                "compiler": compiler_inspect_payload,
            }
            boundary_inspect_root_sha = canonical_sha(boundary_inspect_payload)
            boundary_actuals_available = all(
                payload is not None
                for payload in (
                    network_inspect_payload,
                    volume_inspect_payload,
                    proxy_inspect_payload,
                    compiler_inspect_payload,
                )
            )
            lifecycle_payload = {
                "schema": "tracefold.news.compiler_container_lifecycle.v2",
                "compiler_container_sha256": canonical_sha({"id": compiler_cid}),
                "proxy_container_sha256": canonical_sha({"id": proxy_cid}),
                "socket_volume_sha256": socket_volume_sha,
                "egress_network_sha256": canonical_sha({"name": network_name}),
                "compiler_container_removed": compiler_removed,
                "proxy_container_removed": proxy_removed,
                "init_container_removed": init_removed,
                "socket_volume_removed": volume_removed,
                "egress_network_removed": network_removed,
            }
            lifecycle_sha = canonical_sha(lifecycle_payload)
            receipt = CompilerSandboxLaunchReceipt.issue(
                policy_payload=self._policy.model_dump(mode="json"),
                policy_sha256=self._policy.policy_sha256,
                input_bundle_sha256=bundle_sha,
                compiler_source_sha256=self._compiler_source_sha256,
                compiler_lock_sha256=self._compiler_lock_sha256,
                image_preflight_payload=image_preflight_payload,
                image_preflight_sha256=image_preflight_sha,
                compiler_image_digest=self._compiler_image_digest,
                proxy_image_digest=self._compiler_image_digest,
                proxy_source_sha256=self._proxy_source_sha256,
                proxy_identity_sha256=grant.grant_sha256,
                proxy_config_sha256=grant.proxy_config_sha256,
                proxy_tariff_sha256=grant.tariff_sha256,
                proxy_ready_receipt_sha256=ready_hash,
                proxy_execution_receipt_sha256=execution_hash,
                socket_volume_payload=socket_volume_payload,
                socket_volume_sha256=socket_volume_sha,
                lifecycle_payload=lifecycle_payload,
                lifecycle_sha256=lifecycle_sha,
                boundary_command_payload=boundary_command_payload,
                boundary_command_root_sha256=boundary_command_root_sha,
                boundary_inspect_payload=boundary_inspect_payload,
                boundary_inspect_root_sha256=boundary_inspect_root_sha,
                boundary_actuals_available=boundary_actuals_available,
                environment_payload=environment_payload,
                environment_sha256=environment_sha,
                mount_manifest_payload=mount_manifest_payload,
                mount_manifest_sha256=mount_manifest_sha,
                egress_manifest_payload=egress_manifest_payload,
                egress_manifest_sha256=egress_manifest_sha,
                holdout_mounted=False,
                db_credentials_present=False,
                ambient_credentials_present=False,
                child_process_allowed=False,
                compiler_container_removed=compiler_removed,
                proxy_container_removed=proxy_removed,
                init_container_removed=init_removed,
                socket_volume_removed=volume_removed,
                egress_network_removed=network_removed,
                termination=termination,
                exit_code=exit_code,
                wall_time_ms=max(0, round((time.monotonic() - started) * 1_000)),
                launcher_cpu_time_ms=max(
                    0,
                    round(
                        (usage_after.ru_utime + usage_after.ru_stime - usage_before.ru_utime - usage_before.ru_stime)
                        * 1_000
                    ),
                ),
                launcher_max_rss_bytes=_rss_bytes(usage_after.ru_maxrss),
                container_actuals_available=False,
                container_max_cpu_seconds=self._policy.max_cpu_seconds,
                container_max_rss_bytes=self._policy.max_rss_bytes,
                output_bytes=output_bytes,
                output_file_count=len(output_files),
                stdout_sha256=canonical_sha({"stdout": stdout.decode("utf-8", errors="replace")}),
                stderr_sha256=canonical_sha({"stderr": stderr.decode("utf-8", errors="replace")}),
                output_root_sha256=output_root_sha if termination == "succeeded" else None,
            )
            if (
                termination != "succeeded"
                or output_root_sha is None
                or proxy_execution is None
                or ready is None
                or any(
                    value is None
                    for value in (
                        network_inspect_payload,
                        volume_inspect_payload,
                        proxy_inspect_payload,
                        compiler_inspect_payload,
                    )
                )
                or not all((compiler_removed, proxy_removed, init_removed, volume_removed, network_removed))
            ):
                raise CompilerSandboxFailure(failure_code, receipt=receipt)
            return CompilerLauncherResult(
                patch_document=(paths.output / "patch.json").read_text(encoding="utf-8"),
                runner_receipts_document=(paths.output / "runner_receipts.json").read_text(encoding="utf-8"),
                output_root_sha256=output_root_sha,
                proxy_execution_receipt=proxy_execution,
                launch_receipt=receipt,
            )

    def _volume_init_command(self, *, volume_name: str, container_name: str) -> tuple[str, ...]:
        return (
            self._docker_executable,
            "run",
            "--rm",
            "--name",
            container_name,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "1",
            "--mount",
            f"type=volume,src={volume_name},dst=/run/tracefold/proxy,volume-nocopy",
            "--entrypoint",
            "python",
            self._compiler_image,
            "-I",
            "-c",
            _VOLUME_INIT,
        )

    def _proxy_command(
        self,
        *,
        paths: _StagingPaths,
        volume_name: str,
        network_name: str,
        container_name: str,
    ) -> tuple[str, ...]:
        return (
            self._docker_executable,
            "run",
            "-d",
            "--cidfile",
            _mount_safe_path(paths.proxy_cid),
            "--name",
            container_name,
            "--pull",
            "never",
            "--network",
            network_name,
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
            "--workdir",
            "/",
            "--mount",
            f"type=bind,src={_mount_safe_path(paths.proxy_config)},dst=/run/tracefold/config,readonly",
            "--mount",
            f"type=bind,src={_mount_safe_path(paths.proxy_secrets)},dst=/run/tracefold/secrets,readonly",
            "--mount",
            f"type=bind,src={_mount_safe_path(paths.proxy_receipt)},dst=/run/tracefold/proxy-receipt",
            "--mount",
            f"type=volume,src={volume_name},dst=/run/tracefold/proxy,volume-nocopy",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864",  # noqa: S108
            "--tmpfs",
            "/run/tracefold/home:rw,noexec,nosuid,nodev,size=1048576",
            "--entrypoint",
            "python",
            self._compiler_image,
            "-I",
            "-c",
            _PROXY_BOOTSTRAP,
            "--grant",
            "/run/tracefold/config/grant.json",
            "--secrets",
            "/run/tracefold/secrets/provider.json",
            "--socket",
            "/run/tracefold/proxy/compiler.sock",
            "--output",
            "/run/tracefold/proxy-receipt",
        )

    def _compiler_command(
        self,
        *,
        paths: _StagingPaths,
        volume_name: str,
        container_name: str,
    ) -> tuple[str, ...]:
        max_file_bytes = max(
            self._policy.max_output_bytes,
            self._policy.max_stdout_bytes,
            self._policy.max_stderr_bytes,
        )
        return (
            self._docker_executable,
            "run",
            "--cidfile",
            _mount_safe_path(paths.compiler_cid),
            "--name",
            container_name,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(self._policy.max_processes),
            "--memory",
            str(self._policy.max_rss_bytes),
            "--memory-swap",
            str(self._policy.max_rss_bytes),
            "--cpus",
            "1.0",
            "--ulimit",
            f"nofile={self._policy.max_open_files}:{self._policy.max_open_files}",
            "--ulimit",
            f"fsize={max_file_bytes}:{max_file_bytes}",
            "--ulimit",
            f"cpu={self._policy.max_cpu_seconds}:{self._policy.max_cpu_seconds}",
            "--ipc",
            "none",
            "--log-driver",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--workdir",
            "/",
            "--mount",
            f"type=bind,src={_mount_safe_path(paths.compiler_input)},dst=/run/tracefold/input,readonly",
            "--mount",
            f"type=bind,src={_mount_safe_path(paths.output)},dst=/run/tracefold/output",
            "--mount",
            f"type=volume,src={volume_name},dst=/run/tracefold/proxy,readonly,volume-nocopy",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864",  # noqa: S108
            "--tmpfs",
            "/run/tracefold/home:rw,noexec,nosuid,nodev,size=1048576",
            "--entrypoint",
            "python",
            self._compiler_image,
            "-I",
            "-c",
            _RUNNER_BOOTSTRAP,
            "--input",
            "/run/tracefold/input/bundle.json",
            "--output",
            "/run/tracefold/output",
            "--policy",
            "/run/tracefold/input/policy.json",
            "--proxy-socket",
            "/run/tracefold/proxy/compiler.sock",
        )


class _StagingPaths:
    def __init__(self, root: Path) -> None:
        self.compiler_input = root / "compiler-input"
        self.output = root / "compiler-output"
        self.proxy_config = root / "proxy-config"
        self.proxy_secrets = root / "proxy-secrets"
        self.proxy_receipt = root / "proxy-receipt"
        self.compiler_cid = root / "compiler.cid"
        self.proxy_cid = root / "proxy.cid"


def _prepare_staging(
    staging: Path,
    *,
    canonical_input: str,
    policy: CompilerSandboxPolicy,
    grant: CompilerModelProxyGrant,
    proxy_secret_config: CompilerProxySecretConfig,
) -> _StagingPaths:
    paths = _StagingPaths(staging)
    for directory in (
        paths.compiler_input,
        paths.output,
        paths.proxy_config,
        paths.proxy_secrets,
        paths.proxy_receipt,
    ):
        directory.mkdir(mode=0o700)
    _write_private(paths.compiler_input / "bundle.json", canonical_input, mode=0o444)
    _write_private(
        paths.compiler_input / "policy.json",
        canonical_json(policy.model_dump(mode="json")),
        mode=0o444,
    )
    _write_private(
        paths.proxy_config / "grant.json",
        canonical_json(grant.model_dump(mode="json")),
        mode=0o444,
    )
    _write_private(
        paths.proxy_secrets / "provider.json",
        canonical_json(proxy_secret_config.model_dump(mode="json")),
        mode=0o400,
    )
    return paths


def _write_private(path: Path, document: str, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.write(descriptor, document.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(mode)


def _validated_input_document(value: str, *, expected_sha256: str) -> tuple[str, CompileInputBundle]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, child in pairs:
            if key in parsed:
                raise ValueError(f"news_program_compile_input_duplicate_key:{key}")
            parsed[key] = child
        return parsed

    try:
        payload = json.loads(
            value,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
        bundle = CompileInputBundle.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("news_program_compile_input_document_invalid") from exc
    if bundle.bundle_sha256 != expected_sha256:
        raise ValueError("news_program_compile_input_document_identity_mismatch")
    return canonical_json(bundle.model_dump(mode="json")), bundle


def _wait_for_proxy_ready(path: Path, *, grant: CompilerModelProxyGrant) -> CompilerProxyReadyReceipt:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            ready = CompilerProxyReadyReceipt.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("news_program_compile_proxy_ready_receipt_invalid") from exc
        if (
            ready.grant_sha256 != grant.grant_sha256
            or ready.proxy_config_sha256 != grant.proxy_config_sha256
            or ready.tariff_sha256 != grant.tariff_sha256
            or ready.proxy_source_sha256 != grant.proxy_source_sha256
        ):
            raise ValueError("news_program_compile_proxy_ready_receipt_mismatch")
        return ready
    raise subprocess.TimeoutExpired("compiler-proxy-ready", _READY_TIMEOUT_SECONDS)


def _load_proxy_execution(path: Path, *, grant: CompilerModelProxyGrant) -> CompilerProxyExecutionReceipt:
    try:
        receipt = CompilerProxyExecutionReceipt.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("news_program_compile_proxy_execution_receipt_invalid") from exc
    if receipt.grant_sha256 != grant.grant_sha256 or receipt.tariff_sha256 != grant.tariff_sha256:
        raise ValueError("news_program_compile_proxy_execution_receipt_mismatch")
    return receipt


def _docker_checked(*command: str) -> None:
    try:
        subprocess.run(  # noqa: S603
            command,
            env=_SAFE_DOCKER_ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("news_program_compile_container_runtime_failed") from exc


def _bounded_command(
    command: tuple[str, ...],
    *,
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    on_abort: Callable[[], None] | None = None,
) -> _BoundedCommandResult:
    """Capture fixed-size pipes and kill immediately on timeout or overflow."""

    process = subprocess.Popen(  # noqa: S603
        command,
        env=_SAFE_DOCKER_ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise ValueError("news_program_compile_container_runtime_failed")
    streams = {process.stdout: (bytearray(), max_stdout_bytes), process.stderr: (bytearray(), max_stderr_bytes)}
    selector = selectors.DefaultSelector()
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    termination: Literal["succeeded", "failed", "timed_out", "killed"] | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                termination = "timed_out"
                break
            for key, _ in selector.select(timeout=min(0.1, remaining)):
                stream = cast(BinaryIO, key.fileobj)
                buffer, limit = streams[stream]
                try:
                    chunk = os.read(stream.fileno(), min(65_536, limit - len(buffer) + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                available = max(0, limit - len(buffer))
                buffer.extend(chunk[:available])
                if len(chunk) > available:
                    termination = "killed"
                    break
            if termination is not None:
                break
        if termination is not None:
            if on_abort is not None:
                on_abort()
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
        else:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                termination = "timed_out"
                if on_abort is not None:
                    on_abort()
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        exit_code = int(process.returncode if process.returncode is not None else -1)
        if termination is None:
            termination = "succeeded" if exit_code == 0 else "failed"
        return _BoundedCommandResult(
            exit_code=exit_code,
            termination=termination,
            stdout=bytes(streams[process.stdout][0]),
            stderr=bytes(streams[process.stderr][0]),
        )
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _docker_capture_checked(
    *command: str,
    timeout_seconds: float = 30,
    max_stdout_bytes: int = 65_536,
    max_stderr_bytes: int = 65_536,
    on_abort: Callable[[], None] | None = None,
) -> bytes:
    try:
        result = _bounded_command(
            tuple(command),
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            on_abort=on_abort,
        )
    except OSError as exc:
        raise ValueError("news_program_compile_container_runtime_failed") from exc
    if result.termination != "succeeded":
        raise ValueError("news_program_compile_container_runtime_failed")
    return result.stdout


def _docker_best_effort(*command: str) -> bool:
    try:
        result = subprocess.run(  # noqa: S603
            command,
            env=_SAFE_DOCKER_ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _remove_exact_container(
    docker: str,
    container_id: str | None,
    *,
    fallback_name: str,
    attempted: bool,
) -> bool:
    del attempted
    target = container_id or fallback_name
    if _docker_best_effort(docker, "rm", "--force", target):
        return True
    return _docker_exact_name_absent(docker, "container", fallback_name)


def _remove_exact_named_resource(docker: str, kind: Literal["network", "volume"], name: str) -> bool:
    if _docker_best_effort(docker, kind, "rm", name):
        return True
    return _docker_exact_name_absent(docker, kind, name)


def _docker_exact_name_absent(
    docker: str,
    kind: Literal["container", "network", "volume"],
    name: str,
) -> bool:
    command = [docker, kind, "ls"]
    if kind == "container":
        command.append("-a")
    command.extend(("--filter", f"name={name}", "--format", "{{.Names}}" if kind == "container" else "{{.Name}}"))
    try:
        raw = _docker_capture_checked(
            *command,
            max_stdout_bytes=4_096,
            max_stderr_bytes=4_096,
        )
    except ValueError:
        return False
    values = {value.strip() for value in raw.decode("utf-8", errors="strict").splitlines() if value.strip()}
    return name not in values


def _docker_named_resource_inspect_payload(
    docker: str,
    kind: Literal["network", "volume"],
    name: str,
) -> dict[str, Any]:
    raw = _docker_capture_checked(
        docker,
        kind,
        "inspect",
        name,
        max_stdout_bytes=262_144,
        max_stderr_bytes=4_096,
    )
    try:
        documents = json.loads(raw)
        document = documents[0]
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("news_program_compile_container_inspect_invalid") from exc
    if not isinstance(document, dict) or document.get("Name") != name:
        raise ValueError("news_program_compile_container_inspect_mismatch")
    identity = document.get("Id") if kind == "network" else document.get("Name")
    if not isinstance(identity, str) or not identity:
        raise ValueError("news_program_compile_container_inspect_mismatch")
    return {
        "kind": kind,
        "name_sha256": canonical_sha({"name": name}),
        "daemon_identity_sha256": canonical_sha({"identity": identity}),
        "driver": document.get("Driver"),
        "scope": document.get("Scope"),
    }


def _docker_container_boundary_payload(
    docker: str,
    container_id: str,
    *,
    expected_name: str,
    expected_image: str,
    expected_network: str,
    expected_pids_limit: int,
    expected_memory_bytes: int,
    expected_ulimits: dict[str, tuple[int, int]],
    expected_mounts: dict[str, tuple[Literal["bind", "volume"], bool, str]],
) -> dict[str, Any]:
    raw = _docker_capture_checked(
        docker,
        "container",
        "inspect",
        container_id,
        max_stdout_bytes=1_048_576,
        max_stderr_bytes=4_096,
    )
    try:
        documents = json.loads(raw)
        document = documents[0]
        host = document["HostConfig"]
        mounts = document["Mounts"]
        config = document["Config"]
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("news_program_compile_container_inspect_invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("Id") != container_id
        or document.get("Name") != f"/{expected_name}"
        or document.get("Image") != expected_image
        or not isinstance(host, dict)
        or not isinstance(config, dict)
        or config.get("User") != f"{os.getuid()}:{os.getgid()}"
        or host.get("NetworkMode") != expected_network
        or host.get("ReadonlyRootfs") is not True
        or host.get("IpcMode") != "none"
        or (host.get("LogConfig") or {}).get("Type") != "none"
        or int(host.get("PidsLimit") or 0) != expected_pids_limit
        or int(host.get("Memory") or 0) != expected_memory_bytes
        or int(host.get("MemorySwap") or 0) != expected_memory_bytes
        or int(host.get("NanoCpus") or 0) != 1_000_000_000
        or "ALL" not in (host.get("CapDrop") or [])
        or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])
        or not isinstance(mounts, list)
    ):
        raise ValueError("news_program_compile_container_inspect_mismatch")
    tmpfs = host.get("Tmpfs")
    expected_tmpfs = {
        "/tmp": {"rw", "noexec", "nosuid", "nodev", "size=67108864"},  # noqa: S108
        "/run/tracefold/home": {"rw", "noexec", "nosuid", "nodev", "size=1048576"},
    }
    if not isinstance(tmpfs, dict) or set(tmpfs) != set(expected_tmpfs):
        raise ValueError("news_program_compile_container_inspect_mismatch")
    for destination, expected_options in expected_tmpfs.items():
        actual = tmpfs.get(destination)
        if not isinstance(actual, str) or set(actual.split(",")) != expected_options:
            raise ValueError("news_program_compile_container_inspect_mismatch")
    raw_ulimits = host.get("Ulimits") or []
    if not isinstance(raw_ulimits, list):
        raise ValueError("news_program_compile_container_inspect_mismatch")
    actual_ulimits: dict[str, tuple[int, int]] = {}
    for item in raw_ulimits:
        if not isinstance(item, dict) or not isinstance(item.get("Name"), str):
            raise ValueError("news_program_compile_container_inspect_mismatch")
        actual_ulimits[str(item["Name"])] = (int(item.get("Soft") or 0), int(item.get("Hard") or 0))
    if actual_ulimits != expected_ulimits:
        raise ValueError("news_program_compile_container_inspect_mismatch")
    environment = config.get("Env") or []
    if not isinstance(environment, list) or any(not isinstance(item, str) for item in environment):
        raise ValueError("news_program_compile_container_inspect_mismatch")
    denied_environment_keys = {
        "API_KEY",
        "AUTHORIZATION",
        "DATABASE_URL",
        "DB_DSN",
        "OPENAI_API_KEY",
        "PASSWORD",
        "PGPASSWORD",
        "SECRET",
        "TOKEN",
    }
    for item in environment:
        key, _, value = item.partition("=")
        if key.upper() in denied_environment_keys or "Bearer " in value or value.startswith("sk-"):
            raise ValueError("news_program_compile_container_inspect_ambient_credential")
    actual_mounts: dict[str, dict[str, Any]] = {}
    for mount in mounts:
        if not isinstance(mount, dict):
            raise ValueError("news_program_compile_container_inspect_mismatch")
        raw_destination = mount.get("Destination")
        if not isinstance(raw_destination, str) or raw_destination in actual_mounts:
            raise ValueError("news_program_compile_container_inspect_mismatch")
        actual_mounts[raw_destination] = mount
    if set(actual_mounts) != set(expected_mounts):
        raise ValueError("news_program_compile_container_inspect_mismatch")
    normalized_mounts: list[dict[str, Any]] = []
    for destination, (mount_type, writable, source) in sorted(expected_mounts.items()):
        mount = actual_mounts[destination]
        actual_source = mount.get("Name") if mount_type == "volume" else mount.get("Source")
        source_matches = (
            actual_source == source
            if mount_type == "volume"
            else _docker_bind_source_matches(actual_source=actual_source, expected_source=source)
        )
        if mount.get("Type") != mount_type or mount.get("RW") is not writable or not source_matches:
            raise ValueError("news_program_compile_container_inspect_mismatch")
        normalized_mounts.append(
            {
                "destination": destination,
                "type": mount_type,
                "writable": writable,
                "source_sha256": canonical_sha({"source": source}),
            }
        )
    return {
        "kind": "container",
        "container_id_sha256": canonical_sha({"id": container_id}),
        "name_sha256": canonical_sha({"name": expected_name}),
        "image": expected_image,
        "network_sha256": canonical_sha({"network": expected_network}),
        "readonly_rootfs": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": expected_pids_limit,
        "user": config.get("User"),
        "environment_sha256": canonical_sha(sorted(environment)),
        "ipc_mode": host.get("IpcMode"),
        "log_config": host.get("LogConfig"),
        "tmpfs": {key: sorted(value) for key, value in sorted(expected_tmpfs.items())},
        "memory": expected_memory_bytes,
        "memory_swap": expected_memory_bytes,
        "nano_cpus": 1_000_000_000,
        "ulimits": {key: list(value) for key, value in sorted(expected_ulimits.items())},
        "mounts": normalized_mounts,
    }


def _docker_bind_source_matches(*, actual_source: object, expected_source: str) -> bool:
    if actual_source == expected_source:
        return True
    return (
        sys.platform == "darwin"
        and expected_source.startswith("/private/")
        and actual_source
        in {
            expected_source.removeprefix("/private"),
            f"/host_mnt{expected_source}",
        }
    )


def _kill_exact_container_attempt(docker: str, container_id: str | None, *, fallback_name: str) -> None:
    _docker_best_effort(docker, "kill", container_id or fallback_name)


def _verify_image_payload_before_secrets(
    *,
    docker: str,
    image: str,
    expected_compiler_source_sha256: str,
    expected_proxy_source_sha256: str,
    expected_lock_sha256: str,
    staging: Path,
    container_name: str,
) -> dict[str, Any]:
    """Host-verify image files without executing image code or mounting secrets."""

    actual_image = (
        _docker_capture_checked(
            docker,
            "image",
            "inspect",
            image,
            "--format",
            "{{.Id}}",
            max_stdout_bytes=128,
            max_stderr_bytes=4_096,
        )
        .decode("ascii", errors="strict")
        .strip()
    )
    if actual_image != image:
        raise ValueError("news_program_compile_compiler_image_identity_mismatch")
    cid: str | None = None
    copied = staging / "verified-image"
    copied.mkdir(mode=0o700)
    try:
        raw_cid = (
            _docker_capture_checked(
                docker,
                "create",
                "--name",
                container_name,
                "--pull",
                "never",
                "--network",
                "none",
                "--entrypoint",
                "/bin/false",
                image,
                max_stdout_bytes=128,
                max_stderr_bytes=4_096,
                on_abort=lambda: _best_effort_remove_by_name(docker, container_name),
            )
            .decode("ascii", errors="strict")
            .strip()
        )
        if not _is_container_id(raw_cid):
            raise ValueError("news_program_compile_container_identity_unavailable")
        cid = raw_cid
        _docker_checked(docker, "cp", f"{cid}:/app/src/tracefold", str(copied / "tracefold"))
        _docker_checked(docker, "cp", f"{cid}:/app/uv.lock", str(copied / "uv.lock"))
        tracefold_root = copied / "tracefold"
        lock_path = copied / "uv.lock"
        actual_compiler_source = compiler_source_sha256(tracefold_root=tracefold_root)
        actual_proxy_source = proxy_source_sha256(tracefold_root=tracefold_root)
        actual_lock = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        if (
            actual_compiler_source != expected_compiler_source_sha256
            or actual_proxy_source != expected_proxy_source_sha256
            or actual_lock != expected_lock_sha256
        ):
            raise ValueError("news_program_compile_image_payload_identity_mismatch")
    finally:
        if not _remove_exact_container(
            docker,
            cid,
            fallback_name=container_name,
            attempted=True,
        ):
            raise ValueError("news_program_compile_preflight_cleanup_failed")
    return {
        "schema": "tracefold.news.compiler_image_preflight.v2",
        "image_id": actual_image,
        "compiler_source_sha256": expected_compiler_source_sha256,
        "proxy_source_sha256": expected_proxy_source_sha256,
        "compiler_lock_sha256": expected_lock_sha256,
        "image_code_executed": False,
        "provider_config_mounted": False,
        "network": "none",
        "pull_policy": "never",
    }


def _is_container_id(value: str) -> bool:
    return 12 <= len(value) <= 64 and all(character in "0123456789abcdef" for character in value)


def _best_effort_remove_by_name(docker: str, name: str) -> None:
    _docker_best_effort(docker, "rm", "--force", name)


def _read_container_id(path: Path) -> str:
    value = _optional_container_id(path)
    if value is None:
        raise ValueError("news_program_compile_container_identity_unavailable")
    return value


def _optional_container_id(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    if len(value) < 12 or len(value) > 64 or any(character not in "0123456789abcdef" for character in value):
        return None
    return value


def _mount_safe_path(path: Path) -> str:
    value = str(path)
    if not value.startswith("/") or any(character in value for character in (",", "\n", "\r", "\x00")):
        raise ValueError("news_program_compile_sandbox_mount_path_invalid")
    return value


def _require_sha(value: str, *, code: str) -> str:
    normalized = str(value)
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"news_program_compile_{code}_sha_invalid")
    return normalized


def _pinned_image(value: str) -> tuple[str, str]:
    reference = str(value).strip()
    if not reference.startswith("sha256:"):
        raise ValueError("news_program_compile_compiler_image_unpinned")
    if len(reference) != 71 or any(character not in "0123456789abcdef" for character in reference[7:]):
        raise ValueError("news_program_compile_compiler_image_invalid")
    return reference, reference


def _docker_executable(value: str | None) -> str:
    candidate = str(value or shutil.which("docker") or "").strip()
    if not candidate:
        raise ValueError("news_program_compile_container_runtime_unavailable")
    try:
        resolved = Path(candidate).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("news_program_compile_container_runtime_invalid") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("news_program_compile_container_runtime_invalid")
    return str(resolved)


def _rss_bytes(value: int | float) -> int:
    return max(0, round(value * 1_024 if sys.platform.startswith("linux") else value))


__all__ = [
    "PROXY_MODULE",
    "RUNNER_MODULE",
    "CompilerLauncherResult",
    "CompilerSandboxFailure",
    "ProgramCompilerLauncher",
]
