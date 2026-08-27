"""Install the repository-managed pre-commit hook after validating Git's hook path."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        check=check,
        text=True,
    )


def _configured_hooks_path() -> tuple[str, str] | None:
    configured = _git(
        "config",
        "--show-scope",
        "--get",
        "core.hooksPath",
        check=False,
    )
    if configured.returncode == 1:
        return None
    if configured.returncode != 0:
        raise RuntimeError(configured.stderr.strip() or "git_config_hooks_path_failed")
    scope, _, value = configured.stdout.strip().partition("\t")
    return scope, value


def _absolute_git_path(path: str) -> Path:
    return Path(_git("rev-parse", "--path-format=absolute", "--git-path", path).stdout.strip()).resolve()


def _recovery_command(scope: str) -> str:
    option = {
        "local": "--local",
        "worktree": "--worktree",
        "global": "--global",
        "system": "--system",
    }.get(scope, "--local")
    return f"git config {option} --unset core.hooksPath"


def install() -> int:
    try:
        root = Path(_git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
        configured = _configured_hooks_path()
        if configured is not None:
            scope, value = configured
            resolved = _absolute_git_path("hooks/pre-commit")
            print(
                "core.hooksPath overrides this repository's hooks; "
                f"configured={value!r}, resolved={resolved}.\n"
                f"Clear that setting explicitly, then retry: {_recovery_command(scope)}",
                file=sys.stderr,
            )
            return 2

        completed = subprocess.run(
            [sys.executable, "-m", "pre_commit", "install"],
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            print(completed.stderr.strip() or completed.stdout.strip(), file=sys.stderr)
            return completed.returncode

        hook = _absolute_git_path("hooks/pre-commit")
        common_dir = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
        if not hook.is_relative_to(common_dir):
            print(
                f"installed hook resolved outside this repository's Git directory: {hook}",
                file=sys.stderr,
            )
            return 2
        if not hook.is_file() or not os.access(hook, os.X_OK):
            print(f"pre-commit hook is missing or not executable after install: {hook}", file=sys.stderr)
            return 2

        print(f"installed executable pre-commit hook: {hook}")
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"hook installation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(install())
