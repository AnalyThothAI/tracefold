"""Fixed entrypoint inside the isolated compiler image.

The Docker launcher clears the image environment before importing this module.
This runner revalidates the sealed input, installs child-local defense-in-depth
guards, runs bounded GEPA through the Unix proxy, and writes exactly one typed
patch plus one receipt document.  It has no in-process provider or database
fallback.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_INPUT_PATH = Path("/run/tracefold/input/bundle.json")
_POLICY_PATH = Path("/run/tracefold/input/policy.json")
_OUTPUT_PATH = Path("/run/tracefold/output")
_PROXY_PATH = Path("/run/tracefold/proxy/compiler.sock")
_SAFE_ENVIRONMENT = {
    "HOME": "/run/tracefold/home",
    "TMPDIR": "/tmp",  # noqa: S108 - container-private tmpfs
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONUTF8": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def main(argv: Sequence[str] | None = None) -> int:
    os.environ.clear()
    os.environ.update(_SAFE_ENVIRONMENT)
    try:
        paths = _parse_exact_args(tuple(sys.argv[1:] if argv is None else argv))
        _run(*paths)
    except Exception as exc:
        code = getattr(exc, "code", str(exc))
        if not isinstance(code, str) or not code.startswith("news_program_compile_"):
            code = "news_program_compile_runner_failed"
        sys.stderr.write(code + "\n")
        return 2
    return 0


def _run(input_path: Path, output_path: Path, policy_path: Path, proxy_path: Path) -> None:
    from tracefold.news.agents.program_compiler import (
        CompileBudget,
        CompileRequest,
        ProgramCompiler,
    )
    from tracefold.news.agents.program_compiler_proxy import (
        CompilerModelProxyGrant,
        CompilerProxyLM,
    )
    from tracefold.news.agents.program_compiler_sandbox import (
        CompilerSandboxPolicy,
        apply_compiler_resource_limits,
        install_compiler_sandbox_guards,
        validate_compiler_environment,
    )
    from tracefold.news.agents.program_compiler_security import (
        CompileInputBundle,
        CompilerRunnerReceiptsV2,
    )
    from tracefold.news.agents.program_compiler_source import (
        compiler_source_sha256,
        proxy_source_sha256,
    )
    from tracefold.news.agents.program_compiler_trusted import build_eligible_demo_bank
    from tracefold.news.agents.semantic_program import load_stable_program_artifact
    from tracefold.news.artifact_identity import canonical_json

    _require_fixed_path(input_path, _INPUT_PATH, kind="input")
    _require_fixed_path(output_path, _OUTPUT_PATH, kind="output")
    _require_fixed_path(policy_path, _POLICY_PATH, kind="policy")
    _require_fixed_path(proxy_path, _PROXY_PATH, kind="proxy")
    if not input_path.is_file() or input_path.is_symlink() or not policy_path.is_file() or policy_path.is_symlink():
        raise ValueError("news_program_compile_runner_input_invalid")
    if output_path.is_symlink() or not output_path.is_dir() or any(output_path.iterdir()):
        raise ValueError("news_program_compile_runner_output_invalid")
    if proxy_path.is_symlink() or not proxy_path.exists() or not stat.S_ISSOCK(proxy_path.stat().st_mode):
        raise ValueError("news_program_compile_runner_proxy_invalid")

    policy = CompilerSandboxPolicy.model_validate(_strict_json_loads(policy_path.read_text(encoding="utf-8")))
    bundle = CompileInputBundle.model_validate(_strict_json_loads(input_path.read_text(encoding="utf-8")))
    validate_compiler_environment(
        os.environ,
        sandbox_home=Path(_SAFE_ENVIRONMENT["HOME"]),
        sandbox_tmp=Path(_SAFE_ENVIRONMENT["TMPDIR"]),
    )
    parent = load_stable_program_artifact()
    if (
        parent.program_sha256 != bundle.parent_program_sha256
        or parent.state_sha256 != bundle.parent_state_sha256
        or parent.parent_program_sha256 is not None
        or parent.schema_version != "news_semantic_program_artifact_v2"
        or parent.factory_id != "tracefold.news.semantic_program.factory_v3"
        or parent.quality_kernel.dependency_lock_sha256 != bundle.compiler_lock_sha256
        or policy.policy_sha256 != bundle.sandbox_policy_sha256
        or compiler_source_sha256() != bundle.compiler_source_sha256
        or proxy_source_sha256() != bundle.proxy_source_sha256
    ):
        raise ValueError("news_program_compile_runner_parent_identity_mismatch")
    episodes = tuple(bundle.episodes)
    eligible_demo_bank = build_eligible_demo_bank(
        dataset_sha=bundle.corpus.development_dataset_sha,
        dataset_payload=bundle.dataset_payload,
        episodes=episodes,
    )
    if eligible_demo_bank.eligible_demo_bank_root_sha256 != bundle.eligible_demo_bank_root_sha256:
        raise ValueError("news_program_compile_runner_demo_bank_root_mismatch")
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
    if grant.grant_sha256 != bundle.proxy_grant_sha256:
        raise ValueError("news_program_compile_runner_proxy_grant_mismatch")
    if grant.max_call_cost_microusd != bundle.budget.max_call_cost_microusd:
        raise ValueError("news_program_compile_runner_proxy_reservation_mismatch")

    package_root = Path(__file__).resolve().parents[3]
    readonly_roots = tuple(
        dict.fromkeys(
            (
                input_path.parent,
                package_root,
                Path(sys.prefix),
                Path(sys.base_prefix),
            )
        )
    )
    apply_compiler_resource_limits(policy)
    install_compiler_sandbox_guards(
        readonly_roots=readonly_roots,
        output_root=output_path,
        proxy_socket=proxy_path,
    )

    request = CompileRequest(
        development_dataset_sha=bundle.corpus.development_dataset_sha,
        episodes=episodes,
        budget=CompileBudget.model_validate(bundle.budget.model_dump(mode="json")),
    )
    compiler = ProgramCompiler(
        base_artifact=parent,
        eligible_demo_bank=eligible_demo_bank,
        task_lm=CompilerProxyLM(
            socket_path=proxy_path,
            grant=grant,
            role="task",
            timeout_seconds=policy.wall_timeout_seconds,
        ),
        reflection_lm=CompilerProxyLM(
            socket_path=proxy_path,
            grant=grant,
            role="reflection",
            timeout_seconds=policy.wall_timeout_seconds,
        ),
    )
    result = compiler.compile(request)
    receipts = CompilerRunnerReceiptsV2.issue(
        input_bundle_sha256=bundle.bundle_sha256,
        parent_program_sha256=parent.program_sha256,
        parent_state_sha256=parent.state_sha256,
        proxy_grant_sha256=grant.grant_sha256,
        task_endpoint_identity_sha256=bundle.task_endpoint.binding_sha256,
        reflection_endpoint_identity_sha256=bundle.reflection_endpoint.binding_sha256,
        compiler_source_sha256=compiler_source_sha256(),
        proxy_source_sha256=proxy_source_sha256(),
        compiler_lock_sha256=bundle.compiler_lock_sha256,
        sandbox_policy_sha256=policy.policy_sha256,
        metric=result.receipt_payloads.metric,
        optimizer_config=result.receipt_payloads.optimizer_config,
        trajectory=result.receipt_payloads.trajectory,
        checkpoint=result.receipt_payloads.checkpoint,
        failure_cluster_ids=result.failure_cluster_ids,
        target_dimensions=result.target_dimensions,
        metric_calls=result.metric_calls,
        task_model_calls=result.task_model_calls,
        reflection_model_calls=result.reflection_model_calls,
        actual_cost_microusd=result.actual_cost_microusd,
    )
    (output_path / "patch.json").write_text(
        canonical_json(result.patch.model_dump(mode="json")),
        encoding="utf-8",
    )
    (output_path / "runner_receipts.json").write_text(
        canonical_json(receipts.model_dump(mode="json")),
        encoding="utf-8",
    )


def _parse_exact_args(argv: tuple[str, ...]) -> tuple[Path, Path, Path, Path]:
    expected_flags = ("--input", "--output", "--policy", "--proxy-socket")
    if len(argv) != 8 or tuple(argv[index] for index in range(0, 8, 2)) != expected_flags:
        raise ValueError("news_program_compile_runner_arguments_invalid")
    return Path(argv[1]), Path(argv[3]), Path(argv[5]), Path(argv[7])


def _require_fixed_path(value: Path, expected: Path, *, kind: str) -> None:
    if value != expected or not value.is_absolute() or any(part == ".." for part in value.parts):
        raise ValueError(f"news_program_compile_runner_{kind}_path_invalid")


def _strict_json_loads(document: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"news_program_compile_runner_duplicate_key:{key}")
            payload[key] = value
        return payload

    return json.loads(
        document,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"news_program_compile_runner_nonfinite:{value}")
        ),
    )


if __name__ == "__main__":  # pragma: no cover - fixed container entrypoint
    raise SystemExit(main())
