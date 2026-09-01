"""Prove the built distribution — not the flat working tree — carries the product.

#373 moved the production package out of its old `src/` parent to the repository root, so the
package directory is now `tracefold/`. The flat layout means any process whose current
directory is the checkout can `import tracefold` straight off the working tree, so ordinary
pytest green no longer distinguishes "the package works" from "the wheel ships the package".
A file dropped from `[tool.hatch.build.targets.wheel]`, a resource that stops being package
data, or an Alembic tree that silently leaves the distribution would all stay invisible.

This module closes that hole with the standard tooling: `uv build` produces the real wheel and
sdist, `uv pip install` puts the wheel in a throwaway environment outside the repository, and
that environment is asked to import the product and read its packaged resources with the
checkout absent from the current directory, `PYTHONPATH`, and the effective `sys.path`. The
constraint file pins the isolated environment to the same locked dependency versions the rest
of CI uses, so a third-party release cannot turn this smoke red on its own.

Nothing here manipulates `sys.path` to make an assertion pass: a missing file in the wheel has
to surface as a failure, which is the entire reason the proof exists.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

# `package` keeps this out of `make test-fast`. Everything else in the default developer loop is
# hermetic, and this module is not: it shells out to `uv build`, `uv venv` and `uv pip install`, so
# offline or with a cold cache it fails rather than skips. `python-hermetic` excludes by marker name
# and does not name this one, so the required lane still runs it.
pytestmark = pytest.mark.package

ROOT = Path(__file__).resolve().parents[2]
DISTRIBUTION_NAME = "tracefold"
# Read, not restated. A version bump would otherwise leave `uv build` succeeding while the fixture
# looked for a filename that no longer exists, and report it as "uv build produced no wheel".
DISTRIBUTION_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

_PROBE = '''
"""Report what the isolated environment can actually see. Written next to the venv, not in the repo."""

import json
import sys
from pathlib import Path

import tracefold
import tracefold.trading
from tracefold.app.cli.main import main as cli_main
from tracefold.news.program.artifact import load_stable_program_artifact

package_root = Path(tracefold.__file__).resolve().parent
alembic_root = package_root / "platform" / "postgres" / "alembic"

print(
    json.dumps(
        {
            "package_root": str(package_root),
            "sys_path": [str(entry) for entry in sys.path],
            "cwd": str(Path.cwd()),
            "cli_main_module": cli_main.__module__,
            "program_sha256": load_stable_program_artifact().program_sha256,
            "trading_root": str(Path(tracefold.trading.__file__).resolve().parent),
            "alembic_env_py": (alembic_root / "env.py").is_file(),
            "alembic_baseline_sql": (alembic_root / "current_schema_20260831_0340.sql").is_file(),
            "alembic_revisions": len(sorted((alembic_root / "versions").glob("*.py"))),
        }
    )
)
'''


def _uv() -> str:
    executable = shutil.which("uv")
    assert executable is not None, "uv builds and installs the distribution under test"
    return executable


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, f"{command} failed ({completed.returncode}):\n{completed.stderr}"
    return completed.stdout


def _isolated_env() -> dict[str, str]:
    """The checkout must not reach the child through inherited interpreter configuration."""

    env = dict(os.environ)
    for leak in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        env.pop(leak, None)
    return env


@dataclass(frozen=True)
class BuiltDistribution:
    wheel: Path
    sdist: Path


@pytest.fixture(scope="module")
def built_distribution(tmp_path_factory: pytest.TempPathFactory) -> BuiltDistribution:
    out_dir = tmp_path_factory.mktemp("tracefold-dist")
    _run([_uv(), "build", "--out-dir", str(out_dir)], cwd=ROOT)
    wheel = out_dir / f"{DISTRIBUTION_NAME}-{DISTRIBUTION_VERSION}-py3-none-any.whl"
    sdist = out_dir / f"{DISTRIBUTION_NAME}-{DISTRIBUTION_VERSION}.tar.gz"
    assert wheel.is_file(), f"uv build produced no wheel in {out_dir}"
    assert sdist.is_file(), f"uv build produced no sdist in {out_dir}"
    return BuiltDistribution(wheel=wheel, sdist=sdist)


@pytest.fixture(scope="module")
def isolated_probe(
    built_distribution: BuiltDistribution,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, object]]:
    home = tmp_path_factory.mktemp("tracefold-isolated")
    constraints = home / "constraints.txt"
    _run(
        [
            _uv(),
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
            "--output-file",
            str(constraints),
        ],
        cwd=ROOT,
    )
    venv = home / "venv"
    _run([_uv(), "venv", "--python", f"{sys.version_info.major}.{sys.version_info.minor}", str(venv)], cwd=home)
    python = venv / "bin" / "python"
    _run(
        [
            _uv(),
            "pip",
            "install",
            "--python",
            str(python),
            "--constraint",
            str(constraints),
            str(built_distribution.wheel),
        ],
        cwd=home,
        env=_isolated_env(),
    )
    probe = home / "probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    stdout = _run([str(python), str(probe)], cwd=home, env=_isolated_env())
    payload = json.loads(stdout)
    payload["_venv"] = str(venv)
    payload["_home"] = str(home)
    yield payload


def _wheel_members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def _sdist_members(sdist: Path) -> list[str]:
    with tarfile.open(sdist, "r:gz") as archive:
        prefix = f"{DISTRIBUTION_NAME}-{DISTRIBUTION_VERSION}/"
        return [name.removeprefix(prefix) for name in archive.getnames() if name.startswith(prefix)]


def test_wheel_ships_the_flat_package_and_no_src_prefix(built_distribution: BuiltDistribution) -> None:
    members = _wheel_members(built_distribution.wheel)
    dist_info = f"{DISTRIBUTION_NAME}-{DISTRIBUTION_VERSION}.dist-info/"
    payload = [name for name in members if not name.startswith(dist_info)]

    assert payload, "the wheel carries no package payload"
    assert not [name for name in members if name.startswith("src/")]
    assert not [name for name in payload if not name.startswith(f"{DISTRIBUTION_NAME}/")]


def test_wheel_ships_the_packaged_resources_the_runtime_reads(built_distribution: BuiltDistribution) -> None:
    members = set(_wheel_members(built_distribution.wheel))
    alembic = f"{DISTRIBUTION_NAME}/platform/postgres/alembic/"

    assert f"{alembic}env.py" in members
    assert f"{alembic}current_schema_20260831_0340.sql" in members
    assert sorted(name for name in members if name.startswith(f"{alembic}versions/") and name.endswith(".py")) == [
        f"{alembic}versions/20260831_0340_baseline.py",
        f"{alembic}versions/20260901_0341_trading_signal_hard_cut.py",
        f"{alembic}versions/20260901_0342_trading_notification_deliveries.py",
        f"{alembic}versions/20260901_0343_trading_execution_runtime_state.py",
        f"{alembic}versions/20260901_0344_news_oi_push_cut.py",
        f"{alembic}versions/20260901_0345_trading_runtime_exposure_race.py",
        f"{alembic}versions/20260901_0346_trading_notification_result.py",
        f"{alembic}versions/20260901_0347_drop_retired_trading_tables.py",
        f"{alembic}versions/20260902_0348_trading_runtime_control_state.py",
        f"{alembic}versions/20260902_0349_trading_account_projection.py",
    ]
    assert f"{DISTRIBUTION_NAME}/news/program/resources/registry.json" in members


def test_sdist_carries_the_flat_tree_and_no_src_directory(built_distribution: BuiltDistribution) -> None:
    members = _sdist_members(built_distribution.sdist)

    assert f"{DISTRIBUTION_NAME}/__init__.py" in members
    assert f"{DISTRIBUTION_NAME}/platform/postgres/alembic/env.py" in members
    assert not [name for name in members if name == "src" or name.startswith("src/")]


def test_import_resolves_to_the_installed_distribution_not_the_checkout(
    isolated_probe: dict[str, object],
) -> None:
    package_root = Path(str(isolated_probe["package_root"]))
    venv = Path(str(isolated_probe["_venv"]))

    assert package_root.is_relative_to(venv), f"tracefold resolved from {package_root}, outside {venv}"
    assert not package_root.is_relative_to(ROOT)
    assert not Path(str(isolated_probe["trading_root"])).is_relative_to(ROOT)
    assert isolated_probe["cli_main_module"] == "tracefold.app.cli.main"


def test_the_checkout_is_absent_from_the_isolated_interpreter(isolated_probe: dict[str, object]) -> None:
    cwd = Path(str(isolated_probe["cwd"]))
    entries = [Path(entry) for entry in isolated_probe["sys_path"]]  # type: ignore[union-attr]

    assert not cwd.is_relative_to(ROOT), "the probe must not run from inside the checkout"
    assert not [entry for entry in entries if entry == ROOT or ROOT.is_relative_to(entry)]


def test_installed_distribution_reads_its_own_program_artifact(isolated_probe: dict[str, object]) -> None:
    from tracefold.news.program.artifact import load_stable_program_artifact

    assert isolated_probe["program_sha256"] == load_stable_program_artifact().program_sha256


def test_installed_distribution_carries_the_alembic_tree(isolated_probe: dict[str, object]) -> None:
    assert isolated_probe["alembic_env_py"] is True
    assert isolated_probe["alembic_baseline_sql"] is True
    assert isolated_probe["alembic_revisions"] == 10


@pytest.mark.parametrize("entrypoint", ["console-script", "python-m"])
def test_installed_entrypoints_run_from_the_distribution(
    isolated_probe: dict[str, object],
    entrypoint: str,
) -> None:
    venv = Path(str(isolated_probe["_venv"]))
    home = Path(str(isolated_probe["_home"]))
    command = (
        [str(venv / "bin" / DISTRIBUTION_NAME), "--help"]
        if entrypoint == "console-script"
        else [str(venv / "bin" / "python"), "-m", DISTRIBUTION_NAME, "--help"]
    )

    stdout = _run(command, cwd=home, env=_isolated_env())

    assert stdout.startswith(f"usage: {DISTRIBUTION_NAME}")
    assert "serve" in stdout
