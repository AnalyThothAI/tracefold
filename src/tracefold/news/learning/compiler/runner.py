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
    from tracefold.news.artifact_identity import canonical_json
    from tracefold.news.learning.compiler.root import (
        CompileBudget,
        CompileRequest,
        ProgramCompiler,
    )
    from tracefold.news.learning.compiler.sandbox import (
        CompilerSandboxPolicy,
        apply_compiler_resource_limits,
        install_compiler_sandbox_guards,
        validate_compiler_environment,
    )
    from tracefold.news.learning.compiler.security import (
        CompileInputBundle,
        CompilerRunnerReceiptsV3,
    )
    from tracefold.news.learning.compiler.source_identity import (
        compiler_source_sha256,
        proxy_source_sha256,
    )
    from tracefold.news.program.artifact import load_stable_program_artifact
    from tracefold.news.program.runtime import PROGRAM_FACTORY_ID, PROGRAM_SCHEMA_VERSION

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
        or parent.schema_version != PROGRAM_SCHEMA_VERSION
        or parent.factory_id != PROGRAM_FACTORY_ID
        or policy.policy_sha256 != bundle.sandbox_policy_sha256
        or compiler_source_sha256() != bundle.compiler_source_sha256
        or proxy_source_sha256() != bundle.proxy_source_sha256
    ):
        raise ValueError("news_program_compile_runner_parent_identity_mismatch")
    episodes = tuple(bundle.episodes)
    grant, task_lm, reflection_lm, judge = _build_compiler_proxy_runtime(bundle, proxy_path)

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
        review_rubric_version=bundle.corpus.review_rubric_version,
        episodes=episodes,
        budget=CompileBudget.model_validate(bundle.budget.model_dump(mode="json")),
    )
    compiler = ProgramCompiler(
        base_artifact=parent,
        # Same trusted rates the proxy reserves against. Without it `_BudgetMeter` still fails closed on the
        # `None` cost every endpoint this project uses actually returns.
        tariff=grant.tariff,
        task_lm=task_lm,
        reflection_lm=reflection_lm,
        judge=judge,
    )
    result = compiler.compile(request)
    receipts = CompilerRunnerReceiptsV3.issue(
        input_bundle_sha256=bundle.bundle_sha256,
        parent_program_sha256=parent.program_sha256,
        proxy_grant_sha256=grant.grant_sha256,
        task_endpoint_identity_sha256=bundle.task.endpoint.binding_sha256,
        reflection_endpoint_identity_sha256=bundle.reflection.endpoint.binding_sha256,
        metric_judge_endpoint_identity_sha256=bundle.metric_judge.endpoint.binding_sha256,
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
        metric_judge_attempts=result.metric_judge_attempts,
        metric_judge_model_calls=result.metric_judge_model_calls,
        metric_judge_failures=result.metric_judge_failures,
        task_cost_microusd=result.task_cost_microusd,
        reflection_cost_microusd=result.reflection_cost_microusd,
        metric_judge_cost_microusd=result.metric_judge_cost_microusd,
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


def _build_compiler_proxy_runtime(bundle: Any, proxy_path: Path) -> tuple[Any, Any, Any, Any]:
    """Rebuild the sealed three-role grant and clients used by the container runner."""

    from tracefold.news.learning.compiler.proxy import CompilerModelProxyGrant, CompilerProxyLM
    from tracefold.news.learning.compiler.security import CompileInputBundle
    from tracefold.news.learning.judge import CardEquivalenceJudge

    parsed = bundle if isinstance(bundle, CompileInputBundle) else CompileInputBundle.model_validate(bundle)
    grant = CompilerModelProxyGrant.issue(
        task=parsed.task,
        reflection=parsed.reflection,
        metric_judge=parsed.metric_judge,
        max_task_model_calls=parsed.budget.max_task_model_calls,
        max_reflection_model_calls=parsed.budget.max_reflection_model_calls,
        max_metric_judge_model_calls=parsed.budget.max_metric_judge_model_calls,
        max_cost_microusd=parsed.budget.max_cost_microusd,
        tariff=parsed.proxy_tariff,
        proxy_config_sha256=parsed.proxy_config_sha256,
        proxy_source_sha256=parsed.proxy_source_sha256,
    )
    if grant.grant_sha256 != parsed.proxy_grant_sha256:
        raise ValueError("news_program_compile_runner_proxy_grant_mismatch")
    if grant.max_call_cost_microusd != parsed.budget.max_call_cost_microusd:
        raise ValueError("news_program_compile_runner_proxy_reservation_mismatch")
    task_lm = CompilerProxyLM(socket_path=proxy_path, grant=grant, role="task")
    reflection_lm = CompilerProxyLM(socket_path=proxy_path, grant=grant, role="reflection")
    metric_judge_lm = CompilerProxyLM(socket_path=proxy_path, grant=grant, role="metric_judge")
    judge = CardEquivalenceJudge(
        metric_judge_lm,
        max_tokens=parsed.metric_judge.max_output_tokens,
        max_model_calls=parsed.budget.max_metric_judge_model_calls,
        require_exact_accounting=True,
    )
    return grant, task_lm, reflection_lm, judge


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
