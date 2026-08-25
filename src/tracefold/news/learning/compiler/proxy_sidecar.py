"""Fixed trusted model-proxy sidecar entrypoint.

The sidecar receives no compiler corpus or output mount.  It reads one
secret-free grant and one ephemeral provider secret file, owns the only
provider egress, serves a Unix socket in a dedicated Docker volume, and writes
only secret-free lifecycle receipts to a separate private host mount.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_GRANT_PATH = Path("/run/tracefold/config/grant.json")
_SECRET_PATH = Path("/run/tracefold/secrets/provider.json")
_SOCKET_PATH = Path("/run/tracefold/proxy/compiler.sock")
_OUTPUT_PATH = Path("/run/tracefold/proxy-receipt")
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
            code = "news_program_compile_proxy_sidecar_failed"
        sys.stderr.write(code + "\n")
        return 2
    return 0


def _run(grant_path: Path, secret_path: Path, socket_path: Path, output_path: Path) -> None:
    from tracefold.news.artifact_identity import canonical_json
    from tracefold.news.learning.compiler.proxy import (
        CompilerModelProxyGrant,
        CompilerProxyReadyReceipt,
        CompilerProxySecretConfig,
        TrustedCompilerModelProxy,
        build_proxy_provider_lm,
    )
    from tracefold.news.learning.compiler.source_identity import proxy_source_sha256

    _require_fixed_path(grant_path, _GRANT_PATH, kind="grant")
    _require_fixed_path(secret_path, _SECRET_PATH, kind="secret")
    _require_fixed_path(socket_path, _SOCKET_PATH, kind="socket")
    _require_fixed_path(output_path, _OUTPUT_PATH, kind="output")
    if (
        not grant_path.is_file()
        or grant_path.is_symlink()
        or not secret_path.is_file()
        or secret_path.is_symlink()
        or stat.S_IMODE(secret_path.stat().st_mode) & 0o077
    ):
        raise ValueError("news_program_compile_proxy_sidecar_config_invalid")
    if output_path.is_symlink() or not output_path.is_dir() or any(output_path.iterdir()):
        raise ValueError("news_program_compile_proxy_sidecar_output_invalid")
    if socket_path.exists() or socket_path.parent.is_symlink() or not socket_path.parent.is_dir():
        raise ValueError("news_program_compile_proxy_sidecar_socket_invalid")

    grant = CompilerModelProxyGrant.model_validate(_strict_json_loads(grant_path.read_text(encoding="utf-8")))
    secrets = CompilerProxySecretConfig.model_validate(_strict_json_loads(secret_path.read_text(encoding="utf-8")))
    if (
        secrets.task.binding("task") != grant.task
        or secrets.reflection.binding("reflection") != grant.reflection
        or secrets.metric_judge.binding("metric_judge") != grant.metric_judge
        or secrets.tariff != grant.tariff
        or secrets.secret_free_config_sha256 != grant.proxy_config_sha256
        or proxy_source_sha256() != grant.proxy_source_sha256
    ):
        raise ValueError("news_program_compile_proxy_sidecar_grant_mismatch")
    proxy = TrustedCompilerModelProxy(
        grant=grant,
        task_lm=build_proxy_provider_lm(secrets.task, role="task"),
        reflection_lm=build_proxy_provider_lm(secrets.reflection, role="reflection"),
        metric_judge_lm=build_proxy_provider_lm(secrets.metric_judge, role="metric_judge"),
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with proxy.serve(socket_path):
        ready = CompilerProxyReadyReceipt.issue(
            grant=grant,
            proxy_source_sha256=proxy_source_sha256(),
        )
        (output_path / "ready.json").write_text(
            canonical_json(ready.model_dump(mode="json")),
            encoding="utf-8",
        )
        stop.wait()
    receipt = proxy.execution_receipt()
    (output_path / "execution.json").write_text(
        canonical_json(receipt.model_dump(mode="json")),
        encoding="utf-8",
    )


def _parse_exact_args(argv: tuple[str, ...]) -> tuple[Path, Path, Path, Path]:
    expected_flags = ("--grant", "--secrets", "--socket", "--output")
    if len(argv) != 8 or tuple(argv[index] for index in range(0, 8, 2)) != expected_flags:
        raise ValueError("news_program_compile_proxy_sidecar_arguments_invalid")
    return Path(argv[1]), Path(argv[3]), Path(argv[5]), Path(argv[7])


def _require_fixed_path(value: Path, expected: Path, *, kind: str) -> None:
    if value != expected or not value.is_absolute() or any(part == ".." for part in value.parts):
        raise ValueError(f"news_program_compile_proxy_sidecar_{kind}_path_invalid")


def _strict_json_loads(document: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"news_program_compile_proxy_sidecar_duplicate_key:{key}")
            payload[key] = value
        return payload

    return json.loads(
        document,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"news_program_compile_proxy_sidecar_nonfinite:{value}")
        ),
    )


if __name__ == "__main__":  # pragma: no cover - fixed sidecar entrypoint
    raise SystemExit(main())
