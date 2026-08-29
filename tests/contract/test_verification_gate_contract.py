from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_JOBS = {
    "quality-static",
    "python-hermetic",
    "postgres-behavior",
    "migration",
    "runtime-process",
    "frontend",
}
pytestmark = pytest.mark.contract


def _workflow() -> dict[str, object]:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_required_ci_runs_one_fixed_full_plan_for_every_event() -> None:
    workflow = _workflow()
    triggers = workflow[True]  # YAML 1.1 parses GitHub's unquoted on key as boolean true.
    assert isinstance(triggers, dict)
    assert {"pull_request", "push", "workflow_dispatch", "release"} <= set(triggers)
    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["push"]["branches"] == ["main"]
    assert "paths" not in triggers["pull_request"]
    assert "paths-ignore" not in triggers["pull_request"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert workflow["env"]["TESTED_SHA"] == "${{ github.event.pull_request.head.sha || github.sha }}"

    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == REQUIRED_JOBS | {"ci-gate"}
    for name in REQUIRED_JOBS:
        job = jobs[name]
        assert "needs" not in job
        assert "if" not in job
        assert job["runs-on"] == "ubuntu-24.04"
        assert int(job["timeout-minutes"]) > 0
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        assert 'test "$(git rev-parse HEAD)" = "$TESTED_SHA"' in commands
        checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"] == {
            "fetch-depth": 0,
            "persist-credentials": False,
            "ref": "${{ env.TESTED_SHA }}",
        }
        upload = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        assert upload["if"] == "always()"
        assert upload["with"]["path"] == "artifacts/test-results"
        assert upload["with"]["if-no-files-found"] == "error"
        assert "${{ env.TESTED_SHA }}" in upload["with"]["name"]

    for job in jobs.values():
        for step in job.get("steps", []):
            action = step.get("uses")
            if action:
                assert FULL_SHA.fullmatch(action.rsplit("@", 1)[1]), action


def test_ci_gate_is_a_unique_thin_all_success_interface() -> None:
    jobs = _workflow()["jobs"]
    gate = jobs["ci-gate"]

    assert gate["name"] == "ci-gate"
    assert set(gate["needs"]) == REQUIRED_JOBS
    assert gate["if"] == "always()"
    assert "services" not in gate
    assert all("uses" not in step for step in gate["steps"])
    assert len(gate["steps"]) == 1
    assert set(gate["env"]) == {
        "QUALITY_STATIC_RESULT",
        "PYTHON_HERMETIC_RESULT",
        "POSTGRES_BEHAVIOR_RESULT",
        "MIGRATION_RESULT",
        "RUNTIME_PROCESS_RESULT",
        "FRONTEND_RESULT",
    }
    command = gate["steps"][0]["run"]
    assert "scripts/" not in command
    assert "download" not in command
    assert '[ "$result" != success ]' in command

    owners: list[str] = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        workflow_jobs = workflow.get("jobs", {}) if isinstance(workflow, dict) else {}
        owners.extend(
            f"{path.name}:{job_id}"
            for job_id, job in workflow_jobs.items()
            if isinstance(job, dict) and str(job.get("name") or job_id) == "ci-gate"
        )
    assert owners == ["ci.yml:ci-gate"]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "unknown", ""])
def test_thin_gate_rejects_every_result_other_than_success(result: str) -> None:
    gate = _workflow()["jobs"]["ci-gate"]
    env = {name: "success" for name in gate["env"]}
    env["MIGRATION_RESULT"] = result

    process = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", gate["steps"][0]["run"]],
        cwd=ROOT,
        env={**os.environ, **env},
        capture_output=True,
        check=False,
        text=True,
    )

    assert process.returncode != 0
    assert "required CI job did not succeed" in process.stderr


def test_thin_gate_accepts_only_all_success() -> None:
    gate = _workflow()["jobs"]["ci-gate"]
    process = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", gate["steps"][0]["run"]],
        cwd=ROOT,
        env={**os.environ, **dict.fromkeys(gate["env"], "success")},
        capture_output=True,
        check=False,
        text=True,
    )

    assert process.returncode == 0, process.stderr


def test_ci_and_runtime_install_the_same_pinned_uv_from_validated_locks() -> None:
    workflow = _workflow()
    uv_version = workflow["env"]["UV_VERSION"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", uv_version)
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                assert step["with"]["version"] == "${{ env.UV_VERSION }}"
            command = step.get("run", "")
            if "uv sync" in command:
                assert "uv sync --locked" in command
            if "npm ci" in command:
                assert "npm ci --prefix web" in command

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"ARG UV_VERSION={uv_version}" in dockerfile
    assert "uv==${UV_VERSION}" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile
    for image in ("node:22-bookworm-slim", "python:3.13-slim-bookworm"):
        assert re.search(rf"FROM {re.escape(image)}@sha256:[0-9a-f]{{64}}", dockerfile)

    for job_name in ("postgres-behavior", "migration", "runtime-process", "frontend"):
        image = workflow["jobs"][job_name]["services"]["postgres"]["image"]
        assert re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image)
    for job_name in ("postgres-behavior", "runtime-process", "frontend"):
        image = workflow["jobs"][job_name]["services"]["rabbitmq"]["image"]
        assert re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image)


def test_locked_sync_rejects_pyproject_lock_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pyproject = (
        (ROOT / "pyproject.toml")
        .read_text(encoding="utf-8")
        .replace(
            '    "alembic>=1.18.0",',
            '    "alembic>=1.18.0",\n    "tracefold-lock-drift-fixture==0",',
            1,
        )
    )
    (project / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    shutil.copy2(ROOT / "uv.lock", project / "uv.lock")

    result = subprocess.run(
        ["uv", "sync", "--locked", "--dry-run", "--project", str(project), "--no-install-project"],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "lock" in result.stderr.lower()


def test_make_complete_verification_uses_native_reports_and_strict_runner_policy() -> None:
    commands = subprocess.run(
        ["make", "--dry-run", "test-evidence"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout

    for target in (
        "ci-quality-static",
        "ci-python-hermetic",
        "ci-postgres-behavior",
        "ci-migration",
        "ci-runtime-process",
        "ci-frontend",
    ):
        assert target in commands
    assert "PYTEST_ADDOPTS=" in commands
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in commands
    assert "TRACEFOLD_HYPOTHESIS_PROFILE=ci" in commands
    assert "TRACEFOLD_TEST_RESOURCES_REQUIRED=1" in commands
    assert "--maxfail=0" in commands
    assert "--override-ini=xfail_strict=true" in commands
    assert "--junitxml=" in commands
    assert "--reporter=json" in commands
    assert "scripts/require_test_reports.py" in commands
    assert "not live" in commands
    assert "not scheduled" in commands
    assert "tests.support" not in commands
    assert "manifest" not in commands


def test_migration_marker_owns_every_historical_migration_module() -> None:
    modules = sorted((ROOT / "tests" / "integration").glob("*_migration.py"))
    modules.append(ROOT / "tests" / "integration" / "test_postgres_schema_runtime.py")
    assert modules
    for module in modules:
        source = module.read_text(encoding="utf-8")
        assert "pytest.mark.migration" in source, module
        assert 'pytest.mark.usefixtures("postgres_migration_dsn")' in source, module
