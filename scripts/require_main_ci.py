"""Fail closed unless deployment source is the exact green origin/main SHA."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

GITHUB_REPOSITORY = "AnalyThothAI/tracefold"
GITHUB_ACTIONS_INTEGRATION_ID = 15_368
REQUIRED_CHECK = "ci-gate"
_COMPOSE_ENVIRONMENT = (
    "COMPOSE_FILE",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_ENV_FILES",
    "COMPOSE_PROFILES",
    "COMPOSE_PATH_SEPARATOR",
    "COMPOSE_DISABLE_ENV_FILE",
)


def require_clean_deployment_environment(environ: Mapping[str, str]) -> None:
    for name in _COMPOSE_ENVIRONMENT:
        if environ.get(name):
            raise RuntimeError(f"deployment_compose_environment_forbidden:{name}")


def require_unique_ci_gate_definition(root: Path) -> None:
    owners: list[str] = []
    for path in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        try:
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeError("deployment_workflow_definition_invalid") from exc
        jobs = workflow.get("jobs", {}) if isinstance(workflow, dict) else {}
        if not isinstance(jobs, dict):
            raise RuntimeError("deployment_workflow_definition_invalid")
        owners.extend(
            f"{path.name}:{job_id}"
            for job_id, job in jobs.items()
            if isinstance(job, dict) and str(job.get("name") or job_id) == REQUIRED_CHECK
        )
    if len(owners) != 1:
        raise RuntimeError("deployment_ci_gate_definition_not_unique")


def require_main_ci(root: Path, payload: dict[str, Any]) -> str:
    git_dir = _git(root, "rev-parse", "--absolute-git-dir")
    git_common_dir = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if git_dir != git_common_dir:
        raise RuntimeError("deployment_checkout_not_primary")
    branch = _git(root, "branch", "--show-current")
    if branch != "main":
        raise RuntimeError("deployment_branch_not_main")
    head = _git(root, "rev-parse", "HEAD")
    try:
        origin_main = _git(root, "rev-parse", "refs/remotes/origin/main")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("deployment_origin_main_missing") from exc
    if head != origin_main:
        raise RuntimeError("deployment_head_not_origin_main")
    try:
        remote_fields = _git(root, "ls-remote", "--exit-code", "origin", "refs/heads/main").split()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("deployment_remote_main_unavailable") from exc
    if len(remote_fields) != 2:
        raise RuntimeError("deployment_remote_main_response_invalid")
    if head != remote_fields[0]:
        raise RuntimeError("deployment_head_not_remote_main")
    if (root / ".env").exists() or _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("deployment_source_dirty")
    require_unique_ci_gate_definition(root)

    runs = [row for row in payload.get("check_runs", []) if row.get("name") == REQUIRED_CHECK]
    if not runs:
        raise RuntimeError("main_ci_gate_missing")
    trusted = [row for row in runs if (row.get("app") or {}).get("id") == GITHUB_ACTIONS_INTEGRATION_ID]
    if not trusted:
        raise RuntimeError("main_ci_gate_wrong_integration")
    latest = max(trusted, key=lambda row: int(row.get("id") or 0))
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise RuntimeError("main_ci_gate_not_success")
    return head


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def _check_runs(sha: str) -> dict[str, Any]:
    result = subprocess.run(
        (
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{GITHUB_REPOSITORY}/commits/{sha}/check-runs?per_page=100",
        ),
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("main_ci_gate_response_invalid")
    return payload


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        require_clean_deployment_environment(os.environ)
        head = _git(root, "rev-parse", "HEAD")
        verified = require_main_ci(root, _check_runs(head))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"deployment verification refused: {exc}", file=sys.stderr)
        return 1
    print(f"deployment source verified: {verified}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
