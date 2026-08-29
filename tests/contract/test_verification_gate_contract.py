from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.support import evidence

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
pytestmark = pytest.mark.slow


def test_required_ci_has_one_stable_fail_closed_gate() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow[True]  # YAML 1.1 parses GitHub's unquoted `on` key as boolean true.

    assert {"pull_request", "push", "workflow_dispatch", "release"} <= set(triggers)
    assert "main" in triggers["pull_request"]["branches"]
    assert "main" in triggers["push"]["branches"]
    assert "paths" not in triggers["pull_request"]
    assert "paths-ignore" not in triggers["pull_request"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert workflow["env"]["TESTED_SHA"] == "${{ github.event.pull_request.head.sha || github.sha }}"

    jobs = workflow["jobs"]
    required_jobs = {
        "quality-static",
        "python-hermetic",
        "trust-root",
        "postgres-behavior",
        "migration",
        "runtime-process",
        "frontend",
    }
    assert required_jobs | {"ci-plan", "evidence-aggregate", "ci-gate"} == set(jobs)
    planner = jobs["ci-plan"]
    planner_outputs = {name.replace("-", "_") for name in required_jobs}
    assert set(planner["outputs"]) == {"plan_sha256", "full", *planner_outputs}
    planner_commands = "\n".join(step.get("run", "") for step in planner["steps"])
    assert "scripts/ci_plan.py plan" in planner_commands
    assert "github.event.pull_request.base.sha" in str(planner)
    assert any(
        step.get("uses", "").startswith("actions/upload-artifact@")
        and "artifacts/ci-plan/plan.json" in step.get("with", {}).get("path", "")
        for step in planner["steps"]
    )
    expected_commands = {
        "quality-static": "test-evidence-quality-static",
        "python-hermetic": "test-evidence-python-hermetic",
        "trust-root": "test-evidence-trust-root",
        "postgres-behavior": "test-evidence-postgres-behavior",
        "migration": "test-evidence-migration",
        "runtime-process": "test-evidence-runtime-process",
        "frontend": "test-evidence-frontend",
    }
    for job_name, expected_command in expected_commands.items():
        job = jobs[job_name]
        assert job["needs"] == "ci-plan"
        output_name = job_name.replace("-", "_")
        assert job["if"] == f"needs.ci-plan.outputs.{output_name} == 'true'"
        assert job["env"]["TRACEFOLD_CI_PLAN_SHA256"] == "${{ needs.ci-plan.outputs.plan_sha256 }}"
        assert int(job["timeout-minutes"]) > 0
        assert any(expected_command in step.get("run", "") for step in job["steps"])
        checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"]["ref"] == "${{ env.TESTED_SHA }}"
        assert any(
            step.get("if") == "always()" and step.get("uses", "").startswith("actions/upload-artifact@")
            for step in job["steps"]
        )
        for step in job["steps"]:
            action = step.get("uses")
            if action:
                _, revision = action.rsplit("@", 1)
                assert FULL_SHA.fullmatch(revision), action

    aggregate = jobs["evidence-aggregate"]
    assert set(aggregate["needs"]) == required_jobs | {"ci-plan"}
    assert aggregate["if"] == "always()"
    aggregate_commands = "\n".join(step.get("run", "") for step in aggregate["steps"])
    assert "TRACEFOLD_CI_PLAN_PATH=" in aggregate_commands
    assert "make test-evidence-aggregate" in aggregate_commands
    assert any(
        step.get("uses", "").startswith("actions/download-artifact@")
        and str(step.get("with", {}).get("name", "")).startswith("ci-plan-")
        for step in aggregate["steps"]
    )
    for job in jobs.values():
        for step in job.get("steps", []):
            action = step.get("uses")
            if action:
                assert FULL_SHA.fullmatch(action.rsplit("@", 1)[1]), action

    gate = jobs["ci-gate"]
    assert gate["name"] == "ci-gate"
    assert set(gate["needs"]) == required_jobs | {"ci-plan", "evidence-aggregate"}
    assert gate["if"] == "always()"
    gate_step = next(step for step in gate["steps"] if "scripts/ci_plan.py gate" in step.get("run", ""))
    assert all(f"needs.{name}.result" in gate_step["run"] for name in required_jobs)
    assert "needs.ci-plan.result" in gate_step["run"]
    assert "needs.evidence-aggregate.result" in gate_step["run"]
    assert '--summary "$GITHUB_STEP_SUMMARY"' in gate_step["run"]
    for job_name, command in expected_commands.items():
        evidence_step = next(step for step in jobs[job_name]["steps"] if command in step.get("run", ""))
        assert "GITHUB_SHA" not in evidence_step.get("env", {})
        assert 'GITHUB_SHA="$TESTED_SHA"' in evidence_step["run"]
    for job_name in ("postgres-behavior", "migration", "runtime-process", "frontend"):
        evidence_step = next(step for step in jobs[job_name]["steps"] if "test-evidence-" in step.get("run", ""))
        assert "TRACEFOLD_TEST_POSTGRES_DSN" in evidence_step["env"]
        assert "GMGN_TEST_POSTGRES_DSN" not in evidence_step["env"]

    quality_commands = subprocess.run(
        ["make", "-n", "test-evidence-quality-static"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    assert "make check-static" in quality_commands
    assert "pytest" not in quality_commands


def test_ci_gate_check_name_is_unique_across_all_workflows() -> None:
    owners: list[str] = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        jobs = workflow.get("jobs", {}) if isinstance(workflow, dict) else {}
        owners.extend(
            f"{path.name}:{job_id}"
            for job_id, job in jobs.items()
            if isinstance(job, dict) and str(job.get("name") or job_id) == "ci-gate"
        )

    assert owners == ["ci.yml:ci-gate"]


def test_ci_and_runtime_install_the_same_pinned_uv_from_a_validated_lock() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    uv_version = workflow["env"]["UV_VERSION"]

    assert re.fullmatch(r"\d+\.\d+\.\d+", uv_version)
    for job in workflow["jobs"].values():
        assert job["runs-on"] == "ubuntu-24.04"
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                assert step["with"]["version"] == "${{ env.UV_VERSION }}"
            command = step.get("run", "")
            if "uv sync" in command:
                assert "uv sync --locked" in command
                assert "--frozen" not in command

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"ARG UV_VERSION={uv_version}" in dockerfile
    assert "uv==${UV_VERSION}" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile
    assert "uv sync --frozen" not in dockerfile
    for image in ("node:22-bookworm-slim", "python:3.13-slim-bookworm"):
        assert re.search(rf"FROM {re.escape(image)}@sha256:[0-9a-f]{{64}}", dockerfile)

    for job_name in ("postgres-behavior", "migration", "runtime-process", "frontend"):
        image = workflow["jobs"][job_name]["services"]["postgres"]["image"]
        assert re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image)
    for job_name in ("runtime-process", "frontend"):
        image = workflow["jobs"][job_name]["services"]["rabbitmq"]["image"]
        assert re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image)
    runtime_commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["runtime-process"]["steps"])
    assert any(
        step.get("uses", "").startswith("actions/setup-node@") for step in workflow["jobs"]["runtime-process"]["steps"]
    )
    assert "npm ci --prefix web" in runtime_commands
    frontend_commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["frontend"]["steps"])
    assert "playwright install --with-deps chromium" in frontend_commands


def test_locked_sync_rejects_pyproject_lock_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = pyproject.replace(
        '    "alembic>=1.18.0",',
        '    "alembic>=1.18.0",\n    "tracefold-lock-drift-fixture==0",',
        1,
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


def test_evidence_entrypoint_seals_every_owner_and_checks_the_aggregate_tree() -> None:
    result = subprocess.run(
        ["make", "-n", "test-evidence"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout.count("python -m tests.support.evidence --assert-clean") == 2
    for lane in evidence.REQUIRED_LANES:
        seal = f'seal-clean --manifest "artifacts/test-evidence/lanes/{lane}.json"'
        assert result.stdout.count(seal) == 1, lane


def test_make_required_lanes_match_the_code_owned_full_plan() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    python_match = re.search(r"^PYTHON_EVIDENCE_LANES := (.+)$", makefile, flags=re.MULTILINE)
    required_match = re.search(r"^EVIDENCE_REQUIRED_LANES := (.+)$", makefile, flags=re.MULTILINE)

    assert python_match is not None
    assert tuple(python_match.group(1).split()) == evidence.PYTHON_LANES
    assert required_match is not None
    expanded = required_match.group(1).replace("$(PYTHON_EVIDENCE_LANES)", python_match.group(1))
    assert tuple(expanded.split()) == evidence.REQUIRED_LANES
    assert set(evidence._FULL_PLAN_COMMANDS) == set(evidence.REQUIRED_LANES)
    dry_run = subprocess.run(
        ["make", "-n", "test-evidence"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    for lane, command_tokens in evidence._FULL_PLAN_COMMANDS.items():
        assert all(token in dry_run for token in command_tokens), lane


def test_tested_head_changes_include_tracked_and_untracked_files(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    tracked = repository / "tracked.txt"
    tracked.write_text("frozen\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tracefold Test",
            "-c",
            "user.email=tests@tracefold.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repository,
        check=True,
    )

    assert evidence.tested_head_changes(repository) == ()
    tracked.write_text("mutated\n", encoding="utf-8")
    assert evidence.tested_head_changes(repository) == (" M tracked.txt",)
    tracked.write_text("frozen\n", encoding="utf-8")
    (repository / "new-snapshot.txt").write_text("silently accepted\n", encoding="utf-8")
    assert evidence.tested_head_changes(repository) == ("?? new-snapshot.txt",)


def _run_evidence_case(
    tmp_path: Path,
    source: str,
    *,
    conftest: str | None = None,
    extra_args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    repository = tmp_path / "repository"
    (repository / "tests" / "support").mkdir(parents=True)
    (repository / "tracefold" / "platform" / "postgres").mkdir(parents=True)
    (repository / "web").mkdir()
    shutil.copy2(Path(evidence.__file__).resolve(), repository / "tests" / "support" / "evidence.py")
    for package in (
        repository / "tests",
        repository / "tests" / "support",
        repository / "tracefold",
        repository / "tracefold" / "platform",
        repository / "tracefold" / "platform" / "postgres",
    ):
        (package / "__init__.py").write_text("", encoding="utf-8")
    (repository / "tracefold" / "platform" / "postgres" / "migrations.py").write_text(
        "def latest_migration_version(): return 'fixture-head'\n", encoding="utf-8"
    )
    case = repository / "tests" / "test_evidence_case.py"
    manifest = repository / "evidence.json"
    executable_dir = repository / "evidence-bin"
    executable_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    git_link = executable_dir / "git"
    git_link.symlink_to(git)
    node = executable_dir / "node"
    node.write_text("#!/bin/sh\nprintf 'v22.0.0-test\\n'\n", encoding="utf-8")
    node.chmod(0o755)
    uv_executable = executable_dir / "uv"
    uv_executable.write_text("#!/bin/sh\nprintf 'uv 0.0.0-test\\n'\n", encoding="utf-8")
    uv_executable.chmod(0o755)
    case.write_text(source, encoding="utf-8")
    (repository / "tests" / "conftest.py").write_text(
        "from hypothesis import settings\n"
        "settings.register_profile('ci', max_examples=5, database=None, derandomize=True, print_blob=True)\n"
        "settings.load_profile('ci')\n" + (conftest or ""),
        encoding="utf-8",
    )
    (repository / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "testpaths = ['tests']\n"
        "markers = ['live: external provider test', 'scheduled: production-duration diagnostic']\n",
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text("fixture lock\n", encoding="utf-8")
    (repository / "web" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tracefold Test",
            "-c",
            "user.email=tests@tracefold.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repository,
        check=True,
    )
    process_env = os.environ.copy()
    # A nested evidence-session test is not the outer GitHub job. The positive case must not inherit the
    # pull_request event's synthetic merge SHA; mismatch behavior is exercised explicitly below.
    process_env.pop("GITHUB_SHA", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.support.evidence",
            "-p",
            "_hypothesis_pytestplugin",
            "-m",
            "not live and not scheduled",
            f"--evidence-manifest={manifest}",
            *extra_args,
        ],
        cwd=repository,
        env={
            **process_env,
            "PATH": str(executable_dir),
            "PYTHONPATH": str(repository),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "TRACEFOLD_TEST_EVIDENCE": "1",
            **(env or {}),
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    return result, json.loads(manifest.read_text(encoding="utf-8"))


def test_evidence_manifest_is_generated_by_the_actual_pytest_session(tmp_path: Path) -> None:
    result, manifest = _run_evidence_case(
        tmp_path,
        "import pytest\n"
        "@pytest.mark.live\n"
        "def test_deselected_live_case(): assert False\n"
        "def test_selected_case(): assert True\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert manifest["schema_version"] == "tracefold_test_lane_v3"
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["commit_sha"])
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["git_tree_sha"])
    assert manifest["lane"] == "python"
    assert manifest["status"] == "success"
    assert manifest["selected"] == 1
    assert manifest["passed"] == 1
    assert manifest["failed"] == 0
    assert manifest["skipped"] == 0
    assert manifest["xfailed"] == 0
    assert manifest["xpassed"] == 0
    assert manifest["rerun"] == 0
    assert manifest["explicitly_deselected_markers"] == ["live", "scheduled"]
    assert re.fullmatch(r"[0-9a-f]{64}", str(manifest["uv_lock_sha256"]))
    assert re.fullmatch(r"[0-9a-f]{64}", str(manifest["package_lock_sha256"]))


@pytest.mark.parametrize(
    ("source", "field"),
    [
        ("import pytest\ndef test_bad(): pytest.skip('no resource')\n", "skipped"),
        ("import pytest\npytest.skip('module unavailable', allow_module_level=True)\n", "skipped"),
        ("import pytest\n@pytest.mark.xfail(reason='known')\ndef test_bad(): assert False\n", "xfailed"),
        ("import pytest\n@pytest.mark.xfail(reason='known')\ndef test_bad(): assert True\n", "xpassed"),
    ],
)
def test_evidence_mode_fails_closed_on_pseudo_green_outcomes(tmp_path: Path, source: str, field: str) -> None:
    result, manifest = _run_evidence_case(tmp_path, source)

    assert result.returncode != 0
    assert manifest[field] == 1


def test_evidence_mode_fails_closed_on_rerun_outcomes(tmp_path: Path) -> None:
    result, manifest = _run_evidence_case(
        tmp_path,
        "def test_would_look_green_after_a_rerun(): assert True\n",
        conftest=(
            "import pytest\n"
            "@pytest.hookimpl(hookwrapper=True)\n"
            "def pytest_runtest_makereport(item, call):\n"
            "    outcome = yield\n"
            "    report = outcome.get_result()\n"
            "    if report.when == 'call': report.outcome = 'rerun'\n"
        ),
    )

    assert result.returncode != 0
    assert manifest["rerun"] == 1


def test_evidence_mode_rejects_maxfail_even_when_the_case_passes(tmp_path: Path) -> None:
    result, manifest = _run_evidence_case(
        tmp_path,
        "def test_green(): assert True\n",
        extra_args=("--maxfail=1",),
    )

    assert result.returncode != 0
    assert "evidence_maxfail_forbidden" in manifest["errors"]


def test_evidence_mode_rejects_collection_without_execution(tmp_path: Path) -> None:
    result, manifest = _run_evidence_case(
        tmp_path,
        "def test_never_executed(): assert True\n",
        extra_args=("--collect-only",),
    )

    assert result.returncode != 0
    assert manifest["selected"] == 1
    assert manifest["passed"] == 0
    assert "evidence_selected_outcome_count_mismatch" in manifest["errors"]


def test_frontend_python_evidence_fails_when_node_is_unavailable(tmp_path: Path) -> None:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    (executable_dir / "git").symlink_to(git)

    result, manifest = _run_evidence_case(
        tmp_path,
        "def test_green(): assert True\n",
        extra_args=("--evidence-lane=frontend-python",),
        env={"PATH": str(executable_dir)},
    )

    assert result.returncode != 0
    assert manifest["tool_versions"]["node"] == "unavailable"
    assert "evidence_node_unavailable" in manifest["errors"]


def test_evidence_mode_fails_when_github_sha_does_not_match_the_tested_head(tmp_path: Path) -> None:
    result, manifest = _run_evidence_case(
        tmp_path,
        "def test_green(): assert True\n",
        env={"GITHUB_SHA": "0" * 40},
    )

    assert result.returncode != 0
    assert "evidence_github_sha_mismatch" in manifest["errors"]
