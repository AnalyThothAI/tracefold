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
    "runtime-broker",
    "deploy-e2e",
    "test-integrity",
    "frontend",
}
# Report-only (#373 PR 2). It is in the workflow but not in `ci-gate`'s dependencies, and the tests
# below hold it to both halves of that: it may not decide a merge, and it may not run a test.
REPORT_ONLY_JOB = "test-effectiveness"
# Scheduled mutation (#373 PR 4) lives in its own workflow for the same reason, taken further:
# `require_main_ci.py` reads the `ci-gate` check run and requires its workflow run's `path` to be
# `.github/workflows/ci.yml`, so a lane outside that file cannot reach a deployment at all.
MUTATION_WORKFLOW = ROOT / ".github" / "workflows" / "mutation.yml"
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
    assert set(jobs) == REQUIRED_JOBS | {"ci-gate", REPORT_ONLY_JOB}
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


@pytest.mark.parametrize("workflow_path", sorted((ROOT / ".github" / "workflows").glob("*.y*ml")), ids=lambda p: p.name)
def test_every_workflow_pins_its_actions_and_keeps_its_credentials(workflow_path: Path) -> None:
    """The supply-chain invariants belong to every workflow, not to the one they were written beside.

    `test_the_gate_is_thin_and_owns_the_required_set` walks only `ci.yml`, so a workflow added later
    inherits none of it — `mutation.yml` and `scheduled-diagnostics.yml` between them run a dozen
    actions that nothing held to a SHA pin, and a `@v5` tag or a `persist-credentials` default could
    land with the whole required suite green. Parametrised over the directory so the next workflow
    is covered by existing.
    """

    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            action = step.get("uses")
            if not action:
                continue
            assert FULL_SHA.fullmatch(action.rsplit("@", 1)[1]), f"{workflow_path.name}: {action} is not SHA-pinned"
            if action.startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False, (
                    f"{workflow_path.name}: checkout must not leave the token in .git/config"
                )


def test_the_effectiveness_job_reports_and_decides_nothing() -> None:
    """#373 PR 2 is measurement without authority: no merge power, no execution, no re-adjudication.

    Three independent halves, and the third is the one that is easy to miss. `ci-gate` not depending
    on it keeps a coverage regression from blocking a merge. Its running no test runner keeps it from
    becoming a second execution truth beside the fixed jobs. And `continue-on-error` keeps it out of
    the *workflow run's* conclusion — which matters because `scripts/require_main_ci.py` requires the
    run to have concluded `success`, not merely `ci-gate`. Without it, a coverage job that failed on
    a green main would raise `main_ci_gate_not_full_plan` and refuse the deployment, which is a great
    deal of authority for a job whose comment says it has none.
    """

    workflow = _workflow()
    jobs = workflow["jobs"]
    job = jobs[REPORT_ONLY_JOB]

    assert REPORT_ONLY_JOB not in set(jobs["ci-gate"]["needs"])
    assert set(job["needs"]) == REQUIRED_JOBS
    assert job["if"] == "always()"
    assert job["continue-on-error"] is True
    assert "services" not in job
    for name, required in jobs.items():
        if name in REQUIRED_JOBS or name == "ci-gate":
            assert "continue-on-error" not in required, name

    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert 'test "$(git rev-parse HEAD)" = "$TESTED_SHA"' in commands
    assert "make ci-test-effectiveness" in commands
    for runner in ("pytest", "vitest", "playwright", "npm ", "junit"):
        assert runner not in commands.lower()

    recipe = subprocess.run(
        ["make", "--dry-run", "ci-test-effectiveness"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    for runner in ("pytest", "vitest", "playwright", "npm ", "require_test_reports"):
        assert runner not in recipe.lower()
    for standard in ("coverage combine", "coverage report", "coverage json", "coverage xml", "coverage html"):
        assert standard in recipe
    assert "--fail-under" not in recipe


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
        "RUNTIME_BROKER_RESULT",
        "DEPLOY_E2E_RESULT",
        "TEST_INTEGRITY_RESULT",
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

    for job_name in ("postgres-behavior", "migration", "runtime-broker", "deploy-e2e", "frontend"):
        image = workflow["jobs"][job_name]["services"]["postgres"]["image"]
        assert re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image)
    for job_name in ("postgres-behavior", "runtime-broker", "frontend"):
        image = workflow["jobs"][job_name]["services"]["rabbitmq"]["image"]
        assert re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image)
    assert set(workflow["jobs"]["runtime-broker"]["services"]) == {"postgres", "rabbitmq"}
    assert set(workflow["jobs"]["deploy-e2e"]["services"]) == {"postgres"}
    assert "services" not in workflow["jobs"]["test-integrity"]


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
        ["make", "--dry-run", "test-ci"],
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
        "ci-runtime-broker",
        "ci-deploy-e2e",
        "ci-test-integrity",
        "ci-frontend",
    ):
        assert target in commands
    assert "ci-runtime-process" not in commands
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


def test_the_mutation_lane_is_scheduled_and_cannot_gate_anything() -> None:
    """The measurement informs; it never decides a merge or a deploy.

    Two independent reasons, both asserted rather than assumed. It defines no `ci-gate` job, so
    `require_unique_ci_gate_definition` still finds exactly one owner and `require_main_ci` never
    reads this workflow. And it does not run on `pull_request` or `push`, so it produces no check
    run on a commit anyone is waiting on.
    """

    workflow = yaml.safe_load(MUTATION_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "ci-gate" not in {str(job.get("name") or job_id) for job_id, job in jobs.items()}

    # PyYAML resolves a bare `on:` key to the boolean True, which is why this is not `workflow["on"]`.
    triggers = workflow[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}


def test_the_mutation_lane_proves_its_harness_before_it_reports_a_score() -> None:
    """A score from a harness that never delivered a mutant is the failure that looks like success."""

    workflow = yaml.safe_load(MUTATION_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert jobs["mutate"]["needs"] == "sentinel"
    assert jobs["classify"]["needs"] == "mutate"
    assert "scripts/mutation_sentinel.py" in str(jobs["sentinel"]["steps"])
    assert jobs["mutate"]["timeout-minutes"] == 30
