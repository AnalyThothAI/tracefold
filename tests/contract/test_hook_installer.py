from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pre_commit.clientlib import load_config

from scripts.run_web_hook import _hook_command

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "install_hooks.py"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
    )


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    shutil.copy2(ROOT / ".pre-commit-config.yaml", repo / ".pre-commit-config.yaml")
    return repo


def _install(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(INSTALLER)],
        cwd=repo,
        capture_output=True,
        check=False,
        text=True,
    )


def test_installer_rejects_an_overriding_hooks_path_with_a_recovery_command(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    external = tmp_path / "other-repository" / ".git" / "hooks"
    _git(repo, "config", "--local", "core.hooksPath", str(external))

    result = _install(repo)

    assert result.returncode == 2
    assert "core.hooksPath overrides the standard repository hook directory" in result.stderr
    assert str(external / "pre-commit") in result.stderr
    assert "git config --local --unset core.hooksPath" in result.stderr
    assert not (external / "pre-commit").exists()


def test_installer_verifies_the_executable_hook_belongs_to_the_repository(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    result = _install(repo)

    assert result.returncode == 0, result.stderr
    hook = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-commit").stdout.strip())
    common_dir = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    assert hook.is_relative_to(common_dir)
    assert hook.is_file()
    assert os.access(hook, os.X_OK)
    assert "pre_commit" in hook.read_text(encoding="utf-8")


def test_pre_commit_uses_the_locked_ruff_and_staged_frontend_files() -> None:
    config = load_config(str(ROOT / ".pre-commit-config.yaml"))
    local_repositories = [repository for repository in config["repos"] if repository["repo"] == "local"]

    assert len(local_repositories) == 1
    hooks = {hook["id"]: hook for hook in local_repositories[0]["hooks"]}
    assert hooks["ruff"]["entry"] == "uv run --locked ruff check --fix"
    assert hooks["ruff-format"]["entry"] == "uv run --locked ruff format"

    eslint = hooks["eslint-web"]
    assert eslint["pass_filenames"] is True
    assert eslint["entry"] == "uv run --locked python scripts/run_web_hook.py eslint"
    assert "test:architecture" not in eslint["entry"]
    for path in (
        "web/src/App.tsx",
        "web/tests/unit/example.test.ts",
        "web/vite.config.ts",
        "web/eslint.config.js",
    ):
        assert re.search(eslint["files"], path), path

    prettier = hooks["prettier-web"]
    assert prettier["pass_filenames"] is True
    assert prettier["entry"] == "uv run --locked python scripts/run_web_hook.py prettier"
    for path in (
        "web/src/style.css",
        "web/tests/unit/example.test.ts",
        "web/playwright.config.ts",
        "web/package.json",
        "web/package-lock.json",
        "web/.prettierrc.json",
        "web/.prettierignore",
        "web/eslint.config.js",
    ):
        assert re.search(prettier["files"], path), path


def test_frontend_tool_configuration_changes_validate_the_full_affected_scope() -> None:
    executable = Path("web/node_modules/.bin/tool")

    assert _hook_command("eslint", ["eslint.config.js"], executable) == ["npm", "run", "lint:eslint"]
    assert _hook_command("prettier", [".prettierrc.json"], executable) == ["npm", "run", "format:check"]
    assert _hook_command("prettier", [".prettierignore"], executable) == ["npm", "run", "format:check"]
    assert _hook_command("prettier", ["src/styles/base.css"], executable) == [
        str(executable),
        "--check",
        "src/styles/base.css",
    ]


@pytest.mark.parametrize(
    ("hook_id", "path"),
    [
        ("eslint-web", "web/src/main.tsx"),
        ("eslint-web", "web/src/lib/types/openapi.ts"),
        ("prettier-web", "web/src/styles/base.css"),
    ],
)
@pytest.mark.slow
def test_staged_frontend_hook_executes_from_the_web_project(hook_id: str, path: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pre_commit", "run", hook_id, "--files", path],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
