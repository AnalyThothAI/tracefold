"""Configuration ownership, actual Compose interpolation and cold-start contracts."""

from __future__ import annotations

import ast
import json
import os
import shlex
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from scripts.deploy import Deployment

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.deploy


def test_config_home_has_one_explicit_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tracefold.platform.paths import app_home, config_path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TRACEFOLD_HOME", raising=False)
    assert app_home() == tmp_path / ".tracefold"
    selected = tmp_path / "operator"
    monkeypatch.setenv("TRACEFOLD_HOME", str(selected))
    assert config_path() == selected / "config.yaml"
    assert app_home(tmp_path / "override") == tmp_path / "override"


def test_init_preserves_config_passwords_and_private_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tracefold.app.cli.commands.config import handle_init

    home = tmp_path / "operator"
    monkeypatch.setenv("TRACEFOLD_HOME", str(home))
    assert handle_init(Namespace(force=False))[0] == 0
    names = ("config.yaml", "postgres_password", "postgres_database_password", "telegram_bot_token")
    before = {name: (home / name).read_bytes() for name in names}
    (home / "config.yaml").chmod(0o644)
    assert handle_init(Namespace(force=False))[0] == 0
    assert {name: (home / name).read_bytes() for name in names} == before
    assert all((home / name).stat().st_mode & 0o777 == 0o600 for name in names)
    assert home.stat().st_mode & 0o777 == 0o700
    assert all((home / name).is_dir() for name in ("cache", "logs", "archive"))


def test_init_refuses_password_symlinks_without_modifying_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tracefold.app.cli.commands.config import handle_init

    home = tmp_path / "operator"
    home.mkdir()
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    (home / "postgres_database_password").symlink_to(target)
    monkeypatch.setenv("TRACEFOLD_HOME", str(home))
    with pytest.raises(ValueError, match="postgres_password_path_not_file"):
        handle_init(Namespace(force=False))
    assert target.read_text() == "unchanged"


def test_real_compose_persists_env_and_make_override_without_leaking_secrets(tmp_path: Path) -> None:
    shutil.copy(ROOT / "compose.yaml", tmp_path)
    shutil.copy(ROOT / "Makefile", tmp_path)
    shutil.copytree(ROOT / "make", tmp_path / "make")
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/deploy.py", tmp_path / "scripts/deploy.py")
    home = tmp_path / "private"
    (tmp_path / ".env").write_text(
        f"COMPOSE_PROJECT_NAME=tracefold-config-test\nTRACEFOLD_HOME={home}\n"
        "TRACEFOLD_API_PORT=18765\nTRACEFOLD_RABBITMQ_PASSWORD=do-not-print-this\n"
    )
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("TRACEFOLD_", "COMPOSE_"))}
    for override, expected in (([], 18765), (["TRACEFOLD_API_PORT=18799"], 18799)):
        result = subprocess.run(
            ["make", "--no-print-directory", "topology", *override],
            cwd=tmp_path,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        assert payload["project"] == "tracefold-config-test"
        assert payload["config"] == str(home / "config.yaml")
        assert payload["urls"]["serve"] == f"http://127.0.0.1:{expected}"
        assert "do-not-print-this" not in result.stdout + result.stderr
    # Rendering only: no Docker daemon, containers, volumes or operator files are changed.
    assert not home.exists()


@pytest.mark.parametrize(
    ("state", "exit_code"), [("disabled", 0), ("running", 0), ("unavailable", 1), ("model_unconfigured", 1)]
)
def test_analysis_probe_accepts_deliberate_disable_but_not_broken_enabled_loop(state: str, exit_code: int) -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    command = compose["services"]["analysis"]["healthcheck"]["test"][1]
    arguments = shlex.split(command.split("|", 1)[1])
    assert arguments[:2] == ["python", "-c"]
    result = subprocess.run(
        [sys.executable, *arguments[1:]],
        input=json.dumps({"data": {"decision": {"state": state}}}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == exit_code


def test_deployment_imports_no_application_or_third_party_dependencies() -> None:
    tree = ast.parse((ROOT / "scripts/deploy.py").read_text())
    roots = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert roots <= sys.stdlib_module_names


def test_private_inputs_are_excluded_from_image_context() -> None:
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert {".env", ".env.*", ".tracefold", ".claude", ".venv", "artifacts"} <= set(ignored)
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert dockerfile.index("--no-install-project") < dockerfile.index("COPY tracefold ./tracefold")
    assert "github_token" not in dockerfile
    assert "github_token" not in (ROOT / "compose.yaml").read_text()


def test_unset_env_file_is_explicit_not_ambient(tmp_path: Path) -> None:
    deployment = Deployment(tmp_path, environ={})
    assert deployment.prefix[deployment.prefix.index("--env-file") + 1] == os.devnull


@pytest.mark.parametrize("suffix", ["", "-dirty"])
def test_runtime_manifest_reports_exact_image_and_honest_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    from types import SimpleNamespace

    from tracefold.app.cli.commands import runtime_manifest
    from tracefold.app.cli.commands.config import handle_init

    monkeypatch.setenv("TRACEFOLD_HOME", str(tmp_path / "operator"))
    handle_init(Namespace(force=False))
    identity = SimpleNamespace(runtime_revision="a" * 40 + suffix, image_digest="sha256:" + "b" * 64)
    monkeypatch.setattr(runtime_manifest, "runtime_identity", lambda: identity)
    code, result = runtime_manifest.handle_runtime_manifest(Namespace())
    assert code == 0
    assert result["data"]["image_digest"] == identity.image_digest
    assert result["data"]["source_dirty"] is bool(suffix)
    assert len(result["data"]["runtime_manifest_sha"]) == 64


def test_runtime_manifest_rejects_an_unversioned_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from tracefold.app.cli.commands import runtime_manifest
    from tracefold.app.cli.commands.config import handle_init

    monkeypatch.setenv("TRACEFOLD_HOME", str(tmp_path / "operator"))
    handle_init(Namespace(force=False))
    monkeypatch.setattr(
        runtime_manifest,
        "runtime_identity",
        lambda: SimpleNamespace(runtime_revision="a" * 40, image_digest="unversioned"),
    )
    code, result = runtime_manifest.handle_runtime_manifest(Namespace())
    assert code == 1 and result["error"] == "runtime_manifest_image_identity_required"


def test_retired_genesis_cli_is_not_a_compatibility_alias() -> None:
    from tracefold.app.cli.parser import build_parser

    parser = build_parser()
    assert parser.parse_args(["runtime-manifest"]).command == "runtime-manifest"
    with pytest.raises(SystemExit):
        parser.parse_args(["db", "news-genesis-manifest"])
