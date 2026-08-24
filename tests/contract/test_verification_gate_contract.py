from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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

    assert set(triggers) == {"pull_request", "push"}
    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["push"]["branches"] == ["main"]
    assert "paths" not in triggers["pull_request"]
    assert "paths-ignore" not in triggers["pull_request"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert workflow["env"]["TESTED_SHA"] == "${{ github.event.pull_request.head.sha || github.sha }}"

    jobs = workflow["jobs"]
    assert set(jobs) == {"quality", "fast", "deterministic-full", "ci-gate"}
    expected_commands = {
        "quality": "make check",
        "fast": "make test-fast",
        "deterministic-full": "make test-evidence",
    }
    for job_name, expected_command in expected_commands.items():
        job = jobs[job_name]
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

    gate = jobs["ci-gate"]
    assert gate["name"] == "ci-gate"
    assert gate["needs"] == ["quality", "fast", "deterministic-full"]
    assert gate["if"] == "always()"
    gate_results = tuple(gate["steps"][0]["env"].values())
    assert all(any(f"needs.{name}.result" in value for value in gate_results) for name in gate["needs"])
    evidence_step = next(
        step for step in jobs["deterministic-full"]["steps"] if "make test-evidence" in step.get("run", "")
    )
    assert "GITHUB_SHA" not in evidence_step["env"]
    assert 'GITHUB_SHA="$TESTED_SHA" make test-evidence' in evidence_step["run"]


def test_evidence_entrypoint_checks_the_tested_head_before_and_after_the_suite() -> None:
    result = subprocess.run(
        ["make", "-n", "test-evidence"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout.count("python -m tests.support.evidence --assert-clean") == 2


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
    case = tmp_path / "test_evidence_case.py"
    manifest = tmp_path / "evidence.json"
    executable_dir = tmp_path / "evidence-bin"
    executable_dir.mkdir(exist_ok=True)
    git = shutil.which("git")
    assert git is not None
    git_link = executable_dir / "git"
    if not git_link.exists():
        git_link.symlink_to(git)
    node = executable_dir / "node"
    node.write_text("#!/bin/sh\nprintf 'v22.0.0-test\\n'\n", encoding="utf-8")
    node.chmod(0o755)
    case.write_text(source, encoding="utf-8")
    if conftest is not None:
        (tmp_path / "conftest.py").write_text(conftest, encoding="utf-8")
    uv = shutil.which("uv")
    assert uv is not None
    result = subprocess.run(
        [
            uv,
            "run",
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.support.evidence",
            str(case),
            "-m",
            "not live",
            f"--evidence-manifest={manifest}",
            *extra_args,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": str(executable_dir),
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
    assert manifest["schema_version"] == "tracefold_test_evidence_v1"
    assert (
        manifest["commit_sha"]
        == subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, check=True, text=True
        ).stdout.strip()
    )
    assert manifest["selected"] == 1
    assert manifest["passed"] == 1
    assert manifest["failed"] == 0
    assert manifest["skipped"] == 0
    assert manifest["xfailed"] == 0
    assert manifest["xpassed"] == 0
    assert manifest["rerun"] == 0
    assert manifest["explicitly_deselected_markers"] == ["live"]
    assert re.fullmatch(r"[0-9a-f]{64}", str(manifest["uv_lock_sha256"]))
    assert re.fullmatch(r"[0-9a-f]{64}", str(manifest["property_lock_sha256"]))
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


def test_evidence_mode_fails_when_node_is_unavailable(tmp_path: Path) -> None:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    (executable_dir / "git").symlink_to(git)

    result, manifest = _run_evidence_case(
        tmp_path,
        "def test_green(): assert True\n",
        env={"PATH": str(executable_dir)},
    )

    assert result.returncode != 0
    assert manifest["node_version"] == "unavailable"
    assert "evidence_node_unavailable" in manifest["errors"]
