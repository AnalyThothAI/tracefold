"""Defense-in-depth guards inside the Docker-isolated compiler runner.

The trusted launcher in :mod:`program_compiler_launcher` owns the actual OS and
container capability boundary.  These irreversible child-local limits and
Python audit hooks are a second layer: they make ordinary violations fail with
stable codes, but they are never treated as proof that in-host GEPA is safe.
"""

from __future__ import annotations

import os
import resource
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...artifact_identity import canonical_sha

SANDBOX_POLICY_SCHEMA: Literal["tracefold.news.compiler_sandbox_policy.v2"] = (
    "tracefold.news.compiler_sandbox_policy.v2"
)
SANDBOX_LAUNCH_SCHEMA: Literal["tracefold.news.compiler_sandbox_launch.v3"] = (
    "tracefold.news.compiler_sandbox_launch.v3"
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUTF8",
        "TMPDIR",
    }
)
_DENIED_ENVIRONMENT_MARKERS = (
    "API_KEY",
    "AUTHORIZATION",
    "AWS_",
    "AZURE_",
    "CREDENTIAL",
    "DATABASE",
    "DB_DSN",
    "GCP_",
    "GH_",
    "GITHUB_",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "OPENAI_",
    "PASSWORD",
    "PGHOST",
    "PGPASSWORD",
    "PGSERVICE",
    "PGUSER",
    "SECRET",
    "TOKEN",
    "TRACEFOLD_",
)
_DENIED_AUDIT_EVENTS = frozenset(
    {
        "os.exec",
        "os.execve",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.spawn",
        "os.system",
        "pty.spawn",
        "subprocess.Popen",
    }
)


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompilerSandboxPolicy(_ExactModel):
    """Hashable policy enforced by both the trusted launcher and child."""

    schema_version: Literal["tracefold.news.compiler_sandbox_policy.v2"] = SANDBOX_POLICY_SCHEMA
    environment_policy: Literal["scrubbed_allowlist"] = "scrubbed_allowlist"
    filesystem_policy: Literal["readonly_runtime_and_input_writeonly_output"] = (
        "readonly_runtime_and_input_writeonly_output"
    )
    network_policy: Literal["trusted_unix_proxy_only"] = "trusted_unix_proxy_only"
    child_process_allowed: Literal[False] = False
    holdout_mounted: Literal[False] = False
    db_credentials_present: Literal[False] = False
    ambient_credentials_present: Literal[False] = False
    repository_writable: Literal[False] = False
    cache_allowed: Literal[False] = False
    telemetry_allowed: Literal[False] = False
    wall_timeout_seconds: int = Field(default=900, ge=1, le=3_600)
    max_cpu_seconds: int = Field(default=600, ge=1, le=3_600)
    max_rss_bytes: int = Field(default=2_147_483_648, ge=134_217_728, le=8_589_934_592)
    max_processes: Literal[1] = 1
    max_open_files: int = Field(default=64, ge=16, le=256)
    max_output_bytes: int = Field(default=2_000_000, ge=1_024, le=16_000_000)
    max_output_files: Literal[2] = 2
    max_stdout_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    max_stderr_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(
        cls,
        *,
        wall_timeout_seconds: int = 900,
        max_cpu_seconds: int = 600,
        max_rss_bytes: int = 2_147_483_648,
        max_output_bytes: int = 2_000_000,
    ) -> CompilerSandboxPolicy:
        values: dict[str, Any] = {
            "schema_version": SANDBOX_POLICY_SCHEMA,
            "environment_policy": "scrubbed_allowlist",
            "filesystem_policy": "readonly_runtime_and_input_writeonly_output",
            "network_policy": "trusted_unix_proxy_only",
            "child_process_allowed": False,
            "holdout_mounted": False,
            "db_credentials_present": False,
            "ambient_credentials_present": False,
            "repository_writable": False,
            "cache_allowed": False,
            "telemetry_allowed": False,
            "wall_timeout_seconds": wall_timeout_seconds,
            "max_cpu_seconds": max_cpu_seconds,
            "max_rss_bytes": max_rss_bytes,
            "max_processes": 1,
            "max_open_files": 64,
            "max_output_bytes": max_output_bytes,
            "max_output_files": 2,
            "max_stdout_bytes": 262_144,
            "max_stderr_bytes": 262_144,
        }
        return cls(**values, policy_sha256=canonical_sha(values))

    @model_validator(mode="after")
    def _policy_matches(self) -> CompilerSandboxPolicy:
        values = self.model_dump(mode="json", exclude={"policy_sha256"})
        if self.policy_sha256 != canonical_sha(values):
            raise ValueError("news_program_compile_sandbox_policy_hash_mismatch")
        return self


class CompilerSandboxLaunchReceipt(_ExactModel):
    """Trusted parent receipt; never self-attested by the optimizer child.

    Nine `*_payload` fields used to travel beside a `*_sha256` of that same embedded payload, and the
    receipt then hashed itself and five sibling receipts it already named. None of that survived into a
    second document, so none of it proved anything: what makes this tamper-evident is the compile record
    that embeds it, which is what the learning ledger is keyed on. The payloads themselves stay — they
    are the boundary evidence, and `_validate_boundary_semantics` still reads every one of them.
    """

    schema_version: Literal["tracefold.news.compiler_sandbox_launch.v3"] = SANDBOX_LAUNCH_SCHEMA
    policy: dict[str, Any] = Field(repr=False)
    input_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_preflight: dict[str, Any] = Field(repr=False)
    compiler_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    proxy_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    socket_volume: dict[str, Any] = Field(repr=False)
    lifecycle: dict[str, Any] = Field(repr=False)
    boundary_command: dict[str, Any] = Field(repr=False)
    boundary_inspect: dict[str, Any] = Field(repr=False)
    boundary_actuals_available: bool
    environment: dict[str, Any] = Field(repr=False)
    mount_manifest: dict[str, Any] = Field(repr=False)
    egress_manifest: dict[str, Any] = Field(repr=False)
    holdout_mounted: Literal[False] = False
    db_credentials_present: Literal[False] = False
    ambient_credentials_present: Literal[False] = False
    child_process_allowed: Literal[False] = False
    compiler_container_removed: bool
    proxy_container_removed: bool
    init_container_removed: bool
    socket_volume_removed: bool
    egress_network_removed: bool
    termination: Literal["succeeded", "failed", "timed_out", "killed"]
    exit_code: int | None = None
    wall_time_ms: int = Field(ge=0)
    launcher_cpu_time_ms: int = Field(ge=0)
    launcher_max_rss_bytes: int = Field(ge=0)
    container_actuals_available: Literal[False] = False
    container_max_cpu_seconds: int = Field(gt=0)
    container_max_rss_bytes: int = Field(gt=0)
    output_bytes: int = Field(ge=0)
    output_file_count: int = Field(ge=0)
    # These three address bytes the receipt deliberately does not retain.
    stdout_sha256: str = Field(pattern=_SHA256_PATTERN)
    stderr_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_root_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, **values: Any) -> CompilerSandboxLaunchReceipt:
        """The launcher states what it observed; this never asserts it on the launcher's behalf.

        `boundary_actuals_available` is computed from whether all four daemon inspections came back, and
        `_receipt_matches` then holds it to that. Defaulting it to True here both collided with the
        caller's own keyword and would have made the *failure* receipt — the one `CompilerSandboxFailure`
        carries, whose actuals are by definition incomplete — impossible to construct.
        """

        return cls(schema_version=SANDBOX_LAUNCH_SCHEMA, **values)

    @model_validator(mode="after")
    def _receipt_matches(self) -> CompilerSandboxLaunchReceipt:
        try:
            policy = CompilerSandboxPolicy.model_validate(self.policy)
        except ValueError as exc:
            raise ValueError("news_program_compile_sandbox_boundary_semantics_invalid") from exc
        expected_schemas = (
            (self.image_preflight, "tracefold.news.compiler_image_preflight.v2"),
            (self.socket_volume, "tracefold.news.compiler_socket_volume.v2"),
            (self.lifecycle, "tracefold.news.compiler_container_lifecycle.v2"),
            (self.boundary_command, "tracefold.news.compiler_boundary_commands.v2"),
            (self.boundary_inspect, "tracefold.news.compiler_boundary_inspections.v2"),
            (self.environment, "tracefold.news.compiler_container_environment.v2"),
            (self.mount_manifest, "tracefold.news.compiler_mount_manifest.v2"),
            (self.egress_manifest, "tracefold.news.compiler_egress_manifest.v2"),
        )
        if any(payload.get("schema") != schema for payload, schema in expected_schemas):
            raise ValueError("news_program_compile_sandbox_boundary_preimage_schema_invalid")
        inspect_keys = {"schema", "image_preflight", "network", "volume", "proxy", "compiler"}
        command_keys = {"schema", "volume_init", "proxy", "compiler"}
        actuals_complete = (
            set(self.boundary_inspect) == inspect_keys
            and self.boundary_inspect.get("image_preflight") == self.image_preflight
            and all(self.boundary_inspect.get(key) is not None for key in ("network", "volume", "proxy", "compiler"))
        )
        if set(self.boundary_command) != command_keys:
            raise ValueError("news_program_compile_sandbox_boundary_command_payload_invalid")
        if self.boundary_actuals_available != actuals_complete:
            raise ValueError("news_program_compile_sandbox_boundary_actuals_claim_invalid")
        lifecycle_flags = {
            "compiler_container_removed": self.compiler_container_removed,
            "proxy_container_removed": self.proxy_container_removed,
            "init_container_removed": self.init_container_removed,
            "socket_volume_removed": self.socket_volume_removed,
            "egress_network_removed": self.egress_network_removed,
        }
        if any(self.lifecycle.get(key) is not value for key, value in lifecycle_flags.items()):
            raise ValueError("news_program_compile_sandbox_lifecycle_claim_invalid")
        _validate_boundary_semantics(self, policy=policy)
        if self.termination == "succeeded":
            if not all(lifecycle_flags.values()):
                raise ValueError("news_program_compile_sandbox_cleanup_incomplete")
            if self.exit_code != 0 or self.output_root_sha256 is None:
                raise ValueError("news_program_compile_sandbox_success_receipt_invalid")
            if not self.boundary_actuals_available:
                raise ValueError("news_program_compile_sandbox_success_actuals_unavailable")
        elif self.output_root_sha256 is not None:
            raise ValueError("news_program_compile_sandbox_failed_output_forbidden")
        return self


def _validate_boundary_semantics(
    receipt: CompilerSandboxLaunchReceipt,
    *,
    policy: CompilerSandboxPolicy,
) -> None:
    """Cross-check retained daemon facts against the trusted launch contract."""

    try:
        _validate_boundary_payload_shapes(receipt)
        _validate_boundary_commands(receipt, policy=policy)
        network = receipt.boundary_inspect.get("network")
        volume = receipt.boundary_inspect.get("volume")
        proxy = receipt.boundary_inspect.get("proxy")
        compiler = receipt.boundary_inspect.get("compiler")
        network_sha = str(receipt.egress_manifest["proxy_network_sha256"])
        volume_name = str(receipt.socket_volume["name"])
        if network is not None:
            _validate_resource_actual(network, kind="network")
            if network["name_sha256"] != network_sha or network["driver"] != "bridge":
                raise ValueError
        if volume is not None:
            _validate_resource_actual(volume, kind="volume")
            if volume["name_sha256"] != canonical_sha({"name": volume_name}) or volume["driver"] != "local":
                raise ValueError
        if proxy is not None:
            _validate_container_actual(
                proxy,
                expected_image=receipt.proxy_image_digest,
                expected_network_sha256=network_sha,
                expected_pids_limit=32,
                expected_memory_bytes=1_073_741_824,
                expected_ulimits={},
                expected_mounts={
                    "/run/tracefold/config": ("bind", False),
                    "/run/tracefold/secrets": ("bind", False),
                    "/run/tracefold/proxy-receipt": ("bind", True),
                    "/run/tracefold/proxy": ("volume", True),
                },
                expected_volume_name=volume_name,
            )
        if compiler is not None:
            max_file_bytes = max(
                policy.max_output_bytes,
                policy.max_stdout_bytes,
                policy.max_stderr_bytes,
            )
            _validate_container_actual(
                compiler,
                expected_image=receipt.compiler_image_digest,
                expected_network_sha256=canonical_sha({"network": "none"}),
                expected_pids_limit=policy.max_processes,
                expected_memory_bytes=policy.max_rss_bytes,
                expected_ulimits={
                    "cpu": [policy.max_cpu_seconds, policy.max_cpu_seconds],
                    "fsize": [max_file_bytes, max_file_bytes],
                    "nofile": [policy.max_open_files, policy.max_open_files],
                },
                expected_mounts={
                    "/run/tracefold/input": ("bind", False),
                    "/run/tracefold/output": ("bind", True),
                    "/run/tracefold/proxy": ("volume", False),
                },
                expected_volume_name=volume_name,
            )
        if (
            receipt.container_max_cpu_seconds != policy.max_cpu_seconds
            or receipt.container_max_rss_bytes != policy.max_rss_bytes
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("news_program_compile_sandbox_boundary_semantics_invalid") from exc


def _validate_boundary_payload_shapes(receipt: CompilerSandboxLaunchReceipt) -> None:
    image = receipt.image_preflight
    if set(image) != {
        "schema",
        "image_id",
        "compiler_source_sha256",
        "proxy_source_sha256",
        "compiler_lock_sha256",
        "image_code_executed",
        "provider_config_mounted",
        "network",
        "pull_policy",
    } or (
        image["image_id"] != receipt.compiler_image_digest
        # The three source identities live in this payload alone now; the compile record's build
        # attestation is where they are compared against what the host and the container computed.
        or not _is_sha256(image["compiler_source_sha256"])
        or not _is_sha256(image["proxy_source_sha256"])
        or not _is_sha256(image["compiler_lock_sha256"])
        or image["image_code_executed"] is not False
        or image["provider_config_mounted"] is not False
        or image["network"] != "none"
        or image["pull_policy"] != "never"
    ):
        raise ValueError
    if set(receipt.socket_volume) != {"schema", "name"} or not str(receipt.socket_volume["name"]).startswith(
        "tracefold-compiler-"
    ):
        raise ValueError
    expected_environment = {
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
    if receipt.environment != expected_environment:
        raise ValueError
    expected_mount = {
        "schema": "tracefold.news.compiler_mount_manifest.v2",
        "input_bundle_sha256": receipt.input_bundle_sha256,
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
    if receipt.mount_manifest != expected_mount:
        raise ValueError
    egress = receipt.egress_manifest
    if set(egress) != {
        "schema",
        "compiler_network",
        "proxy_network_sha256",
        "proxy_grant_sha256",
        "compiler_other_network_allowed",
        "trusted_proxy_egress",
    } or (
        egress["compiler_network"] != "none"
        or not _is_sha256(egress["proxy_grant_sha256"])
        or egress["compiler_other_network_allowed"] is not False
        or egress["trusted_proxy_egress"] is not True
        or not _is_sha256(egress["proxy_network_sha256"])
    ):
        raise ValueError
    lifecycle = receipt.lifecycle
    if set(lifecycle) != {
        "schema",
        "compiler_container_sha256",
        "proxy_container_sha256",
        "socket_volume_sha256",
        "egress_network_sha256",
        "compiler_container_removed",
        "proxy_container_removed",
        "init_container_removed",
        "socket_volume_removed",
        "egress_network_removed",
    } or (
        lifecycle["socket_volume_sha256"] != canonical_sha({"name": receipt.socket_volume["name"]})
        or lifecycle["egress_network_sha256"] != egress["proxy_network_sha256"]
        or not _is_sha256(lifecycle["compiler_container_sha256"])
        or not _is_sha256(lifecycle["proxy_container_sha256"])
    ):
        raise ValueError


def _validate_boundary_commands(
    receipt: CompilerSandboxLaunchReceipt,
    *,
    policy: CompilerSandboxPolicy,
) -> None:
    init = _string_command(receipt.boundary_command["volume_init"])
    proxy = _string_command(receipt.boundary_command["proxy"])
    compiler = _string_command(receipt.boundary_command["compiler"])
    for command in (init, proxy, compiler):
        if len(command) < 4 or command[1] != "run" or _command_option(command, "--network") == "":
            raise ValueError
        if (
            command.count("--read-only") != 1
            or _command_option(command, "--cap-drop") != "ALL"
            or _command_option(command, "--security-opt") != "no-new-privileges:true"
            or command.count(receipt.compiler_image_digest) != 1
        ):
            raise ValueError
    if _command_option(init, "--network") != "none" or _command_option(init, "--pids-limit") != "1":
        raise ValueError
    if (
        _command_option(proxy, "--pids-limit") != "32"
        or _command_option(proxy, "--memory") != "1073741824"
        or _command_option(proxy, "--memory-swap") != "1073741824"
        or _command_option(proxy, "--cpus") != "1.0"
        or _command_option(proxy, "--ipc") != "none"
        or _command_option(proxy, "--log-driver") != "none"
    ):
        raise ValueError
    proxy_network = _command_option(proxy, "--network")
    if canonical_sha({"name": proxy_network}) != receipt.egress_manifest["proxy_network_sha256"]:
        raise ValueError
    max_file_bytes = max(policy.max_output_bytes, policy.max_stdout_bytes, policy.max_stderr_bytes)
    if (
        _command_option(compiler, "--network") != "none"
        or _command_option(compiler, "--pids-limit") != str(policy.max_processes)
        or _command_option(compiler, "--memory") != str(policy.max_rss_bytes)
        or _command_option(compiler, "--memory-swap") != str(policy.max_rss_bytes)
        or _command_option(compiler, "--cpus") != "1.0"
        or _command_option(compiler, "--ipc") != "none"
        or _command_option(compiler, "--log-driver") != "none"
        or set(_command_options(compiler, "--ulimit"))
        != {
            f"cpu={policy.max_cpu_seconds}:{policy.max_cpu_seconds}",
            f"fsize={max_file_bytes}:{max_file_bytes}",
            f"nofile={policy.max_open_files}:{policy.max_open_files}",
        }
    ):
        raise ValueError
    proxy_actual = receipt.boundary_inspect.get("proxy")
    compiler_actual = receipt.boundary_inspect.get("compiler")
    if proxy_actual is not None and _command_option(proxy, "--user") != proxy_actual.get("user"):
        raise ValueError
    if compiler_actual is not None and _command_option(compiler, "--user") != compiler_actual.get("user"):
        raise ValueError


def _validate_resource_actual(value: Any, *, kind: Literal["network", "volume"]) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "kind",
            "name_sha256",
            "daemon_identity_sha256",
            "driver",
            "scope",
        }
        or (
            value["kind"] != kind
            or not _is_sha256(value["name_sha256"])
            or not _is_sha256(value["daemon_identity_sha256"])
        )
    ):
        raise ValueError


def _validate_container_actual(
    value: Any,
    *,
    expected_image: str,
    expected_network_sha256: str,
    expected_pids_limit: int,
    expected_memory_bytes: int,
    expected_ulimits: Mapping[str, list[int]],
    expected_mounts: Mapping[str, tuple[Literal["bind", "volume"], bool]],
    expected_volume_name: str,
) -> None:
    expected_keys = {
        "kind",
        "container_id_sha256",
        "name_sha256",
        "image",
        "network_sha256",
        "readonly_rootfs",
        "cap_drop",
        "security_opt",
        "pids_limit",
        "user",
        "environment_sha256",
        "ipc_mode",
        "log_config",
        "tmpfs",
        "memory",
        "memory_swap",
        "nano_cpus",
        "ulimits",
        "mounts",
    }
    expected_tmpfs = {
        "/tmp": ["nodev", "noexec", "nosuid", "rw", "size=67108864"],  # noqa: S108
        "/run/tracefold/home": ["nodev", "noexec", "nosuid", "rw", "size=1048576"],
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_keys
        or (
            value["kind"] != "container"
            or not _is_sha256(value["container_id_sha256"])
            or not _is_sha256(value["name_sha256"])
            or value["image"] != expected_image
            or value["network_sha256"] != expected_network_sha256
            or value["readonly_rootfs"] is not True
            or value["cap_drop"] != ["ALL"]
            or value["security_opt"] != ["no-new-privileges:true"]
            or value["pids_limit"] != expected_pids_limit
            or not isinstance(value["user"], str)
            or not value["user"]
            or not _is_sha256(value["environment_sha256"])
            or value["ipc_mode"] != "none"
            or value["log_config"] != {"Type": "none", "Config": {}}
            or value["tmpfs"] != expected_tmpfs
            or value["memory"] != expected_memory_bytes
            or value["memory_swap"] != expected_memory_bytes
            or value["nano_cpus"] != 1_000_000_000
            or value["ulimits"] != dict(expected_ulimits)
            or not isinstance(value["mounts"], list)
        )
    ):
        raise ValueError
    mounts = value["mounts"]
    if len(mounts) != len(expected_mounts):
        raise ValueError
    actual_mounts: dict[str, Mapping[str, Any]] = {}
    for item in mounts:
        if not isinstance(item, Mapping) or set(item) != {"destination", "type", "writable", "source_sha256"}:
            raise ValueError
        actual_mounts[str(item["destination"])] = item
    if set(actual_mounts) != set(expected_mounts):
        raise ValueError
    volume_source_sha = canonical_sha({"source": expected_volume_name})
    for destination, (mount_type, writable) in expected_mounts.items():
        item = actual_mounts[destination]
        if item["type"] != mount_type or item["writable"] is not writable or not _is_sha256(item["source_sha256"]):
            raise ValueError
        if mount_type == "volume" and item["source_sha256"] != volume_source_sha:
            raise ValueError


def _string_command(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ValueError
    return tuple(value)


def _command_option(command: tuple[str, ...], option: str) -> str:
    values = _command_options(command, option)
    if len(values) != 1:
        raise ValueError
    return values[0]


def _command_options(command: tuple[str, ...], option: str) -> tuple[str, ...]:
    indexes = tuple(index for index, value in enumerate(command) if value == option)
    if any(index + 1 >= len(command) for index in indexes):
        raise ValueError
    return tuple(command[index + 1] for index in indexes)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def scrubbed_compiler_environment(*, sandbox_home: Path, sandbox_tmp: Path) -> dict[str, str]:
    """Build the entire child environment without copying the parent environment."""

    home = _resolved_directory(sandbox_home, code="sandbox_home")
    temporary = _resolved_directory(sandbox_tmp, code="sandbox_tmp")
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "TMPDIR": str(temporary),
    }
    validate_compiler_environment(environment, sandbox_home=home, sandbox_tmp=temporary)
    return environment


def validate_compiler_environment(
    environment: Mapping[str, str],
    *,
    sandbox_home: Path,
    sandbox_tmp: Path,
) -> None:
    """Fail closed if a launcher accidentally forwards an ambient capability."""

    if set(environment) != _SAFE_ENVIRONMENT_KEYS:
        raise ValueError("news_program_compile_sandbox_environment_keys_invalid")
    for key, value in environment.items():
        normalized = key.upper()
        if any(marker in normalized for marker in _DENIED_ENVIRONMENT_MARKERS):
            raise ValueError("news_program_compile_sandbox_ambient_credential")
        if "\x00" in str(value):
            raise ValueError("news_program_compile_sandbox_environment_value_invalid")
    expected_home = _resolved_directory(sandbox_home, code="sandbox_home")
    expected_tmp = _resolved_directory(sandbox_tmp, code="sandbox_tmp")
    if Path(environment["HOME"]).resolve() != expected_home or Path(environment["TMPDIR"]).resolve() != expected_tmp:
        raise ValueError("news_program_compile_sandbox_environment_path_invalid")


def environment_manifest_sha256(environment: Mapping[str, str]) -> str:
    """Hash environment shape and sandbox-local path identities, never parent data."""

    return canonical_sha({key: str(value) for key, value in sorted(environment.items())})


def apply_compiler_resource_limits(policy: CompilerSandboxPolicy) -> None:
    """Apply child-local hard limits immediately before the runner exec."""

    _set_hard_limit(resource.RLIMIT_CORE, 0)
    _set_hard_limit(resource.RLIMIT_CPU, policy.max_cpu_seconds)
    _set_hard_limit(resource.RLIMIT_AS, policy.max_rss_bytes)
    _set_hard_limit(resource.RLIMIT_NOFILE, policy.max_open_files)
    _set_hard_limit(
        resource.RLIMIT_FSIZE,
        max(policy.max_output_bytes, policy.max_stdout_bytes, policy.max_stderr_bytes),
    )
    os.umask(0o077)


def install_compiler_sandbox_guards(
    *,
    readonly_roots: Sequence[Path],
    output_root: Path,
    proxy_socket: Path,
) -> str:
    """Install irreversible Python audit guards in the optimizer child.

    This must run after the fixed runner and dependencies are imported but before
    optimizer-controlled code executes.  An audit hook cannot replace the
    production container boundary; it makes violations deterministic and blocks
    the ordinary Python/socket/subprocess paths used by DSPy dependencies.
    """

    readable = tuple(_resolved_directory(root, code="sandbox_readonly_root") for root in readonly_roots)
    writable = _resolved_directory(output_root, code="sandbox_output_root")
    proxy = _resolved_socket_parent(proxy_socket)

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event in _DENIED_AUDIT_EVENTS or event.startswith("subprocess."):
            raise PermissionError("news_program_compile_sandbox_child_process_denied")
        if event == "socket.bind":
            raise PermissionError("news_program_compile_sandbox_network_denied")
        if event == "socket.connect":
            address = args[1] if len(args) > 1 else None
            if not isinstance(address, str) or Path(address).resolve() != proxy:
                raise PermissionError("news_program_compile_sandbox_network_denied")
        if event == "open" and args:
            raw_path = args[0]
            if not isinstance(raw_path, (str, bytes, os.PathLike)):
                return
            try:
                path = Path(os.fsdecode(raw_path)).resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                raise PermissionError("news_program_compile_sandbox_filesystem_denied") from exc
            mode = str(args[1] or "r") if len(args) > 1 else "r"
            flags = int(args[2] or 0) if len(args) > 2 else 0
            writing = any(marker in mode for marker in ("w", "a", "+", "x")) or bool(
                flags & (os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR)
            )
            if writing:
                if not _within(path, writable):
                    raise PermissionError("news_program_compile_sandbox_filesystem_denied")
            elif not any(_within(path, root) for root in (*readable, writable)):
                raise PermissionError("news_program_compile_sandbox_filesystem_denied")

    sys.addaudithook(audit)
    return canonical_sha(
        {
            "guard": "tracefold.news.compiler_python_audit_guard.v2",
            "readonly_root_count": len(readable),
            "output_root": canonical_sha({"path": str(writable)}),
            "proxy_socket": canonical_sha({"path": str(proxy)}),
            "denied_audit_events": sorted(_DENIED_AUDIT_EVENTS),
        }
    )


def verify_sandbox_output_directory(output_root: Path, *, policy: CompilerSandboxPolicy) -> str:
    """Accept exactly patch.json and runner_receipts.json under declared bounds."""

    root = _resolved_directory(output_root, code="sandbox_output_root")
    children = tuple(root.iterdir())
    if {child.name for child in children} != {"patch.json", "runner_receipts.json"}:
        raise ValueError("news_program_compile_sandbox_output_files_invalid")
    total_bytes = 0
    identities: dict[str, str] = {}
    for child in children:
        if child.is_symlink() or not child.is_file() or child.resolve().parent != root:
            raise ValueError("news_program_compile_sandbox_output_files_invalid")
        document = child.read_bytes()
        total_bytes += len(document)
        identities[child.name] = canonical_sha({"document": document.decode("utf-8")})
    if len(children) > policy.max_output_files or total_bytes > policy.max_output_bytes:
        raise ValueError("news_program_compile_sandbox_output_budget_exceeded")
    return canonical_sha(identities)


def _resolved_directory(path: Path, *, code: str) -> Path:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"news_program_compile_{code}_invalid") from exc
    if candidate.is_symlink() or not resolved.is_dir():
        raise ValueError(f"news_program_compile_{code}_invalid")
    return resolved


def _resolved_socket_parent(path: Path) -> Path:
    candidate = Path(path)
    parent = _resolved_directory(candidate.parent, code="sandbox_proxy_parent")
    resolved = parent / candidate.name
    if candidate.exists() and candidate.is_symlink():
        raise ValueError("news_program_compile_sandbox_proxy_socket_invalid")
    return resolved


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _set_hard_limit(kind: int, requested: int) -> None:
    _, existing_hard = resource.getrlimit(kind)
    limit = requested if existing_hard == resource.RLIM_INFINITY else min(requested, existing_hard)
    resource.setrlimit(kind, (limit, limit))


__all__ = [
    "SANDBOX_LAUNCH_SCHEMA",
    "SANDBOX_POLICY_SCHEMA",
    "CompilerSandboxLaunchReceipt",
    "CompilerSandboxPolicy",
    "apply_compiler_resource_limits",
    "environment_manifest_sha256",
    "install_compiler_sandbox_guards",
    "scrubbed_compiler_environment",
    "validate_compiler_environment",
    "verify_sandbox_output_directory",
]
